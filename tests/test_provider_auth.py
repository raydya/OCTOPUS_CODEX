from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch

from octopus.provider_auth import ProviderAuth, _default_aliyun_profile, _restore_aliyun_profile, _verification_url, dms_verification_url


URL = "https://signin.volcengine.com/authorize/device?user_code=ABCD-EFGH"
PROMPT = ("Open the following URL to authorize this device:\n"
          "https://signin.volcengine.com/authorize/device\n\n"
          "Then enter the code: ABCD-EFGH\n"
          "Alternatively, open:\n" + URL + "\n"
          "This device code expires in 300 seconds.\n")
SUCCESS = json.dumps({"provider": "console-login", "profile": "default", "region": "cn-beijing",
                      "endpoint": "https://tls-cn-beijing.volces.com", "expires_at": "2030-01-01T00:00:00Z",
                      "masked_access_key": "****"})
DMS_URL = ("https://signin.aliyun.com/oauth2/v1/auth?response_type=code&client_id=4038181954557748008"
           "&redirect_uri=http%3A%2F%2F127.0.0.1%3A12345%2Fcli%2Fcallback&state=azAZ0123456789XY"
           "&code_challenge=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA&code_challenge_method=S256")
DMS_PROMPT = "Please open the following URL in your browser to authorize:\n" + DMS_URL + "\n"
DMS_SAVED = "Saving profile[company-cli-user] ...Done."


class ProviderAuthTests(unittest.TestCase):
    def service(self, script: str, *, runner=None, clock=time.monotonic, dms_verify=None):
        calls = []
        children = []

        def spawn(argv, **kwargs):
            calls.append((argv, kwargs))
            child = subprocess.Popen([sys.executable, "-u", "-c", "import sys,time\n" + script], **kwargs)
            children.append(child)
            return child

        runner = runner or Mock(return_value={"status": "success", "data": {}})
        auth = ProviderAuth(popen=spawn, runner=runner, clock=clock, dms_verify=dms_verify)
        self.addCleanup(auth.close)
        return auth, calls, children, runner

    def wait_state(self, auth, states, *, actor="admin", request_id="request1", provider="tls"):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            result = auth.status(provider, actor, request_id)
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

    def dms_service(self, script, *, verifier=None, clock=time.monotonic, original="company-cli-user"):
        self.enterContext(patch("octopus.provider_auth.shutil.which", return_value="/opt/mock/aliyun"))
        self.enterContext(patch("octopus.provider_auth._default_aliyun_profile", return_value=original))
        return self.service(script, clock=clock, dms_verify=verifier or Mock(return_value=True))

    def test_dms_cli_owns_callback_and_success_requires_production_read(self):
        verify = Mock(return_value=True)
        auth, calls, _, _ = self.dms_service(
            f"print({DMS_PROMPT!r});time.sleep(.3);print({DMS_SAVED!r})", verifier=verify)
        auth.start("dms", "admin", "request1")
        challenge = self.wait_state(auth, {"pending"}, provider="dms")
        self.assertEqual(challenge["verification_url"], DMS_URL)
        self.assertNotIn("user_code", challenge)
        self.assertEqual(auth.start("dms", "admin", "request1")["verification_url"], DMS_URL)
        self.assertEqual(auth.status("tls", "admin", "request1")["state"], "forbidden")
        self.assertEqual(auth.cancel("tls", "admin", "request1")["state"], "forbidden")
        self.assertEqual(auth.status("dms", "other", "request1")["state"], "forbidden")
        self.assertEqual(auth.start("tls", "admin", "other")["state"], "busy")
        self.assertEqual(self.wait_state(auth, {"succeeded"}, provider="dms"),
                         {"provider": "dms", "request_id": "request1", "state": "succeeded"})
        verify.assert_called_once_with()
        self.assertEqual(len(calls), 1)
        argv, kwargs = calls[0]
        self.assertEqual(argv, ["/opt/mock/aliyun", "configure", "--mode", "OAuth", "--oauth-site-type", "CN",
                                "--profile", "company-cli-user"])
        self.assertEqual(kwargs["env"]["PATH"], os.devnull)
        self.assertNotIn("VE_CALLER_TYPE", kwargs["env"])
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertFalse(kwargs["shell"])

    def test_dms_does_not_report_existing_credentials_or_skipped_verification_as_success(self):
        auth, calls, _, _ = self.service("raise AssertionError('should not spawn')")
        self.assertEqual(auth.start("dms", "admin", "request1")["state"], "unavailable")
        self.assertEqual(calls, [])
        for prompt, saved, code, allowed, should_verify in (
                ("", DMS_SAVED, 0, True, False),
                (DMS_PROMPT, "secret from CLI", 0, True, False),
                (DMS_PROMPT, DMS_SAVED, 1, True, False),
                (DMS_PROMPT, DMS_SAVED, 0, False, True),
                (DMS_PROMPT.replace("signin.aliyun.com", "untrusted.example"), DMS_SAVED, 0, True, False)):
            with self.subTest(prompt=bool(prompt), saved=saved == DMS_SAVED, code=code, allowed=allowed):
                verifier = Mock(return_value=allowed)
                script = f"print({prompt!r});print({saved!r});sys.exit({code})"
                auth, _, _, _ = self.dms_service(script, verifier=verifier)
                auth.start("dms", "admin", "request1")
                result = self.wait_state(auth, {"failed"}, provider="dms")
                self.assertNotIn("secret", str(result))
                self.assertNotIn("verification_url", result)
                self.assertEqual(verifier.call_count, int(should_verify))

    def test_dms_cancel_and_expiry_stop_cli_and_discard_private_challenge(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                now = [1.0]
                verifier = Mock()
                auth, _, children, _ = self.dms_service(f"print({DMS_PROMPT!r});time.sleep(50)",
                                                       clock=lambda: now[0], verifier=verifier)
                auth.start("dms", "admin", "request1")
                self.wait_state(auth, {"pending"}, provider="dms")
                if cancel:
                    auth.cancel("dms", "admin", "request1")
                else:
                    now[0] += 301
                terminal = self.wait_state(auth, {"cancelled" if cancel else "expired"}, provider="dms")
                self.assertNotIn("verification_url", terminal)
                children[0].wait(timeout=2)
                verifier.assert_not_called()

    def test_dms_verifier_exception_and_failed_cli_still_restore_default(self):
        for verifier, exit_code in ((Mock(side_effect=ValueError("private")), 0), (Mock(), 1)):
            with self.subTest(exit_code=exit_code), patch("octopus.provider_auth._restore_aliyun_profile") as restore:
                script = f"print({DMS_PROMPT!r});print({DMS_SAVED!r});sys.exit({exit_code})"
                auth, _, _, _ = self.dms_service(script, verifier=verifier, original="original")
                auth.start("dms", "admin", "request1")
                result = self.wait_state(auth, {"failed"}, provider="dms")
                self.assertNotIn("private", str(result))
                restore.assert_called_once()
                self.assertEqual(restore.call_args.args[2], "original")

    def test_aliyun_default_profile_uses_only_name_and_preserves_concurrent_selection(self):
        with patch("octopus.provider_auth.subprocess.run") as run:
            run.return_value.stdout = b"Profile | Credential | Valid\nother * | sensitive | Valid\n"
            self.assertEqual(_default_aliyun_profile("/cli", {}), "other")
            _restore_aliyun_profile("/cli", {}, "original")
            self.assertTrue(all(call.args[0] == ["/cli", "configure", "list"] for call in run.call_args_list))
            run.return_value.stdout = b"company-cli-user * | ignored | Valid\n"
            _restore_aliyun_profile("/cli", {}, "original")
            self.assertEqual(run.call_args.args[0], ["/cli", "configure", "switch", "--profile", "original"])
            run.return_value.stdout += b"another * | ignored | Valid\n"
            with self.assertRaises(ValueError):
                _default_aliyun_profile("/cli", {})

    def test_dms_challenge_accepts_only_native_cn_pkce_origin_and_fields(self):
        self.assertEqual(dms_verification_url(DMS_URL), DMS_URL)
        for url in (DMS_URL.replace("signin.aliyun.com", "evil.com"), DMS_URL.replace("https:", "http:", 1),
                    DMS_URL + "&code=secret", DMS_URL + "#fragment", DMS_URL.replace("127.0.0.1", "evil.com"),
                    DMS_URL.replace("12345", "12445"), DMS_URL.replace("S256", "plain"),
                    DMS_URL.replace("4038181954557748008", "other-client"), DMS_URL.replace("state=", "state=x&state="),
                    DMS_URL.replace("azAZ", "%0Aaz"), DMS_URL.replace("/auth?", "/auth)foo?")):
            with self.subTest(url=url), self.assertRaises(ValueError):
                dms_verification_url(url)
