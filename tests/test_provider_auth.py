from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch

from octopus.provider_auth import ProviderAuth, _verification_url


URL = "https://signin.volcengine.com/authorize/device?user_code=ABCD-EFGH"
PROMPT = ("Open the following URL to authorize this device:\n"
          "https://signin.volcengine.com/authorize/device\n\n"
          "Then enter the code: ABCD-EFGH\n"
          "Alternatively, open:\n" + URL + "\n"
          "This device code expires in 300 seconds.\n")
SUCCESS = json.dumps({"provider": "console-login", "profile": "default", "region": "cn-beijing",
                      "endpoint": "https://tls-cn-beijing.volces.com", "expires_at": "2030-01-01T00:00:00Z",
                      "masked_access_key": "****"})


class ProviderAuthTests(unittest.TestCase):
    def service(self, script: str, *, runner=None, clock=time.monotonic):
        calls = []
        children = []

        def spawn(argv, **kwargs):
            calls.append((argv, kwargs))
            child = subprocess.Popen([sys.executable, "-u", "-c", "import sys,time\n" + script], **kwargs)
            children.append(child)
            return child

        runner = runner or Mock(return_value={"status": "success", "data": {}})
        auth = ProviderAuth(popen=spawn, runner=runner, clock=clock)
        self.addCleanup(auth.close)
        return auth, calls, children, runner

    def wait_state(self, auth, states, *, actor="admin", request_id="request1"):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            result = auth.status("tls", actor, request_id)
            if result["state"] in states:
                return result
            time.sleep(0.01)
        self.fail(f"authorization did not reach {states}: {result}")

    def test_complete_url_is_actor_bound_and_never_exposes_raw_output(self):
        auth, calls, children, runner = self.service(f"sys.stderr.write({PROMPT!r});sys.stderr.flush();time.sleep(20)")
        with patch.dict(os.environ, {"VOLCENGINE_ACCESS_KEY_ID": "private-ak", "OPENAI_API_KEY": "private-model"}):
            auth.start("tls", "admin", "request1")
            result = self.wait_state(auth, {"pending"})
        self.assertEqual(result["verification_url"], URL)
        self.assertNotIn("user_code", result)
        self.assertLessEqual(result["expires_in"], 300)
        self.assertEqual(auth.status("tls", "other", "request1")["state"], "forbidden")
        self.assertEqual(auth.cancel("tls", "other", "request1")["state"], "forbidden")
        self.assertIsNone(children[0].poll())
        self.assertEqual(calls[0][0], ["ve", "volclog", "login", "--no-browser"])
        self.assertFalse(calls[0][1]["shell"])
        self.assertTrue(calls[0][1]["start_new_session"])
        self.assertNotIn("VOLCENGINE_ACCESS_KEY_ID", calls[0][1]["env"])
        self.assertNotIn("OPENAI_API_KEY", calls[0][1]["env"])
        runner.assert_not_called()

    def test_without_complete_url_returns_short_lived_user_code(self):
        prompt = PROMPT.replace("Alternatively, open:\n" + URL + "\n", "")
        auth, _, _, _ = self.service(f"sys.stderr.write({prompt!r});sys.stderr.flush();time.sleep(20)")
        auth.start("tls", "admin", "request1")
        result = self.wait_state(auth, {"pending"})
        self.assertEqual(result["user_code"], "ABCD-EFGH")
        self.assertEqual(result["verification_url"], "https://signin.volcengine.com/authorize/device")

    def test_success_requires_read_query_and_clears_authorization_material(self):
        script = f"sys.stderr.write({PROMPT!r});sys.stderr.flush();print({SUCCESS!r})"
        auth, calls, _, runner = self.service(script)
        auth.start("tls", "admin", "request1")
        result = self.wait_state(auth, {"succeeded"})
        self.assertEqual(set(result), {"provider", "request_id", "state"})
        runner.assert_called_once_with(
            ["ve", "volclog", "tool", "exec", "project.describe-projects", "--input",
             '{"PageSize":1}', "--output-mode", "stdout"], tls=True, timeout=15)
        self.assertEqual(auth.start("tls", "admin", "request1"), result)
        self.assertEqual(len(calls), 1)

    def test_read_query_failure_cannot_be_reported_as_success(self):
        runner = Mock(return_value={"status": "failed", "error": {"kind": "auth", "message": "secret"}})
        auth, _, _, _ = self.service(f"print({SUCCESS!r})", runner=runner)
        auth.start("tls", "admin", "request1")
        result = self.wait_state(auth, {"failed"})
        self.assertNotIn("secret", str(result))
        self.assertIn("查询权限验证未通过", result["error"])

    def test_verification_exception_is_safe_and_does_not_claim_login_failed(self):
        runner = Mock(side_effect=RuntimeError("provider echoed private-token"))
        auth, _, _, _ = self.service(f"print({SUCCESS!r})", runner=runner)
        auth.start("tls", "admin", "request1")
        result = self.wait_state(auth, {"failed"})
        self.assertIn("登录已完成", result["error"])
        self.assertNotIn("private-token", str(result))

    def test_duplicate_start_and_concurrent_other_request_do_not_spawn_again(self):
        auth, calls, _, _ = self.service(f"sys.stderr.write({PROMPT!r});sys.stderr.flush();time.sleep(20)")
        auth.start("tls", "admin", "request1")
        self.wait_state(auth, {"pending"})
        self.assertEqual(auth.start("tls", "admin", "request1")["state"], "pending")
        self.assertEqual(auth.start("tls", "other", "request1")["state"], "forbidden")
        self.assertEqual(auth.start("tls", "admin", "request2")["state"], "busy")
        self.assertEqual(len(calls), 1)

    def test_cancel_terminates_process_and_cannot_be_replayed(self):
        auth, calls, children, _ = self.service(f"sys.stderr.write({PROMPT!r});sys.stderr.flush();time.sleep(20)")
        auth.start("tls", "admin", "request1")
        self.wait_state(auth, {"pending"})
        self.assertEqual(auth.cancel("tls", "admin", "request1")["state"], "cancelled")
        self.assertIsNotNone(children[0].poll())
        self.assertEqual(auth.start("tls", "admin", "request1")["state"], "cancelled")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("verification_url", auth.status("tls", "admin", "request1"))

    def test_provider_ttl_and_host_cap_terminate_process(self):
        for ttl in (2, 600):
            with self.subTest(ttl=ttl):
                now = [10.0]
                prompt = PROMPT.replace("300 seconds", f"{ttl} seconds")
                auth, _, children, _ = self.service(
                    f"sys.stderr.write({prompt!r});sys.stderr.flush();time.sleep(20)", clock=lambda: now[0])
                auth.start("tls", "admin", "request1")
                pending = self.wait_state(auth, {"pending"})
                self.assertEqual(pending["expires_in"], min(ttl, 300))
                now[0] += min(ttl, 300) + 1
                self.wait_state(auth, {"expired"})
                children[0].wait(timeout=2)
                self.assertNotIn("verification_url", auth.status("tls", "admin", "request1"))

    def test_only_exact_session_replace_prompt_receives_approved_yes(self):
        replace = 'Profile "default" is bound to a different login session. Replace it? [y/N]: '
        script = (f"sys.stderr.write({PROMPT!r});sys.stderr.flush();"
                  f"sys.stderr.write({replace!r});sys.stderr.flush();"
                  "answer=sys.stdin.readline();"
                  f"print({SUCCESS!r} if answer=='y\\n' else '{{}}')")
        auth, _, _, _ = self.service(script)
        auth.start("tls", "admin", "request1")
        self.wait_state(auth, {"succeeded"})

    def test_unrecognized_prompt_is_not_automatically_approved(self):
        now = [0.0]
        script = f"sys.stderr.write({PROMPT!r});sys.stderr.write('Delete profile? [y/N]: ');sys.stderr.flush();sys.stdin.readline()"
        auth, _, _, runner = self.service(script, clock=lambda: now[0])
        auth.start("tls", "admin", "request1")
        self.wait_state(auth, {"pending"})
        now[0] = 301
        self.wait_state(auth, {"expired"})
        runner.assert_not_called()

    def test_invalid_url_and_oversized_or_failed_output_fail_closed(self):
        scripts = [
            f"sys.stderr.write({PROMPT.replace(URL, 'https://evil.example/steal')!r});sys.stderr.flush();time.sleep(20)",
            "sys.stderr.write('private-token'*2000);sys.stderr.flush();time.sleep(20)",
            "sys.stderr.write('Bearer private-token');sys.exit(2)",
            "print('{\"access_token\":\"private-token\"}')",
        ]
        for script in scripts:
            with self.subTest(script=script[:30]):
                auth, _, _, runner = self.service(script)
                auth.start("tls", "admin", "request1")
                result = self.wait_state(auth, {"failed"})
                self.assertNotIn("private-token", str(result))
                self.assertNotIn("verification_url", result)
                runner.assert_not_called()

    def test_invalid_requests_cannot_start_process(self):
        auth, calls, _, _ = self.service("raise SystemExit(99)")
        for request in [("grafana", "admin", "r"), ("tls", "a;id", "r"), ("tls", "a", "r\n"),
                        ("tls", "a", "x"*129), ("tls", None, "r")]:
            with self.subTest(request=request), self.assertRaises(ValueError):
                auth.start(*request)
        self.assertEqual(calls, [])

    def test_close_cancels_active_authorization_and_refuses_start(self):
        auth, _, children, _ = self.service(f"sys.stderr.write({PROMPT!r});sys.stderr.flush();time.sleep(20)")
        auth.start("tls", "admin", "request1")
        self.wait_state(auth, {"pending"})
        auth.close()
        self.assertIsNotNone(children[0].poll())
        self.assertEqual(auth.status("tls", "admin", "request1")["state"], "cancelled")
        self.assertEqual(auth.start("tls", "admin", "request2")["state"], "unavailable")

    def test_authorization_url_allowlist_rejects_origin_and_secret_injection(self):
        for value in ["http://signin.volcengine.com/device", "https://signin.volcengine.com.evil.test/device",
                      "https://user@signin.volcengine.com/device", "https://signin.volcengine.com:444/device",
                      "https://signin.volcengine.com/device#fragment", "https://signin.volcengine.com/%0Aevil",
                      "https://signin.volcengine.com/device?access_token=secret", "https://signin.volcengine.com\\@evil.test",
                      "https://signin.volcengine.com/device?device_code=secret"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                _verification_url(value)
        self.assertEqual(_verification_url(URL), URL)
        self.assertEqual(_verification_url("https://signin.volcengine.com:443/device"),
                         "https://signin.volcengine.com:443/device")
