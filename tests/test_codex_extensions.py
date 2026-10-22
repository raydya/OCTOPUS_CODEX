from __future__ import annotations

from copy import deepcopy
import tomllib
import unittest

from octopus.codex_extensions import (
    normalize_mcp_servers,
    toml_dotted_assignments,
    validate_readonly_mcp_servers,
)


class ReadonlyMcpConfigurationTests(unittest.TestCase):
    def test_transport_credentials_and_user_tool_selection_are_preserved_without_mutation(self):
        raw = {"metrics": {
            "url": "https://example.invalid/mcp",
            "http_headers": {"Authorization": "Bearer synthetic-secret"},
            "env_http_headers": {"X-Token": "TOKEN_ENV"},
            "enabled": True,
            "enabled_tools": ["query", "describe"],
            "disabled_tools": ["delete"],
            "startup_timeout_sec": 15.5,
            "default_tools_approval_mode": "approve",
            "tools": {"query": {"approval_mode": "approve", "output_token_limit": 1234}},
        }}
        original = deepcopy(raw)
        normalized = normalize_mcp_servers(raw)
        self.assertEqual(raw, original)
        expected = deepcopy(raw)
        expected["metrics"]["default_tools_approval_mode"] = "writes"
        expected["metrics"]["tools"]["query"]["approval_mode"] = "writes"
        self.assertEqual(normalized, expected)
        self.assertTrue(validate_readonly_mcp_servers(normalized))
        normalized["metrics"]["http_headers"]["Authorization"] = "changed"
        self.assertEqual(raw, original)

    def test_explicit_server_and_tool_approval_bypasses_are_removed(self):
        normalized = normalize_mcp_servers({"docs": {
            "url": "https://example.invalid/mcp", "approval_mode": "approve",
            "tools": {"read": {"approval_mode": "approve", "default_tools_approval_mode": "approve"}},
        }})
        self.assertNotIn("approval_mode", normalized["docs"])
        self.assertNotIn("default_tools_approval_mode", normalized["docs"]["tools"]["read"])
        self.assertEqual(normalized["docs"]["tools"]["read"]["approval_mode"], "writes")
        self.assertTrue(validate_readonly_mcp_servers(normalized))

    def test_unknown_approval_override_is_rejected_without_disclosing_its_value(self):
        for raw in ({"docs": {"auto_approve": "synthetic-private-token"}},
                    {"docs": {"tools": {"read": {"approval_policy": "synthetic-private-token"}}}}):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as error:
                    normalize_mcp_servers(raw)
                self.assertNotIn("synthetic-private-token", str(error.exception))
                self.assertFalse(validate_readonly_mcp_servers(raw))

    def test_generic_execution_and_computer_browser_servers_are_disabled(self):
        cases = {
            "node_repl": {"command": "/somewhere/node_repl", "enabled": True},
            "python-repl": {"command": "uvx", "args": ["mcp-python-repl"]},
            "computer-use": {"command": "/app/SkyComputerUseClient", "args": ["mcp"]},
            "innocent-name": {"command": "npx", "args": ["@playwright/mcp"]},
            "shell": {"url": "https://example.invalid/mcp"},
            "code-execution": {"command": "safe-looking", "env": {"TOKEN": "synthetic"}},
            "shell-wrapper": {"command": "/bin/bash", "args": ["-lc", "some-fixed-server"]},
            "inline-runtime": {"command": "python3", "args": ["-c", "some_fixed_server()"]},
        }
        normalized = normalize_mcp_servers(cases)
        for name, server in normalized.items():
            self.assertIs(server["enabled"], False, name)
            self.assertTrue(validate_readonly_mcp_servers({name: server}), name)
            self.assertFalse(validate_readonly_mcp_servers({name: {**server, "enabled": True}}), name)
        self.assertEqual(normalized["code-execution"]["env"], cases["code-execution"]["env"])

    def test_ordinary_python_mcp_transport_is_not_confused_with_python_execution_tool(self):
        result = normalize_mcp_servers({"docs": {"command": "python", "args": ["-m", "docs_mcp"]}})
        self.assertNotIn("enabled", result["docs"])
        self.assertTrue(validate_readonly_mcp_servers(result))

    def test_grafana_direct_and_local_uvx_transport_force_disable_write(self):
        for name, command, args in (
            ("grafana", "uvx", ["mcp-grafana"]),
            ("metrics", "/usr/bin/mcp-grafana", []),
            ("metrics", "/usr/bin/uvx", ["mcp-grafana", "--transport", "stdio"]),
        ):
            with self.subTest(command=command, args=args):
                result = normalize_mcp_servers({name: {"command": command, "args": args}})
                self.assertEqual(result[name]["args"], [*args, "--disable-write"])
                self.assertTrue(validate_readonly_mcp_servers(result))
                self.assertEqual(normalize_mcp_servers(result), result)

    def test_grafana_conflicting_flags_and_values_are_removed(self):
        for conflicting in (
            ["--enable-query"], ["--enable-query=true"], ["--enable-query", "false"],
            ["--enable-write-tools", "create_dashboard,delete_dashboard"],
            ["--enable-write-tools=create_dashboard"], ["--enable-write-tools"],
            ["--disable-write=false"], ["--disable-write", "false"],
            ["--disable-write", "--disable-write"],
        ):
            with self.subTest(conflicting=conflicting):
                result = normalize_mcp_servers({"grafana": {
                    "command": "uvx", "args": ["mcp-grafana", *conflicting, "--transport", "stdio"],
                }})
                self.assertEqual(result["grafana"]["args"], ["mcp-grafana", "--transport", "stdio", "--disable-write"])
                self.assertTrue(validate_readonly_mcp_servers(result))

    def test_ambiguous_grafana_argv_and_new_write_toggles_fail_closed(self):
        for args in (
            ["--", "--disable-write"], ["--enable-query", "an-unknown-positional-value"],
            ["--enable-query=all"], ["--disable-write=maybe"],
            ["--allow-write"], ["--disable-writes=false"], ["--enable-write-tools="],
        ):
            with self.subTest(args=args):
                raw = {"grafana": {"command": "mcp-grafana", "args": args}}
                with self.assertRaises(ValueError):
                    normalize_mcp_servers(raw)
                self.assertFalse(validate_readonly_mcp_servers(raw))

    def test_unrecognised_grafana_wrapper_cannot_be_enabled(self):
        for server in (
            {"command": "uvx", "args": ["--from", "package", "mcp-grafana"]},
            {"command": "/local/wrapper", "args": []},
            {"url": "https://example.invalid/mcp"},
        ):
            with self.subTest(server=server):
                with self.assertRaises(ValueError):
                    normalize_mcp_servers({"grafana": server})
                disabled = normalize_mcp_servers({"grafana": {**server, "enabled": False}})
                self.assertTrue(validate_readonly_mcp_servers(disabled))

    def test_renaming_grafana_server_does_not_bypass_command_safeguard(self):
        for args in (["--from", "mcp-grafana", "mcp-grafana"], ["mcp-grafana@latest"]):
            with self.assertRaises(ValueError):
                normalize_mcp_servers({"custom-metrics": {"command": "uvx", "args": args}})

    def test_grafana_attestation_rejects_missing_or_overridden_disable_write(self):
        for args in ([], ["--disable-write=false"], ["--disable-write", "--enable-write-tools=all"]):
            self.assertFalse(validate_readonly_mcp_servers({"grafana": {
                "command": "mcp-grafana", "args": args, "default_tools_approval_mode": "writes",
            }}))

    def test_effective_config_defaults_can_be_null_without_weakening_approval(self):
        raw = {"docs": {
            "url": "https://example.invalid/mcp", "command": None, "args": None,
            "enabled": None, "enabled_tools": None, "disabled_tools": None,
            "default_tools_approval_mode": "writes",
            "tools": {"read": {"approval_mode": None, "output_token_limit": None}},
        }}
        self.assertTrue(validate_readonly_mcp_servers(raw))
        self.assertEqual(normalize_mcp_servers(raw), {"docs": {
            "url": "https://example.invalid/mcp", "default_tools_approval_mode": "writes",
            "tools": {"read": {"approval_mode": "writes"}},
        }})

    def test_null_tools_defaults_and_absent_servers_are_allowed(self):
        self.assertEqual(normalize_mcp_servers(None), {})
        self.assertTrue(validate_readonly_mcp_servers(None))
        self.assertTrue(validate_readonly_mcp_servers({}))
        self.assertTrue(validate_readonly_mcp_servers({"docs": {
            "url": "https://example.invalid/mcp", "default_tools_approval_mode": "writes", "tools": None,
        }}))

    def test_attestation_requires_effective_writes_including_disabled_servers(self):
        for mode in (None, "approve", "prompt"):
            for enabled in (True, False):
                self.assertFalse(validate_readonly_mcp_servers({"docs": {
                    "enabled": enabled, "default_tools_approval_mode": mode,
                }}))
        for mode in ("approve", "prompt"):
            self.assertFalse(validate_readonly_mcp_servers({"docs": {
                "default_tools_approval_mode": "writes", "tools": {"read": {"approval_mode": mode}},
            }}))

    def test_malformed_config_is_rejected(self):
        for raw in ([], "not-a-map", {"": {}}, {"x": []}, {"x": {"enabled": "false"}},
                    {"x": {"args": "--arg"}}, {"x": {"args": [1]}},
                    {"x": {"command": 12}},
                    {"x": {"tools": []}}, {"x": {"tools": {"read": "approve"}}},
                    {"x": {"startup_timeout_sec": float("nan")}}):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    normalize_mcp_servers(raw)
                self.assertFalse(validate_readonly_mcp_servers(raw))


class TomlAssignmentTests(unittest.TestCase):
    def test_round_trip_quotes_keys_transport_credentials_and_nested_arrays(self):
        value = {"mcp_servers": {
            'docs.with.dot"and-quote': {
                "command": "/path/with spaces/reader",
                "args": ["--path", "$(no-shell-expansion)`nor-this`\\\n\t中文🐙"],
                "env": {"TOKEN": "synthetic\"token", "EMPTY": ""},
                "enabled": False,
                "startup_timeout_sec": 12.5,
                "tools": {"read.files": {"approval_mode": "writes", "output_token_limit": 1200}},
                "empty": {}, "empty_list": [],
                "records": [{"x.y": "one", "nested": [True, 12, {"empty": {}}]}],
            },
        }}
        assignments = toml_dotted_assignments(value)
        self.assertEqual(tomllib.loads("\n".join(assignments)), value)

    def test_prefix_and_nullable_defaults_are_serialized_as_config(self):
        data = {"reader": {"url": "https://example.invalid/mcp", "args": None}}
        result = toml_dotted_assignments(data, ("mcp_servers",))
        self.assertEqual(tomllib.loads("\n".join(result)), {
            "mcp_servers": {"reader": {"url": "https://example.invalid/mcp"}},
        })

    def test_nonfinite_values_and_null_array_elements_are_rejected(self):
        for data in ({"x": float("inf")}, {"x": [None]}, {"x": object()}, {"x": "\ud800"}):
            with self.assertRaises(ValueError):
                toml_dotted_assignments(data)


if __name__ == "__main__":
    unittest.main()
