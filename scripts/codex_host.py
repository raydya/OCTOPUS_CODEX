"""Launch the host Codex server with project-local settings, without shell eval."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "host.docker.internal")


def project_settings() -> dict[str, str]:
    path = PROJECT_ROOT / ".env"
    if not path.is_file():
        return {}
    settings: dict[str, str] = {}
    for line in path.read_text().splitlines():
        match = re.match(
            r"^\s*(?:export\s+)?(CODEX_PORT|CODEX_PROXY_URL|CODEX_DIAGNOSTICS_ENABLED|CODEX_READ_ONLY|CODEX_WORKSPACE)\s*=\s*(.*)$",
            line,
        )
        if match:
            # Parse only host settings; never source .env as shell code.
            values = shlex.split(match[2], comments=True, posix=True)
            if len(values) > 1:
                raise ValueError(f"{match[1]} must contain one value")
            settings[match[1]] = values[0] if values else ""
    return settings


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate diagnostics policy field")
        result[key] = value
    return result


def diagnostics_overrides(enabled: str) -> list[str]:
    """Translate a narrowly scoped policy into arguments for this process only."""
    setting = enabled.strip().lower()
    if setting == "false":
        return []
    if setting != "true":
        raise ValueError("CODEX_DIAGNOSTICS_ENABLED must be true or false")

    policy = json.loads(
        (PROJECT_ROOT / "config" / "codex-diagnostics.json").read_text(encoding="utf-8"),
        object_pairs_hook=_unique_json_object,
    )
    if not isinstance(policy, dict) or set(policy) != {"developer_instructions", "mcp_servers"}:
        raise ValueError("Invalid diagnostics policy fields")
    instructions = policy["developer_instructions"]
    servers = policy["mcp_servers"]
    if not isinstance(instructions, str) or not isinstance(servers, dict) or not servers:
        raise ValueError("Invalid diagnostics policy structure")
    # TOML accepts JSON string escapes, but also requires DEL to be escaped.
    # Reject isolated surrogate code points rather than passing invalid Unicode to exec.
    instructions.encode("utf-8")
    encoded_instructions = json.dumps(instructions, ensure_ascii=False).replace("\x7f", "\\u007f")
    overrides = ["-c", "developer_instructions=" + encoded_instructions]
    for server, tool_names in servers.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]+", server):
            raise ValueError("Invalid diagnostics server name")
        if not isinstance(tool_names, list) or not tool_names:
            raise ValueError("Invalid diagnostics tool list")
        if any(not isinstance(tool, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", tool)
               for tool in tool_names):
            raise ValueError("Invalid diagnostics tool name")
        if len(set(tool_names)) != len(tool_names):
            raise ValueError("Duplicate diagnostics tool name")
        prefix = f"mcp_servers.{server}"
        overrides.extend([
            "-c", f"{prefix}.enabled_tools=" + json.dumps(tool_names),
            "-c", f'{prefix}.default_tools_approval_mode="prompt"',
        ])
        for tool in tool_names:
            overrides.extend(["-c", f'{prefix}.tools.{tool}.approval_mode="approve"'])
    return overrides


def readonly_environment(env: dict[str, str]) -> dict[str, str]:
    """Create a private configuration root without inheriting personal capabilities.

    Only the existing host auth.json is reused, without inheriting personal MCP
    or plugin configuration. Native tools can read host files; the permission
    profile denies filesystem writes and outbound command traffic.
    """
    executable = shutil.which("codex", path=env.get("PATH"))
    version = subprocess.run([executable, "--version"], capture_output=True, text=True,
                             timeout=10, check=True)
    if version.stdout.strip() != "codex-cli 0.163.0":
        raise ValueError("Read-only runtime requires the verified Codex version")
    source_home = Path(env.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()
    auth = source_home / "auth.json"
    if not auth.is_file() or auth.is_symlink():
        raise ValueError("Read-only runtime requires an existing regular auth.json")
    root = PROJECT_ROOT / ".data" / "codex-readonly"
    home = root / "home"
    cwd = Path(env.get("CODEX_WORKSPACE") or Path.home() / "Projects" / "rere").expanduser()
    if not cwd.is_absolute() or not cwd.is_dir():
        raise ValueError("Read-only runtime requires an existing absolute source workspace")
    cwd = cwd.resolve()
    for directory in (PROJECT_ROOT / ".data", root, home):
        if directory.is_symlink():
            raise ValueError("Read-only runtime directories must not be symlinks")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
    link = home / "auth.json"
    if link.is_symlink():
        if link.resolve() != auth:
            raise ValueError("Unexpected read-only authentication target")
    elif link.exists():
        raise ValueError("Unexpected read-only authentication file")
    else:
        link.symlink_to(auth)
    policy = json.loads((PROJECT_ROOT / "config" / "codex-readonly.json").read_text(),
                        object_pairs_hook=_unique_json_object)
    if not isinstance(policy, dict) or not all(
        isinstance(key, str) and isinstance(value, (str, bool, int))
        for key, value in policy.items()
    ):
        raise ValueError("Invalid read-only policy")
    policy['permissions.octopus-readonly.filesystem.' + json.dumps(str(cwd.resolve()))] = "read"
    config = home / "config.toml"
    if config.is_symlink():
        raise ValueError("Read-only configuration must not be a symlink")
    encoded = "\n".join(key + " = " + json.dumps(value, ensure_ascii=False)
                        for key, value in policy.items()) + "\n"
    config.write_text(encoded, encoding="utf-8")
    config.chmod(0o600)
    # No ambient API keys, MCP variables, shell startup settings or personal
    # CODEX_* overrides survive into the model runtime.
    keep = {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR",
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
            "http_proxy", "https_proxy", "all_proxy", "no_proxy"}
    clean = {key: value for key, value in env.items() if key in keep}
    clean["CODEX_HOME"] = str(home.resolve())
    clean["HOME"] = str(root.resolve())
    os.chdir(cwd)
    return clean


def launch() -> None:
    settings = project_settings()
    env = os.environ.copy()
    port = env.get("CODEX_PORT", settings.get("CODEX_PORT", "4500")) or "4500"
    if not re.fullmatch(r"[0-9]+", port) or not 1 <= int(port) <= 65535:
        raise ValueError("CODEX_PORT must be an integer between 1 and 65535")
    token_path = PROJECT_ROOT / ".secrets" / "codex_token"
    if not token_path.is_file() or not token_path.stat().st_size:
        raise ValueError("Missing Codex authentication token. Run scripts/bootstrap.sh first")
    read_only = env.get("CODEX_READ_ONLY", settings.get("CODEX_READ_ONLY", "false")).lower().strip()
    if read_only not in {"true", "false"}:
        raise ValueError("CODEX_READ_ONLY must be true or false")
    overrides = [] if read_only == "true" else diagnostics_overrides(env.get(
        "CODEX_DIAGNOSTICS_ENABLED", settings.get("CODEX_DIAGNOSTICS_ENABLED", "false"),
    ))
    if not shutil.which("codex", path=env.get("PATH")):
        raise ValueError("The codex executable is not available on PATH")

    proxy = env.get("CODEX_PROXY_URL", settings.get("CODEX_PROXY_URL", ""))
    if proxy:
        try:
            parsed = urlsplit(proxy)
            valid = (
                parsed.scheme in {"http", "https", "socks5", "socks5h"}
                and bool(parsed.hostname)
                and (parsed.port is None or 1 <= parsed.port <= 65535)
                and parsed.path in {"", "/"}
                and not parsed.query and not parsed.fragment
                and not any(c.isspace() for c in proxy)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("CODEX_PROXY_URL must be a valid HTTP(S) or SOCKS5 proxy URL")
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            env[name] = env[name.lower()] = proxy

    bypass: list[str] = []
    for entry in (env.get("NO_PROXY", "") + "," + env.get("no_proxy", "")).split(",") + list(LOCAL_HOSTS):
        entry = entry.strip()
        if entry and entry not in bypass:
            bypass.append(entry)
    env["NO_PROXY"] = env["no_proxy"] = ",".join(bypass)
    if proxy:
        print("Codex host: project proxy enabled; local connections bypass proxy.", flush=True)
    if read_only == "true":
        env["CODEX_WORKSPACE"] = env.get("CODEX_WORKSPACE", settings.get("CODEX_WORKSPACE", ""))
        env = readonly_environment(env)
        print("Codex host: native read-only tools enabled with isolated configuration.", flush=True)
    os.execvpe("codex", [
        "codex", "app-server", "--listen", f"ws://127.0.0.1:{int(port)}",
        "--ws-auth", "capability-token", "--ws-token-file", str(token_path),
        *overrides,
    ], env)


if __name__ == "__main__":
    try:
        launch()
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        # A parsing/OS error can include configuration values; do not echo them.
        print(f"Cannot launch Codex host ({type(exc).__name__}); check CODEX_PORT, "
              "CODEX_PROXY_URL, CODEX_DIAGNOSTICS_ENABLED, CODEX_READ_ONLY, the policies, "
              "the token file, and codex on PATH.", file=sys.stderr)
        sys.exit(1)
