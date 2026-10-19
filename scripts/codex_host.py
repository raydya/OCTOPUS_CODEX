"""Launch the host Codex server with project-local settings, without shell eval."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import shutil
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
        match = re.match(r"^\s*(?:export\s+)?(CODEX_PORT|CODEX_PROXY_URL)\s*=\s*(.*)$", line)
        if match:
            # Parse only the two host settings; never source .env as shell code.
            values = shlex.split(match[2], comments=True, posix=True)
            if len(values) > 1:
                raise ValueError(f"{match[1]} must contain one value")
            settings[match[1]] = values[0] if values else ""
    return settings


def launch() -> None:
    settings = project_settings()
    env = os.environ.copy()
    port = env.get("CODEX_PORT", settings.get("CODEX_PORT", "4500")) or "4500"
    if not re.fullmatch(r"[0-9]+", port) or not 1 <= int(port) <= 65535:
        raise ValueError("CODEX_PORT must be an integer between 1 and 65535")
    token_path = PROJECT_ROOT / ".secrets" / "codex_token"
    if not token_path.is_file() or not token_path.stat().st_size:
        raise ValueError("Missing Codex authentication token. Run scripts/bootstrap.sh first")
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
    os.execvpe("codex", [
        "codex", "app-server", "--listen", f"ws://127.0.0.1:{int(port)}",
        "--ws-auth", "capability-token", "--ws-token-file", str(token_path),
    ], env)


if __name__ == "__main__":
    try:
        launch()
    except (ValueError, OSError) as exc:
        # A parsing/OS error can include configuration values; do not echo them.
        print(f"Cannot launch Codex host ({type(exc).__name__}); check CODEX_PORT, "
              "CODEX_PROXY_URL, the token file, and codex on PATH.", file=sys.stderr)
        sys.exit(1)
