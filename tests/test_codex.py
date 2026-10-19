from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import AsyncMock

from octopus.codex import ActiveTurnError, AppServerClient, readonly_thread_config
from octopus.contracts import CodexEvent

FIXTURE = Path(__file__).parent / "fixtures" / "fake_codex.py"


class ReadonlyAppServerTests(unittest.IsolatedAsyncioTestCase):
    cwd = "/test/project/.data/codex-readonly/workspace"

    def config(self):
        policy = json.loads((Path(__file__).parents[1] / "config" / "codex-readonly.json").read_text())
        config = tomllib.loads("\n".join(key + "=" + json.dumps(value) for key, value in policy.items()))
        config["permissions"]["octopus-readonly"]["filesystem"][self.cwd] = "read"
        config["permissions"]["octopus-readonly"]["filesystem"]["glob_scan_max_depth"] = None
        return config

    def client(self, handler=None):
        client = AppServerClient(read_only=True, tool_specs=[{
            "type": "function", "name": "code_search", "description": "Search allowed source code",
            "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
        }], tool_handler=handler)
        client._transport = object()
        client._ready = True
        client._read_only_home = self.cwd.removesuffix("workspace") + "home"
        client._write = AsyncMock()
        client._request = AsyncMock(return_value={"config": self.config()})
        return client

    def thread_response(self):
        return {"thread": {"id": "thread-safe", "status": {"type": "idle"}},
                "approvalPolicy": "never", "cwd": self.cwd, "runtimeWorkspaceRoots": [],
                "instructionSources": [], "activePermissionProfile": {"id": "octopus-readonly"},
                "sandbox": {"type": "readOnly", "networkAccess": False}}

    async def test_readonly_host_attestation_and_every_thread_turn_overrides(self):
        client = self.client()
        await client._attest_read_only()
        client._request.return_value = self.thread_response()
        self.assertEqual(await client.create_thread(self.cwd), "thread-safe")
        params = client._request.call_args.args[1]
        self.assertEqual(params["environments"], [])
        self.assertEqual(params["dynamicTools"], client.tool_specs)
        self.assertNotIn("sandbox", params)
        self.assertEqual(params["approvalPolicy"], "never")
        self.assertEqual(params["permissions"], "octopus-readonly")
        self.assertEqual(params["config"], readonly_thread_config())
        await client.resume_thread("thread-safe", self.cwd)
        resume = client._request.call_args.args[1]
        self.assertNotIn("sandbox", resume)
        self.assertEqual(resume["config"], readonly_thread_config())
        with self.assertRaises(ValueError):
            await client.create_thread("/personal/source")
        client.active_turn = AsyncMock(return_value=None)
        client._request.return_value = {"turn": {"id": "turn-safe"}}
        await client.start_turn("thread-safe", "query")
        turn = client._request.call_args.args[1]
        self.assertEqual(turn["environments"], [])
        self.assertEqual(turn["permissions"], "octopus-readonly")

    async def test_drifted_host_fails_closed_before_any_thread_starts(self):
        mutations = [
            lambda c: c["features"].update(shell_tool=True),
            lambda c: c["features"].update(code_mode_host=False),
            lambda c: c["features"].pop("stable_environment_tools"),
            lambda c: c.update(approval_policy="on-request"),
            lambda c: c.update(mcp_servers={"unexpected": {"command": "private"}}),
            lambda c: c.update(plugins={"unexpected": {"enabled": True}}),
            lambda c: c["permissions"]["octopus-readonly"]["filesystem"].update({"/personal": "read"}),
            lambda c: c["permissions"]["octopus-readonly"]["filesystem"].update({":root": "read"}),
            lambda c: c["permissions"]["octopus-readonly"]["network"].update(enabled=True),
            lambda c: c["permissions"]["octopus-readonly"].update(extends=":workspace"),
        ]
        for mutation in mutations:
            client = self.client()
            config = self.config()
            mutation(config)
            client._request.return_value = {"config": config}
            with self.assertRaises(RuntimeError):
                await client._attest_read_only()
            self.assertIsNone(client._read_only_cwd)

    async def test_thread_response_must_confirm_actual_restrictions(self):
        client = self.client()
        await client._attest_read_only()
        for changes in ({"approvalPolicy": "on-request"}, {"instructionSources": ["/private/AGENTS.md"]},
                        {"sandbox": {"type": "readOnly", "networkAccess": True}},
                        {"activePermissionProfile": {"id": ":read-only"}}, {"runtimeWorkspaceRoots": ["/"]}):
            client._request.return_value = {**self.thread_response(), **changes}
            with self.assertRaises(RuntimeError):
                await client.create_thread(self.cwd)
            with self.assertRaises(RuntimeError):
                await client.resume_thread("thread-safe", self.cwd)

    async def test_dynamic_tool_does_not_block_protocol_reader(self):
        gate = asyncio.Event()
        async def handler(thread, turn, name, args):
            await gate.wait()
            return {"matches": ["safe evidence"]}
        client = self.client(AsyncMock(side_effect=handler))
        params = {"threadId": "t", "turnId": "u", "namespace": None,
                  "tool": "code_search", "arguments": {"query": "business"}}
        await asyncio.wait_for(client._server_request(1, "item/tool/call", params), .1)
        await client._server_request(2, "item/permissions/requestApproval", {})
        self.assertEqual(client._write.call_args.args[0]["result"], {"permissions": {}, "scope": "turn"})
        await client._server_request(1, "item/tool/call", params)
        gate.set()
        await asyncio.gather(*list(client._tool_tasks.values()))
        client.tool_handler.assert_awaited_once_with("t", "u", "code_search", {"query": "business"})
        result = client._write.call_args.args[0]["result"]
        self.assertTrue(result["success"])
        self.assertEqual(result["contentItems"][0]["type"], "inputText")
        self.assertIn("safe evidence", result["contentItems"][0]["text"])

    async def test_unknown_tools_malformed_arguments_and_errors_are_sanitized(self):
        handler = AsyncMock(side_effect=ValueError("SECRET_TOKEN_DO_NOT_ECHO"))
        client = self.client(handler)
        valid = {"threadId": "t", "turnId": "u", "tool": "code_search", "arguments": {}}
        for change in ({"tool": "exec_command"}, {"arguments": []}, {"namespace": {}}, {"threadId": ""}):
            await client._dynamic_tool(1, {**valid, **change})
            self.assertFalse(client._write.call_args.args[0]["result"]["success"])
        handler.assert_not_called()
        await client._dynamic_tool(1, valid)
        self.assertNotIn("SECRET_TOKEN", json.dumps(client._write.call_args.args[0]))
        self.assertFalse(client._write.call_args.args[0]["result"]["success"])
        handler.side_effect = None
        handler.return_value = {"content": "x" * 128_001}
        await client._dynamic_tool(2, valid)
        self.assertFalse(client._write.call_args.args[0]["result"]["success"])

    async def test_readonly_denies_command_file_and_permission_approvals_without_cards(self):
        client = self.client()
        events = []
        client.on_event(events.append)
        for method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            await client._server_request(1, method, {"threadId": "t", "turnId": "u", "command": "private command"})
            self.assertEqual(client._write.call_args.args[0]["result"], {"decision": "decline"})
        self.assertEqual(events, [])
        with self.assertRaises(ValueError):
            await client.respond_approval(1, "accept")

    async def test_dynamic_tool_roundtrip_over_authenticated_websocket(self):
        from websockets.asyncio.server import serve
        completed = asyncio.Event()
        replies = []
        async def peer(socket):
            self.assertEqual(socket.request.headers.get("Authorization"), "Bearer synthetic-token")
            async for payload in socket:
                message = json.loads(payload)
                method = message.get("method")
                request_id = message.get("id")
                result = None
                if method == "initialize":
                    result = {"userAgent": "test/0.163.0", "codexHome": self.cwd.removesuffix("workspace") + "home"}
                elif method == "config/read":
                    result = {"config": self.config()}
                elif method in {"thread/start", "thread/resume"}:
                    result = self.thread_response()
                elif method == "thread/read":
                    result = {"thread": {"status": {"type": "idle"}}}
                elif method == "turn/start":
                    await socket.send(json.dumps({"id": request_id, "result": {"turn": {"id": "turn-safe"}}}))
                    await socket.send(json.dumps({"id": "query-rpc", "method": "item/tool/call", "params": {
                        "threadId": "thread-safe", "turnId": "turn-safe", "callId": "call-1",
                        "namespace": None, "tool": "code_search", "arguments": {"query": "test"}}}))
                elif request_id == "query-rpc":
                    replies.append(message)
                    completed.set()
                if result is not None:
                    await socket.send(json.dumps({"id": request_id, "result": result}))
        server = await serve(peer, "127.0.0.1", 0)
        client = None
        try:
            with tempfile.TemporaryDirectory() as directory:
                token = Path(directory) / "token"
                token.write_text("synthetic-token")
                handler = AsyncMock(return_value={"ok": True, "matches": []})
                client = AppServerClient(url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}",
                    token_file=str(token), read_only=True, tool_specs=self.client().tool_specs, tool_handler=handler)
                await client.start()
                thread = await client.create_thread(self.cwd)
                await client.start_turn(thread, "query")
                await asyncio.wait_for(completed.wait(), 2)
                handler.assert_awaited_once_with("thread-safe", "turn-safe", "code_search", {"query": "test"})
                self.assertEqual(replies[0]["result"], {"contentItems": [{"type": "inputText", "text": '{"ok": true, "matches": []}'}], "success": True})
        finally:
            if client:
                await client.close()
            server.close()
            await server.wait_closed()


class AppServerClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.record = Path(self.directory.name) / "rpc.jsonl"
        self.events: asyncio.Queue[CodexEvent] = asyncio.Queue()
        self.history: list[CodexEvent] = []

    async def client(self, scenario="normal", timeout=2):
        client = AppServerClient(sys.executable, model="test-model", request_timeout=timeout,
            extra_args=[str(FIXTURE), "--record", str(self.record), "--scenario", scenario])
        client.on_event(self.events.put_nowait)
        client.on_event(self.history.append)
        self.addAsyncCleanup(client.close)
        await client.start()
        return client

    async def event(self, kind):
        async with asyncio.timeout(3):
            while True:
                event = await self.events.get()
                if event.type == kind:
                    return event

    def calls(self):
        return [json.loads(line) for line in self.record.read_text().splitlines()]

    async def test_handshake_safe_threads_stream_and_canonical_completion(self):
        client = await self.client()
        await asyncio.gather(client.start(), client.start())
        self.assertEqual(await client.create_thread("/tmp/project"), "thread-1")
        await client.resume_thread("thread-1", "/tmp/project")
        self.assertEqual(await client.start_turn("thread-1", "你好"), "turn-1")
        result = await self.event("completed")
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.text, "最终答案")
        self.assertTrue(any(event.delta == "partial" for event in self.history))
        self.assertTrue(any("echo ok" in event.text for event in self.history))
        calls = self.calls()
        self.assertEqual([call["method"] for call in calls[:2]], ["initialize", "initialized"])
        self.assertEqual(sum(call.get("method") == "initialize" for call in calls), 1)
        self.assertTrue(calls[0]["params"]["capabilities"]["experimentalApi"])
        for call in calls:
            if call.get("method") in {"thread/start", "thread/resume"}:
                self.assertEqual(call["params"]["approvalPolicy"], "on-request")
                self.assertEqual(call["params"]["approvalsReviewer"], "user")
                self.assertEqual(call["params"]["sandbox"], "workspace-write")
                self.assertEqual(call["params"]["model"], "test-model")

    async def test_approval_uses_original_rpc_id_and_is_one_shot(self):
        client = await self.client("approval")
        await client.start_turn("thread-1", "approve")
        prompt = await self.event("approval")
        self.assertEqual(prompt.request_id, 7)
        self.assertIn("echo safe", prompt.detail)
        await client.respond_approval(7, "accept")
        with self.assertRaises(ValueError):
            await client.respond_approval(7, "accept")
        await self.event("completed")
        replies = [call for call in self.calls() if "result" in call]
        self.assertEqual(replies, [{"id": 7, "result": {"decision": "accept"}}])

    async def test_server_resolved_and_restricted_decisions_cannot_be_accepted(self):
        client = await self.client("resolved")
        await client.start_turn("thread-1", "resolved")
        await self.event("request_resolved")
        with self.assertRaises(ValueError):
            await client.respond_approval(7, "accept")
        await client.close()
        restricted = await self.client("restricted")
        await restricted.start_turn("thread-1", "restricted")
        await self.event("approval")
        with self.assertRaises(ValueError):
            await restricted.respond_approval(7, "accept")
        await restricted.respond_approval(7, "decline")
        await self.event("completed")

    async def test_question_maps_answers_to_protocol_and_rejects_unknown_keys(self):
        client = await self.client("question")
        await client.start_turn("thread-1", "question")
        prompt = await self.event("question")
        self.assertTrue(prompt.questions[0].is_other)
        self.assertEqual(prompt.questions[0].options[0]["label"], "全部")
        with self.assertRaises(ValueError):
            await client.respond_question("question-rpc", {"wrong": ["x"]})
        await client.respond_question("question-rpc", {"scope": ["全部"]})
        await self.event("completed")
        reply = next(call for call in self.calls() if call.get("id") == "question-rpc")
        self.assertEqual(reply["result"], {"answers": {"scope": {"answers": ["全部"]}}})

    async def test_unknown_requests_and_permissions_fail_closed(self):
        client = await self.client("unsupported")
        await client.start_turn("thread-1", "unsupported")
        await self.event("completed")
        replies = {call.get("id"): call for call in self.calls() if "result" in call or "error" in call}
        self.assertEqual(replies["unknown"]["error"]["code"], -32601)
        self.assertEqual(replies["permissions"]["result"], {"permissions": {}, "scope": "turn"})

    async def test_interrupt_waits_for_explicit_completion_event(self):
        client = await self.client("pending")
        await client.start_turn("thread-1", "hold")
        await client.interrupt("thread-1", "turn-1")
        event = await self.event("completed")
        self.assertEqual(event.status, "interrupted")

    async def test_timeout_is_not_automatically_retried_and_close_is_idempotent(self):
        client = await self.client("timeout", timeout=0.2)
        with self.assertRaisesRegex(TimeoutError, "执行状态未知"):
            await client.start_turn("thread-1", "timeout")
        self.assertEqual(sum(call.get("method") == "turn/start" for call in self.calls()), 1)
        self.assertFalse(client._pending)
        process = client._process
        await asyncio.gather(client.close(), client.close())
        self.assertIsNotNone(process.returncode)
        with self.assertRaises(RuntimeError):
            await client.start()

    async def test_disconnect_rejects_inflight_request_and_invalidates_prompts(self):
        client = await self.client("disconnect")
        with self.assertRaises(ConnectionError):
            await client.start_turn("thread-1", "exit")
        await self.event("disconnect")
        self.assertFalse(client._pending)
        other = await self.client("approval-disconnect")
        await other.start_turn("thread-1", "exit with prompt")
        await self.event("disconnect")
        with self.assertRaises(ValueError):
            await other.respond_approval(7, "accept")

    async def test_rpc_error_and_malformed_protocol_are_reported(self):
        client = await self.client("rpc-error")
        with self.assertRaisesRegex(RuntimeError, "invalid cwd"):
            await client.create_thread("invalid")
        invalid = await self.client("invalid-json")
        with self.assertRaises(ConnectionError):
            await invalid.start_turn("thread-1", "invalid-json")
        event = await self.event("disconnect")
        self.assertIn("协议", event.error)

    async def test_initialize_timeout_cleans_up_process(self):
        client = AppServerClient(sys.executable, request_timeout=0.2,
            extra_args=[str(FIXTURE), "--record", str(self.record), "--scenario", "init-timeout"])
        self.addAsyncCleanup(client.close)
        with self.assertRaises(TimeoutError):
            await client.start()
        self.assertIsNone(client._process)
        self.assertFalse(client._pending)

    async def test_active_recovered_turn_is_never_steered_and_can_be_explicitly_stopped(self):
        client = await self.client("active")
        with self.assertRaises(ActiveTurnError) as resumed:
            await client.resume_thread("thread-1", "/tmp/project")
        self.assertEqual(resumed.exception.turn_id, "old-turn")
        self.assertEqual(await client.active_turn("thread-1"), "old-turn")
        with self.assertRaises(ActiveTurnError):
            await client.start_turn("thread-1", "must not steer")
        self.assertFalse(any(call.get("method") == "turn/start" for call in self.calls()))
        self.assertFalse(any(call.get("method") == "turn/interrupt" for call in self.calls()))
        await client.interrupt("thread-1", "old-turn")
        self.assertIsNone(await client.active_turn("thread-1"))
        self.assertEqual(await client.start_turn("thread-1", "after explicit stop"), "turn-1")

    async def test_active_status_without_turn_id_fails_closed(self):
        client = await self.client("unknown-active")
        with self.assertRaises(ActiveTurnError) as failure:
            await client.active_turn("thread-1")
        self.assertIsNone(failure.exception.turn_id)
        with self.assertRaises(ActiveTurnError):
            await client.start_turn("thread-1", "must not submit")
        self.assertFalse(any(call.get("method") == "turn/start" for call in self.calls()))


class WebSocketAppServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from websockets.asyncio.server import serve

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.token = Path(self.directory.name) / "token"
        self.token.write_text("synthetic-test-token\n")
        self.calls = []
        self.headers = []
        self.mode = "interactive"
        self.server = await serve(self.peer, "127.0.0.1", 0)
        self.addAsyncCleanup(self.stop_server)
        self.url = f"ws://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"
        self.events: asyncio.Queue[CodexEvent] = asyncio.Queue()

    async def stop_server(self):
        self.server.close()
        await self.server.wait_closed()

    async def peer(self, websocket):
        self.headers.append(websocket.request.headers.get("Authorization"))
        async for payload in websocket:
            message = json.loads(payload)
            self.calls.append(message)
            method = message.get("method")
            request_id = message.get("id")
            if method == "initialize":
                await websocket.send(json.dumps({"id": request_id, "result": {"userAgent": "fake-ws"}}))
            elif method in {"thread/start", "thread/resume"}:
                await websocket.send(json.dumps({"id": request_id, "result": {"thread": {"id": "ws-thread", "status": {"type": "idle"}}}}))
            elif method == "thread/read":
                await websocket.send(json.dumps({"id": request_id, "result": {"thread": {"id": "ws-thread", "status": {"type": "idle"}}}}))
            elif method == "turn/start":
                if self.mode == "disconnect":
                    await websocket.close()
                    return
                await websocket.send(json.dumps({"id": request_id, "result": {"turn": {"id": "ws-turn"}}}))
                await websocket.send(json.dumps({"id": "ws-approval", "method": "item/fileChange/requestApproval", "params": {
                    "threadId": "ws-thread", "turnId": "ws-turn", "itemId": "file-item", "reason": "edit file",
                }}))
            elif request_id == "ws-approval" and "result" in message:
                await websocket.send(json.dumps({"id": 9, "method": "item/tool/requestUserInput", "params": {
                    "threadId": "ws-thread", "turnId": "ws-turn", "itemId": "q-item",
                    "questions": [{"id": "target", "header": "目标", "question": "输入目标", "options": None}],
                }}))
            elif request_id == 9 and "result" in message:
                await websocket.send(json.dumps({"method": "item/agentMessage/delta", "params": {
                    "threadId": "ws-thread", "turnId": "ws-turn", "itemId": "reply", "delta": "结果",
                }}))
                await websocket.send(json.dumps({"method": "turn/completed", "params": {
                    "threadId": "ws-thread", "turn": {"id": "ws-turn", "status": "completed", "items": [
                        {"type": "agentMessage", "id": "reply", "text": "最终结果", "phase": "final_answer"},
                    ], "error": None},
                }}))

    async def client(self):
        # Deliberately invalid executable proves WebSocket mode never launches a subprocess.
        client = AppServerClient(executable="/does/not/exist", url=self.url,
                                 token_file=str(self.token), request_timeout=2)
        client.on_event(self.events.put_nowait)
        self.addAsyncCleanup(client.close)
        await client.start()
        return client

    async def event(self, kind):
        async with asyncio.timeout(3):
            while True:
                event = await self.events.get()
                if event.type == kind:
                    return event

    async def test_bearer_file_auth_shared_protocol_and_close_leaves_server_running(self):
        client = await self.client()
        self.assertEqual(await client.create_thread("/tmp/project"), "ws-thread")
        self.assertEqual(await client.start_turn("ws-thread", "hi"), "ws-turn")
        self.assertEqual((await self.event("approval")).request_id, "ws-approval")
        await client.respond_approval("ws-approval", "accept")
        self.assertEqual((await self.event("question")).request_id, 9)
        await client.respond_question(9, {"target": ["docs"]})
        self.assertEqual((await self.event("completed")).text, "最终结果")
        self.assertEqual(self.headers, ["Bearer synthetic-test-token"])
        self.assertNotIn("synthetic-test-token", json.dumps(self.calls))
        self.assertIsNone(client._process)
        await client.close()
        second = await self.client()
        await second.resume_thread("ws-thread", "/tmp/project")
        self.assertEqual(len(self.headers), 2)

    async def test_disconnect_rejects_pending_and_reconnect_does_not_replay_turn(self):
        self.mode = "disconnect"
        client = await self.client()
        with self.assertRaises(ConnectionError):
            await client.start_turn("ws-thread", "once")
        await self.event("disconnect")
        await client.start()
        await client.resume_thread("ws-thread", "/tmp/project")
        self.assertEqual(sum(call.get("method") == "initialize" for call in self.calls), 2)
        self.assertEqual(sum(call.get("method") == "turn/start" for call in self.calls), 1)

    async def test_token_required_and_invalid_tokens_fail_before_connect(self):
        with self.assertRaises(ValueError):
            AppServerClient(url=self.url)
        with self.assertRaises(ValueError):
            AppServerClient(url="http://localhost:1234", token_file=str(self.token))
        self.token.write_text("first\nsecond")
        client = AppServerClient(url=self.url, token_file=str(self.token))
        self.addAsyncCleanup(client.close)
        with self.assertRaisesRegex(ValueError, "单行"):
            await client.start()
        self.assertEqual(self.headers, [])


if __name__ == "__main__":
    unittest.main()
