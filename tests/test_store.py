from __future__ import annotations

import asyncio
import os
import unittest
import uuid
from dataclasses import replace
from unittest.mock import patch

from psycopg import AsyncConnection, sql

from octopus.contracts import IncomingMessage
from octopus.store import CONTROL_INTERRUPTED_ERROR, INTERRUPTED_ERROR, MEMBER_DISABLED_ERROR, Session, Store


def message(id: str) -> IncomingMessage:
    return IncomingMessage(id, "chat", "owner", f"任务 {id}", "p2p", 1000)


@unittest.skipUnless(os.environ.get("OCTOPUS_TEST_DATABASE_URL"), "Set OCTOPUS_TEST_DATABASE_URL to run PostgreSQL integration tests")
class StoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.database_url = os.environ["OCTOPUS_TEST_DATABASE_URL"]
        self.schema = f"octopus_test_{uuid.uuid4().hex}"
        self.addAsyncCleanup(self.drop_schema)
        self.store = await self.open()
        await self.store.session("session", "chat", "owner")

    async def drop_schema(self) -> None:
        async with await AsyncConnection.connect(self.database_url, autocommit=True) as connection:
            await connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(self.schema)))

    async def open(self, app_id: str = "cli_independent_app") -> Store:
        store = await Store.open(self.database_url, app_id, schema=self.schema)
        self.addAsyncCleanup(store.close)
        return store

    async def job_states(self) -> list[tuple[str, str, str | None]]:
        async with await AsyncConnection.connect(self.database_url, autocommit=True) as connection:
            cursor = await connection.execute(sql.SQL("SELECT id, state, error FROM {}.jobs ORDER BY sequence").format(sql.Identifier(self.schema)))
            return await cursor.fetchall()

    async def control_states(self) -> list[tuple[str, str, str | None]]:
        async with await AsyncConnection.connect(self.database_url, autocommit=True) as connection:
            cursor = await connection.execute(sql.SQL("SELECT id, state, error FROM {}.controls ORDER BY sequence").format(sql.Identifier(self.schema)))
            return await cursor.fetchall()

    async def test_inbound_dedup_is_separate_from_persistent_jobs(self) -> None:
        self.assertTrue(await self.store.claim_message("message-1"))
        self.assertFalse(await self.store.claim_message("message-1"))
        self.assertTrue(await self.store.enqueue("session", message("message-1")))
        self.assertFalse(await self.store.enqueue("session", message("message-1")))
        await self.store.close()
        reopened = await self.open()
        self.assertFalse(await reopened.claim_message("message-1"))
        self.assertFalse(await reopened.enqueue("session", message("message-1")))
        self.assertEqual([job.message for job in await reopened.next_jobs()], [message("message-1")])

    async def test_order_and_atomic_claim_across_connections(self) -> None:
        for id in ("z", "a", "m"):
            await self.store.enqueue("session", message(id))
        self.assertEqual([job.id for job in await self.store.next_jobs()], ["z", "a", "m"])
        second = await self.open()
        claims = await asyncio.gather(self.store.start_job("z"), second.start_job("z"))
        self.assertEqual(sorted(claims), [False, True])
        self.assertFalse(await second.start_job("unknown"))
        self.assertEqual([job.id for job in await second.next_jobs()], ["a", "m"])
        self.assertEqual(await second.pending_count("session"), 2)
        await self.store.finish_job("z")
        self.assertFalse(await second.start_job("z"))

    async def test_restart_fails_interrupted_job_without_replaying_it(self) -> None:
        await self.store.enqueue("session", message("running"))
        await self.store.enqueue("session", message("queued"))
        await self.store.start_job("running")
        await self.store.close()
        reopened = await self.open()
        recovered = await reopened.recover_interrupted()
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].id, "running")
        self.assertEqual(recovered[0].state, "failed")
        self.assertEqual(recovered[0].error, INTERRUPTED_ERROR)
        self.assertEqual(await reopened.blocked_session_keys(), {"session"})
        self.assertEqual((await reopened.get_session("session")).blocked_reason, INTERRUPTED_ERROR)
        self.assertEqual(await reopened.recover_interrupted(), [])
        self.assertEqual([job.id for job in await reopened.next_jobs()], ["queued"])
        self.assertFalse(await reopened.enqueue("session", message("running")))
        await reopened.finish_job("running")
        self.assertEqual(await self.job_states(), [("running", "failed", INTERRUPTED_ERROR), ("queued", "queued", None)])

    async def test_app_binding_and_persistent_sessions_and_owners(self) -> None:
        await self.store.set_thread("session", "thread-id")
        await self.store.add_owner("ou-owner")
        await self.store.add_owner("ou-owner")
        await self.store.close()
        with self.assertRaisesRegex(ValueError, "已绑定其他飞书应用"):
            await Store.open(self.database_url, "cli_another_app", schema=self.schema)
        reopened = await self.open()
        self.assertEqual(await reopened.get_session("session"), Session("session", "chat", "owner", "thread-id"))
        self.assertEqual(await reopened.owners(), ["ou-owner"])
        with self.assertRaisesRegex(ValueError, "已绑定其他聊天或用户"):
            await reopened.session("session", "different-chat", "owner")
        self.assertIsNone(await reopened.get_session("missing"))
        await reopened.set_thread("session", None)
        self.assertIsNone((await reopened.get_session("session")).thread_id)

    async def test_cancel_removes_only_queued_jobs_for_requested_session(self) -> None:
        await self.store.session("other-session", "other-chat", "owner")
        for id in ("running", "cancel-1", "cancel-2"):
            await self.store.enqueue("session", message(id))
        await self.store.enqueue("other-session", replace(message("other"), chat_id="other-chat"))
        await self.store.start_job("running")
        self.assertEqual(await self.store.cancel_queued("session"), 2)
        self.assertEqual(await self.store.cancel_queued("session"), 0)
        self.assertEqual(await self.store.pending_count("session"), 0)
        self.assertEqual([job.id for job in await self.store.next_jobs()], ["other"])
        self.assertEqual([job.id for job in await self.store.recover_interrupted()], ["running"])

    async def test_policy_retirement_covers_all_legacy_queues_without_deleting_history(self) -> None:
        current = "readonly:v2:current"
        blocked = "readonly:v1:blocked"
        reason = "查询策略已更新，请重新发送任务"
        await self.store.session(current, "chat", "owner")
        await self.store.session(blocked, "chat", "owner")
        await self.store.set_thread(blocked, "old-thread")
        await self.store.set_blocked(blocked, "旧会话暂停")
        await self.store.save_result(blocked, "已有结果", "completed")
        for id in ("running", "done", "failed"):
            await self.store.enqueue("session", message(id))
            await self.store.start_job(id)
        await self.store.finish_job("done")
        await self.store.finish_job("failed", "原错误")
        old_ids = [f"old-{index:03d}" for index in range(101)]
        for id in old_ids:
            await self.store.enqueue("session", message(id))
        await self.store.enqueue(current, message("current"))
        await self.store.enqueue(blocked, message("blocked"))
        await self.store.save_reaction("blocked", "reaction-to-remove")
        old_ids.append("blocked")

        retired = await self.store.retire_legacy_jobs("readonly:v2:", reason)

        self.assertEqual([job.id for job in retired], old_ids)
        self.assertEqual([job.message for job in retired], [message(id) for id in old_ids])
        self.assertTrue(all(job.state == "failed" and job.error == reason for job in retired))
        self.assertEqual(retired[-1].reaction_id, "reaction-to-remove")
        self.assertEqual(await self.store.finished_reactions(), [("blocked", "reaction-to-remove")])
        self.assertEqual(await self.job_states(), [
            ("running", "running", None), ("done", "done", None), ("failed", "failed", "原错误"),
            *[(id, "failed", reason) for id in old_ids[:-1]],
            ("current", "queued", None), ("blocked", "failed", reason),
        ])
        self.assertEqual(await self.store.get_session(blocked), Session(blocked, "chat", "owner", "old-thread", "旧会话暂停"))
        self.assertEqual(await self.store.last_result(blocked), {"text": "已有结果", "status": "completed"})
        self.assertEqual([job.id for job in await self.store.next_jobs()], ["current"])
        self.assertEqual(await self.store.retire_legacy_jobs("readonly:v2:", reason), [])
        self.assertFalse(await self.store.enqueue(blocked, message("blocked")))
        self.assertEqual([job.id for job in await self.store.recover_interrupted()], ["running"])

    async def test_policy_retirement_uses_literal_prefix_and_is_atomic_across_connections(self) -> None:
        prefix = "readonly:%_:"
        for key, id in ((prefix + "current", "literal"), ("readonly:v2:old", "wildcard-lookalike")):
            await self.store.session(key, "chat", "owner")
            await self.store.enqueue(key, message(id))
        second = await self.open()
        batches = await asyncio.gather(
            self.store.retire_legacy_jobs(prefix, "策略已更新"),
            second.retire_legacy_jobs(prefix, "策略已更新"),
        )
        self.assertEqual([job.id for batch in batches for job in batch], ["wildcard-lookalike"])
        self.assertEqual([job.id for job in await self.store.next_jobs()], ["literal"])

    async def test_policy_retirement_rejects_invalid_metadata_without_changing_queue(self) -> None:
        await self.store.enqueue("session", message("queued"))
        for prefix in (None, 1, "", "  ", "v2\n", "x" * 129):
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                await self.store.retire_legacy_jobs(prefix, "策略已更新")
        for reason in (None, 1, "", "  ", "原因\x7f", "x" * 513):
            with self.subTest(reason=reason), self.assertRaises(ValueError):
                await self.store.retire_legacy_jobs("readonly:v2:", reason)
        self.assertEqual(await self.job_states(), [("queued", "queued", None)])

    async def test_finished_and_failed_jobs_keep_their_terminal_state(self) -> None:
        for id in ("success", "failure"):
            await self.store.enqueue("session", message(id))
            await self.store.start_job(id)
        await self.store.finish_job("success")
        await self.store.finish_job("failure", "Codex unavailable")
        await self.store.finish_job("failure")
        self.assertEqual(await self.job_states(), [("success", "done", None), ("failure", "failed", "Codex unavailable")])
        self.assertEqual(await self.store.next_jobs(), [])

    async def test_reactions_survive_restart_and_clear_only_the_expected_id(self) -> None:
        await self.store.enqueue("session", message("waiting"))
        self.assertIsNone(await self.store.get_reaction("waiting"))
        self.assertIsNone(await self.store.get_reaction("missing"))
        await self.store.save_reaction("waiting", "reaction-old")
        await self.store.close()
        reopened = await self.open()
        self.assertEqual(await reopened.get_reaction("waiting"), "reaction-old")
        self.assertEqual((await reopened.next_jobs())[0].reaction_id, "reaction-old")
        await reopened.save_reaction("waiting", "reaction-new")
        await reopened.clear_reaction("waiting", "reaction-old")
        self.assertEqual(await reopened.get_reaction("waiting"), "reaction-new")
        await reopened.clear_reaction("waiting", "reaction-new")
        self.assertIsNone(await reopened.get_reaction("waiting"))
        await reopened.clear_reaction("waiting", "reaction-new")
        await reopened.clear_reaction("missing", "reaction-new")
        with self.assertRaisesRegex(ValueError, "任务不存在"):
            await reopened.save_reaction("missing", "reaction-new")

    async def test_finished_reactions_include_only_terminal_jobs_with_saved_reactions(self) -> None:
        for id in ("queued", "running", "done", "failed", "done-without-reaction"):
            await self.store.enqueue("session", message(id))
            if id != "done-without-reaction":
                await self.store.save_reaction(id, f"reaction-{id}")
            if id != "queued":
                await self.store.start_job(id)
        await self.store.finish_job("done")
        await self.store.finish_job("failed", "Codex unavailable")
        await self.store.finish_job("done-without-reaction")
        self.assertEqual(await self.store.finished_reactions(), [("done", "reaction-done"), ("failed", "reaction-failed")])
        await self.store.clear_reaction("done", "reaction-done")
        self.assertEqual(await self.store.finished_reactions(), [("failed", "reaction-failed")])
        # A late acknowledgement can arrive after cancellation/completion and must
        # still be discoverable for removal on the next cleanup pass.
        await self.store.save_reaction("done-without-reaction", "late-reaction")
        self.assertEqual(await self.store.finished_reactions(), [("failed", "reaction-failed"), ("done-without-reaction", "late-reaction")])

    async def test_cancelled_and_recovered_jobs_keep_reactions_available_for_cleanup(self) -> None:
        for id in ("cancelled", "interrupted"):
            await self.store.enqueue("session", message(id))
            await self.store.save_reaction(id, f"reaction-{id}")
        await self.store.start_job("interrupted")
        await self.store.cancel_queued("session")
        await self.store.close()
        reopened = await self.open()
        recovered = await reopened.recover_interrupted()
        self.assertEqual([(job.id, job.reaction_id) for job in recovered], [("interrupted", "reaction-interrupted")])
        self.assertEqual(await reopened.finished_reactions(), [("cancelled", "reaction-cancelled"), ("interrupted", "reaction-interrupted")])

    async def test_reaction_migration_preserves_jobs_in_existing_schema(self) -> None:
        await self.store.enqueue("session", message("existing"))
        await self.store.close()
        async with await AsyncConnection.connect(self.database_url, autocommit=True) as connection:
            await connection.execute(sql.SQL("ALTER TABLE {}.jobs DROP COLUMN reaction_id").format(sql.Identifier(self.schema)))
        reopened = await self.open()
        jobs = await reopened.next_jobs()
        self.assertEqual([job.message for job in jobs], [message("existing")])
        self.assertIsNone(jobs[0].reaction_id)
        await reopened.save_reaction("existing", "migrated-reaction")
        self.assertEqual(await reopened.get_reaction("existing"), "migrated-reaction")

    async def test_last_result_survives_restart_and_is_scoped_to_a_session(self) -> None:
        self.assertIsNone(await self.store.last_result("session"))
        await self.store.session("other-session", "other-chat", "owner")
        await self.store.save_result("session", "初次结果", "completed")
        await self.store.save_result("session", "飞书发送失败前保存的结果", "failed")
        await self.store.save_result("other-session", "另一个对话的结果", "interrupted")
        await self.store.close()
        reopened = await self.open()
        self.assertEqual(await reopened.last_result("session"), {"text": "飞书发送失败前保存的结果", "status": "failed"})
        self.assertEqual(await reopened.last_result("other-session"), {"text": "另一个对话的结果", "status": "interrupted"})
        self.assertIsNone(await reopened.last_result("unknown"))

    async def test_service_lock_excludes_second_bridge_and_releases_on_close(self) -> None:
        second = await self.open()
        self.assertTrue(await self.store.acquire_service_lock())
        self.assertTrue(await self.store.acquire_service_lock())
        self.assertFalse(await second.acquire_service_lock())
        await self.store.close()
        # Closing the client socket does not wait for the independent server
        # process to release its session locks. Startup also polls this lock.
        async with asyncio.timeout(2):
            while not await second.acquire_service_lock():
                await asyncio.sleep(0.01)

    async def test_concurrent_recovery_does_not_return_the_same_job_twice(self) -> None:
        await self.store.enqueue("session", message("running"))
        await self.store.start_job("running")
        second = await self.open()
        recovered = await asyncio.gather(self.store.recover_interrupted(), second.recover_interrupted())
        self.assertEqual([job.id for jobs in recovered for job in jobs], ["running"])
        result, owner_result = await asyncio.gather(self.store.recover_interrupted(), self.store.add_owner("ou-owner"))
        self.assertEqual(result, [])
        self.assertIsNone(owner_result)
        self.assertEqual(await self.store.owners(), ["ou-owner"])

    async def test_schema_names_cannot_inject_sql(self) -> None:
        with self.assertRaisesRegex(ValueError, "安全标识符"):
            await Store.open(self.database_url, "cli_independent_app", schema='bad"; DROP SCHEMA public CASCADE; --')
        self.assertEqual(await self.store.get_session("session"), Session("session", "chat", "owner", None))

    async def test_attach_requires_initialized_matching_schema_without_changing_it(self) -> None:
        attached = await Store.open(self.database_url, "cli_independent_app", schema=self.schema, migrate=False)
        self.addAsyncCleanup(attached.close)
        self.assertEqual(await attached.get_session("session"), Session("session", "chat", "owner", None))
        with self.assertRaisesRegex(ValueError, "已绑定其他飞书应用"):
            await Store.open(self.database_url, "cli_wrong_app", schema=self.schema, migrate=False)
        missing = f"octopus_missing_{uuid.uuid4().hex}"
        with self.assertRaisesRegex(ValueError, "尚未初始化"):
            await Store.open(self.database_url, "cli_independent_app", schema=missing, migrate=False)
        async with await AsyncConnection.connect(self.database_url, autocommit=True) as connection:
            cursor = await connection.execute("SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname = %s", (missing,))
            self.assertIsNone(await cursor.fetchone())
            await connection.execute(sql.SQL("UPDATE {}.meta SET value = 'outdated' WHERE key = 'schema_version'").format(sql.Identifier(self.schema)))
        with self.assertRaisesRegex(ValueError, "版本不匹配"):
            await Store.open(self.database_url, "cli_independent_app", schema=self.schema, migrate=False)
        with self.assertRaisesRegex(ValueError, "布尔值"):
            await Store.open(self.database_url, "cli_independent_app", schema=self.schema, migrate="false")

    async def test_session_pause_persists_until_explicitly_cleared(self) -> None:
        await self.store.session("other-session", "other-chat", "owner")
        await self.store.enqueue("session", message("paused-job"))
        self.assertEqual(await self.store.blocked_session_keys(), set())
        await self.store.set_blocked("session", "连接断开，执行状态未知")
        await self.store.close()
        reopened = await self.open()
        self.assertEqual(await reopened.blocked_session_keys(), {"session"})
        self.assertEqual((await reopened.get_session("session")).blocked_reason, "连接断开，执行状态未知")
        self.assertIsNone((await reopened.get_session("other-session")).blocked_reason)
        self.assertFalse(await reopened.start_job("paused-job"))
        self.assertEqual(await reopened.pending_count("session"), 1)
        await reopened.set_blocked("session", None)
        self.assertEqual(await reopened.blocked_session_keys(), set())
        self.assertIsNone((await reopened.get_session("session")).blocked_reason)
        self.assertTrue(await reopened.start_job("paused-job"))
        with self.assertRaisesRegex(ValueError, "会话不存在"):
            await reopened.set_blocked("unknown", "reason")

    async def test_control_queue_persists_deduplicates_and_claims_atomically(self) -> None:
        controls = [replace(message(id), text=text) for id, text in [("z", "/stop"), ("a", "/resume"), ("m", "/new")]]
        for control in controls:
            self.assertTrue(await self.store.enqueue_control(control))
        self.assertFalse(await self.store.enqueue_control(controls[0]))
        await self.store.close()
        reopened = await self.open()
        self.assertEqual(await reopened.next_controls(), controls)
        self.assertFalse(await reopened.enqueue_control(controls[0]))
        second = await self.open()
        claims = await asyncio.gather(reopened.start_control("z"), second.start_control("z"))
        self.assertEqual(sorted(claims), [False, True])
        self.assertFalse(await second.start_control("unknown"))
        self.assertEqual(await second.next_controls(), controls[1:])

    async def test_restart_recovers_controls_without_replaying_uncertain_commands(self) -> None:
        running = replace(message("running-control"), text="/resume")
        queued = replace(message("queued-control"), text="/stop")
        await self.store.enqueue_control(running)
        await self.store.enqueue_control(queued)
        await self.store.start_control(running.id)
        await self.store.close()
        reopened = await self.open()
        second = await self.open()
        recoveries = await asyncio.gather(reopened.recover_controls(), second.recover_controls())
        self.assertEqual([control for batch in recoveries for control in batch], [running])
        self.assertEqual(await reopened.next_controls(), [queued])
        self.assertFalse(await reopened.start_control(running.id))
        self.assertFalse(await reopened.enqueue_control(running))
        await reopened.finish_control(running.id)
        self.assertEqual(await reopened.recover_controls(), [])
        self.assertEqual(await self.control_states(), [(running.id, "failed", CONTROL_INTERRUPTED_ERROR), (queued.id, "queued", None)])
        self.assertTrue(await reopened.start_control(queued.id))

    async def test_control_completion_and_failure_are_terminal(self) -> None:
        for id in ("success-control", "failed-control", "queued-control"):
            await self.store.enqueue_control(replace(message(id), text="/stop"))
        await self.store.start_control("success-control")
        await self.store.start_control("failed-control")
        await self.store.finish_control("success-control")
        await self.store.finish_control("failed-control", "Codex unavailable")
        await self.store.finish_control("failed-control")
        await self.store.finish_control("queued-control")
        self.assertEqual(await self.control_states(), [
            ("success-control", "done", None),
            ("failed-control", "failed", "Codex unavailable"),
            ("queued-control", "queued", None),
        ])
        self.assertFalse(await self.store.start_control("success-control"))
        self.assertFalse(await self.store.start_control("failed-control"))

    async def test_limited_controls_require_membership_and_share_atomic_actor_budget(self) -> None:
        control = replace(message("unknown-control"), text="/stop")
        self.assertFalse(await self.store.enqueue_control(control, max_pending=1))
        await self.store.ensure_member("owner")
        second = await self.open()
        controls = [replace(message("first-control"), text="/stop"),
                    replace(message("second-control"), chat_id="other-chat", text="/status")]
        results = await asyncio.gather(
            self.store.enqueue_control(controls[0], max_pending=1),
            second.enqueue_control(controls[1], max_pending=1),
        )
        self.assertEqual(sorted(results), [False, True])
        accepted = (await self.store.next_controls())[0]
        self.assertTrue(await self.store.start_control(accepted.id))
        self.assertFalse(await second.enqueue_control(replace(control, id="while-running"), max_pending=1))
        await self.store.finish_control(accepted.id)
        self.assertFalse(await second.enqueue_control(accepted, max_pending=1))
        self.assertTrue(await second.enqueue_control(replace(control, id="after-completion"), max_pending=1))
        await self.store.set_member_enabled("owner", False)
        self.assertEqual(await self.store.next_controls(), [])
        self.assertFalse(await second.enqueue_control(replace(control, id="after-revocation"), max_pending=1))

    async def test_control_revocation_and_admission_cannot_leave_queued_work(self) -> None:
        await self.store.ensure_member("owner")
        second = await self.open()
        await asyncio.gather(
            self.store.enqueue_control(replace(message("race-control"), text="/status"), max_pending=5),
            second.set_member_enabled("owner", False),
        )
        self.assertEqual(await self.store.next_controls(), [])

    async def test_control_fetch_is_bounded_and_invalid_limits_are_rejected(self) -> None:
        for index in range(105):
            await self.store.enqueue_control(replace(message(f"control-{index}"), text="/status"))
        controls = await self.store.next_controls()
        self.assertEqual(len(controls), 100)
        self.assertEqual(controls[0].id, "control-0")
        self.assertEqual(controls[-1].id, "control-99")
        for limit in (0, -1, True, 1.5, 1001):
            with self.assertRaises(ValueError):
                await self.store.enqueue_control(message("bad-control"), max_pending=limit)

    async def test_control_daily_budget_is_atomic_and_expires_after_24_hours(self) -> None:
        await self.store.ensure_member("owner")
        await self.store.ensure_member("other")
        second = await self.open()
        async with await AsyncConnection.connect(self.database_url, autocommit=True) as connection:
            await connection.execute(
                sql.SQL("""INSERT INTO {}.controls (id, message_json, state, created_at, updated_at)
                           SELECT 'budget-' || number, jsonb_build_object('sender_id', 'owner'), 'done', 100000000, 100000000
                           FROM generate_series(1, 999) AS number""").format(sql.Identifier(self.schema))
            )
        with patch("octopus.store._now", return_value=100_001_000):
            results = await asyncio.gather(
                self.store.enqueue_control(replace(message("daily-first"), text="/status"), max_pending=5),
                second.enqueue_control(replace(message("daily-second"), text="/help"), max_pending=5),
            )
            self.assertEqual(sorted(results), [False, True])
            self.assertTrue(await self.store.enqueue_control(replace(message("other-budget"), sender_id="other", text="/status"), max_pending=5))
        with patch("octopus.store._now", return_value=186_400_001):
            self.assertTrue(await self.store.enqueue_control(replace(message("after-budget-window"), text="/status"), max_pending=5))

    async def test_members_migrate_owners_without_reenabling_or_promoting_existing_records(self) -> None:
        async with await AsyncConnection.connect(self.database_url, autocommit=True) as connection:
            await connection.execute(sql.SQL("INSERT INTO {}.owners (open_id, created_at) VALUES ('legacy-owner', 1000)").format(sql.Identifier(self.schema)))
        await self.store.ensure_member("shared-user")
        await self.store.set_member_enabled("shared-user", False)
        await self.store.add_owner("shared-user")
        await self.store.close()
        reopened = await self.open()
        self.assertEqual(await reopened.member("legacy-owner"), {"open_id": "legacy-owner", "role": "admin", "enabled": True})
        self.assertEqual(await reopened.member("shared-user"), {"open_id": "shared-user", "role": "member", "enabled": False})
        await reopened.set_member_enabled("legacy-owner", False)
        await reopened.ensure_member("legacy-owner", "admin")
        await reopened.ensure_member("shared-user", "admin")
        await reopened.close()
        reopened = await self.open()
        self.assertEqual(await reopened.members(), [
            {"open_id": "legacy-owner", "role": "admin", "enabled": False},
            {"open_id": "shared-user", "role": "member", "enabled": False},
        ])
        self.assertIsNone(await reopened.member("unknown"))
        await reopened.add_owner("new-owner")
        self.assertEqual(await reopened.member("new-owner"), {"open_id": "new-owner", "role": "admin", "enabled": True})

    async def test_admission_requires_enabled_membership_and_matching_session_identity(self) -> None:
        self.assertEqual(await self.store.admission("session", message("unknown-member")), "disabled")
        await self.store.ensure_member("owner")
        for invalid in (replace(message("bad-sender"), sender_id="other"), replace(message("bad-chat"), chat_id="other")):
            with self.assertRaisesRegex(ValueError, "会话未绑定"):
                await self.store.admission("session", invalid)
        with self.assertRaisesRegex(ValueError, "会话未绑定"):
            await self.store.admission("missing-session", message("missing-session"))
        self.assertEqual(await self.store.admission("session", message("accepted")), "accepted")
        self.assertEqual(await self.store.admission("session", message("accepted")), "duplicate")
        await self.store.set_member_enabled("owner", False)
        self.assertEqual(await self.store.admission("session", message("disabled")), "disabled")
        self.assertEqual(await self.job_states(), [("accepted", "failed", MEMBER_DISABLED_ERROR)])

    async def test_admission_enforces_shared_actor_budget_atomically_across_connections_and_chats(self) -> None:
        await self.store.ensure_member("owner")
        await self.store.session("other-session", "other-chat", "owner")
        second = await self.open()
        outcomes = await asyncio.gather(
            self.store.admission("session", message("first"), max_pending=1, min_interval_seconds=0),
            second.admission("other-session", replace(message("second"), chat_id="other-chat"), max_pending=1, min_interval_seconds=0),
        )
        self.assertEqual(sorted(outcomes), ["accepted", "queue_full"])
        accepted = (await self.store.next_jobs())[0]
        self.assertTrue(await self.store.start_job(accepted.id))
        self.assertEqual(await second.admission("session", message("while-running"), max_pending=1, min_interval_seconds=0), "queue_full")
        await self.store.finish_job(accepted.id)
        self.assertEqual(await second.admission("session", message("after-completion"), max_pending=1, daily_limit=1, min_interval_seconds=0), "daily_limit")
        async with await AsyncConnection.connect(self.database_url, autocommit=True) as connection:
            await connection.execute(sql.SQL("UPDATE {}.jobs SET created_at = 1").format(sql.Identifier(self.schema)))
        self.assertEqual(await second.admission("session", message("next-day"), max_pending=1, daily_limit=1, min_interval_seconds=0), "accepted")

    async def test_admission_rate_limit_is_atomic_and_only_accepted_tasks_consume_it(self) -> None:
        await self.store.ensure_member("owner")
        second = await self.open()
        with patch("octopus.store._now", return_value=100_000):
            outcomes = await asyncio.gather(
                self.store.admission("session", message("first"), min_interval_seconds=2),
                second.admission("session", message("second"), min_interval_seconds=2),
            )
        self.assertEqual(sorted(outcomes), ["accepted", "rate_limit"])
        self.assertEqual(len(await self.store.next_jobs()), 1)
        with patch("octopus.store._now", return_value=101_999):
            self.assertEqual(await self.store.admission("session", message("too-soon")), "rate_limit")
        with patch("octopus.store._now", return_value=102_000):
            self.assertEqual(await self.store.admission("session", message("next")), "accepted")

    async def test_revocation_and_admission_cannot_leave_new_queued_work(self) -> None:
        await self.store.ensure_member("owner")
        second = await self.open()
        result, _ = await asyncio.gather(
            self.store.admission("session", message("racing-revocation"), min_interval_seconds=0),
            second.set_member_enabled("owner", False),
        )
        self.assertIn(result, {"accepted", "disabled"})
        self.assertEqual(await self.store.next_jobs(), [])
        self.assertFalse((await self.store.member("owner"))["enabled"])

    async def test_revocation_cancels_only_members_queued_work_and_preserves_unknown_running_state(self) -> None:
        await self.store.ensure_member("owner")
        await self.store.ensure_member("other")
        await self.store.session("other-session", "other-chat", "other")
        for id in ("running", "queued"):
            await self.store.enqueue("session", message(id))
            await self.store.enqueue_control(replace(message(f"{id}-control"), text="/stop"))
        other_message = replace(message("other-queued"), chat_id="other-chat", sender_id="other")
        await self.store.enqueue("other-session", other_message)
        await self.store.enqueue_control(replace(other_message, id="other-control", text="/stop"))
        await self.store.start_job("running")
        await self.store.start_control("running-control")
        await self.store.set_member_enabled("owner", False)
        self.assertEqual(await self.job_states(), [("running", "running", None), ("queued", "failed", MEMBER_DISABLED_ERROR), ("other-queued", "queued", None)])
        self.assertEqual(await self.control_states(), [("running-control", "running", None), ("queued-control", "failed", MEMBER_DISABLED_ERROR), ("other-control", "queued", None)])
        await self.store.ensure_member("owner")
        self.assertFalse((await self.store.member("owner"))["enabled"])
        await self.store.set_member_enabled("owner", True)
        self.assertTrue((await self.store.member("owner"))["enabled"])
        self.assertEqual([job.id for job in await self.store.next_jobs()], ["other-queued"])

    async def test_job_listing_alternates_senders_across_chats_and_caps_each_fetch(self) -> None:
        await self.store.session("same-user-other-chat", "other-chat", "owner")
        await self.store.session("other-user", "chat", "other")
        await self.store.enqueue("session", message("owner-1"))
        await self.store.enqueue("same-user-other-chat", replace(message("owner-2"), chat_id="other-chat"))
        await self.store.enqueue("session", message("owner-3"))
        await self.store.enqueue("other-user", replace(message("other-1"), sender_id="other"))
        await self.store.enqueue("other-user", replace(message("other-2"), sender_id="other"))
        self.assertEqual([job.id for job in await self.store.next_jobs()], ["owner-1", "other-1", "owner-2", "other-2", "owner-3"])
        for index in range(100):
            await self.store.enqueue("session", message(f"extra-{index}"))
        self.assertEqual(len(await self.store.next_jobs()), 100)

    async def test_eligible_job_fetch_does_not_starve_behind_paused_backlog(self) -> None:
        await self.store.session("ready-session", "ready-chat", "owner")
        for index in range(100):
            await self.store.enqueue("session", message(f"paused-{index}"))
        await self.store.set_blocked("session", "review interrupted work")
        await self.store.enqueue("ready-session", replace(message("ready"), chat_id="ready-chat"))
        self.assertEqual(len(await self.store.next_jobs()), 100)
        self.assertEqual([job.id for job in await self.store.next_jobs(eligible_only=True)], ["ready"])
        with self.assertRaises(ValueError):
            await self.store.next_jobs(eligible_only="true")

    async def test_audit_stores_only_explicit_bounded_metadata(self) -> None:
        await self.store.audit("owner", "query", "tls:topic-id", "allow", "job-id")
        await self.store.audit("owner", "member.disable", "other", "deny")
        async with await AsyncConnection.connect(self.database_url, autocommit=True) as connection:
            cursor = await connection.execute(sql.SQL("SELECT actor, action, resource, decision, job_id FROM {}.audit_events ORDER BY id").format(sql.Identifier(self.schema)))
            self.assertEqual(await cursor.fetchall(), [("owner", "query", "tls:topic-id", "allow", "job-id"), ("owner", "member.disable", "other", "deny", None)])
        for args in (("", "query"), ("owner", "query\nresult"), ("owner", "query", "x" * 513), ("owner", "query", "", "x" * 33), ("owner", "query", "", "allow", "x" * 257)):
            with self.assertRaises(ValueError):
                await self.store.audit(*args)

    async def test_retention_removes_expired_payloads_but_keeps_active_work_and_dedup(self) -> None:
        await self.store.session("result-only", "result-chat", "owner")
        with patch("octopus.store._now", return_value=1_000):
            for id in ("old-done", "old-failed", "old-running", "old-queued"):
                await self.store.enqueue("session", message(id))
                await self.store.enqueue_control(replace(message(f"{id}-control"), text="/stop"))
                await self.store.claim_message(id)
                await self.store.claim_message(f"{id}-control")
                if id != "old-queued":
                    await self.store.start_job(id)
                    await self.store.start_control(f"{id}-control")
                if id in {"old-done", "old-failed"}:
                    error = "failure" if id == "old-failed" else None
                    await self.store.finish_job(id, error)
                    await self.store.finish_control(f"{id}-control", error)
            await self.store.claim_message("old-inbound-only")
            await self.store.save_result("result-only", "old result", "completed")
            await self.store.save_result("session", "active session previous result", "completed")
            await self.store.audit("owner", "old-query")
        await self.store.enqueue("session", message("recent"))
        await self.store.start_job("recent")
        await self.store.finish_job("recent")
        await self.store.audit("owner", "recent-query")
        deleted = await self.store.purge_history()
        self.assertEqual(deleted, {"jobs": 2, "controls": 2, "inbound_ids": 5, "last_results": 1, "audit_events": 1, "access_requests": 0, "access_deliveries": 0})
        self.assertEqual(await self.job_states(), [("old-running", "running", None), ("old-queued", "queued", None), ("recent", "done", None)])
        self.assertFalse(await self.store.claim_message("old-running"))
        self.assertFalse(await self.store.claim_message("old-queued-control"))
        self.assertTrue(await self.store.claim_message("old-done"))
        self.assertIsNone(await self.store.last_result("result-only"))
        self.assertIsNotNone(await self.store.last_result("session"))
        self.assertEqual(await self.store.purge_history(), {"jobs": 0, "controls": 0, "inbound_ids": 0, "last_results": 0, "audit_events": 0, "access_requests": 0, "access_deliveries": 0})

    async def test_governance_rejects_invalid_roles_limits_and_states(self) -> None:
        for role in ("owner", "", None, 1, []):
            with self.assertRaises(ValueError):
                await self.store.ensure_member("owner", role)
        for value in (0, -1, 1.5, True, 1_000_001):
            with self.assertRaises(ValueError):
                await self.store.admission("session", message("bad-limit"), daily_limit=value)
        for value in (0, -1, True, 1001):
            with self.assertRaises(ValueError):
                await self.store.admission("session", message("bad-pending"), max_pending=value)
        for value in (-1, True, float("nan"), float("inf"), 86401, "2"):
            with self.assertRaises(ValueError):
                await self.store.admission("session", message("bad-interval"), min_interval_seconds=value)
        for value in ("true", 1, None):
            with self.assertRaises(ValueError):
                await self.store.set_member_enabled("owner", value)
        with self.assertRaisesRegex(ValueError, "成员不存在"):
            await self.store.set_member_enabled("missing", False)
        for value in (0, -1, True, 3651):
            with self.assertRaises(ValueError):
                await self.store.purge_history(days=value)
            with self.assertRaises(ValueError):
                await self.store.purge_history(audit_days=value)


if __name__ == "__main__":
    unittest.main()
