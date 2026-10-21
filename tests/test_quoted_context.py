from __future__ import annotations

import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

from octopus.contracts import IncomingMessage, ReferenceContextError
from octopus.feishu import FeishuMessenger, parse_message


UNAVAILABLE = "暂时无法读取你引用的消息内容。"
ALERT_CARD = {
    "title": "等级看图重拍—推送 IPQC 重拍指令异常",
    "elements": [
        [{"tag": "text", "text": "ENV: "}, {"tag": "text", "text": "pro"}],
        [{"tag": "text", "text": "错误详情: "},
         {"tag": "text", "text": "java.lang.IllegalStateException: missing task\n"
                                 "at example.RetakeService.push(RetakeService.java:42)"}],
        [{"tag": "a", "text": "任务详情", "href": "https://example.invalid/task"}],
    ],
}


def trigger(**changes):
    return replace(IncomingMessage("om_question", "oc_private", "ou_person", "分析这个错误的原因", "p2p", 2000,
                                   parent_id="om_card", root_id="om_root"), **changes)


def quoted(kind="interactive", content=None, **changes):
    values = {"message_id": "om_card", "chat_id": "oc_private", "create_time": "1000",
              "update_time": "1000", "deleted": False, "msg_type": kind,
              "sender": SimpleNamespace(sender_type="app"),
              "body": SimpleNamespace(content=json.dumps(ALERT_CARD if content is None else content))}
    values.update(changes)
    return SimpleNamespace(**values)


class QuotedContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.logs = []
        self.api = Mock()
        self.api.get_message.return_value = SimpleNamespace(code=0, data=SimpleNamespace(items=[quoted()]))
        self.messenger = FeishuMessenger("cli_test", "secret", self.logs.append, api=self.api)

    async def asyncTearDown(self):
        await self.messenger.close()

    def response(self, item):
        self.api.get_message.return_value = SimpleNamespace(code=0, data=SimpleNamespace(items=[item]))

    async def assert_unavailable(self, message=None):
        with self.assertRaises(ReferenceContextError) as error:
            await self.messenger.fetch_quoted_context(message or trigger())
        self.assertEqual(str(error.exception), UNAVAILABLE)

    async def test_forwarded_alert_preserves_title_and_complete_exception_using_direct_parent(self):
        context = await self.messenger.fetch_quoted_context(trigger())
        self.assertEqual((context.message_id, context.chat_id, context.created_at),
                         ("om_card", "oc_private", 1000))
        self.assertFalse(context.has_unread_images)
        self.assertEqual(context.text, "等级看图重拍—推送 IPQC 重拍指令异常\nENV: pro\n错误详情: "
                                      "java.lang.IllegalStateException: missing task\n"
                                      "at example.RetakeService.push(RetakeService.java:42)\n任务详情")
        self.api.get_message.assert_called_once_with("om_card")
        self.api.list_thread.assert_not_called()
        self.api.download_image.assert_not_called()

    async def test_root_is_fallback_only_when_parent_is_absent_and_never_recurses(self):
        self.response(quoted(message_id="om_root", parent_id="om_ancestor", root_id="om_ancestor"))
        context = await self.messenger.fetch_quoted_context(trigger(parent_id=""))
        self.assertEqual(context.message_id, "om_root")
        self.api.get_message.assert_called_once_with("om_root")

    async def test_no_reference_and_group_inputs_do_not_fetch(self):
        for message in (trigger(parent_id="", root_id=""), trigger(chat_type="group")):
            self.assertIsNone(await self.messenger.fetch_quoted_context(message))
        self.api.get_message.assert_not_called()

    async def test_invalid_reference_and_current_message_are_rejected_before_api(self):
        for changes in ({"parent_id": "om_../private"}, {"parent_id": "om_question"},
                        {"id": "om_../private"}, {"parent_id": "", "root_id": "bad"},
                        {"created_at": 0}, {"created_at": True}):
            with self.subTest(changes=changes):
                await self.assert_unavailable(trigger(**changes))
        self.api.get_message.assert_not_called()

    async def test_reply_is_bound_to_exact_chat_id_message_id_and_valid_prior_time(self):
        for changes in ({"chat_id": "oc_another"}, {"message_id": "om_another"}, {"deleted": True},
                        {"create_time": 2001}, {"create_time": 0}, {"create_time": "bad"},
                        {"create_time": True}, {"update_time": 2001}, {"update_time": "bad"},
                        {"update_time": -1}, {"update_time": 999},
                        {"sender": SimpleNamespace(sender_type="unknown")}):
            with self.subTest(changes=changes):
                self.response(quoted(**changes))
                await self.assert_unavailable()

    async def test_unedited_user_text_and_localized_post_are_preserved(self):
        self.response(quoted("text", {"text": "current error detail"}, update_time=None,
                             sender=SimpleNamespace(sender_type="user")))
        self.assertEqual((await self.messenger.fetch_quoted_context(trigger())).text, "current error detail")
        self.response(quoted("post", {"zh_cn": {"title": "报错", "content": [
            [{"tag": "text", "text": "错误 "}, {"tag": "a", "text": "日志", "href": "https://example.invalid"}],
            [{"tag": "code_block", "text": "select state from orders;"}],
        ]}}))
        self.assertEqual((await self.messenger.fetch_quoted_context(trigger())).text,
                         "报错\n错误 日志\n```\nselect state from orders;\n```")

    async def test_card_header_body_markdown_and_fields_include_only_visible_content(self):
        self.response(quoted(content={"schema": "2.0", "config": {"update_multi": True},
            "header": {"title": {"tag": "plain_text", "content": "告警"}},
            "body": {"elements": [
                {"tag": "markdown", "content": "**状态异常**", "value": {"secret": "hidden-value"}},
                {"tag": "div", "fields": [{"is_short": False,
                    "text": {"tag": "lark_md", "content": "服务: example"}}]},
                {"tag": "action", "actions": [{"tag": "button", "text": {"tag": "plain_text", "content": "审批"},
                    "value": {"text": "hidden-action"}, "url": "http://localhost/private"}]},
            ]}, "callback": {"text": "hidden-callback"}, "value": {"text": "hidden-root"}}))
        text = (await self.messenger.fetch_quoted_context(trigger())).text
        self.assertEqual(text, "告警\n**状态异常**\n服务: example")
        self.assertNotIn("hidden", text)
        self.api.download_image.assert_not_called()

    async def test_unknown_empty_and_malformed_content_is_not_silently_dropped(self):
        for kind, content in (
                ("interactive", {}), ("interactive", {"title": "only title", "elements": "bad"}),
                ("interactive", {"title": "title", "elements": [{"tag": "unknown", "text": "evidence"}]}),
                ("interactive", {"title": "title", "elements": [[42]]}),
                ("interactive", {"title": "title", "elements": [{"tag": "text", "text": []}]}),
                ("interactive", {"template_id": "template", "data": {"text": "hidden"}}),
                ("text", {"text": None}), ("text", {"text": "  "}), ("text", {"text": "bad\x00text"}),
                ("post", {"content": [[{"tag": "file", "file_key": "file_abc"}]]}),
                ("post", {"content": [[{"tag": "text", "text": []}]]}),
                ("post", {"unknown": {"content": [[{"tag": "text", "text": "evidence"}]]}}),
                ("image", {"image_key": "img_abc"}), ("file", {"file_key": "file_abc"}),
                ("interactive", {"elements": [{"tag": "img", "img_key": "img_abc"}]}),
                ("post", {"content": [[{"tag": "img", "image_key": "img_abc"}]]}),
                ("interactive", {"elements": [{"tag": "markdown", "content": "![only image](img_abc)"}]})):
            with self.subTest(kind=kind, content=content):
                self.response(quoted(kind, content))
                await self.assert_unavailable()

    async def test_mixed_text_and_image_is_marked_as_unread_and_never_downloaded(self):
        for kind, content in (
                ("post", {"content": [[{"tag": "text", "text": "错误截图"},
                                        {"tag": "img", "image_key": "img_abc"}]]}),
                ("interactive", {"elements": [{"tag": "markdown", "content": "错误截图"},
                                               {"tag": "img", "img_key": "img_abc"}]}),
                ("interactive", {"elements": [{"tag": "markdown", "content": "错误截图 ![图](img_abc)"}]})):
            self.response(quoted(kind, content))
            context = await self.messenger.fetch_quoted_context(trigger())
            self.assertIn("错误截图", context.text)
            self.assertIn("引用图片未读取", context.text)
            self.assertTrue(context.has_unread_images)
        self.api.download_image.assert_not_called()

    async def test_div_extra_images_are_unread_but_button_values_are_not_body_text(self):
        for extra, unread in (({"tag": "img", "img_key": "img_extra"}, True),
                              ({"tag": "button", "text": {"tag": "plain_text", "content": "执行"},
                                "value": {"text": "hidden-action"}}, False)):
            self.response(quoted(content={"elements": [
                {"tag": "div", "text": {"tag": "plain_text", "content": "错误详情"}, "extra": extra}]}))
            context = await self.messenger.fetch_quoted_context(trigger())
            self.assertEqual(context.has_unread_images, unread)
            self.assertEqual(context.text, "错误详情" + ("\n[引用图片未读取]" if unread else ""))
        self.api.download_image.assert_not_called()

    async def test_header_custom_icon_is_unread_and_standard_icon_is_decorative(self):
        for icon, unread in (({"tag": "custom_icon", "img_key": "img_icon"}, True),
                             ({"img_key": "img_legacy_icon"}, True),
                             ({"tag": "standard_icon", "token": "warning_outlined"}, False)):
            self.response(quoted(content={"header": {"title": {"tag": "plain_text", "content": "告警"},
                                                      "icon": icon},
                                          "elements": [{"tag": "markdown", "content": "错误详情"}]}))
            context = await self.messenger.fetch_quoted_context(trigger())
            self.assertEqual(context.has_unread_images, unread)
            self.assertEqual("引用图片未读取" in context.text, unread)
        self.api.download_image.assert_not_called()

    async def test_unknown_visible_extra_or_header_icon_fails_instead_of_losing_content(self):
        for content in (
                {"elements": [{"tag": "div", "text": {"tag": "plain_text", "content": "详情"},
                               "extra": {"tag": "unknown", "content": "重要证据"}}]},
                {"header": {"title": {"tag": "plain_text", "content": "告警"},
                            "icon": {"tag": "unknown", "content": "重要证据"}}, "elements": []}):
            self.response(quoted(content=content))
            await self.assert_unavailable()

    async def test_oversized_body_or_output_is_rejected_instead_of_truncated(self):
        for content in ({"text": "a" * 12_001}, {"text": "small", "value": "a" * 200_001}):
            self.response(quoted("text", content))
            await self.assert_unavailable()
        self.response(quoted("text", {"text": "a" * 12_000}))
        self.assertEqual(len((await self.messenger.fetch_quoted_context(trigger())).text), 12_000)

    async def test_columns_keep_order_and_excessive_nesting_is_rejected(self):
        node = {"tag": "column_set", "columns": [
            {"tag": "column", "elements": [{"tag": "markdown", "content": "first"}]},
            {"tag": "column", "elements": [{"tag": "markdown", "content": "second"}]},
        ]}
        self.response(quoted(content={"elements": [node]}))
        self.assertEqual((await self.messenger.fetch_quoted_context(trigger())).text, "first\nsecond")
        nested = {"tag": "text", "text": "deep"}
        for _ in range(20):
            nested = [nested]
        self.response(quoted(content={"elements": nested}))
        await self.assert_unavailable()

    async def test_permission_missing_invalid_response_and_network_errors_are_sanitized(self):
        for response in (SimpleNamespace(code=99991672, msg="secret-token private-content"),
                         SimpleNamespace(code=0, data=SimpleNamespace(items=[])),
                         SimpleNamespace(code=0, data=SimpleNamespace(items=[quoted(), quoted()])),
                         SimpleNamespace(code=0, data=SimpleNamespace(items=None))):
            self.api.get_message.return_value = response
            await self.assert_unavailable()
        for body in ("{", "[]", "null", 123):
            self.response(quoted(body=SimpleNamespace(content=body)))
            await self.assert_unavailable()
        self.api.get_message.side_effect = RuntimeError("secret-token private-content")
        await self.assert_unavailable()
        self.assertEqual(self.logs, [])

    def test_unquoted_forwarded_card_still_does_not_trigger_a_model_task(self):
        event = {"event": {"sender": {"sender_type": "user", "sender_id": {"open_id": "ou_person"}},
                           "message": {"message_id": "om_forward", "chat_id": "oc_private", "chat_type": "p2p",
                                       "message_type": "interactive", "create_time": "1000",
                                       "content": json.dumps(ALERT_CARD)}}}
        self.assertIsNone(parse_message(event))
