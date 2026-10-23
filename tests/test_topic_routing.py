from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import AsyncMock

from octopus.contracts import QuotedContext, ReferenceContextError
from octopus.topic_routing import RoutingInputError, TopicRouter
from test_bridge import FakeCodex, FakeMessenger, message
from test_group_bridge import group
from topic_memory import TopicMemoryStore


def classification(action="new", topic_id=None, **updates):
    result = {"action": action, "topic_id": topic_id, "title": "支付问题",
              "summary": "用户想确认订单支付失败原因", "reason": "distinct_question",
              "clarification": "", "use_pending": False, "context_ids": []}
    result.update(updates)
    return result


class TopicRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store, self.codex, self.messenger = TopicMemoryStore(), FakeCodex(), FakeMessenger()
        self.codex.classify_topic = AsyncMock(return_value=classification())
        self.messenger.fetch_thread_context = AsyncMock(return_value=())
        self.messenger.fetch_quoted_context = AsyncMock(return_value=None)
        self.router = TopicRouter(self.store, self.codex, self.messenger, "/project")
        self.scope = "readonly:v2:oc_shared:ou_alice"
        await self.store.session(self.scope, "oc_shared", "ou_alice")

    async def topic(self, title="支付失败", summary="订单一支付失败", *, scope=None, actor="ou_alice"):
        return await self.store.new_topic(scope or self.scope, actor, title, summary)

    async def original(self, key, identifier, text, *, allowed=True, actor="ou_alice"):
        item = message(identifier, text, sender=actor)
        await self.store.enqueue(self.scope, item)
        await self.store.freeze_route(identifier, self.scope, key, title="问题", summary=text,
            prompt=text, question=text, data_allowed=allowed, action="continue", reason="test")
        return item

    def quote(self, identifier, text="原始问题", *, has_images=False):
        self.messenger.fetch_quoted_context.return_value = QuotedContext(
            identifier, "oc_shared", text, 1_792_368_000_000, has_images)

    async def test_new_question_only_receives_selected_original_inputs(self):
        old = await self.topic(summary="旧主题的模型摘要不能成为业务证据")
        self.codex.classify_topic.return_value = classification(summary="路由摘要不是事实")
        decision = await self.router.decide(message("new", "质检为何重复拍摄"), self.scope)
        self.assertEqual((decision.action, decision.topic_id), ("new", None))
        self.assertIn("质检为何重复拍摄", decision.prompt)
        self.assertNotIn("旧主题的模型摘要", decision.prompt)
        self.assertNotIn("路由摘要不是事实", decision.prompt)
        payload = self.codex.classify_topic.call_args.args[1]
        self.assertEqual(payload["topics"][0]["id"], old)

    async def test_reply_recovers_old_topic_outside_recent_candidate_page(self):
        old = await self.topic("旧订单", "最早的问题")
        await self.store.map_topic_message(self.scope, old, "old_answer")
        for number in range(10):
            await self.topic(f"新主题 {number}", f"别的问题 {number}")
        self.quote("old_answer", "旧订单还没有查到日志")
        self.codex.classify_topic.return_value = classification("resume", old)
        decision = await self.router.decide(replace(message("follow", "继续查日志"), parent_id="old_answer"), self.scope)
        self.assertEqual(decision.topic_id, old)
        self.assertIn(old, {t["id"] for t in self.codex.classify_topic.call_args.args[1]["topics"]})
        self.assertIn("旧订单还没有查到日志", decision.question)

    async def test_foreign_private_topic_cannot_be_chosen_or_added_to_payload(self):
        foreign_scope = "readonly:v2:oc_other:ou_bob"
        await self.store.session(foreign_scope, "oc_other", "ou_bob")
        foreign = await self.topic("其他人私聊", "不应泄露的内容", scope=foreign_scope, actor="ou_bob")
        self.codex.classify_topic.return_value = classification("resume", foreign)
        decision = await self.router.decide(message("bad", "继续刚才的问题"), self.scope)
        self.assertEqual(decision.action, "clarify")
        self.assertNotIn("不应泄露", str(self.codex.classify_topic.call_args))
        self.assertNotIn("不应泄露", decision.prompt)

    async def test_unknown_context_id_fails_closed(self):
        self.codex.classify_topic.return_value = classification(context_ids=["foreign_message"])
        decision = await self.router.decide(message("bad", "查一下"), self.scope)
        self.assertEqual(decision.action, "clarify")
        self.assertEqual(decision.context_ids, ())

    async def test_classifier_failure_uses_explicit_reply_but_never_recent_pointer(self):
        old = await self.topic()
        self.codex.classify_topic.side_effect = TimeoutError("private transport detail")
        decision = await self.router.decide(message("without_ref", "继续查"), self.scope)
        self.assertEqual(decision.action, "clarify")
        self.assertNotIn("private transport", decision.clarification)
        await self.store.map_topic_message(self.scope, old, "answer")
        self.quote("answer", "订单支付超时")
        decision = await self.router.decide(replace(message("with_ref", "继续查"), parent_id="answer"), self.scope)
        self.assertEqual((decision.action, decision.topic_id), ("continue", old))

    async def test_explicit_new_overrides_classifier_continue(self):
        old = await self.topic()
        self.codex.classify_topic.return_value = classification("continue", old)
        decision = await self.router.decide(message("new", "另一个问题：质检为什么重复拍摄"), self.scope)
        self.assertEqual((decision.action, decision.topic_id), ("new", None))

    async def test_manual_new_empty_topic_is_used_for_next_question(self):
        fresh = await self.store.new_topic(self.scope, "ou_alice")
        decision = await self.router.decide(message("after_new", "支付状态为什么异常"), self.scope)
        self.assertEqual((decision.action, decision.topic_id), ("continue", fresh))

    async def test_pending_clarification_keeps_actual_question_and_restriction(self):
        item = message("ambiguous", "这个订单为什么失败")
        await self.store.enqueue(self.scope, item)
        await self.store.defer_topic_route(item.id, self.scope,
            {"question": item.text, "data_allowed": False})
        old = await self.topic()
        # Creating a topic intentionally clears pending; restore it after setup.
        await self.store.enqueue(self.scope, message("pending_again", item.text))
        await self.store.defer_topic_route("pending_again", self.scope,
            {"question": item.text, "data_allowed": False})
        self.codex.classify_topic.return_value = classification("continue", old, use_pending=True)
        decision = await self.router.decide(message("clarified", "我说的是订单 A"), self.scope)
        self.assertIn(item.text, decision.question)
        self.assertIn("订单 A", decision.question)
        self.assertFalse(decision.data_allowed)
        self.assertIn(item.text, decision.prompt)

    async def test_new_topic_derived_from_restricted_reply_stays_restricted(self):
        old = await self.topic()
        await self.store.restrict_session_data(old)
        await self.store.map_topic_message(self.scope, old, "answer")
        self.quote("answer", "已知支付超时")
        decision = await self.router.decide(replace(message("derived", "另一个问题：这个支付状态如何解释"),
                                                  parent_id="answer"), self.scope)
        self.assertEqual(decision.action, "new")
        self.assertFalse(decision.data_allowed)

    async def test_image_is_sent_only_to_classifier_input_and_restricts_data(self):
        item = replace(message("image", "看看这个截图"), image_keys=("img_test",))
        decision = await self.router.decide(item, self.scope, images=("data:image/png;base64,example",))
        self.assertEqual(self.codex.classify_topic.call_args.kwargs["images"], ("data:image/png;base64,example",))
        self.assertFalse(decision.data_allowed)
        self.assertNotIn("base64", decision.prompt)

    async def test_standalone_new_correction_reuses_original_not_summary(self):
        current = await self.topic(summary="模型生成的摘要")
        await self.original(current, "original", "订单 Z 的支付失败原因")
        decision = await self.router.decide(message("correction", "这是新问题"), self.scope)
        self.assertEqual(decision.action, "new")
        self.assertIn("订单 Z 的支付失败原因", decision.question)
        self.assertNotIn("模型生成的摘要", decision.prompt)

    async def test_previous_question_correction_never_moves_current_problem_backwards(self):
        previous = await self.topic("支付", "支付失败")
        await self.original(previous, "pay", "订单 A 的支付失败原因")
        current = await self.topic("质检", "重复拍摄")
        await self.original(current, "inspect", "质检 B 的重复拍摄原因")
        self.codex.classify_topic.return_value = classification("resume", previous)
        for text in ("接着上一题", "回到上个问题"):
            with self.subTest(text=text):
                decision = await self.router.decide(message("back", text), self.scope)
                self.assertEqual(decision.topic_id, previous)
                self.assertNotIn("质检 B 的重复拍摄原因", decision.question)
                self.assertTrue(self.codex.classify_topic.call_args.args[1]["current_message"]["explicit_previous"])

    async def test_invalid_private_reference_is_rejected_before_classifier(self):
        self.messenger.fetch_quoted_context.return_value = QuotedContext("ref", "oc_foreign", "内容", 1)
        with self.assertRaises(ReferenceContextError):
            await self.router.decide(replace(message("bad_ref"), parent_id="ref"), self.scope)
        self.codex.classify_topic.assert_not_awaited()

    async def test_group_prompt_excludes_other_topic_even_if_classifier_selects_it(self):
        scope = "readonly:v2:group:oc_support:om_topic"
        await self.store.session(scope, "oc_support", "ou_bob", topic_id="om_topic")
        pay = await self.topic("支付", "支付失败", scope=scope, actor="ou_bob")
        inspection = await self.topic("质检", "重复拍摄", scope=scope, actor="ou_charlie")
        await self.store.map_topic_message(scope, pay, "pay_message")
        await self.store.map_topic_message(scope, inspection, "inspection_message")
        self.messenger.fetch_thread_context.return_value = (
            group("pay_message", "支付请求超时"), group("inspection_message", "质检重复拍摄细节"))
        self.codex.classify_topic.return_value = classification("continue", pay,
            context_ids=["pay_message", "inspection_message"])
        decision = await self.router.decide(group("follow", "继续查支付日志"), scope)
        self.assertIn("支付请求超时", decision.prompt)
        self.assertNotIn("质检重复拍摄细节", decision.prompt)
        self.assertEqual(decision.context_ids, ("pay_message",))

    async def test_group_other_root_is_rejected_before_classifier(self):
        scope = "readonly:v2:group:oc_support:om_topic"
        await self.store.session(scope, "oc_support", "ou_bob", topic_id="om_topic")
        self.messenger.fetch_thread_context.return_value = (group("other", root="om_other"),)
        with self.assertRaises(RoutingInputError):
            await self.router.decide(group("follow"), scope)
        self.codex.classify_topic.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
