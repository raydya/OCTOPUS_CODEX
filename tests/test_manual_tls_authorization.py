from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from octopus.bridge import Bridge
from octopus.config import Config
from octopus.contracts import CardAction
from test_bridge import FakeCodex, FakeMessenger, message
from test_group_bridge import group
from test_provider_authorization import DMS_URL, Queries
from test_topic_routing import classification
from topic_memory import TopicMemoryStore


class ManualTLSAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = TopicMemoryStore()
        await self.store.ensure_member("ou_member")
        self.codex = FakeCodex()
        self.codex.classify_topic = AsyncMock()
        self.messenger = FakeMessenger()
        self.messenger.send_user = AsyncMock(side_effect=self.messenger.send)
        self.queries = Queries()
        self.queries.check = AsyncMock()
        self.queries.call = AsyncMock(return_value={"ok": True, "data": {}})
        config = Config("cli_test", "secret", "unused", "/isolated", None, None,
                        read_only=True, topic_routing=True, allow_groups=True, bot_open_id="ou_bot")
        self.bridge = Bridge(config, self.store, self.messenger, self.codex, self.queries)
        self.enterContext(patch("octopus.provider_authorization.POLL_SECONDS", 0.005))
        await self.bridge.start()
        self.addAsyncCleanup(self.bridge.close)

    async def test_private_text_starts_login_despite_healthy_credentials_and_leaves_commands_responsive(self):
        request = message("manual", "重新发起tls授权")
        await self.messenger.on_message(request)
        self.assertIn(request.id, self.store.controls)
        card = await self.messenger.wait_record(lambda sent: sent.chat == request.sender_id and sent.view.actions)
        self.assertEqual(self.queries.calls, [])

        # A second admin command must finish before anyone approves the pending login.
        self.bridge._control_times.clear()
        status = message("status-before-approval", "/status")
        await self.messenger.on_message(status)
        await self.store.wait_control(status.id, "done")
        await self.messenger.wait_record(lambda sent: sent.reply_to == status.id)

        action = CardAction(card.view.actions[0].ticket_id, "accept", request.sender_id, request.chat_id, card.id)
        await self.messenger.on_action(action)
        await self.messenger.wait_record(lambda sent: sent.id == card.id and self.queries.url in sent.view.text)
        self.queries.state = "succeeded"
        terminal = await self.messenger.wait_record(
            lambda sent: sent.id == card.id and sent.operation == "update" and not sent.view.actions)
        self.assertNotIn(self.queries.url, terminal.view.text)
        self.assertIn("完成", terminal.view.text)
        await self.store.wait_control(request.id, "done")
        # Redelivery after the rate limit elapses must keep the completed control.
        self.bridge._control_times.clear()
        await self.messenger.on_message(request)
        self.assertEqual(self.store.control_states[request.id], "done")
        self.messenger.send_user.assert_awaited_once()
        self.assertEqual([call[2] for call in self.queries.calls if call[0] == "start"], [request.sender_id])
        self.queries.call.assert_not_awaited()
        self.codex.classify_topic.assert_not_awaited()
        self.assertEqual(self.codex.turns, [])
        self.assertEqual(self.store.jobs, {})

    async def test_dms_command_can_finish_while_tls_is_waiting_for_approval(self):
        self.queries.url = DMS_URL
        tls = message("pending-tls", "/tls_auth")
        await self.messenger.on_message(tls)
        tls_card = await self.messenger.wait_record(lambda sent: "TLS" in sent.view.title and sent.view.actions)
        self.bridge._control_times.clear()
        dms = message("manual-dms", "/dms_auth")
        await self.messenger.on_message(dms)
        dms_card = await self.messenger.wait_record(lambda sent: "DMS" in sent.view.title and sent.view.actions)
        self.assertEqual(self.queries.calls, [])
        action = CardAction(dms_card.view.actions[0].ticket_id, "accept", dms.sender_id, dms.chat_id, dms_card.id)
        await self.messenger.on_action(replace(action, message_id=tls_card.id))
        self.assertEqual(self.queries.calls, [])
        await self.messenger.on_action(action)
        pending = await self.messenger.wait_record(lambda sent: self.queries.url in sent.view.text)
        self.assertEqual(pending.id, dms_card.id)
        self.assertIn("Mac", pending.view.text)
        self.assertNotIn("PRIVATE-CODE", pending.view.text)
        self.queries.state = "succeeded"
        terminal = await self.messenger.wait_record(lambda sent: sent.id == dms_card.id and not sent.view.actions)
        self.assertIn("DMS 授权已完成", terminal.view.text)
        self.assertNotIn(self.queries.url, terminal.view.text)
        self.assertTrue(all(call[1] == "dms" for call in self.queries.calls))
        self.assertEqual(self.store.control_states[tls.id], "running")
        await self.store.wait_control(dms.id, "done")
        self.bridge._control_times.clear()
        await self.messenger.on_message(dms)
        self.assertEqual(self.messenger.send_user.await_count, 2)
        self.assertEqual(self.store.jobs, {})

    async def test_slash_command_requires_private_admin_and_stays_out_of_business_queue(self):
        for item, expected in (
            (message("member", "/tls-auth", sender="ou_member"), "管理员"),
            (group("group-admin", "/tls-auth", sender="ou_alice"), "私聊"),
            (message("dms-member", "/dms_auth", sender="ou_member"), "管理员"),
            (group("dms-group", "/dms_auth", sender="ou_alice"), "私聊"),
        ):
            with self.subTest(item=item.id):
                self.bridge._control_times.clear()
                await self.messenger.on_message(item)
                notice = await self.messenger.wait_record(lambda sent: sent.reply_to == item.id)
                self.assertIn(expected, notice.view.text)
                self.assertFalse(notice.view.actions)
        self.assertEqual(self.queries.calls, [])
        self.messenger.send_user.assert_not_awaited()
        for identifier, command in (("admin-hyphen", "/tls-auth"), ("admin-underscore", "/tls_auth")):
            with self.subTest(command=command):
                self.bridge._control_times.clear()
                after = len(self.messenger.records)
                item = message(identifier, command)
                await self.messenger.on_message(item)
                card = await self.messenger.wait_record(
                    lambda sent: sent.chat == "ou_alice" and sent.view.actions, after=after)
                await self.messenger.on_action(CardAction(card.view.actions[0].ticket_id, "decline", "ou_alice",
                                                          "oc_shared", card.id))
                await self.messenger.wait_record(lambda sent: sent.id == card.id and not sent.view.actions)
                await self.store.wait_control(item.id, "done")
        self.assertEqual(self.queries.calls, [])
        self.codex.classify_topic.assert_not_awaited()
        self.assertEqual(self.store.jobs, {})

    async def test_command_with_arguments_or_image_only_shows_usage(self):
        for item in (message("arguments", "/tls-auth --profile other"), message("alias-args", "/tls_auth other"),
                     replace(message("alias-image", "/tls_auth"), image_keys=("img_one",)),
                     replace(message("image", "/tls-auth\n[图片 1]"), image_keys=("img_one",))):
            with self.subTest(item=item.id):
                self.bridge._control_times.clear()
                await self.messenger.on_message(item)
                await self.store.wait_control(item.id, "done")
                notice = await self.messenger.wait_record(lambda sent: sent.reply_to == item.id)
                self.assertIn("/tls-auth", notice.view.text)
                self.assertIn("/tls_auth", notice.view.text)
                self.assertFalse(notice.view.actions)
        self.messenger.send_user.assert_not_awaited()
        self.assertEqual(self.messenger.download_calls, [])
        self.assertEqual(self.queries.calls, [])
        self.assertEqual(self.store.jobs, {})

    async def test_private_help_lists_both_tls_commands_and_plain_text(self):
        help_message = message("help", "/help")
        await self.messenger.on_message(help_message)
        notice = await self.messenger.wait_record(lambda sent: sent.reply_to == help_message.id)
        self.assertIn("/tls_auth", notice.view.text)
        self.assertIn("/tls-auth", notice.view.text)
        self.assertIn("重新发起 TLS 授权", notice.view.text)
        self.assertIn("/dms_auth", notice.view.text)

    async def test_question_mentioning_reauthorization_does_not_initiate_login(self):
        self.codex.classify_topic.return_value = classification()
        question = message("definition", "重新发起TLS授权是什么意思？")
        await self.messenger.on_message(question)
        turn = await self.codex.next_turn()
        self.assertIn(question.text, turn.text)
        self.codex.complete(turn)
        await self.store.wait_state(question.id, "done")
        self.assertNotIn(question.id, self.store.controls)
        self.messenger.send_user.assert_not_awaited()
        self.assertEqual(self.queries.calls, [])
