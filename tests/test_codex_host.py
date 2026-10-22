from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
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
            "if '--version' in sys.argv:\n"
            "    print(os.environ.get('TEST_CODEX_VERSION', 'codex-cli 0.163.0')); sys.exit(0)\n"
            "keys = ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', "
            "'https_proxy', 'all_proxy', 'NO_PROXY', 'no_proxy', 'CODEX_HOME', 'HOME', "
            "'FAKE_CLOUD_TOKEN', 'OPENAI_API_KEY', 'CODEX_SQLITE_HOME')\n"
            f"Path({str(self.capture)!r}).write_text(json.dumps({{\n"
            "    'argv': sys.argv[1:],\n"
            "    'env': {key: os.environ.get(key) for key in keys},\n"
            "    'cwd': os.getcwd(),\n"
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

    def write_policy(self, policy: object) -> Path:
        path = self.project / "config" / "codex-diagnostics.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(policy, ensure_ascii=False), encoding="utf-8")
        return path

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

    def test_diagnostics_policy_becomes_scoped_toml_arguments_without_shell_execution(self):
        marker = self.directory / "instructions-executed"
        instructions = f'只读查询\nQuotes: "yes"; $(touch "{marker}") `touch "{marker}"` 🐙\x7f'
        policy = {
            "developer_instructions": instructions,
            "mcp_servers": {"grafana": ["query_prometheus", "get-dashboard"], "tls_logs": ["SearchLogs"]},
        }
        path = self.write_policy(policy)
        original_policy = path.read_bytes()
        user_config = self.directory / ".codex" / "config.toml"
        user_config.parent.mkdir()
        user_config.write_text('approval_policy = "on-request"\n', encoding="utf-8")
        original_user_config = user_config.read_bytes()

        result = self.run_host('CODEX_DIAGNOSTICS_ENABLED="true" # this host only\n')
        data = self.captured(result)
        self.assertEqual(data["argv"][:7], [
            "app-server", "--listen", "ws://127.0.0.1:4500",
            "--ws-auth", "capability-token", "--ws-token-file", str(self.token),
        ])
        overrides = data["argv"][7:]
        self.assertEqual(overrides[::2], ["-c"] * (len(overrides) // 2))
        effective = tomllib.loads("\n".join(overrides[1::2]))
        self.assertEqual(set(effective), {"developer_instructions", "mcp_servers"})
        self.assertEqual(effective["developer_instructions"], instructions)
        for name, tools in policy["mcp_servers"].items():
            self.assertEqual(effective["mcp_servers"][name], {
                "enabled_tools": tools,
                "default_tools_approval_mode": "prompt",
                "tools": {tool: {"approval_mode": "approve"} for tool in tools},
            })
        self.assertFalse(marker.exists())
        self.assertEqual(path.read_bytes(), original_policy)
        self.assertEqual(user_config.read_bytes(), original_user_config)
        self.assertNotIn(instructions, result.stdout + result.stderr)

    def test_disabled_diagnostics_ignores_policy_and_environment_overrides_dotenv(self):
        path = self.write_policy({"invalid": "ignored while disabled"})
        path.write_text("malformed json", encoding="utf-8")
        for dotenv, environment in (
            ("", {}),
            ("CODEX_DIAGNOSTICS_ENABLED=false\n", {}),
            ("CODEX_DIAGNOSTICS_ENABLED=true\n", {"CODEX_DIAGNOSTICS_ENABLED": "false"}),
        ):
            with self.subTest(dotenv=dotenv, environment=environment):
                data = self.captured(self.run_host(dotenv, environment=environment))
                self.assertEqual(len(data["argv"]), 7)
                self.assertNotIn("-c", data["argv"])

    def test_environment_can_enable_diagnostics_over_dotenv(self):
        self.write_policy({"developer_instructions": "Read queries only.", "mcp_servers": {"tls": ["query"]}})
        data = self.captured(self.run_host("CODEX_DIAGNOSTICS_ENABLED=false\n", environment={
            "CODEX_DIAGNOSTICS_ENABLED": "true",
        }))
        self.assertIn('mcp_servers.tls.enabled_tools=["query"]', data["argv"])

    def test_enabled_diagnostics_requires_a_valid_policy_file(self):
        secret = "synthetic-policy-secret-never-print"
        path = self.project / "config" / "codex-diagnostics.json"
        for content in (None, secret, '{"developer_instructions":"' + secret + '",'):
            with self.subTest(content=content):
                if content is not None:
                    path.parent.mkdir(exist_ok=True)
                    path.write_text(content, encoding="utf-8")
                result = self.run_host("CODEX_DIAGNOSTICS_ENABLED=true\n")
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.capture.exists())
                self.assertNotIn(secret, result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_invalid_diagnostics_policy_never_launches_or_prints_values(self):
        secret = "synthetic-sensitive-instructions"
        valid = {"developer_instructions": secret, "mcp_servers": {"grafana": ["query"]}}
        policies = [
            None, [], {},
            {**valid, "approval_policy": "never"},
            {**valid, "sandbox_mode": "danger-full-access"},
            {**valid, "developer_instructions": 123},
            {**valid, "mcp_servers": []},
            {**valid, "mcp_servers": {}},
        ]
        for server in ("", "grafana.tools.write", 'grafana"', "grafana\n", "图表"):
            policies.append({**valid, "mcp_servers": {server: ["query"]}})
        for tools in ([], "query", ["query", "query"], [False], [None], [{}], [""],
                      ["query.write"], ['query"'], ["query\n"], ["$(touch bad)"]):
            policies.append({**valid, "mcp_servers": {"grafana": tools}})
        for policy in policies:
            with self.subTest(policy=policy):
                self.write_policy(policy)
                result = self.run_host("CODEX_DIAGNOSTICS_ENABLED=true\n")
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.capture.exists(), "invalid policy launched Codex")
                self.assertNotIn(secret, result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_duplicate_policy_fields_are_rejected(self):
        path = self.write_policy({})
        for content in (
            '{"developer_instructions":"read", "developer_instructions":"other", "mcp_servers":{"tls":["query"]}}',
            '{"developer_instructions":"read", "mcp_servers":{"tls":["query"],"tls":["write"]}}',
        ):
            with self.subTest(content=content):
                path.write_text(content, encoding="utf-8")
                result = self.run_host("CODEX_DIAGNOSTICS_ENABLED=true\n")
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.capture.exists())

    def test_invalid_diagnostics_toggle_is_rejected(self):
        for value in ("", "enabled", "tru", "true; echo injected"):
            with self.subTest(value=value):
                result = self.run_host(environment={"CODEX_DIAGNOSTICS_ENABLED": value})
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.capture.exists())

    def prepare_readonly(self):
        config = self.project / "config"
        config.mkdir(exist_ok=True)
        shutil.copy2(PROJECT_ROOT / "config" / "codex-readonly.json", config)
        auth = self.directory / ".codex" / "auth.json"
        auth.parent.mkdir(exist_ok=True)
        auth.write_text('{"synthetic":"test-only-auth"}')
        workspace = self.directory / "Projects" / "rere"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "service.py").write_text("business_source = True\n")
        return auth

    def test_readonly_isolates_config_auth_cwd_and_ambient_secrets(self):
        auth = self.prepare_readonly()
        personal_config = auth.parent / "config.toml"
        personal_config.write_text('[mcp_servers.unsafe]\ncommand = "not-executed"\n')
        data = self.captured(self.run_host(
            "CODEX_READ_ONLY=true\nCODEX_DIAGNOSTICS_ENABLED=true\nCODEX_PROXY_URL=http://127.0.0.1:7890\n",
            environment={"FAKE_CLOUD_TOKEN": "test-secret", "OPENAI_API_KEY": "test-key",
                         "CODEX_SQLITE_HOME": "/personal-state"}))
        root = self.project / ".data" / "codex-readonly"
        workspace = self.directory / "Projects" / "rere"
        self.assertEqual(data["cwd"], str(workspace))
        self.assertEqual(data["env"]["CODEX_HOME"], str(root / "home"))
        self.assertEqual(data["env"]["HOME"], str(root))
        self.assertEqual((root / "home" / "auth.json").resolve(), auth)
        self.assertIn("mcp_servers.unsafe", personal_config.read_text())
        for key in ("FAKE_CLOUD_TOKEN", "OPENAI_API_KEY", "CODEX_SQLITE_HOME"):
            self.assertIsNone(data["env"][key])
        config = tomllib.loads((root / "home" / "config.toml").read_text())
        self.assertNotIn("mcp_servers", config)
        self.assertEqual(config["approval_policy"], "never")
        self.assertTrue(config["features"]["shell_tool"])
        self.assertTrue(config["features"]["view_image"])
        self.assertFalse(config["features"]["stable_environment_tools"])
        profile = config["permissions"]["octopus-readonly"]
        self.assertEqual(profile["filesystem"], {":root": "read", ":minimal": "read", str(workspace): "read"})
        self.assertFalse(profile["network"]["enabled"])
        self.assertEqual((root / "home" / "config.toml").stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(data["argv"]), 7)
        self.assertEqual(data["env"]["HTTP_PROXY"], "http://127.0.0.1:7890")

    def test_readonly_fails_closed_for_unverified_version_or_missing_auth(self):
        self.prepare_readonly()
        result = self.run_host("CODEX_READ_ONLY=true", environment={"TEST_CODEX_VERSION": "codex-cli 9.0.0"})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.capture.exists())
        (self.directory / ".codex" / "auth.json").unlink()
        result = self.run_host("CODEX_READ_ONLY=true")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.capture.exists())

    def test_readonly_accepts_source_workspace_but_rejects_symlinked_state(self):
        self.prepare_readonly()
        workspace = self.directory / "source with spaces"
        workspace.mkdir()
        (workspace / "service.py").write_text("source = True\n")
        data = self.captured(self.run_host(f'CODEX_READ_ONLY=true\nCODEX_WORKSPACE="{workspace}"\n'))
        self.assertEqual(data["cwd"], str(workspace))
        self.capture.unlink()
        shutil.rmtree(self.project / ".data")
        elsewhere = self.directory / "elsewhere"
        elsewhere.mkdir()
        (self.project / ".data").symlink_to(elsewhere, target_is_directory=True)
        self.assertNotEqual(self.run_host("CODEX_READ_ONLY=true").returncode, 0)
        self.assertFalse(self.capture.exists())
        self.assertFalse(any(elsewhere.iterdir()))

    def test_readonly_workspace_override_is_validated_without_shell_expansion(self):
        self.prepare_readonly()
        for workspace in ("relative/path", str(self.directory / "missing"), "$(touch marker)"):
            with self.subTest(workspace=workspace):
                result = self.run_host("CODEX_READ_ONLY=true", environment={"CODEX_WORKSPACE": workspace})
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.capture.exists())
        selected = self.directory / "selected"
        selected.mkdir()
        data = self.captured(self.run_host('CODEX_READ_ONLY=true\nCODEX_WORKSPACE="/missing"\n',
                                         environment={"CODEX_WORKSPACE": str(selected)}))
        self.assertEqual(data["cwd"], str(selected))

    def test_readonly_toggle_requires_boolean(self):
        for value in ("", "1", "yes", "true;echo test"):
            with self.subTest(value=value):
                self.assertNotEqual(self.run_host(environment={"CODEX_READ_ONLY": value}).returncode, 0)
                self.assertFalse(self.capture.exists())


if __name__ == "__main__":
    unittest.main()
