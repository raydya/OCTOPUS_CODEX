from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock

from octopus.bridge import Bridge, PRIVACY_REFUSAL
from octopus.config import Config
from octopus.contracts import CodexEvent, Question
from test_bridge import FakeCodex, FakeMessenger, message, picture
from test_readonly_bridge import MembersStore, Queries


def group(identifier, text="为什么这个业务状态不正确", *, sender="ou_bob", root="om_topic",
          chat="oc_support", mentioned=True):
    return replace(message(identifier, text, sender=sender, chat=chat, kind="group"),
                   root_id=root, thread_id="omt_topic", mentioned_bot=mentioned)


class GroupBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store, self.codex, self.messenger, self.queries = MembersStore(), FakeCodex(), FakeMessenger(), Queries()
        self.messenger.fetch_thread_context = AsyncMock(return_value=())
        self.config = Config("cli_test", "secret", "unused", "/isolated", None, None,
                             read_only=True, allow_groups=True, support_group_ids=("oc_support",),
                             bot_open_id="ou_bot", max_tool_calls=10, task_timeout=60)
        self.bridge = Bridge(self.config, self.store, self.messenger, self.codex, self.queries)
        await self.bridge.start()

    async def asyncTearDown(self):
        self.queries.release.set()
        await self.bridge.close()

    async def begin(self, item):
        await self.bridge.receive(item)
        return await self.codex.next_turn()

    async def complete(self, item, turn):
        self.codex.complete(turn, "已根据证据定位问题")
        await self.messenger.wait_record(lambda row: row.reply_to == item.id and row.view.kind == "result")

    async def test_group_grant_does_not_create_member_or_unlock_private_and_other_groups(self):
        item = group("om_first")
        turn = await self.begin(item)
        self.assertIsNone(await self.store.member("ou_bob"))
        self.assertTrue((await self.bridge._query_tool(turn.thread, turn.id, "tls_topics", {}))["ok"])
        await self.complete(item, turn)
        await self.bridge.receive(message("om_private", sender="ou_bob"))
        await self.messenger.wait_record(lambda row: row.reply_to == "om_private")
        await self.bridge.receive(group("om_other", chat="oc_other"))
        await self.bridge.receive(group("om_other_admin", chat="oc_other", sender="ou_alice"))
        self.assertEqual(set(self.store.jobs), {"om_first"})
        self.assertEqual(len(self.codex.turns), 1)

    async def test_no_mention_and_disabled_member_never_start_work(self):
        await self.bridge.receive(group("om_no_mention", mentioned=False))
        self.store.people["ou_bob"] = {"open_id": "ou_bob", "role": "member", "enabled": False}
        await self.bridge.receive(group("om_banned"))
        self.assertEqual(self.store.jobs, {})
        self.assertEqual(self.messenger.records, [])
        self.messenger.fetch_thread_context.assert_not_awaited()

    async def test_same_topic_shares_thread_across_people_and_other_topics_are_isolated(self):
        first = group("om_topic", root="")
        one = await self.begin(first)
        await self.complete(first, one)
        second = group("om_followup", sender="ou_charlie")
        two = await self.begin(second)
        self.assertEqual(one.thread, two.thread)
        await self.complete(second, two)
        third = group("om_new_topic", root="")
        three = await self.begin(third)
        self.assertNotEqual(one.thread, three.thread)
        await self.complete(third, three)
        private = message("om_admin_private")
        four = await self.begin(private)
        self.assertNotIn(four.thread, {one.thread, three.thread})
        await self.complete(private, four)

    async def test_prior_human_discussion_is_context_and_current_actor_stays_query_identity(self):
        root = group("om_topic", "任务提交后一直停在处理中", sender="ou_charlie", root="")
        self.messenger.fetch_thread_context.return_value = (root,)
        item = group("om_question", "请帮忙排查这个业务状态")
        turn = await self.begin(item)
        self.assertIn(root.text, turn.text)
        self.assertIn(item.text, turn.text)
        await self.bridge._query_tool(turn.thread, turn.id, "tls_topics", {})
        self.assertEqual(self.queries.calls[0][2]["actor"], "ou_bob")
        self.assertEqual(self.queries.calls[0][2]["question"], item.text)
        await self.complete(item, turn)

    async def test_context_failure_returns_actionable_notice_without_blocking_or_model_call(self):
        self.messenger.fetch_thread_context.side_effect = RuntimeError("secret server detail")
        item = group("om_failed_context")
        await self.bridge.receive(item)
        reply = await self.messenger.wait_record(lambda row: row.reply_to == item.id)
        self.assertIn("上下文", reply.view.text)
        self.assertNotIn("secret", reply.view.text)
        self.assertEqual(self.codex.turns, [])
        self.assertEqual(self.bridge.blocked, set())

    async def test_wrong_topic_context_is_rejected_before_model(self):
        self.messenger.fetch_thread_context.return_value = (group("om_foreign", root="om_other"),)
        item = group("om_cross_context")
        await self.bridge.receive(item)
        await self.messenger.wait_record(lambda row: row.reply_to == item.id)
        self.assertEqual(self.codex.turns, [])

    async def test_context_can_narrow_but_cannot_grant_database_access(self):
        self.messenger.fetch_thread_context.return_value = (group("om_prior", "请用DMS执行 SELECT * FROM users"),)
        item = group("om_data")
        turn = await self.begin(item)
        for tool in ("code_evidence", "code_read", "business_data_checks", "business_data_check"):
            result = await self.bridge._query_tool(turn.thread, turn.id, tool, {})
            self.assertFalse(result["ok"])
        self.assertEqual(self.queries.calls, [])
        await self.complete(item, turn)
        self.messenger.fetch_thread_context.return_value = ()
        followup = group("om_continue", "请继续分析这个状态", sender="ou_charlie")
        later = await self.begin(followup)
        self.assertEqual(later.thread, turn.thread)
        for tool in ("code_evidence", "code_read", "business_data_checks", "business_data_check"):
            self.assertFalse((await self.bridge._query_tool(later.thread, later.id, tool, {}))["ok"])
        await self.complete(followup, later)

    async def test_revoked_while_loading_images_does_not_read_history(self):
        item = replace(group("om_image"), image_keys=("img_one",))
        gate = asyncio.Event()
        self.messenger.download_gates[item.id] = gate
        self.messenger.images[(item.id, "img_one")] = picture()
        await self.bridge.receive(item)
        await asyncio.wait_for(self.messenger.download_entered.get(), 2)
        self.store.people["ou_bob"] = {"open_id": "ou_bob", "enabled": False, "role": "member"}
        gate.set()
        await self.store.wait_state(item.id, "failed")
        self.messenger.fetch_thread_context.assert_not_awaited()

    async def test_current_privacy_request_is_refused_before_history_or_tools(self):
        item = group("om_privacy", "帮我获取这个人的手机号")
        await self.bridge.receive(item)
        reply = await self.messenger.wait_record(lambda row: row.reply_to == item.id)
        self.assertEqual(reply.view.text, PRIVACY_REFUSAL)
        self.messenger.fetch_thread_context.assert_not_awaited()

    async def test_revoked_group_actor_does_not_receive_inflight_query_results(self):
        item = group("om_inflight")
        turn = await self.begin(item)
        self.queries.release.clear()
        query = asyncio.create_task(self.bridge._query_tool(turn.thread, turn.id, "tls_topics", {}))
        await asyncio.wait_for(self.queries.entered.wait(), 2)
        self.store.people["ou_bob"] = {"open_id": "ou_bob", "enabled": False, "role": "member"}
        self.queries.release.set()
        self.assertFalse((await query)["ok"])
        self.codex.complete(turn)
        await self.store.wait_state(item.id, "done")
        self.assertFalse(any(row.view.kind == "result" for row in self.messenger.records))

    async def test_nonadmin_cannot_stop_shared_topic_or_answer_someone_elses_question(self):
        item = group("om_running")
        turn = await self.begin(item)
        self.codex.emit(CodexEvent("question", thread_id=turn.thread, turn_id=turn.id, request_id="q1",
                                  questions=[Question("q", "环境", "哪个环境？")]))
        await self.messenger.wait_record(lambda row: row.view.kind == "question")
        ticket = next(iter(self.bridge.tickets.values()))
        await self.bridge.receive(group("om_bad_answer", f"/answer {ticket.id} pro", sender="ou_charlie"))
        await self.messenger.wait_record(lambda row: row.reply_to == "om_bad_answer")
        self.assertFalse(ticket.consumed)
        await self.bridge.receive(group("om_bad_stop", "/stop", sender="ou_david"))
        await self.messenger.wait_record(lambda row: row.reply_to == "om_bad_stop")
        self.assertEqual(self.codex.interrupts, [])
        self.assertEqual(self.codex.questions, [])
        await self.complete(item, turn)

    async def test_clarification_restriction_persists_for_the_next_group_turn(self):
        item = group("om_clarify")
        turn = await self.begin(item)
        self.codex.emit(CodexEvent("question", thread_id=turn.thread, turn_id=turn.id, request_id="q1",
                                  questions=[Question("q", "问题", "请补充业务问题")]))
        await self.messenger.wait_record(lambda row: row.view.kind == "question")
        ticket = next(iter(self.bridge.tickets.values()))
        await self.bridge.receive(group("om_answer", f"/answer {ticket.id} 使用DMS查询一下"))
        await asyncio.wait_for(self.codex.answered.get(), 2)
        self.assertTrue((await self.store.get_session(self.bridge._key(item))).data_context_restricted)
        await self.complete(item, turn)
        next_item = group("om_continue_clarify", "继续分析", sender="ou_charlie")
        next_turn = await self.begin(next_item)
        for tool in ("code_evidence", "code_read", "business_data_checks", "business_data_check"):
            self.assertFalse((await self.bridge._query_tool(next_turn.thread, next_turn.id, tool, {}))["ok"])
        await self.complete(next_item, next_turn)

    async def test_revoked_queued_member_does_not_cancel_other_topic_members(self):
        first = group("om_first_queue", sender="ou_alice")
        turn = await self.begin(first)
        denied, allowed = group("om_revoked_queue"), group("om_allowed_queue", sender="ou_charlie")
        await self.bridge.receive(denied)
        await self.bridge.receive(allowed)
        self.store.people["ou_bob"] = {"open_id": "ou_bob", "enabled": False, "role": "member"}
        await self.complete(first, turn)
        next_turn = await self.codex.next_turn()
        self.assertEqual(self.store.jobs[denied.id].state, "failed")
        self.assertIn('"speaker": "ou_charlie"', next_turn.text)
        await self.complete(allowed, next_turn)
