from __future__ import annotations

import asyncio
import json
import unittest
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from octopus.contracts import Button, CardAction, IncomingMessage, View
from octopus.feishu import (CARD_BUDGET, FeishuMessenger, _heartbeat, _post_with_timeout, _roundtrip,
                            build_card, parse_action, parse_message)


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
        for changes in ({"message_type": "image"}, {"content": "not-json"},
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
