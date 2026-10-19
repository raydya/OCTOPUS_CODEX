from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROXY_KEYS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
)
LOCAL_BYPASS = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}


class CodexHostTests(unittest.TestCase):
    """Exercise the installed entry script without starting a real Codex server."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name).resolve()
        self.project = self.directory / "project with spaces"
        scripts = self.project / "scripts"
        scripts.mkdir(parents=True)
        for name in ("codex-host.sh", "codex_host.py"):
            source = PROJECT_ROOT / "scripts" / name
            shutil.copy2(source, scripts / name)
        self.script = scripts / "codex-host.sh"
        self.token = self.project / ".secrets" / "codex_token"
        self.token.parent.mkdir()
        self.token.write_text("synthetic-test-token-" + "a" * 64, encoding="utf-8")
        self.capture = self.directory / "codex-capture.json"
        executable_dir = self.directory / "bin"
        executable_dir.mkdir()
        fake = executable_dir / "codex"
        fake.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "keys = ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', "
            "'https_proxy', 'all_proxy', 'NO_PROXY', 'no_proxy')\n"
            "Path(os.environ['TEST_CODEX_CAPTURE']).write_text(json.dumps({\n"
            "    'argv': sys.argv[1:],\n"
            "    'env': {key: os.environ.get(key) for key in keys},\n"
            "}), encoding='utf-8')\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        # Use the test interpreter for a wrapper that selects `python3` from PATH.
        (executable_dir / "python3").symlink_to(sys.executable)
        self.env = {
            "PATH": str(executable_dir) + os.pathsep + os.defpath,
            "HOME": str(self.directory),
            "TEST_CODEX_CAPTURE": str(self.capture),
        }

    def run_host(self, dotenv: str = "", *, environment: dict[str, str] | None = None):
        (self.project / ".env").write_text(dotenv, encoding="utf-8")
        self.capture.unlink(missing_ok=True)
        return subprocess.run(
            ["/bin/bash", str(self.script)],
            cwd=self.directory,
            env={**self.env, **(environment or {})},
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def captured(self, result: subprocess.CompletedProcess[str]):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.capture.exists(), "fake Codex was not invoked")
        return json.loads(self.capture.read_text(encoding="utf-8"))

    def assert_bypass(self, environment: dict[str, str], *preserved: str):
        required = LOCAL_BYPASS | set(preserved)
        for key in ("NO_PROXY", "no_proxy"):
            with self.subTest(key=key):
                entries = {part.strip() for part in (environment.get(key) or "").split(",")}
                self.assertTrue(required <= entries, f"{key} is missing {required - entries}")

    def test_dotenv_proxy_sets_all_spellings_and_keeps_existing_bypass_entries(self):
        result = self.run_host(
            '# Local Clash proxy\nCODEX_PROXY_URL="http://127.0.0.1:7890" # inline comment\n',
            environment={
                "HTTP_PROXY": "http://old-upper.example:8080",
                "https_proxy": "http://old-lower.example:8080",
                "NO_PROXY": "internal.example, .corp.example",
                "no_proxy": "localhost,service.internal",
            },
        )
        data = self.captured(result)
        for key in PROXY_KEYS:
            self.assertEqual(data["env"][key], "http://127.0.0.1:7890", key)
        self.assert_bypass(data["env"], "internal.example", ".corp.example", "service.internal")
        self.assertEqual(data["argv"], [
            "app-server", "--listen", "ws://127.0.0.1:4500",
            "--ws-auth", "capability-token", "--ws-token-file", str(self.token),
        ])

    def test_environment_proxy_and_port_override_dotenv(self):
        data = self.captured(self.run_host(
            "CODEX_PROXY_URL='http://127.0.0.1:7890'\nCODEX_PORT='4501'\n",
            environment={"CODEX_PROXY_URL": "http://127.0.0.1:7891", "CODEX_PORT": "4510"},
        ))
        for key in PROXY_KEYS:
            self.assertEqual(data["env"][key], "http://127.0.0.1:7891", key)
        self.assertEqual(data["argv"][2], "ws://127.0.0.1:4510")
        self.assert_bypass(data["env"])

    def test_quoted_dotenv_port_with_comment_is_used(self):
        data = self.captured(self.run_host("CODEX_PORT='4520' # configured port\n"))
        self.assertEqual(data["argv"][2], "ws://127.0.0.1:4520")

    def test_absent_codex_proxy_preserves_existing_proxies_and_adds_local_bypass(self):
        existing = {key: f"http://proxy-{index}.example:8080" for index, key in enumerate(PROXY_KEYS)}
        data = self.captured(self.run_host(
            environment={**existing, "NO_PROXY": "upper.internal", "no_proxy": "lower.internal"},
        ))
        for key, value in existing.items():
            self.assertEqual(data["env"][key], value, key)
        self.assert_bypass(data["env"], "upper.internal", "lower.internal")

    def test_absent_proxy_does_not_invent_proxy_settings(self):
        data = self.captured(self.run_host())
        for key in PROXY_KEYS:
            self.assertIsNone(data["env"][key], key)
        self.assert_bypass(data["env"])

    def test_dotenv_command_substitution_is_never_executed(self):
        dollar_marker = self.directory / "dollar-command-executed"
        backtick_marker = self.directory / "backtick-command-executed"
        data = self.captured(self.run_host(
            f'IGNORED="$(touch {dollar_marker})"\n'
            f'ALSO_IGNORED="`touch {backtick_marker}`"\n'
            "CODEX_PROXY_URL='http://127.0.0.1:7890'\n",
        ))
        self.assertFalse(dollar_marker.exists(), "dotenv executed $() shell substitution")
        self.assertFalse(backtick_marker.exists(), "dotenv executed backtick shell substitution")
        self.assertEqual(data["env"]["HTTP_PROXY"], "http://127.0.0.1:7890")

    def test_dotenv_variable_references_are_not_expanded(self):
        result = self.run_host(
            'CODEX_PORT="${TEST_DESIRED_PORT}"\n',
            environment={"TEST_DESIRED_PORT": "4509"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.capture.exists(), "expanded dotenv values launched Codex")

    def test_invalid_proxy_fails_before_codex_starts(self):
        for proxy in (
            "127.0.0.1:7890", "ftp://127.0.0.1:7890", "http://",
            "http://127.0.0.1:invalid", "http://127.0.0.1:70000",
        ):
            with self.subTest(proxy=proxy):
                result = self.run_host(f"CODEX_PROXY_URL='{proxy}'\n")
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.capture.exists(), "invalid proxy still launched Codex")

    def test_invalid_port_fails_before_codex_starts(self):
        for port in ("0", "65536", "not-a-port", "4500.5", "4500; echo injected"):
            with self.subTest(port=port):
                result = self.run_host(f"CODEX_PORT='{port}'\n")
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.capture.exists(), "invalid port still launched Codex")


if __name__ == "__main__":
    unittest.main()
