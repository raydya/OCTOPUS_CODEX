from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock

from octopus.bridge import Bridge
from octopus.config import Config
from octopus.contracts import CodexEvent, Question, QuotedContext
from test_bridge import FakeCodex, FakeMessenger, message, picture, until
from test_group_bridge import group
from test_readonly_bridge import Queries
from test_topic_routing import classification
from topic_memory import TopicMemoryStore


class TopicBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store, self.codex, self.messenger, self.queries = TopicMemoryStore(), FakeCodex(), FakeMessenger(), Queries()
        self.codex.classify_topic = AsyncMock(return_value=classification())
        self.messenger.fetch_thread_context = AsyncMock(return_value=())
        self.messenger.fetch_quoted_context = AsyncMock(return_value=None)
        self.config = Config("cli_test", "secret", "unused", "/isolated", None, None,
            read_only=True, topic_routing=True, allow_groups=True, support_group_ids=("oc_support",),
            bot_open_id="ou_bot", max_concurrent=2, task_timeout=60)
        self.bridge = Bridge(self.config, self.store, self.messenger, self.codex, self.queries)
        await self.bridge.start()

    async def asyncTearDown(self):
        self.queries.release.set()
        await self.bridge.close()

    async def begin(self, item, decision=None):
        if decision is not None:
            self.codex.classify_topic.return_value = decision
        await self.bridge.receive(item)
        return await self.codex.next_turn()

    async def complete(self, item, turn, text="已根据证据回答当前问题"):
        self.codex.complete(turn, text)
        return await self.messenger.wait_record(lambda r: r.reply_to == item.id and r.view.kind == "result")

    async def command(self, identifier, text):
        item = message(identifier, text)
        self.bridge._control_times.clear()
        await self.bridge.receive(item)
        return await self.messenger.wait_record(lambda r: r.reply_to == item.id)

    async def test_independent_questions_use_distinct_threads_and_reply_recovers_old_one(self):
        first = message("pay", "订单 A 为什么支付失败")
        one = await self.begin(first)
        reply = await self.complete(first, one)
        first_key = self.store.jobs[first.id].session_key
        second = message("inspect", "质检 B 为什么重复拍摄")
        two = await self.begin(second, classification(title="质检问题", summary=second.text))
        await self.complete(second, two)
        self.assertNotEqual(one.thread, two.thread)
        self.assertNotIn(first.text, two.text)
        self.messenger.fetch_quoted_context.return_value = QuotedContext(reply.id, first.chat_id, reply.view.text, first.created_at)
        follow = replace(message("follow", "继续查日志"), parent_id=reply.id)
        three = await self.begin(follow, classification("resume", first_key))
        self.assertEqual(one.thread, three.thread)
        self.assertEqual(self.store.routes[follow.id].session_key, first_key)
        await self.complete(follow, three)

    async def test_same_group_people_have_separate_current_topics_and_shared_explicit_reply(self):
        first = group("pay", "订单 A 为什么支付失败", sender="ou_bob")
        one = await self.begin(first)
        reply = await self.complete(first, one)
        key_a, scope = self.store.jobs[first.id].session_key, self.bridge._key(first)
        second = group("inspect", "质检 B 为什么重复拍摄", sender="ou_charlie")
        two = await self.begin(second, classification(title="质检问题", summary=second.text))
        await self.complete(second, two)
        key_b = self.store.jobs[second.id].session_key
        self.assertNotEqual(key_a, key_b)
        self.assertEqual(await self.store.current_topic(scope, "ou_bob"), key_a)
        self.assertEqual(await self.store.current_topic(scope, "ou_charlie"), key_b)
        follow = replace(group("pay_again", "继续查日志", sender="ou_charlie"), parent_id=reply.id)
        three = await self.begin(follow, classification("resume", key_a))
        self.assertEqual(three.thread, one.thread)
        self.assertEqual(await self.store.current_topic(scope, "ou_charlie"), key_a)
        await self.complete(follow, three)

    async def test_clarification_followup_includes_original_question_without_starting_first_turn(self):
        ambiguous = message("ambiguous", "它为什么失败")
        self.codex.classify_topic.return_value = classification("clarify", clarification="你指的是哪一个订单？")
        await self.bridge.receive(ambiguous)
        reply = await self.messenger.wait_record(lambda r: r.reply_to == ambiguous.id)
        self.assertEqual(reply.view.text, "你指的是哪一个订单？")
        self.assertEqual(self.codex.turns, [])
        self.assertEqual(self.store.jobs[ambiguous.id].state, "done")
        item = message("specific", "订单 A")
        turn = await self.begin(item, classification(use_pending=True))
        self.assertIn(ambiguous.text, turn.text)
        self.assertIn(item.text, turn.text)
        self.assertIsNone(await self.store.get_topic_pending(self.bridge._key(item), item.sender_id))
        await self.complete(item, turn)

    async def test_new_command_preserves_old_thread_and_status_last_follow_selected_topic(self):
        first = message("pay", "订单 A 为什么支付失败")
        one = await self.begin(first)
        await self.complete(first, one, "支付问题的结果")
        old = self.store.jobs[first.id].session_key
        await self.command("reset", "/new")
        scope = self.bridge._key(first)
        fresh = await self.store.current_topic(scope, first.sender_id)
        self.assertNotEqual(fresh, old)
        self.assertEqual(self.store.sessions[old].thread_id, one.thread)
        self.assertIsNone(self.store.sessions[fresh].thread_id)
        second = message("inspect", "质检为什么重复拍摄")
        two = await self.begin(second)
        await self.complete(second, two, "质检问题的结果")
        self.assertEqual(self.store.jobs[second.id].session_key, fresh)
        status = await self.command("status", "/status")
        self.assertIn(two.thread, status.view.text)
        last = await self.command("last", "/last")
        self.assertEqual(last.view.text, "质检问题的结果")
        await self.command("switch", "/switch " + old.rsplit(":", 1)[-1][-8:])
        previous_result = await self.command("old_last", "/last")
        self.assertEqual(previous_result.view.text, "支付问题的结果")

    async def test_assigned_queued_job_does_not_follow_later_actor_pointer(self):
        first = message("first", "订单 A 为什么支付失败")
        one = await self.begin(first)
        a = self.store.jobs[first.id].session_key
        second = message("second", "继续查订单 A 日志")
        self.codex.classify_topic.return_value = classification("continue", a)
        await self.bridge.receive(second)
        await until(self.store.changed, lambda: second.id in self.store.routes)
        self.assertEqual(self.store.jobs[second.id].state, "queued")
        scope = self.bridge._key(first)
        b = await self.store.new_topic(scope, first.sender_id, "别的问题", "不同问题")
        await self.bridge.receive(second)  # replayed delivery cannot classify twice
        self.assertEqual(self.codex.classify_topic.await_count, 2)
        await self.complete(first, one)
        two = await self.codex.next_turn()
        self.assertEqual(two.thread, one.thread)
        self.assertEqual(self.store.routes[second.id].session_key, a)
        self.assertEqual(await self.store.current_topic(scope, first.sender_id), b)
        await self.complete(second, two)

    async def test_pending_routed_job_survives_restart_without_reclassification(self):
        self.bridge.config = replace(self.config, max_concurrent=1)
        first = message("running", "支付问题")
        one = await self.begin(first)
        second = message("queued", "质检问题")
        self.codex.classify_topic.return_value = classification(title="质检问题", summary=second.text)
        await self.bridge.receive(second)
        await until(self.store.changed, lambda: second.id in self.store.routes)
        frozen = self.store.routes[second.id]
        self.assertEqual(self.store.jobs[second.id].state, "queued")
        await self.bridge.close()
        self.codex = FakeCodex()
        self.codex.thread_count = 10
        self.codex.classify_topic = AsyncMock(side_effect=AssertionError("Frozen job must not be reclassified"))
        self.bridge = Bridge(self.config, self.store, self.messenger, self.codex, self.queries)
        await self.bridge.start()
        resumed = await self.codex.next_turn()
        self.assertEqual(self.store.routes[second.id], frozen)
        self.assertIn(second.text, resumed.text)
        self.codex.classify_topic.assert_not_awaited()
        await self.complete(second, resumed)

    async def test_image_classifier_receives_attachment_and_business_data_stays_disabled(self):
        item = replace(message("image", "看看这个支付错误"), image_keys=("img_one",))
        self.messenger.images[(item.id, "img_one")] = picture()
        turn = await self.begin(item)
        self.assertEqual(len(turn.images), 1)
        self.assertEqual(len(self.codex.classify_topic.call_args.kwargs["images"]), 1)
        self.assertFalse(self.store.routes[item.id].data_allowed)
        result = await self.bridge._query_tool(turn.thread, turn.id, "business_data_checks", {})
        self.assertFalse(result["ok"])
        self.assertEqual(self.queries.calls, [])
        await self.complete(item, turn)

    async def test_revocation_while_classifying_prevents_business_execution_and_reply(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def held(*args, **kwargs):
            entered.set()
            await release.wait()
            return classification()
        self.codex.classify_topic.side_effect = held
        item = message("revoke", "订单 A 为什么失败")
        await self.bridge.receive(item)
        await asyncio.wait_for(entered.wait(), 2)
        self.store.people[item.sender_id]["enabled"] = False
        release.set()
        await self.store.wait_state(item.id, "failed")
        self.assertEqual(self.codex.turns, [])
        self.assertNotIn(item.id, self.store.routes)
        self.assertFalse(any(r.reply_to == item.id for r in self.messenger.records))

    async def test_pending_image_question_restores_original_attachment_after_text_clarification(self):
        original = replace(message("pending_image", "截图里的错误是什么原因"), image_keys=("img_pending",))
        self.messenger.images[(original.id, "img_pending")] = picture("blue")
        self.codex.classify_topic.return_value = classification("clarify", clarification="你指的是支付还是质检？")
        await self.bridge.receive(original)
        await self.messenger.wait_record(lambda r: r.reply_to == original.id)
        self.assertEqual(self.codex.turns, [])
        followup = message("clarify_image", "支付问题")
        turn = await self.begin(followup, classification(use_pending=True))
        self.assertEqual(len(turn.images), 1)
        self.assertIn(original.text, turn.text)
        self.assertFalse(self.store.routes[followup.id].data_allowed)
        self.assertGreaterEqual(self.messenger.download_calls.count((original.id, "img_pending")), 2)
        await self.complete(followup, turn)

    async def test_new_question_correction_restores_original_image_in_fresh_thread(self):
        original = replace(message("image_original", "这个支付错误如何解释"), image_keys=("img_original",))
        self.messenger.images[(original.id, "img_original")] = picture("green")
        one = await self.begin(original)
        await self.complete(original, one)
        correction = message("image_correction", "这是新问题")
        two = await self.begin(correction, classification())
        self.assertNotEqual(one.thread, two.thread)
        self.assertEqual(two.images, one.images)
        self.assertIn(original.text, two.text)
        self.assertFalse(self.store.routes[correction.id].data_allowed)
        await self.complete(correction, two)

    async def test_restored_and_current_images_share_total_attachment_limit(self):
        original = replace(message("many_images", "这些截图的错误是什么原因"),
                           image_keys=tuple(f"img_{n}" for n in range(4)))
        for key in original.image_keys:
            self.messenger.images[(original.id, key)] = picture()
        self.codex.classify_topic.return_value = classification("clarify", clarification="哪一个业务问题？")
        await self.bridge.receive(original)
        await self.messenger.wait_record(lambda r: r.reply_to == original.id)
        followup = replace(message("one_more", "支付问题，还有这张图"), image_keys=("img_extra",))
        self.messenger.images[(followup.id, "img_extra")] = picture()
        self.codex.classify_topic.return_value = classification(use_pending=True)
        await self.bridge.receive(followup)
        reply = await self.messenger.wait_record(lambda r: r.reply_to == followup.id)
        self.assertIn("4", reply.view.text)
        self.assertEqual(self.codex.turns, [])

    async def test_stop_during_classifier_wait_responds_and_cancels_before_business_execution(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def held(*args, **kwargs):
            entered.set()
            await release.wait()
            return classification()
        self.codex.classify_topic.side_effect = held
        item = message("held_route", "订单 A 为什么支付失败")
        await self.bridge.receive(item)
        await asyncio.wait_for(entered.wait(), 2)
        try:
            reply = await self.command("stop_routing", "/stop")
            self.assertIn("取消", reply.view.text)
            self.assertEqual(self.store.jobs[item.id].state, "failed")
        finally:
            release.set()
        # /new waits for the classifier scope lock, so its response confirms
        # that the late classifier result has been handled without a turn.
        await self.command("after_cancel", "/new")
        self.assertEqual(self.codex.turns, [])
        self.assertNotIn(item.id, self.store.routes)

    async def test_new_waiting_for_one_classifier_does_not_block_another_users_status(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def held(*args, **kwargs):
            entered.set()
            await release.wait()
            return classification()
        self.codex.classify_topic.side_effect = held
        item = message("held_for_new", "订单 A 为什么失败")
        await self.bridge.receive(item)
        await asyncio.wait_for(entered.wait(), 2)
        self.bridge._control_times.clear()
        await self.bridge.receive(message("waiting_new", "/new"))
        self.assertEqual(self.store.control_states["waiting_new"], "queued")
        self.store.people["ou_bob"] = {"open_id": "ou_bob", "role": "member", "enabled": True}
        other = message("other_status", "/status", sender="ou_bob", chat="oc_bob")
        try:
            await self.bridge.receive(other)
            reply = await self.messenger.wait_record(lambda r: r.reply_to == other.id)
            self.assertIn("状态", reply.view.text)
            self.assertEqual(self.store.control_states["waiting_new"], "queued")
            self.assertFalse(any(r.reply_to == "waiting_new" for r in self.messenger.records))
        finally:
            release.set()
        await self.messenger.wait_record(lambda r: r.reply_to == "waiting_new")

    async def test_new_command_immediately_followed_by_question_uses_fresh_topic(self):
        first = message("before_new", "订单 A 的支付问题")
        one = await self.begin(first)
        await self.complete(first, one)
        old = self.store.jobs[first.id].session_key
        self.codex.classify_topic.return_value = classification("continue", old)
        self.bridge._control_times.clear()
        # Do not wait for the command response. Admission order must determine
        # the destination even if the classifier would prefer the old topic.
        await self.bridge.receive(message("immediate_new", "/new"))
        next_question = message("immediate_question", "订单 A 另外一个支付问题")
        await self.bridge.receive(next_question)
        two = await self.codex.next_turn()
        scope = self.bridge._key(first)
        fresh = await self.store.current_topic(scope, first.sender_id)
        self.assertNotEqual(fresh, old)
        self.assertEqual(self.store.jobs[next_question.id].session_key, fresh)
        self.assertNotEqual(two.thread, one.thread)
        self.assertEqual(self.store.sessions[old].thread_id, one.thread)
        payload = self.codex.classify_topic.call_args.args[1]
        self.assertEqual(payload["active_topic_id"], fresh)
        await self.complete(next_question, two)

    async def test_earlier_question_routes_before_new_and_late_result_keeps_new_pointer(self):
        first = message("prior_topic", "订单 A 的支付问题")
        one = await self.begin(first)
        await self.complete(first, one)
        old = self.store.jobs[first.id].session_key
        question = message("before_command", "质检 B 的重复拍摄问题")
        self.codex.classify_topic.return_value = classification(title="质检 B", summary=question.text)
        await self.bridge.receive(question)
        self.bridge._control_times.clear()
        await self.bridge.receive(message("after_question_new", "/new"))
        two = await self.codex.next_turn()
        await self.messenger.wait_record(lambda r: r.reply_to == "after_question_new")
        scope = self.bridge._key(first)
        routed = self.store.routes[question.id].session_key
        fresh = await self.store.current_topic(scope, first.sender_id)
        self.assertNotIn(fresh, {old, routed})
        self.assertEqual(self.codex.classify_topic.call_args.args[1]["active_topic_id"], old)
        self.assertIsNone(self.store.sessions[fresh].thread_id)
        await self.complete(question, two)
        self.assertEqual(await self.store.current_topic(scope, first.sender_id), fresh)

    async def test_question_ticket_answers_original_topic_after_current_topic_changes(self):
        first = message("ask_pay", "订单 A 为什么支付失败")
        one = await self.begin(first)
        second = message("ask_inspection", "质检 B 为什么重复拍摄")
        two = await self.begin(second, classification(title="质检", summary=second.text))
        self.codex.emit(CodexEvent("question", thread_id=one.thread, turn_id=one.id, request_id="question-pay",
            questions=[Question("order", "订单", "具体是哪个订单？")]))
        card = await self.messenger.wait_record(lambda r: r.reply_to == first.id and r.view.kind == "question")
        ticket = next(iter(self.bridge.tickets.values()))
        self.assertEqual(await self.store.topic_for_message(self.bridge._key(first), card.id),
                         self.store.jobs[first.id].session_key)
        self.assertEqual(await self.store.current_topic(self.bridge._key(first), first.sender_id),
                         self.store.jobs[second.id].session_key)
        await self.bridge.receive(message("answer_pay", f"/answer {ticket.id} 订单 A"))
        request_id, answers = await asyncio.wait_for(self.codex.answered.get(), 2)
        self.assertEqual((request_id, answers), ("question-pay", {"order": ["订单 A"]}))
        await self.complete(first, one)
        await self.complete(second, two)

    async def test_question_ticket_rejects_other_group_actor_and_different_topic_scope(self):
        first = group("group_question", "订单 A 为什么失败", sender="ou_bob")
        turn = await self.begin(first)
        self.codex.emit(CodexEvent("question", thread_id=turn.thread, turn_id=turn.id, request_id="question-bob",
            questions=[Question("order", "订单", "具体是哪个订单？")]))
        await self.messenger.wait_record(lambda r: r.view.kind == "question")
        ticket = next(iter(self.bridge.tickets.values()))
        for item in (
            group("other_actor_answer", f"/answer {ticket.id} 订单 A", sender="ou_charlie"),
            group("other_scope_answer", f"/answer {ticket.id} 订单 A", sender="ou_bob", root="om_other"),
        ):
            self.bridge._control_times.clear()
            await self.bridge.receive(item)
            reply = await self.messenger.wait_record(lambda r: r.reply_to == item.id)
            self.assertIn("不可用", reply.view.title)
            self.assertFalse(ticket.consumed)
        self.assertEqual(self.codex.questions, [])
        await self.complete(first, turn)

    async def test_sql_answer_restriction_persists_in_personal_business_topic(self):
        first = message("personal_question", "订单 A 为什么支付失败")
        one = await self.begin(first)
        key = self.store.jobs[first.id].session_key
        self.codex.emit(CodexEvent("question", thread_id=one.thread, turn_id=one.id, request_id="question-data",
            questions=[Question("context", "问题", "请补充业务问题")]))
        await self.messenger.wait_record(lambda r: r.view.kind == "question")
        ticket = next(iter(self.bridge.tickets.values()))
        await self.bridge.receive(message("sql_answer", f"/answer {ticket.id} 通过 DMS 执行 SELECT * FROM orders"))
        await asyncio.wait_for(self.codex.answered.get(), 2)
        self.assertTrue(self.store.sessions[key].data_context_restricted)
        await self.complete(first, one)
        followup = message("after_sql_answer", "继续查订单状态")
        two = await self.begin(followup, classification("continue", key))
        self.assertEqual(two.thread, one.thread)
        self.assertFalse(self.store.routes[followup.id].data_allowed)
        result = await self.bridge._query_tool(two.thread, two.id, "business_data_checks", {})
        self.assertFalse(result["ok"])
        self.assertEqual(self.queries.calls, [])
        await self.complete(followup, two)


if __name__ == "__main__":
    unittest.main()
