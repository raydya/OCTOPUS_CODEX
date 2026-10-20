from __future__ import annotations

import asyncio
import unittest
import uuid
from dataclasses import FrozenInstanceError, replace
from unittest.mock import AsyncMock, patch

from octopus.access_test import MAX_PENDING, MAX_RECENT, TEST_COOLDOWN, TEST_TTL, AccessSelfTest
from octopus.contracts import CardAction
from test_access import AccessMessenger
from test_bridge import message


class AccessSelfTestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store, self.messenger = AsyncMock(), AccessMessenger()
        self.store.member.return_value = {"role": "admin", "enabled": True}
        self.access = AccessSelfTest(self.store, self.messenger)
        self.clock = patch("octopus.access_test.monotonic", return_value=1000)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)

    async def make_card(self, identifier="self-test", sender="ou_alice"):
        await self.access.create(message(identifier, "/access-test", sender=sender))
        card = self.messenger.records[-1]
        return CardAction(card.view.actions[0].ticket_id, "accept", sender, "oc_admin", card.id)

    def assert_read_only_store(self):
        self.assertTrue(all(call[0] == "member" for call in self.store.mock_calls))

    async def test_only_enabled_admin_in_private_chat_can_create(self):
        for actor in (None, {"role": "member", "enabled": True}, {"role": "admin", "enabled": False}):
            self.store.member.return_value = actor
            self.assertIn("管理员", await self.access.create(message("denied", "/access-test")))
        self.store.member.return_value = {"role": "admin", "enabled": True}
        self.assertIn("私聊", await self.access.create(message("group", "/access-test", kind="group")))
        self.assertFalse(self.messenger.records)
        self.assert_read_only_store()

    async def test_accept_and_decline_are_labelled_and_never_change_membership(self):
        for index, result in enumerate(("accept", "decline")):
            self.now.return_value = 1000 + index * TEST_COOLDOWN
            action = replace(await self.make_card(f"request-{index}"), decision=result)
            pending = self.messenger.records[-1]
            self.assertEqual(pending.chat, "ou_alice")
            self.assertEqual(pending.view.title, "权限申请测试")
            self.assertIn("服务重启", pending.view.text)
            self.assertTrue(action.ticket_id.startswith("access-test:"))
            self.assertEqual(len(pending.view.actions), 2)
            decision = await self.access.decide(action)
            self.assertIsNotNone(decision)
            self.assertEqual(len(self.messenger.records), index * 3 + 1)
            with self.assertRaises(FrozenInstanceError):
                decision.result = "decline"
            await self.access.deliver_result(decision)
            updated, delivered = self.messenger.records[-2:]
            self.assertEqual(updated.operation, "update")
            self.assertEqual(updated.id, pending.id)
            self.assertFalse(updated.view.actions)
            self.assertEqual(delivered.view.title, "")
            self.assertIn("没有改变任何人的权限", delivered.view.text)
            self.assertIn("已通过" if result == "accept" else "已拒绝", delivered.view.text)
            self.assertEqual(delivered.chat, "ou_alice")
            uuid.UUID(decision.request_id)
        self.assert_read_only_store()

    async def test_forged_copied_foreign_and_replayed_cards_fail(self):
        action = await self.make_card()
        for forged in (
            replace(action, ticket_id="access:" + str(uuid.uuid4())),
            replace(action, ticket_id="access-test:invalid"),
            replace(action, sender_id="ou_bob"),
            replace(action, chat_id=None),
            replace(action, chat_id=""),
            replace(action, message_id=None),
            replace(action, message_id="om_copied"),
            replace(action, decision="other"),
        ):
            self.assertIsNone(await self.access.decide(forged))
        self.store.member.reset_mock()
        self.assertIsNotNone(await self.access.decide(action))
        self.assertIsNone(await self.access.decide(action))
        self.store.member.assert_awaited_once_with("ou_alice")
        self.assertEqual(len(self.messenger.records), 1)
        self.assert_read_only_store()

    async def test_revoked_or_demoted_admin_cannot_decide(self):
        action = await self.make_card()
        for actor in (None, {"role": "admin", "enabled": False}, {"role": "member", "enabled": True}):
            self.store.member.return_value = actor
            self.assertIsNone(await self.access.decide(action))
        self.assertEqual(len(self.messenger.records), 1)
        self.assert_read_only_store()

    async def test_expired_or_restarted_test_cannot_decide(self):
        action = await self.make_card()
        restarted = AccessSelfTest(self.store, self.messenger)
        self.assertIsNone(await restarted.decide(action))
        self.now.return_value += TEST_TTL
        self.assertIsNone(await self.access.decide(action))
        self.assertFalse(self.access._tickets)
        self.assertIn("已发给你", await self.access.create(message("new-test", "/access-test")))
        self.assertEqual(len(self.messenger.records), 2)

    async def test_duplicate_commands_and_one_pending_per_admin(self):
        first = message("first", "/access-test")
        self.assertEqual(await self.access.create(first), await self.access.create(first))
        self.assertIn("待处理", await self.access.create(message("second", "/access-test")))
        self.assertEqual(len(self.messenger.records), 1)
        await self.make_card("bob", "ou_bob")
        self.assertEqual({record.chat for record in self.messenger.records}, {"ou_alice", "ou_bob"})

    async def test_cooldown_after_consuming_card(self):
        action = await self.make_card()
        await self.access.decide(action)
        self.assertIn("30秒", await self.access.create(message("again", "/access-test")))
        self.now.return_value += TEST_COOLDOWN
        self.assertIn("已发给你", await self.access.create(message("later", "/access-test")))
        self.assertEqual(len(self.messenger.records), 2)

    async def test_inflight_create_is_reserved_and_callback_does_no_network(self):
        self.messenger.direct_gate.clear()
        pending = asyncio.create_task(self.access.create(message("first", "/access-test")))
        await self.messenger.direct_entered.wait()
        self.assertIn("待处理", await self.access.create(message("second", "/access-test")))
        self.messenger.direct_gate.set()
        await pending
        card = self.messenger.records[-1]
        action = CardAction(card.view.actions[0].ticket_id, "accept", "ou_alice", "oc_admin", card.id)
        self.messenger.direct_gate.clear()
        self.assertIsNotNone(await asyncio.wait_for(self.access.decide(action), 0.2))
        self.assertEqual(len(self.messenger.records), 1)

    async def test_concurrent_clicks_have_only_one_decision(self):
        action = await self.make_card()
        entered, release = asyncio.Queue(), asyncio.Event()

        async def lookup(_sender):
            await entered.put(True)
            await release.wait()
            return {"role": "admin", "enabled": True}

        self.store.member.side_effect = lookup
        first = asyncio.create_task(self.access.decide(action))
        second = asyncio.create_task(self.access.decide(replace(action, decision="decline")))
        await entered.get()
        await entered.get()
        release.set()
        decisions = await asyncio.gather(first, second)
        self.assertEqual(sum(decision is not None for decision in decisions), 1)
        self.assert_read_only_store()

    async def test_expiry_during_membership_lookup_is_rechecked(self):
        action = await self.make_card()

        async def lookup(_sender):
            self.now.return_value += TEST_TTL
            return {"role": "admin", "enabled": True}

        self.store.member.side_effect = lookup
        self.assertIsNone(await self.access.decide(action))
        self.assertFalse(self.access._tickets)

    async def test_failed_create_cleans_ticket_without_replaying_same_event(self):
        with patch.object(self.messenger, "send_user", AsyncMock(side_effect=RuntimeError("token=secret"))):
            with self.assertLogs("octopus.access_test", "WARNING") as logs:
                result = await self.access.create(message("failed", "/access-test"))
        self.assertIn("暂未送达", result)
        self.assertNotIn("secret", " ".join(logs.output))
        self.assertFalse(self.access._tickets)
        self.assertEqual(await self.access.create(message("failed", "/access-test")), result)
        self.assertFalse(self.messenger.records)
        self.now.return_value += TEST_COOLDOWN
        self.assertIn("已发给你", await self.access.create(message("retry", "/access-test")))

    async def test_cancelled_create_cleans_ticket(self):
        self.messenger.direct_gate.clear()
        pending = asyncio.create_task(self.access.create(message("cancel", "/access-test")))
        await self.messenger.direct_entered.wait()
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertFalse(self.access._tickets)

    async def test_result_retries_have_stable_send_uuid_and_no_extra_recipient(self):
        decision = await self.access.decide(await self.make_card())
        await self.access.deliver_result(decision)
        await self.access.deliver_result(decision)
        sends = [record for record in self.messenger.records if record.operation == "send"]
        self.assertEqual(len(sends), 2)
        self.assertEqual({record.chat for record in self.messenger.records}, {"ou_alice"})
        self.assertIn(decision.request_id, self.messenger.dedup)
        self.assert_read_only_store()

    async def test_result_notice_can_deliver_even_if_card_update_failed(self):
        decision = await self.access.decide(await self.make_card())
        with patch.object(self.messenger, "update", AsyncMock(side_effect=RuntimeError("token=secret"))):
            with self.assertLogs("octopus.access_test", "WARNING") as logs:
                await self.access.deliver_result(decision)
        self.assertNotIn("secret", " ".join(logs.output))
        self.assertEqual(self.messenger.records[-1].chat, "ou_alice")
        self.assertIn("测试已结束", self.messenger.records[-1].view.text)

    async def test_result_rechecks_current_admin_before_notification(self):
        decision = await self.access.decide(await self.make_card())
        self.store.member.return_value = {"role": "admin", "enabled": False}
        await self.access.deliver_result(decision)
        self.assertEqual(len(self.messenger.records), 1)
        self.assert_read_only_store()

    async def test_total_pending_and_metadata_are_bounded(self):
        for index in range(MAX_PENDING):
            await self.make_card(f"request-{index}", f"ou_admin_{index}")
        self.assertIn("较多", await self.access.create(message("full", "/access-test", sender="ou_extra")))
        self.assertEqual(len(self.messenger.records), MAX_PENDING)
        self.assertEqual(len(self.access._tickets), MAX_PENDING)
        for index in range(MAX_RECENT + 1):
            self.access._remember(("ou_alice", str(index)), 1000, "completed")
        self.assertEqual(len(self.access._recent), MAX_RECENT)
        self.now.return_value += TEST_TTL
        self.access._cleanup(self.now.return_value)
        self.assertFalse(self.access._tickets)
        self.assertFalse(self.access._recent)
        self.assertFalse(self.access._attempts)
