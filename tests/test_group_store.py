from __future__ import annotations

import asyncio
import os
import unittest
import uuid
from dataclasses import replace
from unittest.mock import patch

from psycopg import AsyncConnection, sql

from octopus.contracts import IncomingMessage
from octopus.store import MEMBER_DISABLED_ERROR, SCHEMA_VERSION, Session, Store


SUPPORT_GROUP = "oc_support"


def group_message(identifier: str, *, sender: str = "ou_alice", chat: str = SUPPORT_GROUP,
                  root: str = "om_topic") -> IncomingMessage:
    return IncomingMessage(identifier, chat, sender, "请帮忙分析这个业务问题", "group", 1000,
                           root_id=root, mentioned_bot=True)


def topic_key(chat: str = SUPPORT_GROUP, root: str = "om_topic") -> str:
    return f"readonly-policy:group:{chat}:{root}"


@unittest.skipUnless(os.environ.get("OCTOPUS_TEST_DATABASE_URL"),
                     "Set OCTOPUS_TEST_DATABASE_URL to run PostgreSQL integration tests")
class GroupStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.database_url = os.environ["OCTOPUS_TEST_DATABASE_URL"]
        self.schema = f"octopus_group_test_{uuid.uuid4().hex}"
        self.addAsyncCleanup(self.drop_schema)
        self.store = await self.open()

    async def drop_schema(self) -> None:
        async with await AsyncConnection.connect(self.database_url, autocommit=True) as connection:
            await connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(self.schema)))

    async def open(self, *, migrate: bool = True) -> Store:
        store = await Store.open(self.database_url, "cli_group_test", schema=self.schema, migrate=migrate)
        self.addAsyncCleanup(store.close)
        return store

    async def topic(self, *, chat: str = SUPPORT_GROUP, root: str = "om_topic", sender: str = "ou_alice") -> Session:
        return await self.store.session(topic_key(chat, root), chat, sender, topic_id=root)

    async def admit(self, message: IncomingMessage, **kwargs: object) -> str:
        return await self.store.admission(topic_key(message.chat_id, message.topic_id), message,
                                          support_group_ids=(SUPPORT_GROUP,), min_interval_seconds=0, **kwargs)

    async def job_states(self) -> list[tuple[str, str, str | None]]:
        async with await AsyncConnection.connect(self.database_url, autocommit=True) as connection:
            cursor = await connection.execute(sql.SQL("SELECT id, state, error FROM {}.jobs ORDER BY sequence").format(
                sql.Identifier(self.schema)))
            return await cursor.fetchall()

    async def test_same_topic_is_shared_and_different_topic_or_group_is_isolated(self) -> None:
        first = await self.topic()
        await self.store.set_thread(first.key, "codex-topic-one")
        shared = await self.topic(sender="ou_bob")
        self.assertEqual(shared.scope, "topic")
        self.assertEqual(shared.sender_id, "")
        self.assertEqual(shared.topic_id, "om_topic")
        self.assertEqual(shared.thread_id, "codex-topic-one")
        other_topic = await self.topic(root="om_other_topic")
        other_group = await self.topic(chat="oc_other")
        self.assertIsNone(other_topic.thread_id)
        self.assertIsNone(other_group.thread_id)
        reopened = await self.open(migrate=False)
        self.assertEqual(await reopened.get_session(first.key), shared)

    async def test_group_admission_creates_no_global_membership_and_cannot_authorize_p2p(self) -> None:
        await self.topic()
        first = group_message("om_first")
        self.assertEqual(await self.admit(first), "accepted")
        self.assertEqual(await self.admit(first), "duplicate")
        self.assertEqual(await self.admit(group_message("om_second", sender="ou_bob")), "accepted")
        self.assertEqual(await self.store.members(), [])
        await self.store.session("private-alice", "oc_private", "ou_alice")
        private = replace(first, id="om_private", chat_id="oc_private", chat_type="p2p", root_id="")
        self.assertEqual(await self.store.admission("private-alice", private,
                         support_group_ids=(SUPPORT_GROUP,), min_interval_seconds=0), "disabled")
        self.assertEqual([job.message.sender_id for job in await self.store.next_jobs()], ["ou_alice", "ou_bob"])

    async def test_allowlist_is_required_even_for_enabled_members(self) -> None:
        await self.topic()
        await self.topic(chat="oc_other")
        await self.store.ensure_member("ou_alice")
        self.assertEqual(await self.store.admission(topic_key(), group_message("om_no_scope")), "disabled")
        self.assertEqual(await self.admit(group_message("om_wrong_group", chat="oc_other")), "disabled")
        self.assertEqual(await self.admit(replace(group_message("om_not_mentioned"), mentioned_bot=False)), "disabled")
        self.assertEqual(await self.admit(group_message("om_allowed")), "accepted")

    async def test_group_only_actors_can_queue_controls_without_becoming_global_members(self) -> None:
        controls = [replace(group_message(f"om_control_{index}"), text=text)
                    for index, text in enumerate(("/status", "/help", "/answer ticket 答案"))]
        for item in controls:
            self.assertTrue(await self.store.enqueue_control(item, max_pending=5,
                                                             support_group_ids=(SUPPORT_GROUP,)))
        self.assertFalse(await self.store.enqueue_control(controls[0], max_pending=5,
                                                          support_group_ids=(SUPPORT_GROUP,)))
        self.assertEqual(await self.store.next_controls(), controls)
        self.assertEqual(await self.store.members(), [])
        reopened = await self.open(migrate=False)
        self.assertEqual(await reopened.next_controls(), controls)

    async def test_group_control_scope_fails_closed_for_private_other_group_or_missing_mention(self) -> None:
        control = replace(group_message("om_control"), text="/status")
        for invalid in (replace(control, chat_type="p2p", chat_id="oc_private"),
                        replace(control, chat_id="oc_other"), replace(control, mentioned_bot=False)):
            self.assertFalse(await self.store.enqueue_control(invalid, max_pending=5,
                                                              support_group_ids=(SUPPORT_GROUP,)))
        self.assertFalse(await self.store.enqueue_control(control, max_pending=5))
        await self.store.ensure_member("ou_alice")
        for invalid in (replace(control, chat_id="oc_other"), replace(control, mentioned_bot=False)):
            self.assertFalse(await self.store.enqueue_control(invalid, max_pending=5,
                                                              support_group_ids=(SUPPORT_GROUP,)))
        self.assertFalse(await self.store.enqueue_control(control, max_pending=5))
        await self.store.set_member_enabled("ou_alice", False)
        self.assertFalse(await self.store.enqueue_control(control, max_pending=5,
                                                          support_group_ids=(SUPPORT_GROUP,)))
        self.assertEqual(await self.store.next_controls(), [])
        # Supplying scoped authorization must not depend on a quota argument.
        self.assertFalse(await self.store.enqueue_control(control, support_group_ids=(SUPPORT_GROUP,)))

    async def test_group_controls_keep_atomic_per_actor_pending_budget(self) -> None:
        first = replace(group_message("om_control_one"), text="/status")
        second_item = replace(group_message("om_control_two", chat="oc_other"), text="/help")
        groups = (SUPPORT_GROUP, "oc_other")
        second = await self.open()
        results = await asyncio.gather(
            self.store.enqueue_control(first, max_pending=1, support_group_ids=groups),
            second.enqueue_control(second_item, max_pending=1, support_group_ids=groups))
        self.assertEqual(sorted(results), [False, True])
        accepted = (await self.store.next_controls())[0]
        await self.store.start_control(accepted.id)
        self.assertFalse(await self.store.enqueue_control(replace(first, id="om_while_running"),
                                                          max_pending=1, support_group_ids=groups))
        self.assertTrue(await self.store.enqueue_control(replace(first, id="om_bob_control", sender_id="ou_bob"),
                                                         max_pending=1, support_group_ids=groups))
        await self.store.finish_control(accepted.id)
        self.assertTrue(await self.store.enqueue_control(replace(first, id="om_next_control"),
                                                         max_pending=1, support_group_ids=groups))

    async def test_group_control_revocation_race_cancels_only_disabled_actor(self) -> None:
        first = replace(group_message("om_racing_control"), text="/status")
        bob = replace(first, id="om_bob_control", sender_id="ou_bob")
        await self.store.enqueue_control(bob, max_pending=5, support_group_ids=(SUPPORT_GROUP,))
        second = await self.open()
        await asyncio.gather(
            self.store.enqueue_control(first, max_pending=5, support_group_ids=(SUPPORT_GROUP,)),
            second.set_member_enabled("ou_alice", False))
        self.assertEqual(await self.store.next_controls(), [bob])
        self.assertFalse(await self.store.enqueue_control(replace(first, id="om_after_disable"),
                                                          max_pending=5, support_group_ids=(SUPPORT_GROUP,)))

    async def test_group_control_daily_budget_is_shared_and_expires(self) -> None:
        async with await AsyncConnection.connect(self.database_url, autocommit=True) as connection:
            await connection.execute(sql.SQL(
                """INSERT INTO {}.controls (id, message_json, state, created_at, updated_at)
                   SELECT 'budget-' || number, jsonb_build_object('sender_id', 'ou_alice'), 'done', 100000000, 100000000
                   FROM generate_series(1, 999) AS number""").format(sql.Identifier(self.schema)))
        second = await self.open()
        control = replace(group_message("om_daily_first"), text="/status")
        with patch("octopus.store._now", return_value=100_001_000):
            results = await asyncio.gather(
                self.store.enqueue_control(control, max_pending=5, support_group_ids=(SUPPORT_GROUP,)),
                second.enqueue_control(replace(control, id="om_daily_second"), max_pending=5,
                                       support_group_ids=(SUPPORT_GROUP,)))
            self.assertEqual(sorted(results), [False, True])
            self.assertTrue(await self.store.enqueue_control(replace(control, id="om_daily_bob", sender_id="ou_bob"),
                                                             max_pending=5, support_group_ids=(SUPPORT_GROUP,)))
        with patch("octopus.store._now", return_value=186_400_001):
            self.assertTrue(await self.store.enqueue_control(replace(control, id="om_after_window"),
                                                             max_pending=5, support_group_ids=(SUPPORT_GROUP,)))

    async def test_root_and_reply_use_same_topic_and_invalid_scope_cannot_be_rebound(self) -> None:
        first = group_message("om_topic", root="")
        session = await self.store.session(topic_key(), first.chat_id, first.sender_id, topic_id=first.topic_id)
        self.assertEqual(await self.admit(first), "accepted")
        reply = group_message("om_reply", sender="ou_bob")
        self.assertEqual(await self.admit(reply), "accepted")
        for invalid in (replace(reply, root_id="om_other"), replace(reply, chat_id="oc_other"),
                        replace(reply, chat_type="p2p")):
            with self.assertRaisesRegex(ValueError, "会话未绑定"):
                await self.store.admission(session.key, invalid, support_group_ids=(SUPPORT_GROUP,))
        with self.assertRaisesRegex(ValueError, "会话键"):
            await self.store.session(session.key, first.chat_id, first.sender_id)
        with self.assertRaisesRegex(ValueError, "群话题会话键"):
            await self.store.session("personal-key", first.chat_id, first.sender_id, topic_id=first.topic_id)
        with self.assertRaisesRegex(ValueError, "群话题会话键"):
            await self.store.session("p:group:oc_support:om_topic:extra", first.chat_id, first.sender_id,
                                     topic_id="om_topic:extra")
        await self.store.session("personal", first.chat_id, first.sender_id)
        with self.assertRaisesRegex(ValueError, "会话未绑定"):
            await self.store.admission("personal", group_message("om_scope_confusion"),
                                       support_group_ids=(SUPPORT_GROUP,))

    async def test_unknown_actor_can_be_denied_without_any_global_enabled_state(self) -> None:
        await self.topic()
        await self.admit(group_message("om_alice_queued"))
        await self.admit(group_message("om_bob_queued", sender="ou_bob"))
        self.assertIsNone(await self.store.member("ou_alice"))
        await self.store.set_member_enabled("ou_alice", False)
        self.assertEqual(await self.store.member("ou_alice"),
                         {"open_id": "ou_alice", "role": "member", "enabled": False})
        self.assertEqual(await self.admit(group_message("om_alice_denied")), "disabled")
        self.assertEqual(await self.job_states(), [
            ("om_alice_queued", "failed", MEMBER_DISABLED_ERROR), ("om_bob_queued", "queued", None)])
        await self.store.ensure_member("ou_alice")
        self.assertFalse((await self.store.member("ou_alice"))["enabled"])
        with self.assertRaisesRegex(ValueError, "成员不存在"):
            await self.store.set_member_enabled("ou_unknown", True)
        self.assertIsNone(await self.store.member("ou_unknown"))

    async def test_revocation_race_cannot_leave_new_group_work_queued(self) -> None:
        await self.topic()
        second = await self.open()
        result, _ = await asyncio.gather(
            self.admit(group_message("om_race")), second.set_member_enabled("ou_alice", False))
        self.assertIn(result, {"accepted", "disabled"})
        self.assertEqual(await self.store.next_jobs(), [])
        self.assertFalse((await self.store.member("ou_alice"))["enabled"])

    async def test_cancel_one_job_preserves_other_senders_and_running_jobs(self) -> None:
        await self.topic()
        await self.admit(group_message("om_running"))
        await self.admit(group_message("om_rejected"))
        await self.admit(group_message("om_other_sender", sender="ou_bob"))
        await self.store.start_job("om_running")
        self.assertFalse(await self.store.cancel_queued_job("om_running"))
        self.assertTrue(await self.store.cancel_queued_job("om_rejected"))
        self.assertFalse(await self.store.cancel_queued_job("om_rejected"))
        self.assertFalse(await self.store.cancel_queued_job("missing"))
        self.assertEqual(await self.job_states(), [
            ("om_running", "running", None), ("om_rejected", "failed", "任务已取消"),
            ("om_other_sender", "queued", None)])

    async def test_limits_remain_per_sender_across_shared_topics_and_chats(self) -> None:
        await self.topic()
        await self.topic(root="om_other_topic")
        await self.store.ensure_member("ou_alice")
        await self.store.session("private-alice", "oc_private", "ou_alice")
        self.assertEqual(await self.admit(group_message("om_first"), max_pending=1), "accepted")
        self.assertEqual(await self.admit(group_message("om_other", root="om_other_topic"), max_pending=1), "queue_full")
        private = replace(group_message("om_private"), chat_id="oc_private", chat_type="p2p", root_id="")
        self.assertEqual(await self.store.admission("private-alice", private,
                         max_pending=1, min_interval_seconds=0), "queue_full")
        self.assertEqual(await self.admit(group_message("om_bob", sender="ou_bob"), max_pending=1), "accepted")

    async def test_shared_topic_fifo_survives_cross_chat_actor_fairness(self) -> None:
        await self.topic()
        await self.store.ensure_member("ou_alice")
        await self.store.session("private-alice", "oc_private", "ou_alice")
        private = replace(group_message("om_private"), chat_id="oc_private", chat_type="p2p", root_id="")
        self.assertEqual(await self.store.admission("private-alice", private, min_interval_seconds=0), "accepted")
        self.assertEqual(await self.admit(group_message("om_earlier_alice")), "accepted")
        self.assertEqual(await self.admit(group_message("om_later_bob", sender="ou_bob")), "accepted")
        eligible = await self.store.next_jobs(eligible_only=True)
        self.assertEqual([job.id for job in eligible], ["om_private", "om_earlier_alice"])
        self.assertTrue(await self.store.cancel_queued_job("om_earlier_alice"))
        self.assertEqual([job.id for job in await self.store.next_jobs(eligible_only=True)],
                         ["om_private", "om_later_bob"])

    async def test_topic_data_restriction_survives_restart_and_only_new_thread_clears_it(self) -> None:
        session = await self.topic()
        other = await self.topic(root="om_other_topic")
        await self.store.restrict_session_data(session.key)
        await self.store.set_thread(session.key, "existing-thread")
        await self.store.close()
        reopened = await self.open(migrate=False)
        restored = await reopened.get_session(session.key)
        self.assertTrue(restored.data_context_restricted)
        self.assertEqual(restored.thread_id, "existing-thread")
        self.assertFalse((await reopened.get_session(other.key)).data_context_restricted)
        await reopened.restrict_session_data(session.key)
        self.assertTrue((await reopened.get_session(session.key)).data_context_restricted)
        await reopened.set_thread(session.key, None)
        self.assertFalse((await reopened.get_session(session.key)).data_context_restricted)
        await reopened.set_thread(session.key, "fresh-thread")
        self.assertFalse((await reopened.get_session(session.key)).data_context_restricted)
        await reopened.session("private", "oc_private", "ou_alice")
        for invalid in ("private", "missing"):
            with self.assertRaisesRegex(ValueError, "群话题会话不存在"):
                await reopened.restrict_session_data(invalid)

    async def test_schema_migration_preserves_existing_personal_threads(self) -> None:
        await self.store.session("legacy-personal", "oc_private", "ou_alice")
        await self.store.set_thread("legacy-personal", "existing-thread")
        await self.store.close()
        async with await AsyncConnection.connect(self.database_url, autocommit=True) as connection:
            await connection.execute(sql.SQL("ALTER TABLE {}.sessions DROP COLUMN scope, DROP COLUMN topic_id, DROP COLUMN data_context_restricted").format(
                sql.Identifier(self.schema)))
            await connection.execute(sql.SQL("UPDATE {}.meta SET value = '3' WHERE key = 'schema_version'").format(
                sql.Identifier(self.schema)))
        with self.assertRaisesRegex(ValueError, "数据库版本不匹配"):
            await self.open(migrate=False)
        reopened = await self.open()
        self.assertEqual(await reopened.get_session("legacy-personal"),
                         Session("legacy-personal", "oc_private", "ou_alice", "existing-thread"))
        async with await AsyncConnection.connect(self.database_url, autocommit=True) as connection:
            cursor = await connection.execute(sql.SQL("SELECT value FROM {}.meta WHERE key = 'schema_version'").format(
                sql.Identifier(self.schema)))
            self.assertEqual(await cursor.fetchone(), (SCHEMA_VERSION,))
        self.assertIsNotNone(await (await self.open(migrate=False)).get_session("legacy-personal"))

    async def test_allowlist_cannot_be_a_substring_container(self) -> None:
        await self.topic()
        for invalid in (SUPPORT_GROUP, ("bad:group",), (None,), [SUPPORT_GROUP]):
            with self.assertRaisesRegex(ValueError, "支持群"):
                await self.store.admission(topic_key(), group_message("om_invalid"), support_group_ids=invalid)
            with self.assertRaisesRegex(ValueError, "支持群"):
                await self.store.enqueue_control(group_message("om_invalid"), max_pending=5, support_group_ids=invalid)
