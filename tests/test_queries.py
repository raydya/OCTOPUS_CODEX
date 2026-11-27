from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from octopus.queries import NoRedirect, QueryClient, TOOL_SPECS


class QueryClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        token = Path(directory.name) / "token"
        token.write_text("x" * 40)
        self.client = QueryClient("http://127.0.0.1:4501", str(token))

    def http_failure(self, status, payload):
        stream = io.BytesIO(payload)
        opener = Mock()
        opener.open.side_effect = HTTPError(self.client.url + "/v1/query", status,
                                            "private-http-message", {}, stream)
        with patch("octopus.queries.build_opener", return_value=opener):
            result = self.client._request("tls_topics", {"query": "synthetic"})
        self.assertTrue(stream.closed)
        self.assertFalse(result["ok"])
        self.assertNotIn("private", json.dumps(result))
        return result

    def test_tls_auth_failure_is_identifiable_without_exposing_provider_message(self):
        payload = json.dumps({"ok": False, "error_code": "tls_auth_required",
                              "error": "private-provider-message"}).encode()
        expected = {"ok": False, "error_code": "tls_auth_required",
                    "error": "TLS 查询身份已失效，需要管理员重新授权；本次未获取日志。"}
        self.assertEqual(self.http_failure(502, payload), expected)
        opener = Mock()
        opener.open.return_value = io.BytesIO(payload)
        with patch("octopus.queries.build_opener", return_value=opener):
            self.assertEqual(self.client._request("tls_topics", {}), expected)

    def test_dms_oauth_grant_error_is_distinct_from_bridge_auth(self):
        error = b'{"ok":false,"error_code":"dms_auth_required","error":"private details"}'
        self.assertEqual(self.http_failure(502, error)["error_code"], "dms_auth_required")
        self.assertNotIn("error_code", self.http_failure(401, error))

    def test_http_statuses_have_safe_distinct_failures(self):
        cases = {
            400: "查询参数无效或不在允许范围内；本次未执行查询。",
            401: "查询桥接认证失败，需管理员检查服务配置；本次未获取查询结果。",
            403: "查询桥接认证失败，需管理员检查服务配置；本次未获取查询结果。",
            408: "查询请求超时；本次未获取查询结果。",
            413: "查询请求超过大小上限；请缩小输入范围后重试。",
            502: "查询提供方执行失败；本次未获取查询结果。",
            500: "查询服务内部错误；本次未获取查询结果。",
            302: "查询服务请求失败；本次未获取查询结果。",
        }
        for status, message in cases.items():
            with self.subTest(status=status):
                result = self.http_failure(status, b'{"ok":false,"error":"private-provider-message"}')
                self.assertEqual(result["error"], message)

    def test_error_code_is_only_trusted_in_the_expected_envelope_and_status(self):
        for value in (
            {"ok": False, "error_code": "unknown", "error": "private-provider-message"},
            {"ok": True, "error_code": "tls_auth_required", "error": "private-provider-message"},
            {"error_code": "tls_auth_required", "error": "private-provider-message"},
            {"ok": False, "error_code": ["tls_auth_required"], "error": "private-provider-message"},
            ["private-provider-message"],
        ):
            with self.subTest(value=value):
                result = self.http_failure(502, json.dumps(value).encode())
                self.assertEqual(result["error"], "查询提供方执行失败；本次未获取查询结果。")
        result = self.http_failure(401, b'{"ok":false,"error_code":"tls_auth_required"}')
        self.assertEqual(result["error"], "查询桥接认证失败，需管理员检查服务配置；本次未获取查询结果。")

    def test_malformed_and_oversize_error_bodies_are_bounded_and_closed(self):
        for payload in (b'private-not-json', b'{"error":"private-truncated', b'\xffprivate'):
            with self.subTest(payload=payload):
                self.assertEqual(self.http_failure(502, payload)["error"],
                                 "查询提供方执行失败；本次未获取查询结果。")
        payload = json.dumps({"ok": False, "error_code": "tls_auth_required",
                              "error": "private-" + "x" * 65536}).encode()
        self.assertEqual(self.http_failure(502, payload)["error"],
                         "查询结果超过大小上限；请缩小查询范围后重试。")

    def test_success_payload_is_unchanged_and_invalid_responses_are_safe(self):
        success = {"ok": True, "data": {"Topics": [{"TopicName": "synthetic"}]}, "truncated": False}
        opener = Mock()
        opener.open.return_value = io.BytesIO(json.dumps(success).encode())
        with patch("octopus.queries.build_opener", return_value=opener):
            self.assertEqual(self.client._request("tls_topics", {}), success)
        for payload in (b'private-not-json', b'[]', b'{"ok":false,"error":"private-message"}'):
            opener.open.return_value = io.BytesIO(payload)
            with self.subTest(payload=payload), patch("octopus.queries.build_opener", return_value=opener):
                result = self.client._request("tls_topics", {})
                self.assertFalse(result["ok"])
                self.assertNotIn("private", json.dumps(result))
        opener.open.return_value = io.BytesIO(b"x" * 65537)
        with patch("octopus.queries.build_opener", return_value=opener):
            self.assertEqual(self.client._request("tls_topics", {})["error"],
                             "查询结果超过大小上限；请缩小查询范围后重试。")

    def test_network_failures_and_redirects_do_not_disclose_remote_details(self):
        opener = Mock()
        opener.open.side_effect = URLError("private-network-message")
        with patch("octopus.queries.build_opener", return_value=opener):
            result = self.client._request("tls_topics", {})
        self.assertEqual(result["error"], "查询服务连接失败或暂时不可用；本次未获取查询结果。")
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.test"))

    async def test_task_context_crosses_authenticated_boundary_outside_model_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "token"
            token.write_text("x" * 40)
            client = QueryClient("http://127.0.0.1:4501", str(token))
            opener = Mock()
            opener.open.return_value = io.BytesIO(b'{"ok": true, "data": {}}')
            context = {"actor": "alice", "job_id": "job1", "thread_id": "thread1", "turn_id": "turn1",
                       "question": "业务问题" * 3500, "policy_version": "readonly:v2"}
            with patch("octopus.queries.build_opener", return_value=opener):
                result = await client.call("code_evidence", {"path": "service/dao.py"}, context=context)
            self.assertTrue(result["ok"])
            request = opener.open.call_args.args[0]
            self.assertEqual(json.loads(request.data), {
                "tool": "code_evidence", "arguments": {"path": "service/dao.py"}, "context": context})
            self.assertLess(len(request.data), 64000)
            self.assertEqual(request.headers["Authorization"], "Bearer " + "x" * 40)

    def test_model_cannot_select_sql_or_connection_or_supply_authorization(self):
        specs = {spec["name"]: spec["inputSchema"] for spec in TOOL_SPECS}
        self.assertFalse({"code_repositories", "code_search", "code_read"} & specs.keys())
        self.assertEqual(set(specs["code_evidence"]["properties"]), {"path", "start_line", "line_count"})
        self.assertFalse(specs["code_evidence"]["additionalProperties"])
        self.assertEqual(set(specs["business_data_check"]["properties"]),
                         {"evidence_id", "check_id", "day", "time_column", "columns", "mode", "filters"})
        self.assertEqual(set(specs["business_data_checks"]["properties"]), {"evidence_id", "table", "database_keyword"})
        self.assertFalse(specs["business_data_check"]["additionalProperties"])
        self.assertFalse(any("auth" in name or "login" in name for name in specs))

    async def test_authorization_control_plane_is_separate_and_sanitized(self):
        payload = {"provider": "tls", "request_id": "request1", "state": "pending", "expires_in": 120,
                   "verification_url": "https://signin.volcengine.com/device?user_code=TEST-CODE",
                   "user_code": "TEST-CODE", "token": "private-token", "error": "private-error"}
        opener = Mock()
        opener.open.return_value = io.BytesIO(json.dumps(payload).encode())
        with patch("octopus.queries.build_opener", return_value=opener):
            result = await self.client.auth_start("tls", "admin", "request1")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, self.client.url + "/v1/auth/start")
        self.assertEqual(json.loads(request.data), {"provider": "tls", "actor": "admin", "request_id": "request1"})
        self.assertEqual(result["verification_url"], payload["verification_url"])
        self.assertNotIn("private", json.dumps(result))

    async def test_auth_response_cannot_redirect_or_leak_terminal_credentials(self):
        for url in ("http://signin.volcengine.com/", "https://signin.volcengine.com.evil.test/",
                    "https://evil@signin.volcengine.com/", "https://signin.volcengine.com:444/",
                    "https://signin.volcengine.com/\nsecret", "https://signin.volcengine.com/#secret"):
            payload = {"provider": "tls", "request_id": "request1", "state": "pending", "verification_url": url}
            opener = Mock()
            opener.open.return_value = io.BytesIO(json.dumps(payload).encode())
            with self.subTest(url=url), patch("octopus.queries.build_opener", return_value=opener):
                result = await self.client.auth_status("tls", "admin", "request1")
                self.assertEqual(result["state"], "failed")
                self.assertNotIn("verification_url", result)
        payload.update(state="succeeded", verification_url="private-url", user_code="private-code")
        opener.open.return_value = io.BytesIO(json.dumps(payload).encode())
        with patch("octopus.queries.build_opener", return_value=opener):
            self.assertEqual(await self.client.auth_status("tls", "admin", "request1"),
                             {"provider": "tls", "request_id": "request1", "state": "succeeded"})

    async def test_auth_identity_and_errors_fail_closed(self):
        opener = Mock()
        opener.open.return_value = io.BytesIO(b'{"provider":"tls","request_id":"another","state":"succeeded"}')
        with patch("octopus.queries.build_opener", return_value=opener):
            self.assertEqual((await self.client.auth_status("tls", "admin", "request1"))["state"], "failed")
            with self.assertRaises(ValueError):
                await self.client.auth_start("arbitrary-command", "admin", "request1")
        opener.open.side_effect = HTTPError(self.client.url, 403, "private-error", {}, io.BytesIO(b"private"))
        with patch("octopus.queries.build_opener", return_value=opener):
            result = await self.client.auth_cancel("tls", "admin", "request1")
            self.assertEqual(result["state"], "failed")
            self.assertNotIn("private", json.dumps(result))

    async def test_dms_pending_card_accepts_only_the_official_authorization_url(self):
        url = ("https://signin.aliyun.com/oauth2/v1/auth?response_type=code&client_id=4038181954557748008"
               "&redirect_uri=http%3A%2F%2F127.0.0.1%3A12345%2Fcli%2Fcallback&state=abcdefghijklmnop"
               "&code_challenge=" + "a" * 43 + "&code_challenge_method=S256")
        base = {"provider": "dms", "request_id": "test", "state": "pending", "verification_url": url}
        for payload, expected in (
            (base, "pending"),
            ({**base, "verification_url": url + "&code=private"}, "failed"),
            ({**base, "provider": "tls"}, "failed"),
            ({**base, "user_code": "private"}, "failed"),
        ):
            opener = Mock()
            opener.open.return_value = io.BytesIO(json.dumps(payload).encode())
            with patch("octopus.queries.build_opener", return_value=opener):
                result = await self.client.auth_status("dms", "admin", "test")
            self.assertEqual(result["provider"], "dms")
            self.assertEqual(result["state"], expected)
            self.assertEqual(result.get("verification_url"), url if expected == "pending" else None)


if __name__ == "__main__":
    unittest.main()
