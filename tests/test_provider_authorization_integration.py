from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from octopus.contracts import CardAction, IncomingMessage
from octopus.provider_auth import ProviderAuth
from octopus.provider_authorization import ProviderAuthorization
from octopus.queries import QueryClient
from octopus.query_host import QueryHTTPServer
from tests.test_provider_authorization import DMS_URL, Store, Messenger


class ProviderAuthorizationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_card_approval_crosses_real_client_http_and_login_process_boundaries(self):
        prompt = ("Open the following URL to authorize this device:\n"
                  "https://signin.volcengine.com/authorize/device\n"
                  "Then enter the code: TEST-CODE\n"
                  "Alternatively, open:\n"
                  "https://signin.volcengine.com/authorize/device?user_code=TEST-CODE\n"
                  "This device code expires in 120 seconds.\n")
        await self.exercise_login("tls", prompt, "stderr", '{"provider":"console-login","profile":"existing-profile"}',
                                  "signin.volcengine.com", ["ve", "volclog", "login", "--no-browser"])

    async def test_dms_card_login_verifies_the_profile_and_clears_the_mac_login_url(self):
        self.enterContext(patch("octopus.provider_auth._default_aliyun_profile", return_value="company-cli-user"))
        self.enterContext(patch("octopus.provider_auth.shutil.which", return_value="/mock/aliyun"))
        prompt = f"Please open the following URL in your browser to authorize:\n{DMS_URL}\n"
        await self.exercise_login("dms", prompt, "stdout", "Saving profile[company-cli-user] ...Done.",
                                  "signin.aliyun.com", ["/mock/aliyun", "configure", "--mode", "OAuth",
                                                      "--oauth-site-type", "CN", "--profile", "company-cli-user"])

    async def exercise_login(self, name, prompt, stream, success_output, domain, expected_command):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gate = root / "approved"
            token = root / "token"
            token.write_text("x" * 40)
            script = ("import sys,time\nfrom pathlib import Path\n"
                      f"print({prompt!r},file=sys.{stream},flush=True)\n"
                      f"while not Path({str(gate)!r}).exists(): time.sleep(.01)\n"
                      f"print({success_output!r},flush=True)\n")
            launches = []
            def popen(argv, **kwargs):
                launches.append(argv)
                return subprocess.Popen([sys.executable, "-c", script], **kwargs)

            verifier = Mock(return_value={"status": "success", "data": {}})
            dms_verify = Mock(return_value=True)
            provider = ProviderAuth(popen=popen, runner=verifier, dms_verify=dms_verify)
            server = QueryHTTPServer(("127.0.0.1", 0), SimpleNamespace(authorization=provider), "x" * 40)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            store, messenger = Store(), Messenger()
            client = QueryClient(f"http://127.0.0.1:{server.server_port}", str(token))
            auth = ProviderAuthorization(store, messenger, client)
            task = None
            async def until(predicate):
                async with asyncio.timeout(5):
                    while not predicate():
                        await asyncio.sleep(.01)
            try:
                with patch("octopus.provider_authorization.POLL_SECONDS", .01):
                    message = IncomingMessage("om_question", "oc_group", "ou_member", "查日志", "group", 1)
                    task = asyncio.create_task(auth.recover(message, provider=name, still_allowed=AsyncMock(return_value=True)))
                    await until(lambda: bool(messenger.private))
                    actor, view, card_id = messenger.private[0]
                    self.assertEqual(launches, [])
                    await auth.receive_action(CardAction(view.actions[0].ticket_id, "accept", actor,
                                                         "oc_private", card_id))
                    await until(lambda: any(domain in v.text for _, v in messenger.updates))
                    self.assertEqual(launches, [expected_command])
                    self.assertFalse(any("TEST-CODE" in v.text or "signin" in v.text
                                         for _, v, _ in messenger.notices))
                    gate.touch()
                    self.assertTrue(await asyncio.wait_for(task, 5))
                    (dms_verify if name == "dms" else verifier).assert_called_once()
                    last_views = {card: view for card, view in messenger.updates}
                    self.assertTrue(all("TEST-CODE" not in v.text and "signin" not in v.text
                                        for v in last_views.values()))
                    self.assertNotIn("TEST-CODE", json.dumps(store.audits))
                    self.assertNotIn(DMS_URL, json.dumps(store.audits))
                    self.assertEqual(store.audits[-1][1:4], ("provider.auth", name, "succeeded"))
            finally:
                await auth.close()
                if task:
                    await asyncio.gather(task, return_exceptions=True)
                provider.close()
                await asyncio.to_thread(server.shutdown)
                server.server_close()


if __name__ == "__main__":
    unittest.main()
