from __future__ import annotations

import asyncio
import os
import unittest
import uuid
from dataclasses import replace

from psycopg import AsyncConnection, sql

from octopus.contracts import IncomingMessage
from octopus.store import CONTROL_INTERRUPTED_ERROR, INTERRUPTED_ERROR, Session, Store


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

    async def test_finished_and_failed_jobs_keep_their_terminal_state(self) -> None:
        for id in ("success", "failure"):
            await self.store.enqueue("session", message(id))
            await self.store.start_job(id)
        await self.store.finish_job("success")
        await self.store.finish_job("failure", "Codex unavailable")
        await self.store.finish_job("failure")
        self.assertEqual(await self.job_states(), [("success", "done", None), ("failure", "failed", "Codex unavailable")])
        self.assertEqual(await self.store.next_jobs(), [])

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
        self.assertTrue(await second.acquire_service_lock())

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


if __name__ == "__main__":
    unittest.main()
