from __future__ import annotations

import asyncio
import re
import time
from dataclasses import asdict, dataclass
from typing import Any, Literal

from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .contracts import IncomingMessage


@dataclass(frozen=True)
class Session:
    key: str
    chat_id: str
    sender_id: str
    thread_id: str | None
    blocked_reason: str | None = None


@dataclass(frozen=True)
class Job:
    id: str
    session_key: str
    message: IncomingMessage
    state: Literal["queued", "running", "done", "failed"]
    error: str | None


INTERRUPTED_ERROR = "服务重启，上一任务结果未确认；不会自动重跑"
CONTROL_INTERRUPTED_ERROR = "服务重启，控制命令结果未确认；不会自动重放，请重新发送"


def _now() -> int:
    return time.time_ns() // 1_000_000


class Store:
    def __init__(self, connection: AsyncConnection[dict[str, Any]], schema: str):
        self._db = connection
        self._schema = schema
        self._lock = asyncio.Lock()
        self._service_lock_acquired = False

    @classmethod
    async def open(cls, database_url: str, app_id: str, *, schema: str = "octopus") -> Store:
        if not app_id.strip():
            raise ValueError("飞书 App ID 不能为空")
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,62}", schema):
            raise ValueError("数据库 schema 必须是长度不超过 63 的安全标识符")
        connection = await AsyncConnection.connect(database_url, autocommit=True, row_factory=dict_row, connect_timeout=10)
        store = cls(connection, schema)
        try:
            async with store._lock:
                async with connection.transaction():
                    # Serialize migrations when two processes start together.
                    await connection.execute("SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(%s, 0))", (f"octopus:init:{schema}",))
                    await connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
                    await connection.execute(sql.SQL("SET search_path TO {}, pg_catalog").format(sql.Identifier(schema)))
                    await connection.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                    cursor = await connection.execute("SELECT value FROM meta WHERE key = 'app_id'")
                    bound = await cursor.fetchone()
                    if bound and bound["value"] != app_id:
                        raise ValueError("此数据库 schema 已绑定其他飞书应用，请为新应用配置独立数据库")
                    await connection.execute("INSERT INTO meta (key, value) VALUES ('app_id', %s) ON CONFLICT (key) DO NOTHING", (app_id,))
                    for statement in _SCHEMA:
                        await connection.execute(statement)
            return store
        except BaseException:
            await connection.close()
            raise

    async def acquire_service_lock(self) -> bool:
        """Keep one bridge active per schema until this connection is closed."""
        async with self._lock:
            if self._service_lock_acquired:
                return True
            cursor = await self._db.execute(
                "SELECT pg_catalog.pg_try_advisory_lock(pg_catalog.hashtextextended(%s, 0)) AS acquired",
                (f"octopus:service:{self._schema}",),
            )
            row = await cursor.fetchone()
            self._service_lock_acquired = bool(row and row["acquired"])
            return self._service_lock_acquired

    async def session(self, key: str, chat_id: str, sender_id: str) -> Session:
        async with self._lock:
            now = _now()
            await self._db.execute(
                "INSERT INTO sessions (key, chat_id, sender_id, created_at, updated_at) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (key) DO NOTHING",
                (key, chat_id, sender_id, now, now),
            )
            session = await self._get_session(key)
            assert session is not None
            if session.chat_id != chat_id or session.sender_id != sender_id:
                raise ValueError("会话键已绑定其他聊天或用户")
            return session

    async def _get_session(self, key: str) -> Session | None:
        cursor = await self._db.execute("SELECT key, chat_id, sender_id, thread_id, blocked_reason FROM sessions WHERE key = %s", (key,))
        row = await cursor.fetchone()
        return Session(**row) if row else None

    async def get_session(self, key: str) -> Session | None:
        async with self._lock:
            return await self._get_session(key)

    async def set_thread(self, key: str, thread_id: str | None) -> None:
        async with self._lock:
            cursor = await self._db.execute("UPDATE sessions SET thread_id = %s, updated_at = %s WHERE key = %s", (thread_id, _now(), key))
            if not cursor.rowcount:
                raise ValueError("会话不存在")

    async def set_blocked(self, key: str, reason: str | None) -> None:
        async with self._lock:
            cursor = await self._db.execute(
                "UPDATE sessions SET blocked_reason = %s, updated_at = %s WHERE key = %s",
                (reason, _now(), key),
            )
            if not cursor.rowcount:
                raise ValueError("会话不存在")

    async def blocked_session_keys(self) -> set[str]:
        async with self._lock:
            cursor = await self._db.execute("SELECT key FROM sessions WHERE blocked_reason IS NOT NULL")
            return {row["key"] for row in await cursor.fetchall()}

    async def enqueue(self, session_key: str, message: IncomingMessage) -> bool:
        async with self._lock:
            now = _now()
            cursor = await self._db.execute(
                """INSERT INTO jobs (id, session_key, message_json, state, created_at, updated_at)
                   VALUES (%s, %s, %s, 'queued', %s, %s) ON CONFLICT (id) DO NOTHING""",
                (message.id, session_key, Jsonb(asdict(message)), now, now),
            )
            return cursor.rowcount == 1

    async def next_jobs(self) -> list[Job]:
        async with self._lock:
            cursor = await self._db.execute("SELECT * FROM jobs WHERE state = 'queued' ORDER BY sequence")
            return [_job(row) for row in await cursor.fetchall()]

    async def start_job(self, id: str) -> bool:
        async with self._lock:
            cursor = await self._db.execute(
                """UPDATE jobs SET state = 'running', updated_at = %s WHERE id = %s AND state = 'queued'
                   AND NOT EXISTS (SELECT 1 FROM sessions WHERE sessions.key = jobs.session_key AND blocked_reason IS NOT NULL)""",
                (_now(), id),
            )
            return cursor.rowcount == 1

    async def finish_job(self, id: str, error: str | None = None) -> None:
        async with self._lock:
            # Late results must not override a terminal state assigned during recovery.
            await self._db.execute(
                "UPDATE jobs SET state = %s, error = %s, updated_at = %s WHERE id = %s AND state = 'running'",
                ("done" if error is None else "failed", error, _now(), id),
            )

    async def cancel_queued(self, session_key: str) -> int:
        async with self._lock:
            cursor = await self._db.execute(
                "UPDATE jobs SET state = 'failed', error = '任务已取消', updated_at = %s WHERE session_key = %s AND state = 'queued'",
                (_now(), session_key),
            )
            return cursor.rowcount

    async def recover_interrupted(self) -> list[Job]:
        async with self._lock:
            async with self._db.transaction():
                cursor = await self._db.execute(
                    "UPDATE jobs SET state = 'failed', error = %s, updated_at = %s WHERE state = 'running' RETURNING *",
                    (INTERRUPTED_ERROR, _now()),
                )
                rows = await cursor.fetchall()
                if rows:
                    await self._db.execute(
                        "UPDATE sessions SET blocked_reason = %s, updated_at = %s WHERE key = ANY(%s)",
                        (INTERRUPTED_ERROR, _now(), list({row["session_key"] for row in rows})),
                    )
                return [_job(row) for row in sorted(rows, key=lambda row: row["sequence"])]

    async def pending_count(self, session_key: str) -> int:
        async with self._lock:
            cursor = await self._db.execute("SELECT COUNT(*) AS count FROM jobs WHERE session_key = %s AND state = 'queued'", (session_key,))
            row = await cursor.fetchone()
            return row["count"] if row else 0

    async def save_result(self, session_key: str, text: str, status: str) -> None:
        async with self._lock:
            await self._db.execute(
                """INSERT INTO last_results (session_key, text, status, updated_at) VALUES (%s, %s, %s, %s)
                   ON CONFLICT (session_key) DO UPDATE SET text = excluded.text, status = excluded.status, updated_at = excluded.updated_at""",
                (session_key, text, status, _now()),
            )

    async def last_result(self, session_key: str) -> dict[str, str] | None:
        async with self._lock:
            cursor = await self._db.execute("SELECT text, status FROM last_results WHERE session_key = %s", (session_key,))
            row = await cursor.fetchone()
            return {"text": row["text"], "status": row["status"]} if row else None

    async def add_owner(self, open_id: str) -> None:
        async with self._lock:
            await self._db.execute("INSERT INTO owners (open_id, created_at) VALUES (%s, %s) ON CONFLICT (open_id) DO NOTHING", (open_id, _now()))

    async def owners(self) -> list[str]:
        async with self._lock:
            cursor = await self._db.execute("SELECT open_id FROM owners ORDER BY open_id")
            return [row["open_id"] for row in await cursor.fetchall()]

    async def claim_message(self, id: str) -> bool:
        async with self._lock:
            # Separate from jobs so a claimed inbound message can still be enqueued.
            cursor = await self._db.execute("INSERT INTO inbound_ids (id, created_at) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING", (id, _now()))
            return cursor.rowcount == 1

    async def enqueue_control(self, message: IncomingMessage) -> bool:
        async with self._lock:
            now = _now()
            cursor = await self._db.execute(
                """INSERT INTO controls (id, message_json, state, created_at, updated_at)
                   VALUES (%s, %s, 'queued', %s, %s) ON CONFLICT (id) DO NOTHING""",
                (message.id, Jsonb(asdict(message)), now, now),
            )
            return cursor.rowcount == 1

    async def next_controls(self) -> list[IncomingMessage]:
        async with self._lock:
            cursor = await self._db.execute("SELECT message_json FROM controls WHERE state = 'queued' ORDER BY sequence")
            return [IncomingMessage(**row["message_json"]) for row in await cursor.fetchall()]

    async def start_control(self, id: str) -> bool:
        async with self._lock:
            cursor = await self._db.execute(
                "UPDATE controls SET state = 'running', updated_at = %s WHERE id = %s AND state = 'queued'",
                (_now(), id),
            )
            return cursor.rowcount == 1

    async def finish_control(self, id: str, error: str | None = None) -> None:
        async with self._lock:
            await self._db.execute(
                "UPDATE controls SET state = %s, error = %s, updated_at = %s WHERE id = %s AND state = 'running'",
                ("done" if error is None else "failed", error, _now(), id),
            )

    async def recover_controls(self) -> list[IncomingMessage]:
        async with self._lock:
            async with self._db.transaction():
                cursor = await self._db.execute(
                    "UPDATE controls SET state = 'failed', error = %s, updated_at = %s WHERE state = 'running' RETURNING sequence, message_json",
                    (CONTROL_INTERRUPTED_ERROR, _now()),
                )
                rows = await cursor.fetchall()
                return [IncomingMessage(**row["message_json"]) for row in sorted(rows, key=lambda row: row["sequence"])]

    async def close(self) -> None:
        async with self._lock:
            await self._db.close()
            self._service_lock_acquired = False


def _job(row: dict[str, Any]) -> Job:
    return Job(row["id"], row["session_key"], IncomingMessage(**row["message_json"]), row["state"], row["error"])


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS sessions (
        key TEXT PRIMARY KEY,
        chat_id TEXT NOT NULL,
        sender_id TEXT NOT NULL,
        thread_id TEXT,
        created_at BIGINT NOT NULL,
        updated_at BIGINT NOT NULL
    )""",
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS blocked_reason TEXT",
    """CREATE TABLE IF NOT EXISTS jobs (
        sequence BIGSERIAL PRIMARY KEY,
        id TEXT NOT NULL UNIQUE,
        session_key TEXT NOT NULL REFERENCES sessions(key),
        message_json JSONB NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('queued', 'running', 'done', 'failed')),
        error TEXT,
        created_at BIGINT NOT NULL,
        updated_at BIGINT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(state, sequence)",
    "CREATE INDEX IF NOT EXISTS jobs_session ON jobs(session_key, state)",
    "CREATE TABLE IF NOT EXISTS owners (open_id TEXT PRIMARY KEY, created_at BIGINT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS inbound_ids (id TEXT PRIMARY KEY, created_at BIGINT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS controls (
        sequence BIGSERIAL PRIMARY KEY,
        id TEXT NOT NULL UNIQUE,
        message_json JSONB NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('queued', 'running', 'done', 'failed')),
        error TEXT,
        created_at BIGINT NOT NULL,
        updated_at BIGINT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS controls_queue ON controls(state, sequence)",
    """CREATE TABLE IF NOT EXISTS last_results (
        session_key TEXT PRIMARY KEY REFERENCES sessions(key),
        text TEXT NOT NULL,
        status TEXT NOT NULL,
        updated_at BIGINT NOT NULL
    )""",
)
