from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlparse


class ConfigurationError(ValueError):
    pass


def _secret(name: str) -> str:
    path = os.environ.get(f"{name}_FILE", "").strip()
    if path:
        try:
            return Path(path).read_text().strip()
        except OSError as exc:
            raise ConfigurationError(f"无法读取 {name}_FILE 指定的文件") from exc
    return os.environ.get(name, "").strip()


def _integer(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ConfigurationError(f"{name} 必须为整数") from exc
    if not low <= value <= high:
        raise ConfigurationError(f"{name} 必须在 {low}–{high} 之间")
    return value


def database_url_from_env() -> str:
    explicit = os.environ.get("DATABASE_URL", "").strip()
    if explicit:
        return explicit
    password = _secret("POSTGRES_PASSWORD")
    if not password:
        raise ConfigurationError("请设置 DATABASE_URL 或 POSTGRES_PASSWORD_FILE")
    user = quote(os.environ.get("POSTGRES_USER", "octopus"), safe="")
    database = quote(os.environ.get("POSTGRES_DB", "octopus"), safe="")
    host = os.environ.get("POSTGRES_HOST", "127.0.0.1")
    port = _integer("POSTGRES_PORT", 5432, 1, 65535)
    return f"postgresql://{user}:{quote(password, safe='')}@{host}:{port}/{database}"


@dataclass(frozen=True)
class Config:
    app_id: str
    app_secret: str
    database_url: str
    workspace: str
    codex_url: str | None
    codex_token_file: str | None
    codex_executable: str = "codex"
    model: str | None = None
    allowed_users: tuple[str, ...] = ()
    allow_groups: bool = False
    bot_open_id: str | None = None
    max_concurrent: int = 1
    task_timeout: int = 7200
    approval_timeout: int = 900
    progress_interval: int = 2

    @classmethod
    def from_env(cls) -> Config:
        app_id = os.environ.get("FEISHU_APP_ID", "").strip()
        app_secret = _secret("FEISHU_APP_SECRET")
        missing = [name for name, value in (("FEISHU_APP_ID", app_id), ("FEISHU_APP_SECRET", app_secret))
                   if not value or value.startswith("REPLACE_")]
        if missing:
            raise ConfigurationError("尚未配置：" + ", ".join(missing))
        if not app_id.startswith("cli_"):
            raise ConfigurationError("FEISHU_APP_ID 应为新应用的 cli_ 开头的 App ID")
        database_url = database_url_from_env()
        workspace = os.environ.get("CODEX_WORKSPACE", "").strip()
        if not workspace or not workspace.startswith("/"):
            raise ConfigurationError("CODEX_WORKSPACE 必须是宿主机项目的绝对路径")
        url = os.environ.get("CODEX_URL", "").strip() or None
        token_file = os.environ.get("CODEX_TOKEN_FILE", "").strip() or None
        if url:
            parsed = urlparse(url)
            if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
                raise ConfigurationError("CODEX_URL 必须是 ws:// 或 wss:// 地址")
            if parsed.username or parsed.password:
                raise ConfigurationError("CODEX_URL 不可包含凭证，请使用 CODEX_TOKEN_FILE")
            local_hosts = {"127.0.0.1", "localhost", "::1", "host.docker.internal"}
            if parsed.scheme == "ws" and parsed.hostname not in local_hosts:
                raise ConfigurationError("远程 Codex 必须使用 wss://；ws:// 仅用于本机或 Docker 宿主机")
            if not token_file or not Path(token_file).is_file():
                raise ConfigurationError("CODEX_TOKEN_FILE 必须指向可读的认证令牌文件")
            try:
                if len(Path(token_file).read_text().strip()) < 32:
                    raise ConfigurationError("Codex 认证令牌至少需要 32 个字符")
            except OSError as exc:
                raise ConfigurationError("无法读取 Codex 认证令牌文件") from exc
        elif not Path(workspace).is_dir():
            raise ConfigurationError("本地模式的 CODEX_WORKSPACE 目录不存在")
        allow_groups = os.environ.get("FEISHU_ALLOW_GROUPS", "false").lower() == "true"
        bot_open_id = os.environ.get("FEISHU_BOT_OPEN_ID", "").strip() or None
        if allow_groups and (not bot_open_id or not bot_open_id.startswith("ou_")):
            raise ConfigurationError("启用群聊需设置新应用机器人自己的 FEISHU_BOT_OPEN_ID，以精确识别 @机器人")
        return cls(
            app_id=app_id, app_secret=app_secret, database_url=database_url, workspace=workspace,
            codex_url=url, codex_token_file=token_file,
            codex_executable=os.environ.get("CODEX_EXECUTABLE", "codex"),
            model=os.environ.get("CODEX_MODEL", "").strip() or None,
            allowed_users=tuple(x.strip() for x in os.environ.get("FEISHU_ALLOWED_USERS", "").split(",") if x.strip()),
            allow_groups=allow_groups, bot_open_id=bot_open_id,
            max_concurrent=_integer("MAX_CONCURRENT_TASKS", 1, 1, 8),
            task_timeout=_integer("TASK_TIMEOUT_SECONDS", 7200, 30, 86400),
            approval_timeout=_integer("APPROVAL_TIMEOUT_SECONDS", 900, 30, 3600),
            progress_interval=_integer("PROGRESS_INTERVAL_SECONDS", 2, 1, 30),
        )
