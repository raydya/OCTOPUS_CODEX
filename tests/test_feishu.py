from __future__ import annotations

import asyncio
import json
import unittest
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

from octopus.contracts import Button, CardAction, View
from octopus.feishu import (CARD_BUDGET, FeishuMessenger, _LarkApi, _heartbeat, _post_with_timeout, _roundtrip,
                            build_card, parse_action, parse_message)
from octopus.images import MAX_IMAGE_BYTES, MAX_IMAGES, ImageInputError


def incoming(**changes):
    message = {"message_id": "om_input", "chat_id": "oc_chat", "chat_type": "p2p",
               "message_type": "text", "create_time": "1792368000000",
               "content": json.dumps({"text": "hello"})}
    message.update(changes)
    return {"event": {"sender": {"sender_type": "user", "sender_id": {"open_id": "ou_owner"}},
                       "message": message}}


class ParserTests(unittest.TestCase):
    def test_messages_filter_and_remove_mentions(self):
        self.assertEqual(parse_message(incoming()).text, "hello")
        for changes in ({"message_type": "file"}, {"content": "not-json"},
                        {"chat_type": "group"}, {"create_time": "bad"}):
            self.assertIsNone(parse_message(incoming(**changes)))
        bot = incoming()
        bot["event"]["sender"]["sender_type"] = "bot"
        self.assertIsNone(parse_message(bot))
        group = incoming(chat_type="group", content=json.dumps({"text": "@_user_1 fix a + b"}),
                         mentions=[{"key": "@_user_1", "id": {"open_id": "ou_bot"}}])
        self.assertIsNone(parse_message(group))  # Unknown bot identity must not accept @anyone.
        self.assertEqual(parse_message(group, bot_open_id="ou_bot").text, "fix a + b")
        self.assertIsNone(parse_message(group, bot_open_id="ou_other"))
        colleague = incoming(chat_type="group", content=json.dumps({"text": "@_user_2 review this"}),
                             mentions=[{"key": "@_user_2", "id": {"open_id": "ou_colleague"}}])
        self.assertIsNone(parse_message(colleague, bot_open_id="ou_bot"))
        self.assertIsNotNone(parse_message(incoming(chat_type="group"), require_group_mention=False))

    def test_image_event_has_default_prompt_and_validated_resource_key(self):
        message = parse_message(incoming(message_type="image",
                                         content=json.dumps({"image_key": "img_v3_example-1"})))
        self.assertEqual(message.image_keys, ("img_v3_example-1",))
        self.assertIn("图片", message.text)
        self.assertEqual(message.attachment_error, "")
        for content in ("not-json", "[]", "{}", json.dumps({"image_key": "https://example.invalid/image"}),
                        json.dumps({"image_key": "img_../../secret"}),
                        json.dumps({"image_key": "img_bad?token=secret"}),
                        json.dumps({"image_key": "img_" + "a" * 253}),
                        json.dumps({"image_key": None})):
            with self.subTest(content=content):
                rejected = parse_message(incoming(message_type="image", content=content))
                self.assertTrue(rejected.attachment_error)
                self.assertEqual(rejected.image_keys, ())
                self.assertNotIn("secret", rejected.attachment_error)

    def test_image_and_post_enforce_user_identity_and_group_mention_before_parsing(self):
        for kind, content in (("image", {"image_key": "img_example"}),
                              ("post", {"content": [[{"tag": "img", "image_key": "img_example"}]]})):
            group = incoming(message_type=kind, chat_type="group", content=json.dumps(content))
            self.assertIsNone(parse_message(group, bot_open_id="ou_bot"))
            group["event"]["message"]["mentions"] = [{"key": "@_user_1", "id": {"open_id": "ou_bot"}}]
            self.assertIsNotNone(parse_message(group, bot_open_id="ou_bot"))
            self.assertIsNone(parse_message(group, bot_open_id="ou_other"))
            group["event"]["sender"]["sender_type"] = "bot"
            self.assertIsNone(parse_message(group, bot_open_id="ou_bot"))
        for field in ("message_id", "chat_id", "create_time"):
            event = incoming(message_type="image", content=json.dumps({"image_key": "img_example"}))
            event["event"]["message"][field] = ""
            self.assertIsNone(parse_message(event))

    def test_post_preserves_text_links_paragraphs_and_deduplicated_image_order(self):
        post = {"title": "排查问题", "content": [
            [{"tag": "at", "user_id": "ou_bot"}, {"tag": "text", "text": "请看"},
             {"tag": "a", "text": "业务文档", "href": "https://example.invalid/docs"}],
            [{"tag": "img", "image_key": "img_second"}, {"tag": "text", "text": "出现错误"},
             {"tag": "img", "image_key": "img_first"}],
            [{"tag": "img", "image_key": "img_second"}],
            [{"tag": "code_block", "language": "SQL", "text": "select 1;"}],
        ]}
        message = parse_message(incoming(message_type="post", content=json.dumps(post)))
        self.assertEqual(message.image_keys, ("img_second", "img_first"))
        self.assertEqual(message.attachment_error, "")
        self.assertEqual(message.text, "排查问题\n请看业务文档 (https://example.invalid/docs)\n"
                                      "[图片 1]出现错误[图片 2]\n[图片 1]\n```\nselect 1;\n```")

    def test_post_locale_selection_and_images_without_text(self):
        chinese = {"content": [[{"tag": "img", "image_key": "img_cn"}]]}
        english = {"title": "English", "content": [[{"tag": "text", "text": "hello"}]]}
        for content, expected_keys, expected_text in (
                ({"en_us": english, "zh_cn": chinese}, ("img_cn",), "图片"),
                ({"ja_jp": chinese, "en_us": english}, (), "English\nhello"),
                ({"ja_jp": chinese}, ("img_cn",), "图片")):
            message = parse_message(incoming(message_type="post", content=json.dumps(content)))
            self.assertEqual(message.image_keys, expected_keys)
            self.assertIn(expected_text, message.text)
            self.assertEqual(message.attachment_error, "")

    def test_post_rejects_too_many_images_without_silently_truncating_input(self):
        images = [{"tag": "img", "image_key": f"img_{index}"} for index in range(MAX_IMAGES)]
        valid = parse_message(incoming(message_type="post", content=json.dumps({"content": [images * 3]})))
        self.assertEqual(len(valid.image_keys), MAX_IMAGES)
        self.assertEqual(valid.attachment_error, "")
        invalid = parse_message(incoming(message_type="post", content=json.dumps(
            {"content": [images + [{"tag": "img", "image_key": "img_extra"}]]})))
        self.assertIn(str(MAX_IMAGES), invalid.attachment_error)
        self.assertEqual(len(invalid.image_keys), MAX_IMAGES)

    def test_post_rejects_unsupported_attachments_malformed_nodes_and_invalid_keys(self):
        for content in (
                {}, {"content": "not-a-list"}, {"content": ["not-a-row"]},
                {"content": [[{"tag": "media", "image_key": "img_cover", "file_key": "file_video"}]]},
                {"content": [[{"tag": "file", "file_key": "file_abc"}]]},
                {"content": [[{"tag": "img", "image_key": "file_abc"}]]},
                {"content": [[{"tag": "text", "text": []}]]},
                {"content": [[42]]},
                {"content": [[{"tag": "md", "text": "![embedded](img_abc)"}]]},
        ):
            with self.subTest(content=content):
                message = parse_message(incoming(message_type="post", content=json.dumps(content)))
                self.assertTrue(message.attachment_error)
                self.assertEqual(message.image_keys, ())

    def test_arbitrary_links_and_plain_markdown_are_text_not_image_downloads(self):
        content = {"content": [[{"tag": "text", "text": "https://example.invalid/screenshot.png"},
                               {"tag": "a", "href": "http://127.0.0.1/private.jpg", "text": "链接"}],
                              [{"tag": "md", "text": "**hello**"}]]}
        message = parse_message(incoming(message_type="post", content=json.dumps(content)))
        self.assertEqual(message.image_keys, ())
        self.assertEqual(message.attachment_error, "")
        self.assertIn("http://127.0.0.1/private.jpg", message.text)
        self.assertIn("**hello**", message.text)

    def test_action_requires_exact_decision_operator_and_context(self):
        event = {"operator": {"open_id": "ou_owner"},
                 "context": {"open_chat_id": "oc_chat", "open_message_id": "om_card"},
                 "action": {"tag": "button", "value": {"ticketId": "ticket", "decision": "accept"}}}
        self.assertEqual(parse_action({"event": event}), CardAction("ticket", "accept", "ou_owner", "oc_chat", "om_card"))
        self.assertIsNone(parse_action({**event, "context": {}}))
        event["action"]["value"]["decision"] = "acceptForSession"
        self.assertIsNone(parse_action(event))

    def test_card_size_accounts_for_utf8_and_nested_json_escaping(self):
        card = build_card(View("approval", "批准", ('测试😀\\"\n' * 20_000),
                               [Button("允许本次", "ticket", "accept"), Button("拒绝", "ticket", "decline")]))
        encoded = json.dumps({"content": json.dumps(card, ensure_ascii=False, separators=(",", ":"))},
                             ensure_ascii=False, separators=(",", ":"))
        self.assertLessEqual(len(encoded.encode()), CARD_BUDGET)
        self.assertEqual(card["schema"], "2.0")
        self.assertTrue(card["config"]["update_multi"])
        self.assertIn("截断", card["body"]["elements"][0]["content"])
        value = card["body"]["elements"][1]["columns"][0]["elements"][0]["behaviors"][0]["value"]
        self.assertEqual(value, {"ticketId": "ticket", "decision": "accept"})

    def test_worker_requires_positive_matching_ack_and_ignores_stale_acks(self):
        connection = Mock()
        connection.poll.return_value = True
        replies = iter([{"delivery_id": "old", "accepted": True}, {"delivery_id": "new", "accepted": True}])
        connection.recv.side_effect = lambda: next(replies)
        with patch("octopus.feishu.uuid.uuid4", return_value=SimpleNamespace(hex="new")):
            _roundtrip(connection, "message", {"text": "hello"})
        connection.poll.return_value = False
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            _roundtrip(connection, "message", {})
        connection.poll.return_value = True
        connection.recv.side_effect = lambda: {"delivery_id": "new", "accepted": False}
        with patch("octopus.feishu.uuid.uuid4", return_value=SimpleNamespace(hex="new")):
            with self.assertRaisesRegex(RuntimeError, "did not accept"):
                _roundtrip(connection, "message", {})


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def messenger(self, api=None):
        self.logs = []
        if api is None:
            api = Mock()
            api.send.return_value = SimpleNamespace(code=0, data=SimpleNamespace(message_id="om_reply"))
            api.send_user.return_value = SimpleNamespace(code=0, data=SimpleNamespace(message_id="om_direct"))
            api.update.return_value = SimpleNamespace(code=0)
        messenger = FeishuMessenger("cli_separate", "secret-never-log", self.logs.append, api=api)
        messenger._connection = Mock()
        return messenger, api

    async def test_send_reply_update_and_no_sensitive_error_details(self):
        messenger, api = self.messenger()
        view = View("progress", "Codex", "Working")
        self.assertEqual(await messenger.send("oc_chat", view, "om_input"), "om_reply")
        self.assertEqual(api.send.call_args.args[0], "oc_chat")
        self.assertEqual(api.send.call_args.args[2], "om_input")
        await messenger.update("om_reply", View("result", "Done", "Finished"))
        self.assertEqual(api.update.call_args.args[0], "om_reply")
        api.send.side_effect = RuntimeError("token=secret-never-log private message")
        with self.assertRaises(RuntimeError) as error:
            await messenger.send("oc_chat", view)
        self.assertNotIn("secret-never-log", str(error.exception))
        self.assertNotIn("private message", str(error.exception))
        api.update.return_value = SimpleNamespace(code=230027)
        with self.assertRaisesRegex(RuntimeError, "230027"):
            await messenger.update("om_reply", view)
        await messenger.close()

    async def test_download_image_binds_message_and_key_and_checks_result(self):
        messenger, api = self.messenger()
        api.download_image.return_value = b"image-data"
        self.assertEqual(await messenger.download_image("om_input", "img_input"), b"image-data")
        api.download_image.assert_called_once_with("om_input", "img_input")
        for data in (None, "", b"", b"x" * (MAX_IMAGE_BYTES + 1)):
            api.download_image.return_value = data
            with self.assertRaises(ImageInputError):
                await messenger.download_image("om_input", "img_input")
        await messenger.close()
        with self.assertRaises(ImageInputError):
            await messenger.download_image("om_input", "img_input")

    async def test_download_invalid_resources_do_not_call_api(self):
        messenger, api = self.messenger()
        for message_id, image_key in (("", "img_ok"), ("om_../outside", "img_ok"),
                                      ("om_ok", "https://example.invalid/a"),
                                      ("om_ok", "img_a?type=file"), (None, "img_ok")):
            with self.assertRaises(ImageInputError):
                await messenger.download_image(message_id, image_key)
        api.download_image.assert_not_called()
        await messenger.close()

    async def test_download_failure_does_not_expose_credentials_or_response_text(self):
        messenger, api = self.messenger()
        api.download_image.side_effect = RuntimeError("Bearer private-token response private-content")
        with self.assertRaises(ImageInputError) as error:
            await messenger.download_image("om_input", "img_input")
        self.assertNotIn("private", str(error.exception))
        self.assertEqual(self.logs, [])
        await messenger.close()

    async def test_send_user_keeps_durable_request_id_across_retries(self):
        messenger, api = self.messenger()
        request_id = "f76b28d7-4b7c-48e6-8b73-9b5d1b324187"
        view = View("approval", "交谈权限申请", "用户希望使用机器人",
                    [Button("同意", "access:" + request_id, "accept")])
        api.send_user.side_effect = [
            TimeoutError("token=secret-never-log private response"),
            SimpleNamespace(code=0, data=SimpleNamespace(message_id="om_direct")),
        ]
        with self.assertRaisesRegex(RuntimeError, "send_user failed") as error:
            await messenger.send_user("ou_owner", view, request_id=request_id)
        self.assertNotIn("secret-never-log", str(error.exception))
        self.assertNotIn("private response", str(error.exception))
        self.assertEqual(await messenger.send_user("ou_owner", view, request_id=request_id), "om_direct")
        self.assertEqual(api.send_user.call_count, 2)
        for call in api.send_user.call_args_list:
            self.assertEqual(call.args[0], "ou_owner")
            self.assertEqual(call.args[2], request_id)
            card = json.loads(call.args[1])
            value = card["body"]["elements"][1]["columns"][0]["elements"][0]["behaviors"][0]["value"]
            self.assertEqual(value, {"ticketId": "access:" + request_id, "decision": "accept"})
        api.send.assert_not_called()
        await messenger.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await messenger.send_user("ou_owner", view, request_id=request_id)

    async def test_send_user_generates_default_id_and_rejects_invalid_identifiers(self):
        messenger, api = self.messenger()
        view = View("info", "", "申请已通过")
        with patch("octopus.feishu.uuid.uuid4", return_value=SimpleNamespace(hex="a" * 32)):
            self.assertEqual(await messenger.send_user("ou_applicant", view), "om_direct")
        self.assertEqual(api.send_user.call_args.args[2], "a" * 32)
        api.send_user.reset_mock()
        for open_id in (None, "", "ou_", "oc_chat", "ou_a\n", "ou_中文", "ou_" + "a" * 126, 1):
            with self.subTest(open_id=open_id):
                with self.assertRaisesRegex(ValueError, "open ID"):
                    await messenger.send_user(open_id, view)
        for request_id in ("", " ", "a" * 51, "secret\nvalue", "秘密", 1):
            with self.subTest(request_id=request_id):
                with self.assertRaisesRegex(ValueError, "request ID") as error:
                    await messenger.send_user("ou_owner", view, request_id=request_id)
                self.assertNotIn("secret", str(error.exception))
        api.send_user.assert_not_called()
        await messenger.close()

    async def test_send_user_failure_codes_and_missing_ids_are_sanitized(self):
        messenger, api = self.messenger()
        view = View("info", "", "申请结果")
        api.send_user.return_value = SimpleNamespace(code=99991672, msg="secret-never-log")
        with self.assertRaisesRegex(RuntimeError, "99991672") as error:
            await messenger.send_user("ou_applicant", view)
        self.assertNotIn("secret-never-log", str(error.exception))
        for data in (None, SimpleNamespace(), SimpleNamespace(message_id=""),
                     SimpleNamespace(message_id="  "), SimpleNamespace(message_id=1)):
            api.send_user.return_value = SimpleNamespace(code=0, data=data)
            with self.assertRaisesRegex(RuntimeError, "no message ID"):
                await messenger.send_user("ou_applicant", view)
        await messenger.close()

    async def test_reaction_lifecycle_returns_own_record_for_removal(self):
        messenger, api = self.messenger()
        api.add_reaction.return_value = SimpleNamespace(code=0, data=SimpleNamespace(reaction_id="reaction_1"))
        api.remove_reaction.return_value = SimpleNamespace(code=0)
        self.assertEqual(await messenger.add_reaction("om_input"), "reaction_1")
        api.add_reaction.assert_called_once_with("om_input", "Typing")
        await messenger.remove_reaction("om_input", "reaction_1")
        api.remove_reaction.assert_called_once_with("om_input", "reaction_1")
        await messenger.add_reaction("om_input", "DONE")
        api.add_reaction.assert_called_with("om_input", "DONE")
        api.send.assert_not_called()
        await messenger.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await messenger.add_reaction("om_input")
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await messenger.remove_reaction("om_input", "reaction_1")

    async def test_reactions_reject_invalid_input_and_missing_response_id(self):
        messenger, api = self.messenger()
        for bad in (None, "", "  ", 1):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    await messenger.add_reaction(bad)
                with self.assertRaises(ValueError):
                    await messenger.add_reaction("om_input", bad)
                with self.assertRaises(ValueError):
                    await messenger.remove_reaction(bad, "reaction_1")
                with self.assertRaises(ValueError):
                    await messenger.remove_reaction("om_input", bad)
        api.add_reaction.assert_not_called()
        api.remove_reaction.assert_not_called()
        for data in (None, SimpleNamespace(), SimpleNamespace(reaction_id=""),
                     SimpleNamespace(reaction_id=1)):
            api.add_reaction.return_value = SimpleNamespace(code=0, data=data)
            with self.assertRaisesRegex(RuntimeError, "no reaction ID"):
                await messenger.add_reaction("om_input")
        await messenger.close()

    async def test_reaction_failures_expose_codes_without_sensitive_details(self):
        messenger, api = self.messenger()
        api.add_reaction.return_value = SimpleNamespace(code=99991672, msg="secret-never-log")
        with self.assertRaisesRegex(RuntimeError, "99991672") as error:
            await messenger.add_reaction("om_input")
        self.assertNotIn("secret-never-log", str(error.exception))
        api.remove_reaction.side_effect = RuntimeError("token=secret-never-log private message")
        with self.assertRaisesRegex(RuntimeError, "remove_reaction failed") as error:
            await messenger.remove_reaction("om_input", "reaction_1")
        self.assertNotIn("secret-never-log", str(error.exception))
        self.assertNotIn("private message", str(error.exception))
        await messenger.close()

    async def test_only_reaction_removal_accepts_documented_absent_resources(self):
        messenger, api = self.messenger()
        for code in (231011, 231003, 230110, 231005, 232009):
            with self.subTest(absent_code=code):
                response = SimpleNamespace(code=code, msg="private error detail")
                api.remove_reaction.return_value = response
                await messenger.remove_reaction("om_input", "reaction_1")
                api.add_reaction.return_value = response
                with self.assertRaisesRegex(RuntimeError, str(code)):
                    await messenger.add_reaction("om_input")
                api.update.return_value = response
                with self.assertRaisesRegex(RuntimeError, str(code)):
                    await messenger.update("om_input", View("info", "Title", "Text"))
        # In-progress, archived/inaccessible, mismatched and unauthorized
        # resources are not evidence that our reaction has disappeared.
        for code in (231015, 231004, 231010, 231007, 231008, 99991672):
            with self.subTest(retry_or_failure_code=code):
                api.remove_reaction.return_value = SimpleNamespace(code=code, msg="private error detail")
                with self.assertRaisesRegex(RuntimeError, str(code)) as error:
                    await messenger.remove_reaction("om_input", "reaction_1")
                self.assertNotIn("private error detail", str(error.exception))
        await messenger.close()

    async def test_ack_is_only_sent_after_durable_inbox_handler_finishes(self):
        messenger, _ = self.messenger()
        release = asyncio.Event()
        persisted = []

        async def on_message(message):
            await release.wait()
            persisted.append(message.id)

        messenger._on_message = on_message
        payload = asdict(parse_message(incoming()))
        task = asyncio.create_task(messenger._deliver({"type": "message", "delivery_id": "delivery", "payload": payload}))
        await asyncio.sleep(0)
        messenger._connection.send.assert_not_called()
        release.set()
        await task
        self.assertEqual(persisted, ["om_input"])
        messenger._connection.send.assert_called_once_with({"delivery_id": "delivery", "accepted": True})
        await messenger.close()

    async def test_enqueue_timeout_and_handler_failures_request_retry(self):
        messenger, _ = self.messenger()

        async def slow(message):
            await asyncio.Event().wait()

        messenger._on_message = slow
        item = {"type": "message", "delivery_id": "delivery", "payload": asdict(parse_message(incoming()))}
        with patch("octopus.feishu.ENQUEUE_TIMEOUT", 0.01):
            await messenger._deliver(item)
        messenger._connection.send.assert_called_with({"delivery_id": "delivery", "accepted": False})

        async def broken(message):
            raise RuntimeError("private content token=abc")

        messenger._on_message = broken
        await messenger._deliver(item)
        self.assertTrue(all("private" not in message and "token" not in message for message in self.logs))
        await messenger.close()

    async def test_spawn_is_used_and_shutdown_terminates_only_owned_worker(self):
        context = Mock()
        parent, child = Mock(), Mock()
        context.Pipe.return_value = parent, child
        process = context.Process.return_value
        process.is_alive.side_effect = [True, False]
        messenger = FeishuMessenger("cli_new", "new-secret", api=Mock(), process_context=context)

        async def handler(value):
            pass

        await messenger.start(handler, handler)
        process.start.assert_called_once()
        child.close.assert_called_once()
        self.assertEqual(context.Process.call_args.kwargs["args"][1:3], ("cli_new", "new-secret"))
        await messenger.close()
        await messenger.close()
        process.terminate.assert_called_once()
        process.kill.assert_not_called()
        parent.close.assert_called_once()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await messenger.send("oc_chat", View("info", "Hi", "Hello"))

    async def test_heartbeat_reports_only_connection_presence_and_stops_on_closed_pipe(self):
        connection = Mock()
        connection.send.side_effect = [None, OSError("closed")]
        client = SimpleNamespace(_conn=object(), _conn_url="secret-url-must-not-leak")
        with patch("octopus.feishu.asyncio.sleep", new_callable=AsyncMock):
            await _heartbeat(connection, client)
        self.assertEqual(connection.send.call_args_list[0].args[0], {"type": "heartbeat", "connected": True})
        self.assertNotIn("secret-url", str(connection.send.call_args_list))


class ReactionSdkTests(unittest.TestCase):
    def setUp(self):
        # The SDK captures a module-level event loop on first import.
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self):
        self.loop.close()
        asyncio.set_event_loop(None)

    def test_sdk_maps_reaction_body_and_exact_delete_target(self):
        api = _LarkApi.__new__(_LarkApi)
        api.client = Mock()
        resource = api.client.im.v1.message_reaction
        self.assertIs(api.add_reaction("om_input", "Typing"), resource.create.return_value)
        request = resource.create.call_args.args[0]
        self.assertEqual(request.paths, {"message_id": "om_input"})
        self.assertEqual(request.request_body.reaction_type.emoji_type, "Typing")
        self.assertEqual(request.uri, "/open-apis/im/v1/messages/:message_id/reactions")
        self.assertIs(api.remove_reaction("om_input", "reaction_1"), resource.delete.return_value)
        request = resource.delete.call_args.args[0]
        self.assertEqual(request.paths, {"message_id": "om_input", "reaction_id": "reaction_1"})
        self.assertEqual(request.uri, "/open-apis/im/v1/messages/:message_id/reactions/:reaction_id")

    def test_sdk_sends_direct_card_with_open_id_and_exact_idempotency_key(self):
        api = _LarkApi.__new__(_LarkApi)
        api.client = Mock()
        resource = api.client.im.v1.message
        content = '{"schema":"2.0","body":{"elements":[]}}'
        request_id = "f76b28d74b7c48e68b739b5d1b3241870000"
        self.assertIs(api.send_user("ou_owner", content, request_id), resource.create.return_value)
        request = resource.create.call_args.args[0]
        self.assertEqual(request.uri, "/open-apis/im/v1/messages")
        self.assertEqual(request.receive_id_type, "open_id")
        self.assertEqual(dict(request.queries), {"receive_id_type": "open_id"})
        self.assertEqual(request.request_body.receive_id, "ou_owner")
        self.assertEqual(request.request_body.msg_type, "interactive")
        self.assertEqual(request.request_body.content, content)
        self.assertEqual(request.request_body.uuid, request_id)
        resource.reply.assert_not_called()

    def test_sdk_chat_send_and_reply_keep_existing_target_semantics(self):
        api = _LarkApi.__new__(_LarkApi)
        api.client = Mock()
        resource = api.client.im.v1.message
        api.send("oc_chat", "content", None, "request1")
        request = resource.create.call_args.args[0]
        self.assertEqual(request.receive_id_type, "chat_id")
        self.assertEqual(request.request_body.receive_id, "oc_chat")
        self.assertEqual(request.request_body.uuid, "request1")
        api.send("oc_chat", "content", "om_input", "request2")
        request = resource.reply.call_args.args[0]
        self.assertEqual(request.paths, {"message_id": "om_input"})
        self.assertEqual(request.request_body.uuid, "request2")


class ImageDownloadSdkTests(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.api = _LarkApi.__new__(_LarkApi)
        self.api.client = SimpleNamespace(config=object())
        self.response = Mock()
        self.response.status_code = 200
        self.response.headers = {"Content-Type": "image/png"}
        self.response.raw.read1.side_effect = [b"first", b"second", b""]
        self.context = Mock()
        self.context.__enter__ = Mock(return_value=self.response)
        self.context.__exit__ = Mock(return_value=False)
        self.get = patch("requests.get", return_value=self.context).start()
        self.token = patch("lark_oapi.core.token.TokenManager.get_self_tenant_token",
                           return_value="app-private-token").start()
        self.addCleanup(patch.stopall)

    def tearDown(self):
        self.loop.close()
        asyncio.set_event_loop(None)

    def test_streams_exact_message_resource_using_same_app_token_without_redirects(self):
        self.assertEqual(self.api.download_image("om_event", "img_v3_key"), b"firstsecond")
        self.token.assert_called_once_with(self.api.client.config)
        self.get.assert_called_once()
        self.assertEqual(self.get.call_args.args,
                         ("https://open.feishu.cn/open-apis/im/v1/messages/om_event/resources/img_v3_key",))
        options = self.get.call_args.kwargs
        self.assertEqual(options["params"], {"type": "image"})
        self.assertEqual(options["headers"]["Authorization"], "Bearer app-private-token")
        self.assertEqual(options["headers"]["Accept-Encoding"], "identity")
        self.assertTrue(options["stream"])
        self.assertFalse(options["allow_redirects"])
        self.assertEqual(options["timeout"], (5, 10))
        self.assertEqual(self.response.raw.read1.call_args_list,
                         [call(65536, decode_content=False)] * 3)
        self.context.__exit__.assert_called_once()

    def test_invalid_identifiers_never_fetch_token_or_make_network_requests(self):
        for message_id, image_key in (("om_../../secret", "img_a"), ("om_a", "img_../secret"),
                                      ("om_a", "img_a#fragment"), ("om_a\n", "img_a")):
            with self.assertRaises(ImageInputError):
                self.api.download_image(message_id, image_key)
        self.token.assert_not_called()
        self.get.assert_not_called()

    def test_declared_size_and_actual_stream_size_are_both_enforced(self):
        self.response.headers["Content-Length"] = str(MAX_IMAGE_BYTES + 1)
        with self.assertRaisesRegex(ImageInputError, "5 MiB"):
            self.api.download_image("om_event", "img_key")
        self.response.raw.read1.assert_not_called()
        del self.response.headers["Content-Length"]
        with patch("octopus.feishu.MAX_IMAGE_BYTES", 8):
            with self.assertRaisesRegex(ImageInputError, "5 MiB"):
                self.api.download_image("om_event", "img_key")
        self.assertEqual(self.response.raw.read1.call_args_list,
                         [call(9, decode_content=False), call(4, decode_content=False)])
        self.assertEqual(self.context.__exit__.call_count, 2)

    def test_accepts_exact_limit_and_refuses_empty_body(self):
        self.response.raw.read1.side_effect = [b"1234", b"5678", b""]
        with patch("octopus.feishu.MAX_IMAGE_BYTES", 8):
            self.assertEqual(self.api.download_image("om_event", "img_key"), b"12345678")
        self.assertEqual(self.response.raw.read1.call_args_list[-1], call(1, decode_content=False))
        self.response.raw.read1.side_effect = [b""]
        with self.assertRaises(ImageInputError):
            self.api.download_image("om_event", "img_key")

    def test_deadline_stops_slow_progress_and_closes_response(self):
        with patch("octopus.feishu.time.monotonic", side_effect=[0, 0, 1, 46]):
            with self.assertRaises(ImageInputError):
                self.api.download_image("om_event", "img_key")
        self.response.raw.read1.assert_called_once()
        self.context.__exit__.assert_called_once()

    def test_rejects_compression_non_images_redirects_and_malformed_lengths(self):
        for status, headers in (
                (200, {"Content-Encoding": "gzip"}),
                (200, {"Content-Type": "application/json"}),
                (200, {"Content-Length": "-1"}),
                (200, {"Content-Length": "five"}),
                (302, {"Location": "https://attacker.invalid/?token=private"}),
                (401, {}), (404, {}),
        ):
            with self.subTest(status=status, headers=headers):
                self.response.status_code, self.response.headers = status, headers
                self.response.raw.read1.side_effect = [b'{"code":234003,"msg":"private details"}']
                with self.assertRaises(ImageInputError) as error:
                    self.api.download_image("om_event", "img_key")
                self.assertNotIn("private", str(error.exception))
                self.assertNotIn("attacker", str(error.exception))
        self.assertEqual(self.context.__exit__.call_count, 7)

    def test_permission_error_only_exposes_fixed_required_scope_guidance(self):
        self.response.status_code = 403
        self.response.raw.read1.side_effect = [b'{"code":99991672,"msg":"secret response"}']
        with self.assertRaisesRegex(ImageInputError, "im:message:readonly") as error:
            self.api.download_image("om_event", "img_key")
        self.assertIn("发布", str(error.exception))
        self.assertNotIn("secret", str(error.exception))
        self.response.raw.read1.assert_called_once_with(4096, decode_content=False)

    def test_token_and_network_errors_are_sanitized(self):
        self.token.side_effect = RuntimeError("private token response")
        with self.assertRaises(ImageInputError) as error:
            self.api.download_image("om_event", "img_key")
        self.assertNotIn("private", str(error.exception))
        self.get.assert_not_called()
        self.token.side_effect = None
        self.response.raw.read1.side_effect = TimeoutError("Authorization: private-token")
        with self.assertRaises(ImageInputError) as error:
            self.api.download_image("om_event", "img_key")
        self.assertNotIn("private", str(error.exception))
        self.context.__exit__.assert_called_once()


class ListenerHealthTests(unittest.TestCase):
    def messenger(self):
        messenger = FeishuMessenger("cli_test", "test-secret", api=Mock())
        messenger._process = Mock()
        messenger._process.is_alive.return_value = True
        messenger._reader = Mock()
        messenger._reader.done.return_value = False
        messenger._started_at = 100.0
        messenger._disconnected_at = 100.0
        return messenger

    def test_initial_process_is_not_ready_and_startup_has_deadline(self):
        messenger = self.messenger()
        with patch("octopus.feishu.time.monotonic", return_value=100):
            self.assertTrue(messenger.is_alive)
            self.assertFalse(messenger.is_connected)
            self.assertFalse(messenger.connection_stalled)
        with patch("octopus.feishu.time.monotonic", return_value=221):
            self.assertTrue(messenger.connection_stalled)
        messenger._closed = True
        self.assertFalse(messenger.connection_stalled)

    def test_connected_requires_fresh_heartbeat_and_recovers_on_new_heartbeat(self):
        messenger = self.messenger()
        with patch("octopus.feishu.time.monotonic", return_value=101):
            messenger._record_status("connected")
            self.assertTrue(messenger.is_connected)
            self.assertFalse(messenger.connection_stalled)
        with patch("octopus.feishu.time.monotonic", return_value=132):
            self.assertFalse(messenger.is_connected)
            self.assertTrue(messenger.connection_stalled)
            messenger._record_connection(True)
            self.assertTrue(messenger.is_connected)
            self.assertFalse(messenger.connection_stalled)

    def test_repeated_reconnect_status_does_not_extend_disconnect_deadline(self):
        messenger = self.messenger()
        with patch("octopus.feishu.time.monotonic", return_value=101):
            messenger._record_status("connected")
        with patch("octopus.feishu.time.monotonic", return_value=110):
            messenger._record_status("reconnecting")
            self.assertFalse(messenger.is_connected)
            self.assertFalse(messenger.connection_stalled)
        with patch("octopus.feishu.time.monotonic", return_value=200):
            messenger._record_status("reconnecting")
        with patch("octopus.feishu.time.monotonic", return_value=231):
            self.assertTrue(messenger.connection_stalled)
            messenger._record_status("connected")
            self.assertFalse(messenger.connection_stalled)

    def test_failed_or_dead_listener_stalls_immediately(self):
        messenger = self.messenger()
        with patch("octopus.feishu.time.monotonic", return_value=101):
            messenger._record_status("failed")
            self.assertTrue(messenger.connection_stalled)
        other = self.messenger()
        other._reader.done.return_value = True
        self.assertFalse(other.is_alive)
        self.assertTrue(other.connection_stalled)

    def test_bootstrap_post_default_and_none_timeouts_are_bounded(self):
        post = Mock(return_value="response")
        bounded = _post_with_timeout(post)
        self.assertEqual(bounded("https://example.invalid", json={"AppID": "cli_test"}), "response")
        self.assertEqual(post.call_args.kwargs["timeout"], (10, 20))
        bounded("https://example.invalid", timeout=None)
        self.assertEqual(post.call_args.kwargs["timeout"], (10, 20))
        bounded("https://example.invalid", timeout=(1, 2))
        self.assertEqual(post.call_args.kwargs["timeout"], (1, 2))


if __name__ == "__main__":
    unittest.main()
