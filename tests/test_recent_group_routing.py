from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import AsyncMock

from octopus.topic_routing import RoutingInputError, TopicRouter
from test_bridge import FakeCodex, FakeMessenger
from test_group_bridge import group
from test_topic_routing import classification
from topic_memory import TopicMemoryStore
import test_topic_bridge


class RecentGroupRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store, self.codex, self.messenger = TopicMemoryStore(), FakeCodex(), FakeMessenger()
        self.codex.classify_topic = AsyncMock(return_value=classification())
        self.messenger.fetch_group_context = AsyncMock(return_value=())
        self.router = TopicRouter(self.store, self.codex, self.messenger, "/project")
        self.item = group("om_now", "看看刚才的问题", root="", chat="oc_new_group")
        self.scope = "readonly:v2:group:oc_new_group:om_now"
        await self.store.session(self.scope, self.item.chat_id, self.item.sender_id, topic_id=self.item.id)
        self.problem = replace(group("om_problem", "质检提交后一直处于处理中", root="", chat=self.item.chat_id),
                               created_at=self.item.created_at - 3000)
        self.detail = replace(group("om_detail", "从下午两点开始，重试后也是一样", root=self.problem.id,
                                    chat=self.item.chat_id), created_at=self.item.created_at - 2000)
        self.chatter = replace(group("om_lunch", "中午去哪里吃饭", root="", chat=self.item.chat_id),
                              created_at=self.item.created_at - 1000)

    async def test_recent_problem_and_details_selected_without_importing_chatter(self):
        self.messenger.fetch_group_context.return_value = (self.problem, self.detail, self.chatter)
        self.codex.classify_topic.return_value = classification(context_ids=[self.problem.id, self.detail.id])
        result = await self.router.decide(self.item, self.scope)
        self.assertEqual(result.action, "new")
        self.assertIn(self.problem.text, result.prompt)
        self.assertIn(self.detail.text, result.question)
        self.assertNotIn(self.chatter.text, result.prompt)
        payload = self.codex.classify_topic.call_args.args[1]
        self.assertEqual(payload["context_scope"], "recent_group")
        self.assertEqual(payload["context_messages"][1]["feishu_topic_id"], self.problem.id)
        self.assertEqual(payload["context_messages"][0]["created_at"], self.problem.created_at)

    async def test_ambiguous_problems_remain_a_clarification_without_business_turn(self):
        self.messenger.fetch_group_context.return_value = (self.problem, self.detail)
        self.codex.classify_topic.return_value = classification("clarify", clarification="你指的是哪一个问题？")
        result = await self.router.decide(self.item, self.scope)
        self.assertEqual(result.action, "clarify")
        self.assertEqual(result.clarification, "你指的是哪一个问题？")
        self.assertEqual(self.codex.turns, [])

    async def test_self_contained_question_can_ignore_all_recent_messages(self):
        self.messenger.fetch_group_context.return_value = (self.problem, self.chatter)
        result = await self.router.decide(replace(self.item, text="支付状态字段有什么含义"), self.scope)
        self.assertNotIn(self.problem.text, result.prompt)
        self.assertNotIn(self.chatter.text, result.prompt)

    async def test_cross_group_future_and_self_messages_rejected_before_classifier(self):
        for item in (replace(self.problem, chat_id="oc_other"), self.item,
                     replace(self.problem, created_at=self.item.created_at + 1),
                     replace(self.problem, chat_type="p2p")):
            with self.subTest(item=item):
                self.messenger.fetch_group_context.return_value = (item,)
                with self.assertRaises(RoutingInputError):
                    await self.router.decide(self.item, self.scope)
        self.codex.classify_topic.assert_not_awaited()

    async def test_prior_restricted_topic_cannot_gain_database_access_through_recent_context(self):
        old_scope = f"readonly:v2:group:{self.item.chat_id}:{self.problem.id}"
        await self.store.session(old_scope, self.item.chat_id, self.item.sender_id, topic_id=self.problem.id)
        old_key = await self.store.new_topic(old_scope, self.item.sender_id, "原问题", "不能注入的旧摘要")
        await self.store.map_topic_message(old_scope, old_key, self.problem.id)
        await self.store.restrict_session_data(old_key)
        self.messenger.fetch_group_context.return_value = (self.problem, self.chatter)
        self.codex.classify_topic.return_value = classification(context_ids=[self.problem.id])
        result = await self.router.decide(self.item, self.scope)
        self.assertFalse(result.data_allowed)
        self.assertIsNone(result.topic_id)
        self.assertNotIn("不能注入的旧摘要", str(self.codex.classify_topic.call_args))
        # Restrictions on an unrelated unselected message must not affect a different question.
        self.codex.classify_topic.return_value = classification(context_ids=[])
        result = await self.router.decide(replace(self.item, text="支付业务状态为何不正确"), self.scope)
        self.assertTrue(result.data_allowed)

    async def test_recent_lookup_failure_never_fabricates_history(self):
        self.messenger.fetch_group_context.side_effect = RuntimeError("private credential details")
        with self.assertRaisesRegex(RoutingInputError, "相关群消息") as raised:
            await self.router.decide(self.item, self.scope)
        self.assertNotIn("credential", str(raised.exception))
        self.codex.classify_topic.assert_not_awaited()


class RecentGroupBridgeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_topic_bridge.TopicBridgeTests.asyncSetUp
    asyncTearDown = test_topic_bridge.TopicBridgeTests.asyncTearDown
    begin = test_topic_bridge.TopicBridgeTests.begin
    complete = test_topic_bridge.TopicBridgeTests.complete

    async def test_newly_joined_group_routes_recent_problem_into_new_thread(self):
        item = group("om_recent", "看看刚才的问题", root="", chat="oc_new_group")
        original = replace(group("om_issue", "质检提交后卡住了", root="", chat=item.chat_id),
                           created_at=item.created_at - 1000)
        self.messenger.fetch_group_context = AsyncMock(return_value=(original,))
        turn = await self.begin(item, classification(context_ids=[original.id]))
        self.assertIn(original.text, turn.text)
        self.assertIsNone(await self.store.member(item.sender_id))
        route = self.store.routes[item.id]
        self.assertEqual(route.scope_key, self.bridge._key(item))
        self.assertIn(original.text, route.question)
        self.assertEqual(await self.store.topic_for_message(route.scope_key, original.id), route.session_key)
        await self.complete(item, turn)

    async def test_unmentioned_and_banned_new_group_members_never_fetch_history(self):
        self.messenger.fetch_group_context = AsyncMock(return_value=())
        await self.bridge.receive(group("om_quiet", root="", chat="oc_new_group", mentioned=False))
        self.store.people["ou_bob"] = {"open_id": "ou_bob", "enabled": False, "role": "member"}
        await self.bridge.receive(group("om_banned", root="", chat="oc_new_group"))
        self.messenger.fetch_group_context.assert_not_awaited()
        self.assertEqual(self.store.jobs, {})
        self.assertEqual(self.codex.turns, [])


if __name__ == "__main__":
    unittest.main()
