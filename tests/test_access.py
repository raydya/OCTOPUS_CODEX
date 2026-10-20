from __future__ import annotations

import asyncio
import os
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from psycopg import AsyncConnection, sql

from octopus.access import APPLICATION_HELP, AccessRequests
from octopus.bridge import Bridge
from octopus.config import Config
from octopus.contracts import CardAction, View
from octopus.store import Store
from test_bridge import FakeCodex, FakeMessenger, message
from test_readonly_bridge import Queries


class AccessMessenger(FakeMessenger):
    def __init__(self):
        super().__init__()
        self.dedup = {}
        self.direct_gate = asyncio.Event()
        self.direct_gate.set()
        self.direct_entered = asyncio.Event()

    async def send_user(self, open_id, view, *, request_id=None):
        self.direct_entered.set()
        await self.direct_gate.wait()
        if request_id not in self.dedup:
            self.dedup[request_id] = await self.send(open_id, view)
        return self.dedup[request_id]


class AccessUnitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store, self.messenger = AsyncMock(), AccessMessenger()
        self.store.member.return_value = None
        self.access = AccessRequests(self.store, self.messenger)
        self.identifier = str(uuid.uuid4())
        self.request = {"id": self.identifier, "requester_id": "ou_bob", "status": "pending"}

    async def test_unknown_business_messages_only_get_bounded_instructions(self):
        item = message("private", "show logs", sender="ou_bob")
        self.assertEqual(await self.access.receive(item, authorized=False), (True, APPLICATION_HELP))
        self.assertEqual(await self.access.receive(item, authorized=False), (True, None))
        self.assertEqual(await self.access.receive(message("group", "/apply", sender="ou_bob", kind="group"),
                                                   authorized=False), (True, None))
        self.store.submit_access_request.assert_not_called()
        self.assertFalse(self.messenger.records)

    async def test_applications_persist_without_network_and_duplicates_do_not_resend(self):
        self.store.submit_access_request.side_effect = [("created", self.request), ("duplicate", self.request)]
        self.messenger.direct_gate.clear()
        for _ in range(2):
            self.assertEqual(await asyncio.wait_for(
                self.access.receive(message("apply", "申请权限", sender="ou_bob"), authorized=False), 0.2),
                (True, None))
        self.assertTrue(self.access.wakeup.is_set())
        self.assertFalse(self.messenger.records)
        self.assertEqual(self.store.submit_access_request.await_count, 2)

    async def test_disabled_account_is_not_told_application_was_submitted(self):
        self.store.submit_access_request.return_value = ("disabled", None)
        handled, text = await self.access.receive(message("apply", "/apply", sender="ou_bob"), authorized=False)
        self.assertTrue(handled)
        self.assertIn("停用", text)
        self.assertFalse(self.access.wakeup.is_set())

    async def test_card_requires_context_and_routes_only_access_actions(self):
        for action in [CardAction("other", "accept", "ou_alice", "oc_admin", "om_review"),
                       CardAction("access:invalid", "accept", "ou_alice", "oc_admin", "om_review"),
                       CardAction("access:" + self.identifier, "accept", "ou_alice", None, "om_review"),
                       CardAction("access:" + self.identifier, "accept", "ou_alice", "oc_admin", None)]:
            await self.access.receive_action(action)
        self.store.decide_access_request.assert_not_called()
        await self.access.receive_action(CardAction("access:" + self.identifier, "accept", "ou_alice",
                                                    "oc_admin", "om_review"))
        self.store.decide_access_request.assert_awaited_once_with(
            self.identifier, "ou_alice", "accept", card_id="om_review",
        )

    async def test_nonadmin_cannot_list_or_decide_requests(self):
        self.store.member.return_value = {"role": "member", "enabled": True}
        for text in ["/access-requests", "/access-approve " + self.identifier]:
            self.assertIn("管理员", await self.access.command(message("cmd", text, sender="ou_bob")))
        self.store.pending_access_requests.assert_not_called()
        self.store.decide_access_request.assert_not_called()

    async def test_failed_notification_is_retried_with_same_persisted_identifier(self):
        delivery = {"delivery_id": str(uuid.uuid4()), "recipient_id": "ou_alice", "kind": "review",
                    "message_id": None, "request": self.request}
        self.store.access_deliveries.return_value = [delivery]
        with patch.object(self.messenger, "send_user", AsyncMock(side_effect=RuntimeError("token=secret"))):
            with self.assertLogs("octopus.access", "WARNING") as logs:
                await self.access.deliver_once()
        self.assertNotIn("secret", " ".join(logs.output))
        self.store.complete_access_delivery.assert_not_called()
        self.store.retry_access_delivery.assert_awaited_once_with(delivery["delivery_id"])
        await self.access.deliver_once()
        self.store.complete_access_delivery.assert_awaited_once_with(
            delivery["delivery_id"], self.messenger.records[0].id, terminal=False,
        )
        self.assertIn(delivery["delivery_id"], self.messenger.dedup)
        self.store.ensure_member.assert_not_called()
        self.assertEqual(len(self.messenger.records[0].view.actions), 2)

    async def test_resolved_cards_remove_buttons_and_approved_notice_rechecks_revocation(self):
        terminal = {**self.request, "status": "accepted"}
        await self.messenger.send("ou_alice", View("approval", "review", "pending"))
        self.store.member.return_value = {"role": "member", "enabled": False}
        self.store.access_deliveries.return_value = [
            {"delivery_id": str(uuid.uuid4()), "recipient_id": "ou_alice", "kind": "review",
             "message_id": "om_out_1", "request": terminal},
            {"delivery_id": str(uuid.uuid4()), "recipient_id": "ou_bob", "kind": "decision",
             "message_id": None, "request": terminal},
        ]
        await self.access.deliver_once()
        self.assertEqual(self.messenger.records[1].operation, "update")
        self.assertFalse(self.messenger.records[1].view.actions)
        self.assertIn("已停用", self.messenger.records[2].view.text)

    async def test_terminal_retry_updates_card_created_before_failed_persistence(self):
        delivery = {"delivery_id": str(uuid.uuid4()), "recipient_id": "ou_alice", "kind": "review",
                    "message_id": None, "request": self.request}
        original_id = await self.messenger.send_user("ou_alice", await self.access._view(delivery),
                                                      request_id=delivery["delivery_id"])
        self.store.access_deliveries.return_value = [
            {**delivery, "request": {**self.request, "status": "declined"}},
        ]
        await self.access.deliver_once()
        self.assertEqual(self.messenger.records[-1].id, original_id)
        self.assertEqual(self.messenger.records[-1].operation, "update")
        self.assertFalse(self.messenger.records[-1].view.actions)
        self.store.complete_access_delivery.assert_awaited_once_with(
            delivery["delivery_id"], original_id, terminal=True,
        )


@unittest.skipUnless(os.environ.get("OCTOPUS_TEST_DATABASE_URL"), "Requires isolated PostgreSQL test instance")
class AccessWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.url = os.environ["OCTOPUS_TEST_DATABASE_URL"]
        self.schema = "access_flow_" + uuid.uuid4().hex
        self.store = await Store.open(self.url, "cli_access_test", schema=self.schema)
        await self.store.add_owner("ou_alice")
        self.config = Config("cli_access_test", "secret", self.url, "/isolated", None, None,
                             read_only=True, task_timeout=60)
        self.bridge = None
        await self.start()

    async def start(self):
        self.codex, self.messenger = FakeCodex(), AccessMessenger()
        self.bridge = Bridge(self.config, self.store, self.messenger, self.codex, Queries())
        await self.bridge.start()

    async def asyncTearDown(self):
        if self.bridge:
            await self.bridge.close()
        await self.store.close()
        async with await AsyncConnection.connect(self.url, autocommit=True) as connection:
            await connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    async def apply(self):
        await self.bridge.receive(message("apply", "申请权限", sender="ou_bob"))
        review = await self.messenger.wait_record(lambda row: row.chat == "ou_alice" and row.view.actions)
        # Remote delivery precedes its durable binding. Wait for that database
        # boundary before simulating a later user click or completed restart.
        async with asyncio.timeout(2):
            while True:
                async with self.store._lock:
                    cursor = await self.store._db.execute(
                        "SELECT 1 FROM access_deliveries WHERE message_id = %s", (review.id,),
                    )
                    if await cursor.fetchone():
                        return review
                await asyncio.sleep(0.005)

    async def test_apply_notify_decide_then_start_new_question(self):
        await self.bridge.receive(message("before", "Explain business behavior", sender="ou_bob"))
        await self.messenger.wait_record(lambda row: row.reply_to == "before")
        review = await self.apply()
        self.assertIsNone(await self.store.member("ou_bob"))
        self.assertFalse(self.codex.turns)
        self.assertIsNone(await self.store.get_session("readonly:v2:oc_shared:ou_bob"))
        action = CardAction(review.view.actions[0].ticket_id, "accept", "ou_alice", "oc_admin", review.id)
        await self.bridge.receive_action(action)
        await self.messenger.wait_record(lambda row: row.chat == "ou_bob" and "权限申请已通过" in row.view.text)
        self.assertEqual(await self.store.member("ou_bob"), {"open_id": "ou_bob", "role": "member", "enabled": True})
        self.assertFalse(self.codex.turns)
        await self.bridge.receive(message("after", "Explain business behavior", sender="ou_bob"))
        turn = await self.codex.next_turn()
        self.assertEqual(turn.text, "Explain business behavior")
        self.assertEqual(self.codex.approvals, [])

    async def test_applicant_cannot_approve_own_request_or_use_copied_card(self):
        review = await self.apply()
        ticket = review.view.actions[0].ticket_id
        await self.bridge.receive_action(CardAction(ticket, "accept", "ou_bob", "oc_shared", review.id))
        await self.bridge.receive_action(CardAction(ticket, "accept", "ou_alice", "oc_admin", "om_forged"))
        self.assertIsNone(await self.store.member("ou_bob"))
        await self.bridge.receive_action(CardAction(ticket, "decline", "ou_alice", "oc_admin", review.id))
        await self.messenger.wait_record(lambda row: row.chat == "ou_bob" and "未通过" in row.view.text)
        await self.bridge.receive_action(CardAction(ticket, "accept", "ou_alice", "oc_admin", review.id))
        self.assertIsNone(await self.store.member("ou_bob"))
        self.assertFalse(self.codex.turns)

    async def test_pending_card_survives_restart(self):
        review = await self.apply()
        records, dedup = self.messenger.records, self.messenger.dedup
        await self.bridge.close()
        await self.store.close()
        self.store = await Store.open(self.url, "cli_access_test", schema=self.schema, migrate=False)
        await self.start()
        # The fake transport models the existing remote card across restart.
        self.messenger.records, self.messenger.dedup = records, dedup
        await self.bridge.receive_action(CardAction(review.view.actions[0].ticket_id, "accept", "ou_alice",
                                                    "oc_admin", review.id))
        await self.messenger.wait_record(lambda row: row.chat == "ou_bob" and "权限申请已通过" in row.view.text)
        self.assertTrue((await self.store.member("ou_bob"))["enabled"])

    async def test_admin_can_recover_pending_requests_by_command(self):
        review = await self.apply()
        identifier = review.view.actions[0].ticket_id.removeprefix("access:")
        await self.bridge._command(message("list", "/access-requests"))
        listed = await self.messenger.wait_record(lambda row: row.reply_to == "list")
        self.assertIn(identifier, listed.view.text)
        await self.bridge._command(message("approve", "/access-approve " + identifier))
        await self.messenger.wait_record(lambda row: row.reply_to == "approve")
        self.assertTrue((await self.store.member("ou_bob"))["enabled"])
        self.assertFalse(self.codex.turns)
