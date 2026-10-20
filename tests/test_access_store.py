from __future__ import annotations

import asyncio
import os
import unittest
import uuid
from dataclasses import replace
from unittest.mock import patch

from psycopg import AsyncConnection, sql

from octopus.access_store import ACCESS_COOLDOWN_MS, ACCESS_RETRY_MS, ACCESS_TTL_MS
from octopus.contracts import IncomingMessage
from octopus.store import Store


def message(id: str = "request-message", sender: str = "applicant") -> IncomingMessage:
    return IncomingMessage(id, "chat-" + sender, sender, "private question never stored", "p2p", 1000)


@unittest.skipUnless(os.environ.get("OCTOPUS_TEST_DATABASE_URL"), "Set OCTOPUS_TEST_DATABASE_URL to run PostgreSQL integration tests")
class AccessStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.database_url = os.environ["OCTOPUS_TEST_DATABASE_URL"]
        self.schema = f"octopus_test_{uuid.uuid4().hex}"
        self.addAsyncCleanup(self.drop_schema)
        self.store = await self.open()
        await self.store.ensure_member("admin", "admin")

    async def open(self, *, migrate: bool = True) -> Store:
        store = await Store.open(self.database_url, "cli_access_test", schema=self.schema, migrate=migrate)
        self.addAsyncCleanup(store.close)
        return store

    async def drop_schema(self) -> None:
        async with await AsyncConnection.connect(self.database_url, autocommit=True) as connection:
            await connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(self.schema)))

    async def request(self, id: str = "request-message", sender: str = "applicant") -> dict:
        status, request = await self.store.submit_access_request(message(id, sender))
        self.assertEqual(status, "created")
        self.assertIsNotNone(request)
        return request

    async def review(self, request: dict, actor: str = "admin", message_id: str = "review-card") -> dict:
        delivery = next(
            row for row in await self.store.access_deliveries()
            if row["kind"] == "review" and row["recipient_id"] == actor and row["request_id"] == request["id"]
        )
        await self.store.complete_access_delivery(delivery["delivery_id"], message_id)
        return delivery

    async def test_submission_is_metadata_only_with_enabled_admin_snapshot(self) -> None:
        await self.store.ensure_member("admin2", "admin")
        await self.store.ensure_member("disabled-admin", "admin")
        await self.store.set_member_enabled("disabled-admin", False)
        request = await self.request()
        self.assertEqual(request["status"], "pending")
        self.assertEqual(request["expires_at"] - request["created_at"], ACCESS_TTL_MS)
        self.assertEqual(set(request), {
            "id", "requester_id", "chat_id", "message_id", "status", "created_at",
            "expires_at", "decided_by", "decided_at",
        })
        deliveries = await self.store.access_deliveries()
        self.assertEqual({(d["kind"], d["recipient_id"]) for d in deliveries}, {
            ("requested", "applicant"), ("review", "admin"), ("review", "admin2"),
        })
        self.assertNotIn("private question", repr(deliveries))
        self.assertEqual(len({uuid.UUID(d["delivery_id"]) for d in deliveries}), 3)

    async def test_requests_and_stable_delivery_ids_survive_restart_and_duplicate_events(self) -> None:
        request = await self.request()
        delivery_ids = {d["delivery_id"] for d in await self.store.access_deliveries()}
        await self.store.close()
        reopened = await self.open(migrate=False)
        self.assertEqual(await reopened.submit_access_request(message()), ("duplicate", request))
        self.assertEqual(await reopened.submit_access_request(message("second")), ("pending", request))
        self.assertEqual(await reopened.submit_access_request(message(sender="impostor")), ("duplicate", None))
        self.assertEqual({d["delivery_id"] for d in await reopened.access_deliveries()}, delivery_ids)
        self.assertEqual(len(await reopened.pending_access_requests("admin")), 1)

    async def test_simultaneous_submissions_create_only_one_request_and_outbox(self) -> None:
        second = await self.open()
        results = await asyncio.gather(
            self.store.submit_access_request(message("one")), second.submit_access_request(message("two"))
        )
        self.assertEqual(sorted(result[0] for result in results), ["created", "pending"])
        self.assertEqual(results[0][1]["id"], results[1][1]["id"])
        self.assertEqual(len(await self.store.access_deliveries()), 2)

    async def test_racing_decisions_have_exactly_one_winner_and_grant_member_only(self) -> None:
        await self.store.ensure_member("admin2", "admin")
        request = await self.request()
        second = await self.open()
        results = await asyncio.gather(
            self.store.decide_access_request(request["id"], "admin", "accept"),
            second.decide_access_request(request["id"], "admin2", "decline"),
        )
        self.assertEqual(sorted(result[0] for result in results), ["already_decided", "decided"])
        winning_request = next(result[1] for result in results if result[0] == "decided")
        member = await self.store.member("applicant")
        if winning_request["status"] == "accepted":
            self.assertEqual(member, {"open_id": "applicant", "role": "member", "enabled": True})
        else:
            self.assertIsNone(member)
        self.assertEqual(len([d for d in await self.store.access_deliveries() if d["kind"] == "decision"]), 1)
        async with await AsyncConnection.connect(self.database_url, autocommit=True) as connection:
            cursor = await connection.execute(sql.SQL(
                "SELECT COUNT(*) FROM {} WHERE action = 'access.decide'"
            ).format(sql.Identifier(self.schema, "audit_events")))
            self.assertEqual(await cursor.fetchone(), (1,))

    async def test_cards_are_bound_to_exact_sent_message_and_recipient(self) -> None:
        await self.store.ensure_member("admin2", "admin")
        request = await self.request()
        await self.review(request)
        for actor, card in (("applicant", "review-card"), ("admin2", "review-card"),
                            ("admin", "copied-card"), ("admin", ""), ("admin", "x" * 257)):
            self.assertEqual(
                await self.store.decide_access_request(request["id"], actor, "accept", card_id=card),
                ("forbidden", None),
            )
        self.assertIsNone(await self.store.member("applicant"))
        result, decided = await self.store.decide_access_request(
            request["id"], "admin", "accept", card_id="review-card"
        )
        self.assertEqual((result, decided["status"]), ("decided", "accepted"))
        self.assertEqual((await self.store.decide_access_request(
            request["id"], "admin", "decline", card_id="review-card"
        ))[0], "already_decided")

    async def test_command_review_is_allowed_for_new_admin_but_card_is_not(self) -> None:
        request = await self.request()
        await self.store.ensure_member("new-admin", "admin")
        self.assertEqual(
            await self.store.decide_access_request(request["id"], "new-admin", "accept", card_id="arbitrary"),
            ("forbidden", None),
        )
        self.assertEqual((await self.store.decide_access_request(request["id"], "new-admin", "accept"))[0], "decided")

    async def test_revoked_admin_cannot_list_decide_or_receive_pending_reviews(self) -> None:
        request = await self.request()
        await self.review(request)
        await self.store.set_member_enabled("admin", False)
        with self.assertRaises(PermissionError):
            await self.store.pending_access_requests("admin")
        self.assertEqual(await self.store.decide_access_request(
            request["id"], "admin", "accept", card_id="review-card"
        ), ("forbidden", None))
        self.assertFalse(any(d["kind"] == "review" for d in await self.store.access_deliveries()))
        self.assertIsNone(await self.store.member("applicant"))
        self.assertEqual((await self.store.submit_access_request(message("other", "other")))[0], "no_admin")

    async def test_existing_and_disabled_members_cannot_self_reapply(self) -> None:
        await self.store.ensure_member("applicant")
        self.assertEqual(await self.store.submit_access_request(message()), ("member", None))
        await self.store.set_member_enabled("applicant", False)
        self.assertEqual(await self.store.submit_access_request(message()), ("disabled", None))
        self.assertEqual(await self.store.access_deliveries(), [])

    async def test_manual_enrollment_then_revocation_invalidates_old_request(self) -> None:
        request = await self.request()
        await self.review(request)
        await self.store.ensure_member("applicant")
        await self.store.set_member_enabled("applicant", False)
        outcome, closed = await self.store.decide_access_request(
            request["id"], "admin", "accept", card_id="review-card"
        )
        self.assertEqual((outcome, closed["status"]), ("member_changed", "member_changed"))
        self.assertFalse((await self.store.member("applicant"))["enabled"])
        self.assertEqual((await self.store.submit_access_request(message("new")))[0], "disabled")

    async def test_manual_enrollment_cannot_be_overwritten_by_stale_approval(self) -> None:
        request = await self.request()
        await self.store.ensure_member("applicant", "admin")
        self.assertEqual((await self.store.decide_access_request(request["id"], "admin", "accept"))[0], "member_changed")
        self.assertEqual((await self.store.member("applicant"))["role"], "admin")

    async def test_expiry_closes_request_once_and_suppresses_delayed_acknowledgement(self) -> None:
        with patch("octopus.store._now", return_value=1000):
            request = await self.request()
            await self.review(request)
        with patch("octopus.store._now", return_value=1000 + ACCESS_TTL_MS):
            self.assertEqual(await self.store.pending_access_requests("admin"), [])
            deliveries = await self.store.access_deliveries()
            self.assertEqual({d["kind"] for d in deliveries}, {"decision", "review"})
            self.assertTrue(all(d["request"]["status"] == "expired" for d in deliveries))
            self.assertEqual((await self.store.decide_access_request(request["id"], "admin", "accept"))[0], "already_decided")
            self.assertEqual(len(await self.store.access_deliveries()), 2)
        self.assertIsNone(await self.store.member("applicant"))

    async def test_decision_itself_rechecks_expiry(self) -> None:
        with patch("octopus.store._now", return_value=1000):
            request = await self.request()
        with patch("octopus.store._now", return_value=1000 + ACCESS_TTL_MS):
            result, closed = await self.store.decide_access_request(request["id"], "admin", "accept")
            self.assertEqual((result, closed["status"]), ("expired", "expired"))
            self.assertIsNone(await self.store.member("applicant"))

    async def test_rejected_applicant_must_wait_24_hours_to_reapply(self) -> None:
        with patch("octopus.store._now", return_value=1000):
            request = await self.request()
            await self.store.decide_access_request(request["id"], "admin", "decline")
        with patch("octopus.store._now", return_value=1000 + ACCESS_COOLDOWN_MS - 1):
            self.assertEqual((await self.store.submit_access_request(message("again")))[0], "cooldown")
        with patch("octopus.store._now", return_value=1000 + ACCESS_COOLDOWN_MS):
            second = await self.request("again")
            self.assertNotEqual(request["id"], second["id"])

    async def test_retry_and_terminal_updates_persist_without_rebinding_card(self) -> None:
        with patch("octopus.store._now", return_value=1000):
            request = await self.request()
            review = next(d for d in await self.store.access_deliveries() if d["kind"] == "review")
            await self.store.retry_access_delivery(review["delivery_id"])
            self.assertFalse(any(d["kind"] == "review" for d in await self.store.access_deliveries()))
        await self.store.close()
        self.store = await self.open(migrate=False)
        with patch("octopus.store._now", return_value=1000 + ACCESS_RETRY_MS):
            await self.review(request)
            with self.assertRaisesRegex(ValueError, "绑定其他"):
                await self.store.complete_access_delivery(review["delivery_id"], "replacement-card")
            with self.assertRaisesRegex(ValueError, "提前完成"):
                await self.store.complete_access_delivery(review["delivery_id"], "review-card", terminal=True)
            self.assertFalse(any(d["kind"] == "review" for d in await self.store.access_deliveries()))
            await self.store.decide_access_request(request["id"], "admin", "accept", card_id="review-card")
            terminal = next(d for d in await self.store.access_deliveries() if d["kind"] == "review")
            self.assertEqual(terminal["delivery_id"], review["delivery_id"])
            self.assertEqual(terminal["message_id"], "review-card")
            await self.store.complete_access_delivery(review["delivery_id"], "review-card", terminal=True)
            self.assertFalse(any(d["kind"] == "review" for d in await self.store.access_deliveries()))

    async def test_transaction_rolls_back_grant_and_decision_if_outbox_insert_fails(self) -> None:
        request = await self.request()
        with patch.object(self.store, "_access_notify", side_effect=RuntimeError("test-only injected failure")):
            with self.assertRaises(RuntimeError):
                await self.store.decide_access_request(request["id"], "admin", "accept")
        self.assertIsNone(await self.store.member("applicant"))
        self.assertEqual((await self.store.pending_access_requests("admin"))[0]["status"], "pending")
        self.assertEqual((await self.store.decide_access_request(request["id"], "admin", "accept"))[0], "decided")

    async def test_pending_cap_and_strict_input_validation(self) -> None:
        with patch("octopus.access_store.ACCESS_PENDING_LIMIT", 2):
            await self.request("one", "one")
            await self.request("two", "two")
            self.assertEqual((await self.store.submit_access_request(message("three", "three")))[0], "busy")
        for invalid in (replace(message(), chat_type="group"), replace(message(), sender_id=""),
                        replace(message(), chat_id="x" * 257), replace(message(), id="bad\nid")):
            with self.assertRaises(ValueError):
                await self.store.submit_access_request(invalid)
        self.assertEqual(await self.store.decide_access_request("not-uuid", "admin", "accept"), ("invalid", None))
        self.assertEqual(await self.store.decide_access_request(str(uuid.uuid4()), "admin", "other"), ("invalid", None))
        self.assertEqual(await self.store.decide_access_request(str(uuid.uuid4()), "admin", "accept"), ("missing", None))
        for invalid_limit in (0, 201, True):
            with self.assertRaises(ValueError):
                await self.store.access_deliveries(invalid_limit)
        with self.assertRaises(PermissionError):
            await self.store.pending_access_requests("unknown")

    async def test_retention_keeps_unfinished_notifications_and_pending_requests(self) -> None:
        with patch("octopus.store._now", return_value=1000):
            request = await self.request()
            await self.review(request)
            await self.store.decide_access_request(request["id"], "admin", "decline")
        counts = await self.store.purge_history()
        self.assertEqual(counts["access_requests"], 0)
        for delivery in await self.store.access_deliveries():
            await self.store.complete_access_delivery(
                delivery["delivery_id"], delivery["message_id"] or "decision-card", terminal=True
            )
        counts = await self.store.purge_history()
        self.assertEqual((counts["access_requests"], counts["access_deliveries"]), (1, 3))
        self.assertEqual(await self.store.access_deliveries(), [])

    async def test_expired_request_to_revoked_admin_can_finish_and_be_purged(self) -> None:
        with patch("octopus.store._now", return_value=1000):
            request = await self.request()
            await self.review(request)
            await self.store.set_member_enabled("admin", False)
        with patch("octopus.store._now", return_value=1000 + ACCESS_TTL_MS):
            deliveries = await self.store.access_deliveries()
            self.assertEqual([(d["kind"], d["recipient_id"]) for d in deliveries], [("decision", "applicant")])
            self.assertEqual(deliveries[0]["request"]["status"], "expired")
            await self.store.complete_access_delivery(deliveries[0]["delivery_id"], "expired-decision")
        counts = await self.store.purge_history()
        self.assertEqual((counts["access_requests"], counts["access_deliveries"]), (1, 3))
        self.assertEqual(await self.store.access_deliveries(), [])

    async def test_decided_request_can_be_purged_after_reviewer_is_revoked(self) -> None:
        with patch("octopus.store._now", return_value=1000):
            request = await self.request()
            await self.review(request)
            await self.store.decide_access_request(request["id"], "admin", "accept")
            deliveries = await self.store.access_deliveries()
            decision = next(d for d in deliveries if d["kind"] == "decision")
            await self.store.complete_access_delivery(decision["delivery_id"], "accepted-decision")
            await self.store.set_member_enabled("admin", False)
            # Retention also completes inaccessible terminal reviews even when
            # the bridge has not polled its outbox since the revocation.
        counts = await self.store.purge_history()
        self.assertEqual((counts["access_requests"], counts["access_deliveries"]), (1, 3))
        self.assertEqual(await self.store.access_deliveries(), [])
        self.assertTrue((await self.store.member("applicant"))["enabled"])

    async def test_v2_migration_preserves_members_and_creates_outbox(self) -> None:
        await self.store.ensure_member("disabled")
        await self.store.set_member_enabled("disabled", False)
        async with await AsyncConnection.connect(self.database_url, autocommit=True) as connection:
            await connection.execute(sql.SQL("DROP TABLE {}, {}").format(
                sql.Identifier(self.schema, "access_deliveries"), sql.Identifier(self.schema, "access_requests")
            ))
            await connection.execute(sql.SQL("UPDATE {} SET value = '2' WHERE key = 'schema_version'").format(
                sql.Identifier(self.schema, "meta")
            ))
        with self.assertRaisesRegex(ValueError, "数据库版本不匹配"):
            await self.open(migrate=False)
        migrated = await self.open()
        self.assertFalse((await migrated.member("disabled"))["enabled"])
        self.assertEqual((await migrated.submit_access_request(message()))[0], "created")


if __name__ == "__main__":
    unittest.main()
