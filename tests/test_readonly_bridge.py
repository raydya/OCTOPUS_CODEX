from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock

from octopus.bridge import Bridge, POLICY_MIGRATION_REASON, PRIVACY_REFUSAL, Ticket
from octopus.contracts import CardAction, CodexEvent
from test_bridge import MemoryStore, FakeCodex, FakeMessenger, message, picture, until
from octopus.config import Config
from octopus.access import APPLICATION_HELP


class MembersStore(MemoryStore):
    def __init__(self):
        super().__init__(("ou_alice",))
        self.people = {"ou_alice": {"open_id": "ou_alice", "role": "admin", "enabled": True}}
        self.audits = []
        self.admission_result = "accepted"

    async def ensure_member(self, actor, role="member"):
        self.people.setdefault(actor, {"open_id": actor, "role": role, "enabled": True})

    async def member(self, actor):
        return self.people.get(actor)

    async def members(self):
        return list(self.people.values())

    async def access_deliveries(self, limit=20):
        return []

    async def set_member_enabled(self, actor, enabled):
        self.people[actor]["enabled"] = enabled
        if not enabled:
            for key, session in self.sessions.items():
                if session.sender_id == actor:
                    await self.cancel_queued(key)

    async def admission(self, key, item, **kwargs):
        if self.admission_result != "accepted":
            return self.admission_result
        return "accepted" if await self.enqueue(key, item) else "duplicate"

    async def audit(self, *args):
        self.audits.append(args)

    async def enqueue_control(self, item, **kwargs):
        return await super().enqueue_control(item)

    async def next_jobs(self, **kwargs):
        return await super().next_jobs()


class Queries:
    def __init__(self):
        self.calls = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def check(self):
        pass

    async def call(self, tool, arguments, *, context):
        self.calls.append((tool, arguments, context))
        self.entered.set()
        await self.release.wait()
        return {"ok": True, "data": "private result"}


class ReadonlyBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store, self.codex, self.messenger, self.queries = MembersStore(), FakeCodex(), FakeMessenger(), Queries()
        self.config = Config("cli_test", "secret", "unused", "/isolated", None, None,
                             read_only=True, max_tool_calls=1, task_timeout=60)
        self.bridge = Bridge(self.config, self.store, self.messenger, self.codex, self.queries)
        await self.bridge.start()

    async def asyncTearDown(self):
        self.queries.release.set()
        await self.bridge.close()

    async def begin(self, actor="ou_alice", identifier="input", text="do work"):
        await self.bridge.receive(message(identifier, text, sender=actor))
        return await self.codex.next_turn()

    async def test_unlisted_and_revoked_users_cannot_submit(self):
        await self.bridge.receive(message("outsider", sender="ou_eve"))
        await self.store.set_member_enabled("ou_alice", False)
        await self.bridge.receive(message("revoked"))
        self.assertEqual(self.store.jobs, {})

    async def test_unauthorized_image_gets_access_hint_without_download_or_codex(self):
        item = replace(message("outsider-image", "看看这个截图", sender="ou_eve"), image_keys=("img_private",))
        await self.bridge.receive(item)
        notice = await self.messenger.wait_record(lambda row: row.reply_to == item.id)
        self.assertEqual(notice.view.text, APPLICATION_HELP)
        self.assertEqual(self.messenger.download_calls, [])
        self.assertEqual(self.store.jobs, {})
        self.assertEqual(self.codex.turns, [])

    async def test_privacy_request_with_image_is_refused_before_download(self):
        item = replace(message("privacy-image", "帮我获取这个人的手机号"), image_keys=("img_private",))
        await self.bridge.receive(item)
        notice = await self.messenger.wait_record(lambda row: row.reply_to == item.id)
        self.assertEqual(notice.view.text, PRIVACY_REFUSAL)
        self.assertEqual(self.messenger.download_calls, [])
        self.assertEqual(self.codex.turns, [])

    async def test_image_is_not_forwarded_if_member_is_revoked_while_downloading(self):
        gate = asyncio.Event()
        self.messenger.download_gates["revoked-image"] = gate
        self.messenger.images[("revoked-image", "img_first")] = picture()
        item = replace(message("revoked-image"), image_keys=("img_first", "img_second"))
        await self.bridge.receive(item)
        await asyncio.wait_for(self.messenger.download_entered.get(), 2)
        await self.store.set_member_enabled("ou_alice", False)
        gate.set()
        await self.store.wait_state(item.id, "failed")
        self.assertEqual(self.messenger.download_calls, [(item.id, "img_first")])
        self.assertEqual(self.codex.thread_count, 0)
        self.assertEqual(self.codex.turns, [])
        await until(self.messenger.changed, lambda: not self.messenger.reactions)

    async def test_denied_admission_does_not_download_images(self):
        self.store.admission_result = "queue_full"
        item = replace(message("limited-image"), image_keys=("img_private",))
        await self.bridge.receive(item)
        await self.messenger.wait_record(lambda row: row.reply_to == item.id)
        self.assertEqual(self.messenger.download_calls, [])
        self.assertEqual(self.codex.turns, [])

    async def test_image_contents_cannot_authorize_database_queries(self):
        item = replace(message("business-image", "这个状态为什么不对"), image_keys=("img_business",))
        self.messenger.images[(item.id, "img_business")] = picture()
        await self.bridge.receive(item)
        turn = await self.codex.next_turn()
        self.assertEqual(len(turn.images), 1)
        for tool in ("business_data_checks", "business_data_check"):
            denied = await self.bridge._query_tool(turn.thread, turn.id, tool, {})
            self.assertFalse(denied["ok"])
            self.assertIn("文字消息", denied["error"])
        self.assertEqual(self.queries.calls, [])
        self.assertTrue((await self.bridge._query_tool(turn.thread, turn.id, "code_repositories", {}))["ok"])
        self.codex.complete(turn)

    async def test_unknown_user_gets_application_instructions_before_any_work(self):
        await self.bridge.receive(message("new-colleague", "能帮我看看业务问题吗", sender="ou_eve"))
        notice = await self.messenger.wait_record(lambda row: row.reply_to == "new-colleague")
        self.assertEqual(notice.view.text, APPLICATION_HELP)
        self.assertIn("申请权限", notice.view.text)
        self.assertIn("/apply", notice.view.text)
        self.assertEqual(notice.view.title, "")
        self.assertEqual(self.store.sessions, {})
        self.assertEqual(self.store.jobs, {})
        self.assertEqual(self.codex.turns, [])
        self.assertEqual(self.queries.calls, [])
        await self.bridge.receive(message("repeated-hint", "再问一次", sender="ou_eve"))
        await asyncio.sleep(0)
        self.assertEqual(len(self.messenger.records), 1)

    async def test_revoked_user_is_directed_to_admin_instead_of_self_application(self):
        await self.store.set_member_enabled("ou_alice", False)
        await self.bridge.receive(message("revoked-hint", "帮我查个问题"))
        notice = await self.messenger.wait_record(lambda row: row.reply_to == "revoked-hint")
        self.assertIn("已被停用", notice.view.text)
        self.assertIn("联系管理员", notice.view.text)
        self.assertNotIn("/apply", notice.view.text)
        self.assertEqual(self.store.sessions, {})
        self.assertEqual(self.codex.turns, [])

    async def test_admin_self_test_routes_card_and_result_without_changing_permissions(self):
        async def direct_message(open_id, view, *, request_id=None):
            return await self.messenger.send(open_id, view)

        self.messenger.send_user = AsyncMock(side_effect=direct_message)
        original_members = [dict(member) for member in await self.store.members()]
        await self.bridge.receive(message("test-command", "/access-test"))
        review = await self.messenger.wait_record(
            lambda row: row.chat == "ou_alice" and bool(row.view.actions),
        )
        # Let the completed command bind its card ID before the simulated click.
        await self.messenger.wait_record(lambda row: row.reply_to == "test-command")
        self.assertIn("测试", review.view.title)
        action = CardAction(review.view.actions[0].ticket_id, "accept", "ou_alice", "oc_shared", review.id)
        await self.bridge.receive_action(action)
        await self.messenger.wait_record(
            lambda row: row.chat == "ou_alice" and row.operation == "send" and not row.view.actions,
        )
        self.assertEqual(await self.store.members(), original_members)
        self.assertEqual(self.store.jobs, {})
        self.assertEqual(self.codex.turns, [])
        self.assertEqual(self.codex.approvals, [])
        self.assertEqual(self.queries.calls, [])
        self.assertEqual(self.messenger.send_user.await_count, 2)
    async def test_scope_namespace_never_resumes_personal_thread(self):
        key = "oc_shared:ou_alice"
        await self.store.session(key, "oc_shared", "ou_alice")
        await self.store.set_thread(key, "personal-thread")
        await self.store.enqueue(key, message("old-queued"))
        turn = await self.begin()
        self.assertNotEqual(turn.thread, "personal-thread")
        self.assertNotEqual(self.store.jobs["old-queued"].state, "queued")
        self.assertIn("readonly:v2:oc_shared:ou_alice", self.store.sessions)

    async def test_scope_namespace_never_resumes_previous_readonly_policy(self):
        key = "readonly:v1:oc_shared:ou_alice"
        await self.store.session(key, "oc_shared", "ou_alice")
        await self.store.set_thread(key, "old-policy-thread")
        await self.store.enqueue(key, message("old-policy-queued"))
        turn = await self.begin()
        self.assertNotEqual(turn.thread, "old-policy-thread")
        self.assertNotEqual(self.store.jobs["old-policy-queued"].state, "queued")

    async def test_startup_retires_blocked_legacy_queue_and_interrupts_old_turn_without_resume(self):
        await self.bridge.close()
        key = "readonly:v1:oc_shared:ou_alice"
        await self.store.session(key, "oc_shared", "ou_alice")
        await self.store.set_thread(key, "old-thread")
        await self.store.enqueue(key, message("legacy-running"))
        await self.store.start_job("legacy-running")
        await self.store.enqueue(key, message("legacy-queued"))
        await self.store.save_reaction("legacy-queued", "legacy-reaction")
        await self.store.save_result(key, "retained previous result", "completed")
        self.codex, self.messenger = FakeCodex(), FakeMessenger()
        self.codex.remote_active_turns["old-thread"] = "old-turn"
        self.messenger.reactions["legacy-queued"] = "legacy-reaction"
        self.bridge = Bridge(self.config, self.store, self.messenger, self.codex, self.queries)
        await self.bridge.start()
        await self.messenger.wait_record(lambda record: record.view.title == "查询策略已更新")
        await until(self.messenger.changed, lambda: not self.messenger.reactions)
        self.assertEqual(self.store.jobs["legacy-queued"].state, "failed")
        self.assertEqual(self.store.jobs["legacy-queued"].error, POLICY_MIGRATION_REASON)
        self.assertEqual(self.store.jobs["legacy-queued"].message.id, "legacy-queued")
        self.assertEqual(self.store.results[key]["text"], "retained previous result")
        self.assertEqual(self.store.sessions[key].thread_id, "old-thread")
        self.assertEqual(self.codex.interrupts, [("old-thread", "old-turn")])
        self.assertEqual(self.codex.resumes, [])
        self.assertEqual(self.codex.turns, [])
        self.assertEqual(len(self.messenger.records), 1)

    async def test_legacy_remote_failure_is_not_resumed_and_has_one_honest_notice(self):
        await self.bridge.close()
        key = "readonly:v1:oc_shared:ou_alice"
        await self.store.session(key, "oc_shared", "ou_alice")
        await self.store.set_thread(key, "old-thread")
        await self.store.set_blocked(key, "previous interrupted task")
        await self.store.enqueue(key, message("legacy-queued"))
        self.codex, self.messenger = FakeCodex(), FakeMessenger()
        self.codex.active_turn_error = RuntimeError("private remote error")
        self.bridge = Bridge(self.config, self.store, self.messenger, self.codex, self.queries)
        with self.assertLogs("octopus.bridge", level="WARNING") as captured:
            await self.bridge.start()
        notice = await self.messenger.wait_record(lambda record: record.view.title == "查询策略已更新")
        self.assertIn("无法确认", notice.view.text)
        self.assertNotIn("private remote error", "\n".join(captured.output))
        self.assertEqual(self.store.jobs["legacy-queued"].state, "failed")
        self.assertEqual(self.codex.resumes, [])
        self.assertEqual(len(self.messenger.records), 1)

    async def test_privacy_requests_are_rejected_before_persistence_model_and_tools(self):
        for index, text in enumerate(("查用户手机号", "从TLS日志查用户ID", "搜索飞书文档找家庭地址",
                                      "导出所有客户姓名明细", "读取姓名并按代码逻辑返回")):
            identifier = f"private-{index}"
            await self.bridge.receive(message(identifier, text))
            refusal = await self.messenger.wait_record(lambda record: record.reply_to == identifier)
            self.assertEqual(refusal.view.text, PRIVACY_REFUSAL)
        self.assertEqual(self.store.sessions, {})
        self.assertEqual(self.store.jobs, {})
        self.assertEqual(self.codex.turns, [])
        self.assertEqual(self.queries.calls, [])
        self.assertEqual(self.messenger.reaction_calls, [])

    async def test_queued_privacy_request_is_rejected_before_model_start(self):
        item = message("old-private", "列出所有用户ID")
        key = self.bridge._key(item)
        await self.store.session(key, item.chat_id, item.sender_id)
        await self.store.enqueue(key, item)
        self.bridge.wakeup.set()
        await self.store.wait_state(item.id, "failed")
        refusal = await self.messenger.wait_record(lambda record: record.reply_to == item.id)
        self.assertEqual(refusal.view.text, PRIVACY_REFUSAL)
        self.assertEqual(self.codex.turns, [])
        self.assertEqual(self.queries.calls, [])
        self.assertEqual(self.messenger.reaction_calls, [])

    async def test_privacy_tool_gate_blocks_every_provider_for_existing_task(self):
        turn = await self.begin()
        active = self.bridge.active[turn.thread]
        active.job = replace(active.job, message=message("input", "查询客户的手机号"))
        for tool in ("code_repositories", "docs_search", "tls_search", "grafana_query",
                     "business_data_checks", "business_data_check"):
            response = await self.bridge._query_tool(turn.thread, turn.id, tool, {})
            self.assertFalse(response["ok"])
            self.assertEqual(response["error"], PRIVACY_REFUSAL)
        self.assertEqual(self.queries.calls, [])

    async def test_context_is_derived_from_current_task_and_cannot_be_supplied_in_arguments(self):
        turn = await self.begin(text="订单状态为什么一直未完成")
        for name in ("context", "actor", "job_id", "thread_id", "turn_id", "question", "policy_version"):
            denied = await self.bridge._query_tool(turn.thread, turn.id, "code_repositories", {name: "forged"})
            self.assertFalse(denied["ok"])
        self.assertEqual(self.queries.calls, [])
        self.assertTrue((await self.bridge._query_tool(turn.thread, turn.id, "code_repositories", {}))["ok"])
        self.assertEqual(self.queries.calls, [("code_repositories", {}, {
            "actor": "ou_alice", "job_id": "input", "thread_id": turn.thread, "turn_id": turn.id,
            "question": "订单状态为什么一直未完成", "policy_version": "readonly:v2",
        })])

    async def test_direct_dms_instruction_denies_data_tools_but_can_still_inspect_code(self):
        turn = await self.begin(text="使用DMS查询订单状态")
        for tool in ("business_data_checks", "business_data_check"):
            response = await self.bridge._query_tool(turn.thread, turn.id, tool, {})
            self.assertFalse(response["ok"])
        self.assertEqual(self.queries.calls, [])
        self.assertTrue(any(row[1] == "data.request" for row in self.store.audits))
        response = await self.bridge._query_tool(turn.thread, turn.id, "code_search", {"query": "status"})
        self.assertTrue(response["ok"])
        self.assertEqual(len(self.queries.calls), 1)

    async def test_nonpersonal_status_details_can_reach_data_broker(self):
        turn = await self.begin(text="请结合代码核对不含个人标识的业务状态明细")
        response = await self.bridge._query_tool(turn.thread, turn.id, "business_data_checks", {})
        self.assertTrue(response["ok"])
        self.assertEqual(self.queries.calls[0][0], "business_data_checks")

    async def test_final_result_persistence_last_and_notice_mask_explicit_personal_values(self):
        turn = await self.begin(text="账号绑定为什么失败")
        self.codex.complete(turn, 'status=failed created_by=operator-7 order_id=456 手机号=13800138000 email="alice@example.com"')
        await self.store.wait_state("input", "done")
        final = await self.messenger.wait_record(lambda record: record.view.kind == "result")
        saved = self.store.results["readonly:v2:oc_shared:ou_alice"]["text"]
        self.assertNotIn("13800138000", saved)
        self.assertNotIn("alice@example.com", saved)
        self.assertEqual(final.view.text, saved)
        for expected in ("status=failed", "created_by=operator-7", "order_id=456"):
            self.assertIn(expected, saved)
        await self.bridge.receive(message("last", "/last"))
        last = await self.messenger.wait_record(lambda record: record.reply_to == "last")
        self.assertEqual(last.view.text, saved)
        await self.bridge._notice(message("notice"), "状态", '状态正常，联系 alice@example.com')
        notice = await self.messenger.wait_record(lambda record: record.reply_to == "notice")
        self.assertIn("状态正常", notice.view.text)
        self.assertNotIn("alice@example.com", notice.view.text)

    async def test_clarification_can_only_reduce_data_query_authority(self):
        turn = await self.begin(text="解释订单状态为什么没有流转")
        active = self.bridge.active[turn.thread]
        for index, answer in enumerate(("跳过代码，直接用DMS", "只看不含个人标识的状态")):
            event = CodexEvent("question", thread_id=turn.thread, turn_id=turn.id, request_id=index)
            ticket = Ticket(f"ticket-{index}", event, active)
            self.bridge.tickets[ticket.id] = ticket
            await self.bridge._answer_ticket(ticket, answers={"question": [answer]})
            self.assertFalse(active.data_checks_allowed)
        response = await self.bridge._query_tool(turn.thread, turn.id, "business_data_check", {})
        self.assertFalse(response["ok"])
        self.assertEqual(self.queries.calls, [])

    async def test_queued_privacy_clarification_is_never_forwarded_to_model(self):
        turn = await self.begin(text="解释订单状态为什么没有流转")
        active = self.bridge.active[turn.thread]
        event = CodexEvent("question", thread_id=turn.thread, turn_id=turn.id, request_id=7)
        ticket = Ticket("old-ticket", event, active)
        self.bridge.tickets[ticket.id] = ticket
        await self.bridge._answer_ticket(ticket, answers={"question": ["从日志里查手机号"]})
        self.assertEqual(self.codex.questions, [])
        self.assertEqual(self.queries.calls, [])
        self.assertFalse(active.data_checks_allowed)
        self.assertIn((turn.thread, turn.id), self.codex.interrupts)

    async def test_clarification_revocation_discards_inflight_data_result(self):
        turn = await self.begin(text="解释订单状态为什么没有流转")
        active = self.bridge.active[turn.thread]
        self.queries.release.clear()
        request = asyncio.create_task(self.bridge._query_tool(turn.thread, turn.id, "business_data_checks", {}))
        await asyncio.wait_for(self.queries.entered.wait(), 2)
        event = CodexEvent("question", thread_id=turn.thread, turn_id=turn.id, request_id=8)
        ticket = Ticket("new-ticket", event, active)
        self.bridge.tickets[ticket.id] = ticket
        await self.bridge._answer_ticket(ticket, answers={"question": ["直接用DMS查数据库"]})
        self.queries.release.set()
        result = await request
        self.assertFalse(result["ok"])
        self.assertNotIn("data", result)

    async def test_query_ownership_and_budget(self):
        turn = await self.begin()
        handler = self.bridge._query_tool
        self.assertFalse((await handler(turn.thread, "wrong", "code_repositories", {}))["ok"])
        self.assertFalse((await handler(turn.thread, turn.id, "shell", {}))["ok"])
        self.assertTrue((await handler(turn.thread, turn.id, "code_repositories", {}))["ok"])
        self.assertFalse((await handler(turn.thread, turn.id, "code_repositories", {}))["ok"])
        self.assertEqual(len(self.queries.calls), 1)
        self.assertTrue(any(x[1] == "query.result" for x in self.store.audits))

    async def test_revocation_discards_inflight_query_and_final_delivery(self):
        turn = await self.begin()
        self.queries.release.clear()
        request = asyncio.create_task(self.bridge._query_tool(turn.thread, turn.id, "code_repositories", {}))
        await self.queries.entered.wait()
        await self.store.set_member_enabled("ou_alice", False)
        await self.bridge._revoke_active()
        self.queries.release.set()
        self.assertFalse((await request)["ok"])
        self.assertFalse(self.codex.interrupted.empty())
        self.codex.complete(turn, "sensitive answer")
        await asyncio.wait_for(self.bridge.idle.wait(), 2)
        self.assertEqual(self.messenger.records, [])

    async def test_member_cannot_manage_members_and_admin_can_disable_member(self):
        await self.store.ensure_member("ou_bob")
        await self.bridge._command(message("denied", "/member-add ou_eve", sender="ou_bob"))
        self.assertIsNone(await self.store.member("ou_eve"))
        await self.bridge._command(message("allowed", "/member-add ou_eve"))
        self.assertTrue((await self.store.member("ou_eve"))["enabled"])
        await self.bridge._command(message("disable", "/member-disable ou_eve"))
        await self.bridge._command(message("no-reenable", "/member-add ou_eve"))
        self.assertFalse((await self.store.member("ou_eve"))["enabled"])

    async def test_admission_rejection_does_not_run_model_or_add_reaction(self):
        self.store.admission_result = "daily_limit"
        await self.bridge.receive(message("limited"))
        await self.messenger.wait_record(lambda r: r.reply_to == "limited")
        self.assertEqual(self.codex.turns, [])
        self.assertEqual(self.messenger.reactions, {})

    async def test_readonly_approval_cannot_escalate(self):
        turn = await self.begin()
        await self.bridge._ask(self.bridge.active[turn.thread], CodexEvent(
            "approval", thread_id=turn.thread, turn_id=turn.id, request_id=3, kind="command"))
        self.assertEqual(self.bridge.tickets, {})
        self.assertEqual(self.codex.approvals, [(3, "decline")])
