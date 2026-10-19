from __future__ import annotations

import http.client
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from octopus.query_host import (
    MAX_RESPONSE_BYTES, QueryError, QueryHost, QueryHTTPServer,
    _json, _NoRedirect, bounded_result, read_token, run_cli,
)


class QueryHostTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / "repos"
        self.root.mkdir()
        (self.root / "orders").mkdir()
        (self.root / "orders" / "service.py").write_text("return 'business'\n", encoding="utf-8")
        self.runner = Mock(return_value={"ok": True, "data": {"results": []}})
        self.host = QueryHost(self.root, runner=self.runner,
                              grafana_config=("https://grafana.example.test", "private-grafana-token"))
        self.addCleanup(self.host.close)

    def test_code_tools_are_confined_to_shared_root(self):
        listing = self.host.execute("code_repositories", {})
        self.assertTrue(listing["ok"])
        self.assertEqual(listing["data"]["repositories"][0]["repository"], "orders")
        found = self.host.execute("code_search", {"query": "business"})
        self.assertEqual(found["data"]["matches"][0]["path"], "orders/service.py")
        with self.assertRaises(ValueError):
            self.host.execute("code_read", {"path": "../../.secrets/query_token"})
        self.runner.assert_not_called()

    def test_code_evidence_binds_source_while_model_sees_masked_text(self):
        (self.root / "orders" / "query.sql").write_text("SELECT status FROM orders WHERE phone = '13800138000';\n")
        context = {"actor": "alice", "job_id": "job1", "thread_id": "thread1", "turn_id": "turn1",
                   "policy_version": "readonly:v2", "question": "订单状态为什么没有更新"}
        self.host.dms = Mock()
        self.host.dms.record_code.return_value = "evidence-token"
        result = self.host.execute("code_read", {"path": "orders/query.sql"}, context=context)
        source = self.host.dms.record_code.call_args.args[1]
        self.assertIn("13800138000", source["lines"][0]["content"])
        self.assertNotIn("13800138000", json.dumps(result))
        self.assertEqual(result["data"]["evidence_id"], "evidence-token")

    def test_unknown_tools_extra_parameters_and_wrong_types_are_rejected(self):
        for tool, arguments in (
            ("shell", {"command": "rm -rf /"}), ("docs_search", {"query": "x", "profile": "other"}),
            ("docs_fetch", {"document": "Abc123456", "output": "/tmp/x"}),
            ("tls_topics", {"endpoint": "https://evil.test"}),
            ("grafana_query", {"query": "x"}), ("code_repositories", []),
        ):
            with self.subTest(tool=tool), self.assertRaises(ValueError):
                self.host.execute(tool, arguments)
        self.runner.assert_not_called()

    def test_business_data_is_closed_without_a_policy_or_trusted_context(self):
        for tool, args in (
            ("business_data_checks", {"evidence_id": "invented", "table": "tasks"}),
            ("business_data_check", {"evidence_id": "invented", "check_id": "invented", "day": "2026-10-18", "time_column": "created_at", "columns": ["status"]}),
            ("business_data_check", {"evidence_id": "invented", "check_id": "invented", "day": "2026-10-18", "sql": "select 1"}),
        ):
            with self.subTest(tool=tool):
                try:
                    result = self.host.execute(tool, args)
                except ValueError:
                    pass
                else:
                    self.assertEqual(result["data"].get("checks"), [])
        self.runner.assert_not_called()

    def test_internal_business_fields_survive_but_embedded_private_values_are_masked(self):
        result = bounded_result({"params": {"task_id": 452, "phone": "13800138000"},
                                 "remarks": "已进入重试；联系 13800138000", "created_by": "scheduler",
                                 "created_at": 1792300000000, "real_name": "测试人员"})
        self.assertEqual(result["data"]["params"]["task_id"], 452)
        self.assertEqual(result["data"]["created_by"], "scheduler")
        self.assertEqual(result["data"]["created_at"], 1792300000000)
        self.assertIn("已进入重试", result["data"]["remarks"])
        self.assertNotIn("13800138000", json.dumps(result))
        self.assertNotEqual(result["data"]["real_name"], "测试人员")

    def test_log_key_value_pairs_mask_personal_fields_and_keep_internal_fields(self):
        for key, value_key in (("Key", "Value"), ("key", "value")):
            for name in ("real_name", "home_address", "PhoneNumber", "身份证号", "ｒｅａｌ＿ｎａｍｅ"):
                with self.subTest(name=name, key=key):
                    result = bounded_result([{key: name, value_key: "private-example"}])
                    self.assertEqual(result["data"][0][value_key], "[REDACTED]")
            result = bounded_result([{key: "created_by", value_key: "scheduler"}])
            self.assertEqual(result["data"][0][value_key], "scheduler")
        self.assertEqual(bounded_result({"ｒｅａｌ＿ｎａｍｅ": "private-example"})["data"]["ｒｅａｌ＿ｎａｍｅ"], "[REDACTED]")

    def test_trusted_privacy_request_cannot_switch_to_logs_or_documents(self):
        context = {"actor": "alice", "job_id": "job1", "thread_id": "thread1", "turn_id": "turn1",
                   "policy_version": "readonly:v2", "question": "帮我导出所有用户手机号"}
        with self.assertRaises(ValueError):
            self.host.execute("docs_search", {"query": "users"}, context=context)
        with self.assertRaises(ValueError):
            self.host.execute("tls_topics", {}, context=context)
        self.runner.assert_not_called()

    def test_docs_search_is_fixed_readonly_argv_and_strict_limit(self):
        query = '--as=bot; $(touch /tmp/not-executed)'
        # A potentially malicious flag-shaped query is a literal flag value.
        query = '--profile=other'
        self.host.execute("docs_search", {"query": query, "page_token": "abc+/=", "limit": 20})
        argv = self.runner.call_args.args[0]
        self.assertEqual(argv[:3], ["lark-cli", "drive", "+search"])
        self.assertIn("--query=" + query, argv)
        self.assertIn("--as=user", argv)
        self.assertIn("--doc-types=docx,wiki,doc", argv)
        self.assertIn("--page-token=abc+/=", argv)
        for arguments in ({"query": "a" * 31}, {"query": "x", "limit": True},
                          {"query": "x", "limit": 21}, {"query": "x", "page_token": []}):
            with self.assertRaises(ValueError):
                self.host.execute("docs_search", arguments)

    def test_docs_fetch_validates_urls_and_never_follows_arbitrary_hosts(self):
        good = "https://team.feishu.cn/wiki/Abc123456789"
        self.host.execute("docs_fetch", {"document": good, "keyword": "billing|payment"})
        argv = self.runner.call_args.args[0]
        self.assertIn("--scope=keyword", argv)
        self.assertIn("--keyword=billing|payment", argv)
        self.assertIn("--doc-format=markdown", argv)
        self.host.execute("docs_fetch", {"document": "Abc123456789"})
        self.assertIn("--scope=full", self.runner.call_args.args[0])
        for document in ("file:///etc/passwd", "http://team.feishu.cn/docx/Abc123456789",
                         "https://feishu.cn.evil.test/docx/Abc123456789",
                         "https://evil@team.feishu.cn/docx/Abc123456789",
                         "https://team.feishu.cn:444/docx/Abc123456789",
                         "https://team.feishu.cn/docx/Abc123456789?redirect=https://evil.test",
                         "https://team.feishu.cn/drive/file/Abc123456789", "--profile=other",
                         "../../Abc123456789", "https://team.feishu.cn/docx/Abc123456789#abc"):
            with self.subTest(document=document), self.assertRaises(ValueError):
                self.host.execute("docs_fetch", {"document": document})

    def test_provider_errors_do_not_return_raw_stderr_or_error_objects(self):
        self.runner.return_value = {"ok": False, "error": {"message": "private-secret-value"}}
        with self.assertRaises(QueryError) as caught:
            self.host.execute("docs_search", {"query": "x"})
        self.assertNotIn("private-secret-value", str(caught.exception))
        self.runner.return_value = {"status": "failed", "error": {"message": "private-secret-value"}}
        with self.assertRaises(QueryError) as caught:
            self.host.execute("tls_topics", {})
        self.assertNotIn("private-secret-value", str(caught.exception))

    def test_tls_topics_uses_only_verified_query_fields(self):
        self.runner.return_value = {"status": "success", "summary": {"deliveryMode": "stdout"}, "data": {"Topics": []}}
        self.host.execute("tls_topics", {"query": "billing", "project_id": "project-1", "page": 2})
        argv = self.runner.call_args.args[0]
        self.assertEqual(argv[:5], ["ve", "volclog", "tool", "exec", "topic.describe-topics"])
        self.assertEqual(argv[5], "--input")
        self.assertEqual(json.loads(argv[6]), {"PageNumber": 2, "PageSize": 30, "FuzzySearchKey": "billing", "ProjectId": "project-1"})
        self.assertTrue(self.runner.call_args.kwargs["tls"])
        self.assertEqual(argv[-2:], ["--output-mode", "stdout"])

    def test_tls_search_preserves_millisecond_units_and_enforces_window(self):
        self.runner.return_value = {"status": "success", "summary": {"deliveryMode": "stdout"}, "data": {"Logs": []}}
        args = {"topic_id": "topic-1", "query": 'level:ERROR AND service:"billing"',
                "start_time": 1770000000000, "end_time": 1770000060000, "limit": 5}
        self.host.execute("tls_search", args)
        payload = json.loads(self.runner.call_args.args[0][6])["body"]
        self.assertEqual(payload["StartTime"], args["start_time"])
        self.assertEqual(payload["EndTime"], args["end_time"])
        self.assertEqual(payload["Sort"], "desc")
        for update in ({"query": "* | select * from logs"}, {"start_time": 1770000000},
                       {"end_time": args["start_time"]}, {"end_time": args["start_time"] + 86400001},
                       {"limit": 101}, {"limit": True}, {"topic_id": "../../other"},
                       {"query": "x\nContext:secret"}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.host.execute("tls_search", {**args, **update})

    def test_tls_file_delivery_is_not_opened(self):
        self.runner.return_value = {"status": "success", "summary": {"deliveryMode": "file_auto"}, "data": {"path": "/etc/passwd"}}
        with self.assertRaises(QueryError):
            self.host.execute("tls_topics", {})

    def test_grafana_datasource_output_excludes_connection_configuration(self):
        with patch.object(self.host, "_grafana", return_value=[{
            "uid": "prom1", "name": "Metrics", "type": "prometheus", "url": "http://private-db",
            "basicAuthPassword": "private-secret", "jsonData": {"accessToken": "private-token"},
        }]):
            result = self.host.execute("grafana_datasources", {})
        self.assertEqual(result["data"]["datasources"], [{"uid": "prom1", "name": "Metrics", "type": "prometheus"}])
        self.assertNotIn("private", json.dumps(result))

    def test_grafana_query_requires_prometheus_and_bounded_window(self):
        args = {"datasource_uid": "prom1", "query": "sum(rate(requests_total[5m]))",
                "start_time": "2026-10-19T00:00:00Z", "end_time": "2026-10-19T01:00:00Z"}
        with patch.object(self.host, "_grafana", side_effect=[{"type": "prometheus"}, {"status": "success", "data": []}]) as request:
            self.host.execute("grafana_query", args)
            self.assertEqual(request.call_args_list[0].args[0], "/api/datasources/uid/prom1")
            self.assertEqual(request.call_args.args[0], "/api/datasources/proxy/uid/prom1/api/v1/query_range")
            self.assertEqual(request.call_args.args[1]["step"], 60)
        with patch.object(self.host, "_grafana", return_value={"type": "postgres"}):
            with self.assertRaises(ValueError):
                self.host.execute("grafana_query", args)
        for update in ({"datasource_uid": "../x"}, {"start_time": "2026-10-19T00:00:00"},
                       {"end_time": "2026-10-21T00:00:00Z"}, {"step_seconds": 1},
                       {"step_seconds": True}, {"end_time": "2026-10-20T00:00:00Z", "step_seconds": 15}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.host.execute("grafana_query", {**args, **update})

    def test_grafana_fixed_get_query_and_redirect_protection(self):
        response = io.BytesIO(b'{"status":"success","data":[]}')
        self.host._opener = Mock()
        self.host._opener.open.return_value = response
        self.host.execute("grafana_search_dashboards", {"query": "billing&admin=true"})
        request = self.host._opener.open.call_args.args[0]
        self.assertEqual(request.method, "GET")
        self.assertIn("query=billing%26admin%3Dtrue", request.full_url)
        self.assertIn("type=dash-db", request.full_url)
        self.assertEqual(request.headers["Authorization"], "Bearer private-grafana-token")
        with self.assertRaises(QueryError):
            _NoRedirect().redirect_request(request, None, 302, "", {}, "https://evil.test")
        self.host._grafana_config = ("http://grafana.example.test", "private-token")
        with self.assertRaises(QueryError):
            self.host.execute("grafana_datasources", {})

    def test_output_is_recursively_redacted_and_bounded(self):
        result = bounded_result({"password": "do-not-return", "nested": [{"content": 'api_key="secret-value"'}],
                                 "many": ["中" * 24000] * 300})
        raw = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.assertLessEqual(len(raw), MAX_RESPONSE_BYTES)
        self.assertTrue(result["truncated"])
        self.assertNotIn(b"do-not-return", raw)
        self.assertNotIn(b"secret-value", raw)
        self.assertEqual(bounded_result({"page_token": "opaque-public-pagination"}, preserve_page_token=True)["data"]["page_token"],
                         "opaque-public-pagination")
        short_secrets = {"api_key": "tinyapikey", "token": "tinysecret", "pwd": "tinypassword",
                         "credential": "tinycredential", "page_token": "not-docs-pagination",
                         "contents": [{"Key": "password", "Value": "shortpassword"}]}
        cleaned = json.dumps(bounded_result(short_secrets))
        for secret in ("tinyapikey", "tinysecret", "tinypassword", "tinycredential", "not-docs-pagination", "shortpassword"):
            self.assertNotIn(secret, cleaned)
        self.runner.return_value = {"ok": True, "data": {"page_token": "opaque-public-pagination",
                                  "results": [{"page_token": "nested-secret"}]}}
        result = self.host.execute("docs_search", {"query": "x"})
        self.assertEqual(result["data"]["page_token"], "opaque-public-pagination")
        self.assertNotIn("nested-secret", json.dumps(result))

    def test_json_rejects_duplicate_keys_and_nonfinite_values(self):
        for raw in (b'{"x":1,"x":2}', b'{"a":{"x":1,"x":2}}', b'{"x":NaN}', b'{"x":Infinity}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                _json(raw)

    def test_cli_output_limit_timeout_and_error_redaction(self):
        with patch("octopus.query_host.MAX_PROVIDER_BYTES", 1024):
            with self.assertRaises(QueryError):
                run_cli([sys.executable, "-c", "print('x' * 2000)"])
        with patch("octopus.query_host.PROVIDER_TIMEOUT", 0.1):
            started = time.monotonic()
            with self.assertRaises(QueryError):
                run_cli([sys.executable, "-c", "import time; time.sleep(30)"])
            self.assertLess(time.monotonic() - started, 5)
        with self.assertRaises(QueryError) as caught:
            run_cli([sys.executable, "-c", "import sys; print('private-token',file=sys.stderr);sys.exit(1)"])
        self.assertNotIn("private-token", str(caught.exception))
        result = run_cli([sys.executable, "-c", "print('{\"ok\": true}')"])
        self.assertEqual(result, {"ok": True})

    def test_cli_deadline_override_and_invalid_timeout_do_not_leave_a_process(self):
        for timeout in (0, -1, 46, True, float("nan")):
            with self.subTest(timeout=timeout), patch("octopus.query_host.subprocess.Popen") as spawn:
                with self.assertRaises(ValueError):
                    run_cli([sys.executable, "-c", "pass"], timeout=timeout)
                spawn.assert_not_called()
        started = time.monotonic()
        with self.assertRaises(QueryError):
            run_cli([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.1)
        self.assertLess(time.monotonic() - started, 5)

    def test_tls_subprocess_metadata_and_no_token_inherited(self):
        program = "import json,os;print(json.dumps({k:os.environ.get(k) for k in ['VE_CALLER_TYPE','VE_CALLER_NAME','VE_SKILL_NAME','QUERY_SECRET']}))"
        with patch.dict(os.environ, {"QUERY_SECRET": "must-not-be-inherited"}):
            result = run_cli([sys.executable, "-c", program], tls=True)
        self.assertEqual(result, {"VE_CALLER_TYPE": "ai_agent", "VE_CALLER_NAME": "codex",
                                  "VE_SKILL_NAME": "volcengine-tls", "QUERY_SECRET": None})

    def test_private_token_permissions_and_symlink(self):
        path = self.directory / "token"
        path.write_text("a" * 64, encoding="ascii")
        path.chmod(0o600)
        self.assertEqual(read_token(path), "a" * 64)
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            read_token(path)
        linked = self.directory / "linked"
        linked.symlink_to(path)
        with self.assertRaises(OSError):
            read_token(linked)


class QueryHTTPTests(unittest.TestCase):
    def setUp(self):
        self.host = Mock()
        self.host.execute.return_value = {"ok": True, "data": {"value": 1}, "truncated": False}
        self.token = "a" * 64
        self.server = QueryHTTPServer(("127.0.0.1", 0), self.host, self.token)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def request(self, method="POST", path="/v1/query", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        self.addCleanup(connection.close)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, json.loads(response.read())

    def authorized(self, **kwargs):
        headers = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json"}
        headers.update(kwargs.pop("headers", {}))
        kwargs.setdefault("body", json.dumps({"tool": "code_repositories", "arguments": {}}))
        return self.request(headers=headers, **kwargs)

    def test_authentication_and_health(self):
        status, data = self.request("GET", "/health")
        self.assertEqual(status, 401)
        self.host.execute.assert_not_called()
        status, data = self.authorized(method="GET", path="/health", body=None)
        self.assertEqual((status, data), (200, {"ok": True}))
        status, data = self.authorized()
        self.assertEqual(status, 200)
        self.host.execute.assert_called_once_with("code_repositories", {})

    def test_unknown_route_origins_and_malformed_json_rejected(self):
        self.assertEqual(self.authorized(path="/v1/query?x=1")[0], 404)
        self.assertEqual(self.authorized(headers={"Origin": "https://evil.test"})[0], 403)
        for body in ('{"tool":"x","tool":"y","arguments":{}}',
                     '{"tool":"x","arguments":{"limit":NaN}}',
                     '{"tool":"x","arguments":{},"endpoint":"https://evil.test"}', "[]"):
            with self.subTest(body=body):
                self.assertEqual(self.authorized(body=body)[0], 400)
        self.host.execute.assert_not_called()

    def test_context_is_separate_from_arguments_and_validated_before_dispatch(self):
        context = {"actor": "alice", "job_id": "job1", "thread_id": "thread1", "turn_id": "turn1",
                   "policy_version": "readonly:v2", "question": "任务执行状态为何不同"}
        body = {"tool": "code_read", "arguments": {"path": "service/dao.py"}, "context": context}
        self.assertEqual(self.authorized(body=json.dumps(body))[0], 200)
        self.host.execute.assert_called_once_with("code_read", body["arguments"], context=context)
        self.host.execute.reset_mock()
        for invalid in (None, {}, {**context, "policy_version": "readonly:v1"}, {**context, "admin": True}):
            with self.subTest(context=invalid):
                self.assertEqual(self.authorized(body=json.dumps({**body, "context": invalid}))[0], 400)
        self.host.execute.assert_not_called()

    def test_size_content_type_and_generic_errors(self):
        self.assertEqual(self.authorized(body="x" * 65537)[0], 413)
        self.assertEqual(self.authorized(headers={"Content-Type": "text/plain"})[0], 400)
        self.host.execute.side_effect = RuntimeError("private-secret-value")
        status, data = self.authorized()
        self.assertEqual(status, 500)
        self.assertNotIn("private-secret-value", json.dumps(data))
        self.host.execute.side_effect = ValueError("private-path")
        status, data = self.authorized()
        self.assertEqual(status, 400)
        self.assertNotIn("private-path", json.dumps(data))

    def test_nonloopback_binding_is_rejected(self):
        with self.assertRaises(ValueError):
            QueryHTTPServer(("0.0.0.0", 4501), self.host, self.token)

    def test_concurrency_is_bounded_before_thread_creation(self):
        sockets = []
        self.addCleanup(lambda: [sock.close() for sock in sockets])
        for _ in range(8):
            sock = socket.create_connection(("127.0.0.1", self.port), timeout=2)
            sockets.append(sock)
            sock.sendall(b"POST /v1/query HTTP/1.1\r\n")
        deadline = time.monotonic() + 2
        while self.server.slots._value and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.server.slots._value, 0)
        extra = socket.create_connection(("127.0.0.1", self.port), timeout=2)
        sockets.append(extra)
        extra.settimeout(2)
        self.assertEqual(extra.recv(1), b"")


if __name__ == "__main__":
    unittest.main()
