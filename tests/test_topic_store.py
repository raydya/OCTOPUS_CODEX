from __future__ import annotations

import asyncio
import os
import unittest
import uuid
from dataclasses import asdict, replace
from unittest.mock import patch

from psycopg import AsyncConnection, sql

from octopus.contracts import IncomingMessage
from octopus.store import Store


@unittest.skipUnless(os.environ.get("OCTOPUS_TEST_DATABASE_URL"), "Set OCTOPUS_TEST_DATABASE_URL for PostgreSQL tests")
class TopicStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.url = os.environ["OCTOPUS_TEST_DATABASE_URL"]
        self.schema = "topic_test_" + uuid.uuid4().hex
        self.addAsyncCleanup(self.drop)
        self.store = await self.open()
        self.scope = "readonly:v2:oc_private:ou_alice"
        await self.store.session(self.scope, "oc_private", "ou_alice")

    async def open(self, migrate=True):
        store = await Store.open(self.url, "cli_topic_test", schema=self.schema, migrate=migrate)
        self.addAsyncCleanup(store.close)
        return store

    async def drop(self):
        async with await AsyncConnection.connect(self.url, autocommit=True) as connection:
            await connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(self.schema)))

    def message(self, identifier, text="订单123为什么失败", actor="ou_alice"):
        return IncomingMessage(identifier, "oc_private", actor, text, "p2p", 1000)

    async def route(self, identifier, target=None, **overrides):
        item = self.message(identifier)
        await self.store.enqueue(self.scope, item)
        params = dict(title="订单123失败", summary="目标：确定失败原因；尚未查询日志", prompt=item.text,
                      question=item.text, data_allowed=True, action="continue" if target else "new", reason="同一订单")
        params.update(overrides)
        return await self.store.freeze_route(identifier, self.scope, target, **params)

    async def test_routes_are_atomic_immutable_and_survive_reopen(self):
        first = await self.route("m1")
        self.assertNotEqual(first.session_key, self.scope)
        self.assertEqual(await self.store.current_topic(self.scope, "ou_alice"), first.session_key)
        self.assertEqual(await self.store.pending_routes(), [])
        self.assertEqual((await self.store.next_jobs())[0].session_key, first.session_key)
        self.assertEqual(await self.store.topic_for_message(self.scope, "m1"), first.session_key)
        await self.store.set_thread(first.session_key, "codex-thread-1")
        await self.store.map_topic_message(self.scope, first.session_key, "bot-answer-1")
        reopened = await self.open(migrate=False)
        self.assertEqual(await reopened.get_route("m1"), first)
        self.assertEqual(await reopened.topic_for_message(self.scope, "bot-answer-1"), first.session_key)
        repeated = await self.route("m1", title="不应修改", summary="不应修改", prompt="不应重路由")
        self.assertEqual(repeated, first)
        self.assertEqual((await self.store.topic_by_key(self.scope, first.session_key)).title, "订单123失败")
        self.assertEqual((await self.store.get_session(first.session_key)).thread_id, "codex-thread-1")

    async def test_concurrent_freezes_create_exactly_one_topic(self):
        await self.store.enqueue(self.scope, self.message("m1"))
        second = await self.open()
        kwargs = dict(title="问题", summary="描述", prompt="问题详情", question=None,
                      data_allowed=True, action="new", reason="新问题")
        first, other = await asyncio.gather(
            self.store.freeze_route("m1", self.scope, None, **kwargs),
            second.freeze_route("m1", self.scope, None, **kwargs))
        self.assertEqual(first, other)
        self.assertEqual(len(await self.store.topic_candidates(self.scope, "ou_alice")), 1)

    async def test_cross_scope_and_actor_routes_fail_without_partial_changes(self):
        first = await self.route("m1")
        other_scope = "readonly:v2:oc_bob:ou_bob"
        await self.store.session(other_scope, "oc_bob", "ou_bob")
        other_topic = await self.store.new_topic(other_scope, "ou_bob")
        with self.assertRaises(ValueError):
            await self.route("m2", other_topic)
        self.assertIsNone(await self.store.get_route("m2"))
        self.assertEqual((await self.store.pending_routes())[0].session_key, self.scope)
        with self.assertRaises(ValueError):
            await self.store.select_topic(self.scope, "ou_bob", first.session_key)
        with self.assertRaises(ValueError):
            await self.store.map_topic_message(other_scope, first.session_key, "foreign")
        self.assertIsNone(await self.store.topic_for_message(other_scope, "m1"))
        self.assertIsNone(await self.store.topic_by_key(other_scope, first.session_key))

    async def test_context_can_be_referenced_by_new_topic_without_reassigning_source(self):
        first = await self.route("m1", context_ids=("unprocessed-reference",))
        second = await self.route("m2", context_ids=("unprocessed-reference",), data_allowed=False)
        self.assertNotEqual(first.session_key, second.session_key)
        self.assertFalse(second.data_allowed)
        self.assertEqual(len(await self.store.topic_candidates(self.scope, "ou_alice")), 2)
        self.assertEqual(await self.store.topic_for_message(self.scope, "unprocessed-reference"), first.session_key)
        with self.assertRaises(ValueError):
            other = await self.store.new_topic(self.scope, "ou_alice")
            await self.store.map_topic_message(self.scope, other, "m1")

    async def test_restrictions_survive_continuation_but_not_unrelated_new_topic(self):
        first = await self.route("m1", data_allowed=False)
        self.assertFalse(first.data_allowed)
        second = await self.route("m2", first.session_key)
        self.assertFalse(second.data_allowed)
        third = await self.route("m3")
        self.assertTrue(third.data_allowed)
        self.assertTrue((await self.store.get_session(first.session_key)).data_context_restricted)
        self.assertFalse((await self.store.get_session(third.session_key)).data_context_restricted)
        self.assertEqual((await self.store.topic_last_question(self.scope, first.session_key))["message_id"], "m2")
        self.assertFalse((await self.store.topic_last_question(self.scope, first.session_key))["data_allowed"])

    async def test_group_actor_pointers_are_independent_and_completion_does_not_change_them(self):
        scope = "readonly:v2:group:oc_group:om_root"
        await self.store.session(scope, "oc_group", "ou_alice", topic_id="om_root")
        alice = IncomingMessage("ga", "oc_group", "ou_alice", "支付失败", "group", 1000,
                                root_id="om_root", mentioned_bot=True)
        bob = replace(alice, id="gb", sender_id="ou_bob", text="质检逻辑")
        params = dict(title="问题", summary="详情", prompt="问题", question=None,
                      data_allowed=True, action="new", reason="独立问题")
        await self.store.enqueue(scope, alice)
        await self.store.enqueue(scope, bob)
        a = await self.store.freeze_route(alice.id, scope, None, **params)
        b = await self.store.freeze_route(bob.id, scope, None, **params)
        self.assertEqual(await self.store.current_topic(scope, alice.sender_id), a.session_key)
        self.assertEqual(await self.store.current_topic(scope, bob.sender_id), b.session_key)
        await self.store.start_job(alice.id)
        await self.store.finish_job(alice.id)
        self.assertEqual(await self.store.current_topic(scope, bob.sender_id), b.session_key)
        shared = replace(bob, id="gb2", text="支付为何失败")
        await self.store.enqueue(scope, shared)
        await self.store.freeze_route(shared.id, scope, a.session_key, **{**params, "action": "resume"})
        candidates = await self.store.topic_candidates(scope, bob.sender_id)
        self.assertEqual(next(t.session_key for t in candidates if t.is_current), a.session_key)
        self.assertEqual(next(t.session_key for t in candidates if t.is_previous), b.session_key)
        self.assertEqual((await self.store.topic_last_question(scope, a.session_key, alice.sender_id))["message_id"], alice.id)
        self.assertEqual((await self.store.topic_last_question(scope, a.session_key, bob.sender_id))["message_id"], shared.id)

    async def test_legacy_seed_uses_only_finished_question_and_never_copies_result(self):
        await self.store.set_thread(self.scope, "legacy-codex")
        await self.store.enqueue(self.scope, self.message("old", "此前订单失败"))
        await self.store.start_job("old")
        await self.store.finish_job("old")
        await self.store.save_result(self.scope, "不应该复制的业务答案", "completed")
        await self.store.enqueue(self.scope, self.message("new", "另一个独立问题"))
        candidates = await self.store.topic_candidates(self.scope, "ou_alice")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].session_key, self.scope)
        self.assertEqual(candidates[0].summary, "此前订单失败")
        self.assertEqual(await self.store.topic_for_message(self.scope, "old"), self.scope)
        self.assertIsNone(await self.store.topic_for_message(self.scope, "new"))

    async def test_pending_clarification_expires_and_rejected_jobs_do_not_run(self):
        await self.store.enqueue(self.scope, self.message("m1"))
        pending = {"question": "它为什么失败", "data_allowed": False}
        with patch("octopus.topic_store._now", return_value=1_000_000):
            self.assertTrue(await self.store.defer_topic_route("m1", self.scope, pending))
            self.assertFalse(await self.store.defer_topic_route("m1", self.scope, pending))
            self.assertEqual(await self.store.get_topic_pending(self.scope, "ou_alice"), pending)
            self.assertEqual(await self.store.pending_routes(), [])
        with patch("octopus.topic_store._now", return_value=3_000_001):
            self.assertIsNone(await self.store.get_topic_pending(self.scope, "ou_alice"))
        await self.route("m2")
        self.assertIsNone(await self.store.get_topic_pending(self.scope, "ou_alice"))
        await self.store.enqueue(self.scope, self.message("m3"))
        self.assertTrue(await self.store.reject_topic_route("m3", "invalid_context"))
        self.assertFalse(await self.store.reject_topic_route("m3", "invalid_context"))
        self.assertEqual(await self.store.pending_routes(), [])

    async def test_history_purge_cascades_route_payload_but_keeps_thread_and_message_mapping(self):
        first = await self.route("m1")
        await self.store.start_job("m1")
        await self.store.finish_job("m1")
        with patch("octopus.store._now", return_value=10**15):
            await self.store.purge_history(days=1)
        self.assertIsNone(await self.store.get_route("m1"))
        self.assertIsNotNone(await self.store.get_session(first.session_key))
        self.assertEqual(await self.store.topic_for_message(self.scope, "m1"), first.session_key)

    async def test_clarified_images_survive_restart_and_force_data_restriction(self):
        original = replace(self.message("image-question", "这是什么"), image_keys=("img_original",))
        await self.store.enqueue(self.scope, original)
        await self.store.defer_topic_route(original.id, self.scope,
                                          {"question": original.text, "image_sources": [asdict(original)]})
        routed = await self.route("clarified", image_sources=(asdict(original),))
        self.assertFalse(routed.data_allowed)
        self.assertEqual(routed.image_sources, (asdict(original),))
        reopened = await self.open(migrate=False)
        self.assertEqual(await reopened.get_route("clarified"), routed)
        prior = await reopened.topic_last_question(self.scope, routed.session_key, "ou_alice")
        self.assertTrue(prior["has_images"])
        self.assertEqual(prior["image_sources"], (asdict(original),))

    async def test_image_sources_must_be_actual_accepted_messages_in_same_identity_scope(self):
        original = replace(self.message("image-question"), image_keys=("img_original",))
        await self.store.enqueue(self.scope, original)
        for index, invalid in enumerate((replace(original, image_keys=("img_changed",)),
                                         replace(original, image_keys=("data:image/png;base64,AAAA",)),
                                         replace(original, sender_id="ou_bob"),
                                         replace(original, chat_id="oc_other"),
                                         replace(original, id="missing-message"))):
            with self.assertRaises(ValueError):
                await self.route(f"invalid-{index}", image_sources=(asdict(invalid),))
            self.assertIsNone(await self.store.get_route(f"invalid-{index}"))
        with self.assertRaises(ValueError):
            await self.route("duplicated", image_sources=(asdict(original), asdict(original)))

    async def test_source_images_and_current_images_share_the_four_image_limit(self):
        source = replace(self.message("image-source"), image_keys=("img_1", "img_2", "img_3", "img_4"))
        await self.store.enqueue(self.scope, source)
        current = replace(self.message("with-new-image"), image_keys=("img_5",))
        await self.store.enqueue(self.scope, current)
        with self.assertRaises(ValueError):
            await self.route(current.id, image_sources=(asdict(source),))
        self.assertIsNone(await self.store.get_route(current.id))

    async def test_routed_queue_snapshot_excludes_unassigned_and_preserves_topic_order(self):
        await self.store.enqueue(self.scope, self.message("not-routed"))
        first = await self.route("a1")
        await self.route("a2", first.session_key)
        second = await self.route("b1")
        self.assertEqual([j.id for j in await self.store.next_routed_jobs()], ["a1", "b1"])
        self.assertEqual([j.session_key for j in await self.store.next_routed_jobs()],
                         [first.session_key, second.session_key])
        await self.store.set_blocked(first.session_key, "paused")
        self.assertEqual([j.id for j in await self.store.next_routed_jobs()], ["b1"])

    async def test_cancel_pending_is_actor_scoped_and_routed_cancel_preserves_ingress(self):
        scope = "readonly:v2:group:oc_group:om_root"
        await self.store.session(scope, "oc_group", "ou_alice", topic_id="om_root")
        await self.store.set_thread(scope, "legacy-thread")
        await self.store.topic_candidates(scope, "ou_alice")
        alice = IncomingMessage("alice-pending", "oc_group", "ou_alice", "支付失败", "group", 1000,
                                root_id="om_root", mentioned_bot=True)
        bob = replace(alice, id="bob-pending", sender_id="ou_bob")
        routed = replace(alice, id="alice-routed")
        for item in (alice, bob, routed):
            await self.store.enqueue(scope, item)
        await self.store.freeze_route(routed.id, scope, scope, title="旧问题", summary="继续",
                                     prompt="问题", question="问题", data_allowed=True, action="continue", reason="引用")
        self.assertEqual(await self.store.cancel_routed_jobs(scope), 1)
        self.assertEqual([j.id for j in await self.store.pending_routes()], [alice.id, bob.id])
        self.assertEqual(await self.store.cancel_pending_routes(scope, alice.sender_id), 1)
        self.assertEqual([j.id for j in await self.store.pending_routes()], [bob.id])

    async def test_selection_control_and_question_follow_shared_ingress_order(self):
        new = self.message("new-command", "/new")
        await self.store.enqueue_control(new)
        await self.store.enqueue(self.scope, self.message("after-new"))
        self.assertEqual(await self.store.pending_routes(), [])
        self.assertTrue(await self.store.topic_control_ready(new.id))
        await self.store.start_control(new.id)
        self.assertEqual(await self.store.pending_routes(), [])
        await self.store.finish_control(new.id)
        self.assertEqual([job.id for job in await self.store.pending_routes()], ["after-new"])
        switch = self.message("later-switch", "/SWITCH\t12345678")
        await self.store.enqueue_control(switch)
        await self.store.enqueue(self.scope, self.message("after-switch"))
        self.assertEqual([job.id for job in await self.store.pending_routes()], ["after-new"])
        self.assertFalse(await self.store.topic_control_ready(switch.id))
        await self.route("after-new")
        self.assertTrue(await self.store.topic_control_ready(switch.id))
        await self.store.start_control(switch.id)
        await self.store.finish_control(switch.id)
        self.assertEqual([job.id for job in await self.store.pending_routes()], ["after-switch"])

    async def test_ingress_order_does_not_block_other_actors_or_feishu_topics(self):
        group = "readonly:v2:group:oc_group:om_root"
        other = "readonly:v2:group:oc_group:om_other"
        await self.store.session(group, "oc_group", "ou_alice", topic_id="om_root")
        await self.store.session(other, "oc_group", "ou_alice", topic_id="om_other")
        command = IncomingMessage("selection", "oc_group", "ou_alice", "/new", "group", 1000,
                                  root_id="om_root", mentioned_bot=True)
        await self.store.enqueue_control(command)
        same = replace(command, id="same", text="问题")
        bob = replace(same, id="bob", sender_id="ou_bob")
        elsewhere = replace(same, id="elsewhere", root_id="om_other")
        await self.store.enqueue(group, same)
        await self.store.enqueue(group, bob)
        await self.store.enqueue(other, elsewhere)
        self.assertEqual([job.id for job in await self.store.pending_routes()], ["bob", "elsewhere"])
        other_command = replace(command, id="other-command", root_id="om_another")
        await self.store.enqueue_control(other_command)
        self.assertTrue(await self.store.topic_control_ready(other_command.id))
        status = replace(command, id="status", text="/status")
        await self.store.enqueue_control(status)
        self.assertTrue(await self.store.topic_control_ready(status.id))

    async def test_ingress_migration_backfills_both_tables_in_chronological_order(self):
        with patch("octopus.store._now", return_value=1000):
            await self.store.enqueue(self.scope, self.message("before"))
        with patch("octopus.store._now", return_value=2000):
            await self.store.enqueue_control(self.message("between", "/new"))
        with patch("octopus.store._now", return_value=3000):
            await self.store.enqueue(self.scope, self.message("after"))
        async with await AsyncConnection.connect(self.url, autocommit=True) as conn:
            for table in ("jobs", "controls"):
                await conn.execute(sql.SQL("ALTER TABLE {}.{} DROP COLUMN routing_order").format(
                    sql.Identifier(self.schema), sql.Identifier(table)))
        migrated = await self.open()
        self.assertEqual([job.id for job in await migrated.pending_routes()], ["before"])
        self.assertFalse(await migrated.topic_control_ready("between"))
        await migrated.reject_topic_route("before", "cancelled")
        self.assertTrue(await migrated.topic_control_ready("between"))
