"""Copy MCP settings into the bot's config without granting write approval.

MCP processes run outside Codex's command sandbox.  The approval policy is
therefore required independently of the native filesystem permission profile.
This module never reads or writes the user's configuration or credentials.
"""

from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import PurePath
import re
from typing import Any


_APPROVAL_KEYS = {"approval_mode", "default_tools_approval_mode"}
_DANGEROUS_COMPONENTS = re.compile(
    r"(?:^|[^a-z0-9])(?:node[_-]?repl|python[_-]?repl|code[_-]?(?:executor|execution)|"
    r"execute[_-]?code|computer[_-]?use|browser[_-]?use|playwright|puppeteer|selenium|"
    r"terminal|shell|cua)(?:$|[^a-z0-9])"
)


def _error() -> ValueError:
    # Config values can contain tokens, passwords, and credential-bearing URLs.
    # Never echo a rejected value in diagnostics.
    return ValueError("MCP configuration cannot enforce the bot's read-only policy")


def _clean(value: Any) -> Any:
    """Copy JSON/TOML config data, removing nullable config/read defaults."""
    if isinstance(value, dict):
        if any(not isinstance(key, str) or not key for key in value):
            raise _error()
        for key in value:
            _clean(key)
        return {key: _clean(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        if any(item is None for item in value):
            raise _error()
        return [_clean(item) for item in value]
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise _error() from None
        return value
    if isinstance(value, (bool, int)) or (isinstance(value, float) and math.isfinite(value)):
        return deepcopy(value)
    raise _error()


def _check_approval_keys(config: dict[str, Any]) -> None:
    for key in config:
        # Reject unknown future approval overrides rather than silently copying
        # a setting whose precedence we do not understand.  Transport/env
        # dictionaries are intentionally not examined as policy settings.
        if ("approval" in key.lower() or "approve" in key.lower()) and key not in _APPROVAL_KEYS:
            raise _error()


def _execution_server(name: str, server: dict[str, Any]) -> bool:
    values = [name, server.get("command", ""), server.get("url", "")]
    values.extend(server.get("args", []))
    if any(not isinstance(value, str) for value in values):
        raise _error()
    if name.lower().replace("-", "_") in {"exec", "execute", "node", "python", "code"}:
        return True
    executable = PurePath(server.get("command", "")).name.lower()
    if executable in {"sh", "bash", "zsh", "fish", "pwsh", "powershell", "cmd.exe"}:
        return True
    if (executable in {"python", "python3", "node", "nodejs", "deno", "bun"}
            and any(arg in {"-c", "-e", "--eval", "eval"} for arg in server.get("args", []))):
        return True
    for value in values:
        lowered = value.lower()
        if _DANGEROUS_COMPONENTS.search(lowered) or "skycomputeruseclient" in lowered:
            return True
    return False


def _grafana_args(name: str, server: dict[str, Any]) -> list[str] | None:
    command = server.get("command", "")
    args = server.get("args", [])
    if not isinstance(command, str) or not isinstance(args, list) or any(not isinstance(a, str) for a in args):
        raise _error()
    executable = PurePath(command).name
    direct = executable == "mcp-grafana"
    uvx = executable == "uvx" and bool(args) and args[0] == "mcp-grafana"
    grafana = (name.lower() == "grafana" or direct or uvx
               or any(re.search(r"(?:^|[/@\s])mcp-grafana(?:$|[/@\s=:])", arg) for arg in args))
    if not grafana:
        return None
    if not (direct or uvx):
        # A shell script, remote service, or unrecognised wrapper cannot be made
        # read-only by appending flags to an unknown command line.
        if server.get("enabled") is False:
            return None
        raise _error()

    prefix = ["mcp-grafana"] if uvx else []
    source = args[1:] if uvx else args
    result: list[str] = []
    index = 0
    while index < len(source):
        arg = source[index]
        if arg == "--":
            # Flags after an option terminator are positional, not safeguards.
            raise _error()
        option, equals, value = arg.partition("=")
        if option in {"--disable-write", "--enable-query", "--enable-write-tools"}:
            if equals:
                if option in {"--disable-write", "--enable-query"} and value.lower() not in {"true", "false"}:
                    raise _error()
                if not value:
                    raise _error()
            elif index + 1 < len(source) and not source[index + 1].startswith("-"):
                # These switches may carry explicit values.  Booleans are
                # unambiguous; a non-boolean value is supported only for the
                # enable-write-tools list, never guessed for boolean flags.
                following = source[index + 1]
                if option != "--enable-write-tools" and following.lower() not in {"true", "false"}:
                    raise _error()
                index += 1
            index += 1
            continue
        # Preserve known unrelated options exactly, but reject similarly named
        # write toggles so a future alias cannot silently override the safeguard.
        if arg.startswith("-") and ("write" in option.lower() or "enable-query" in option.lower()):
            raise _error()
        result.append(arg)
        index += 1
    return [*prefix, *result, "--disable-write"]


def normalize_mcp_servers(raw: Any) -> dict[str, Any]:
    """Return an independent MCP config with write approval always required.

    Explicit enable/disable lists and transport settings remain intact.  Known
    arbitrary execution and computer/browser MCP servers remain configured but
    disabled because they can bypass native command sandboxing.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise _error()
    servers = _clean(raw)
    for name, server in servers.items():
        if not isinstance(server, dict):
            raise _error()
        _check_approval_keys(server)
        if "enabled" in server and not isinstance(server["enabled"], bool):
            raise _error()
        if "args" in server and (not isinstance(server["args"], list)
                                 or any(not isinstance(arg, str) for arg in server["args"])):
            raise _error()
        server["default_tools_approval_mode"] = "writes"
        # There is no server-wide approval_mode in the pinned schema.  Do not
        # carry an explicit approve through a future schema change.
        server.pop("approval_mode", None)
        rules = server.get("tools", {})
        if not isinstance(rules, dict):
            raise _error()
        for tool, rule in rules.items():
            if not isinstance(rule, dict):
                raise _error()
            _check_approval_keys(rule)
            rule.pop("default_tools_approval_mode", None)
            rule["approval_mode"] = "writes"
        if _execution_server(name, server):
            server["enabled"] = False
        grafana_args = _grafana_args(name, server)
        if grafana_args is not None:
            server["args"] = grafana_args
    return servers


def validate_readonly_mcp_servers(raw: Any) -> bool:
    """Validate effective config/read data, including nullable default fields."""
    try:
        normalized = normalize_mcp_servers(raw)
        if raw is None:
            return True
        for name, expected in normalized.items():
            actual = raw[name]
            if actual.get("default_tools_approval_mode") != "writes":
                return False
            if actual.get("approval_mode") is not None:
                return False
            for rule in (actual.get("tools") or {}).values():
                # An absent/null override inherits the required server default.
                if rule.get("approval_mode") not in {None, "writes"}:
                    return False
                if rule.get("default_tools_approval_mode") is not None:
                    return False
            if expected.get("enabled") is False and actual.get("enabled") is not False:
                return False
            if _grafana_args(name, expected) is not None and actual.get("args") != expected.get("args"):
                return False
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def _toml_value(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) or (isinstance(value, float) and math.isfinite(value)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(key, ensure_ascii=False)} = {_toml_value(item)}"
                                 for key, item in value.items()) + "}"
    raise _error()


def toml_dotted_assignments(data: dict[str, Any], prefix: tuple[str, ...] = ()) -> list[str]:
    """Serialize nested config into safely quoted TOML dotted assignments.

    Returns data only: callers decide whether to write a private config file or
    pass each assignment as an argument.  Neither shell escaping nor logging is
    performed here.
    """
    if not isinstance(data, dict) or any(not isinstance(part, str) or not part for part in prefix):
        raise _error()
    for part in prefix:
        _clean(part)
    cleaned = _clean(data)
    result: list[str] = []
    for key, value in cleaned.items():
        path = (*prefix, key)
        if isinstance(value, dict) and value:
            result.extend(toml_dotted_assignments(value, path))
        else:
            result.append(".".join(json.dumps(part, ensure_ascii=False) for part in path) + " = " + _toml_value(value))
    return result
