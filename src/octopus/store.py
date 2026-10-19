from __future__ import annotations

import asyncio
import math
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
    reaction_id: str | None = None


INTERRUPTED_ERROR = "服务重启，上一任务结果未确认；不会自动重跑"
CONTROL_INTERRUPTED_ERROR = "服务重启，控制命令结果未确认；不会自动重放，请重新发送"
MEMBER_DISABLED_ERROR = "成员已停用，任务已取消"
SCHEMA_VERSION = "2"


def _now() -> int:
    return time.time_ns() // 1_000_000


def _metadata(value: str, field: str, maximum: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()) or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field} 必须是不含控制字符的短文本")
    return value


def _positive_limit(value: int, field: str, maximum: int = 1_000_000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{field} 必须是 1 至 {maximum} 的整数")
    return value


class Store:
    def __init__(self, connection: AsyncConnection[dict[str, Any]], schema: str):
        self._db = connection
        self._schema = schema
        self._lock = asyncio.Lock()
        self._service_lock_acquired = False

    @classmethod
    async def open(cls, database_url: str, app_id: str, *, schema: str = "octopus", migrate: bool = True) -> Store:
        if not app_id.strip():
            raise ValueError("飞书 App ID 不能为空")
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,62}", schema):
            raise ValueError("数据库 schema 必须是长度不超过 63 的安全标识符")
        if not isinstance(migrate, bool):
            raise ValueError("迁移开关必须是布尔值")
        connection = await AsyncConnection.connect(database_url, autocommit=True, row_factory=dict_row, connect_timeout=10)
        store = cls(connection, schema)
        try:
            async with store._lock:
                async with connection.transaction():
                    if migrate:
                        # Serialize migrations when two processes start together.
                        await connection.execute("SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(%s, 0))", (f"octopus:init:{schema}",))
                        await connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
                    else:
                        cursor = await connection.execute("SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname = %s", (schema,))
                        if not await cursor.fetchone():
                            raise ValueError("数据库尚未初始化，请先使用维护账号执行迁移")
                    await connection.execute(sql.SQL("SET search_path TO {}, pg_catalog").format(sql.Identifier(schema)))
                    if migrate:
                        await connection.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                    cursor = await connection.execute("SELECT value FROM meta WHERE key = 'app_id'")
                    bound = await cursor.fetchone()
                    if bound and bound["value"] != app_id:
                        raise ValueError("此数据库 schema 已绑定其他飞书应用，请为新应用配置独立数据库")
                    if migrate:
                        await connection.execute("INSERT INTO meta (key, value) VALUES ('app_id', %s) ON CONFLICT (key) DO NOTHING", (app_id,))
                        for statement in _SCHEMA:
                            await connection.execute(statement)
                        await connection.execute(
                            "INSERT INTO meta (key, value) VALUES ('schema_version', %s) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                            (SCHEMA_VERSION,),
                        )
                    else:
                        cursor = await connection.execute("SELECT value FROM meta WHERE key = 'schema_version'")
                        version = await cursor.fetchone()
                        if not bound or not version or version["value"] != SCHEMA_VERSION:
                            raise ValueError("数据库版本不匹配，请先使用维护账号执行迁移")
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

    async def admission(
        self,
        session_key: str,
        message: IncomingMessage,
        max_pending: int = 3,
        daily_limit: int = 100,
        min_interval_seconds: float = 2,
    ) -> str:
        """Atomically authorize and enqueue a task, limiting each sender across chats.

        ``daily_limit`` is a rolling 24-hour budget for accepted tasks. Pending
        includes both queued and running tasks, so changing chat cannot bypass it.
        The advisory lock is shared with revocation and works across connections.
        """
        _metadata(message.sender_id, "成员 ID", 256)
        _positive_limit(max_pending, "排队上限", 1000)
        _positive_limit(daily_limit, "每日任务上限")
        if isinstance(min_interval_seconds, bool) or not isinstance(min_interval_seconds, (int, float)) or not math.isfinite(min_interval_seconds) or not 0 <= min_interval_seconds <= 86400:
            raise ValueError("请求间隔必须是 0 至 86400 的有限秒数")
        async with self._lock:
            async with self._db.transaction():
                await self._member_lock(message.sender_id)
                session = await self._get_session(session_key)
                if session is None or session.sender_id != message.sender_id or session.chat_id != message.chat_id:
                    raise ValueError("会话未绑定此聊天和用户")
                cursor = await self._db.execute("SELECT 1 FROM jobs WHERE id = %s", (message.id,))
                if await cursor.fetchone():
                    return "duplicate"
                cursor = await self._db.execute("SELECT enabled FROM users WHERE open_id = %s", (message.sender_id,))
                member = await cursor.fetchone()
                if not member or not member["enabled"]:
                    return "disabled"
                now = _now()
                cursor = await self._db.execute(
                    """SELECT COUNT(*) FILTER (WHERE state IN ('queued', 'running')) AS pending,
                              COUNT(*) FILTER (WHERE created_at >= %s) AS daily,
                              MAX(created_at) AS latest
                       FROM jobs WHERE message_json->>'sender_id' = %s""",
                    (now - 86_400_000, message.sender_id),
                )
                counts = await cursor.fetchone()
                assert counts is not None
                if counts["pending"] >= max_pending:
                    return "queue_full"
                if counts["daily"] >= daily_limit:
                    return "daily_limit"
                if counts["latest"] is not None and now - counts["latest"] < min_interval_seconds * 1000:
                    return "rate_limit"
                cursor = await self._db.execute(
                    """INSERT INTO jobs (id, session_key, message_json, state, created_at, updated_at)
                       VALUES (%s, %s, %s, 'queued', %s, %s) ON CONFLICT (id) DO NOTHING""",
                    (message.id, session_key, Jsonb(asdict(message)), now, now),
                )
                return "accepted" if cursor.rowcount == 1 else "duplicate"

    async def next_jobs(self, *, eligible_only: bool = False) -> list[Job]:
        if not isinstance(eligible_only, bool):
            raise ValueError("任务筛选开关必须是布尔值")
        async with self._lock:
            cursor = await self._db.execute(
                """SELECT * FROM (
                       SELECT jobs.*, row_number() OVER (PARTITION BY message_json->>'sender_id' ORDER BY sequence) AS actor_rank
                       FROM jobs JOIN sessions ON sessions.key = jobs.session_key
                       WHERE jobs.state = 'queued' AND (NOT %s OR sessions.blocked_reason IS NULL)
                   ) AS pending ORDER BY actor_rank, sequence LIMIT 100""",
                (eligible_only,),
            )
            return [_job(row) for row in await cursor.fetchall()]

    async def get_reaction(self, message_id: str) -> str | None:
        async with self._lock:
            cursor = await self._db.execute("SELECT reaction_id FROM jobs WHERE id = %s", (message_id,))
            row = await cursor.fetchone()
            return row["reaction_id"] if row else None

    async def save_reaction(self, message_id: str, reaction_id: str) -> None:
        async with self._lock:
            cursor = await self._db.execute(
                "UPDATE jobs SET reaction_id = %s, updated_at = %s WHERE id = %s",
                (reaction_id, _now(), message_id),
            )
            if not cursor.rowcount:
                raise ValueError("任务不存在")

    async def clear_reaction(self, message_id: str, reaction_id: str) -> None:
        async with self._lock:
            # An earlier removal must not clear a replacement reaction saved meanwhile.
            await self._db.execute(
                "UPDATE jobs SET reaction_id = NULL, updated_at = %s WHERE id = %s AND reaction_id = %s",
                (_now(), message_id, reaction_id),
            )

    async def finished_reactions(self) -> list[tuple[str, str]]:
        async with self._lock:
            cursor = await self._db.execute(
                "SELECT id, reaction_id FROM jobs WHERE state IN ('done', 'failed') AND reaction_id IS NOT NULL ORDER BY sequence"
            )
            return [(row["id"], row["reaction_id"]) for row in await cursor.fetchall()]

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

    async def retire_legacy_jobs(self, current_prefix: str, reason: str) -> list[Job]:
        """Retire all queued jobs outside the current policy, keeping their history."""
        _metadata(current_prefix, "当前会话前缀", 128)
        _metadata(reason, "策略迁移说明", 512)
        async with self._lock:
            # One statement also covers paused sessions and queues larger than
            # the scheduler's page size. LEFT treats '%' and '_' literally.
            cursor = await self._db.execute(
                """UPDATE jobs SET state = 'failed', error = %s, updated_at = %s
                   WHERE state = 'queued' AND LEFT(session_key, char_length(%s)) <> %s
                   RETURNING *""",
                (reason, _now(), current_prefix, current_prefix),
            )
            rows = await cursor.fetchall()
            return [_job(row) for row in sorted(rows, key=lambda row: row["sequence"])]

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
        _metadata(open_id, "成员 ID", 256)
        async with self._lock:
            async with self._db.transaction():
                now = _now()
                await self._db.execute("INSERT INTO owners (open_id, created_at) VALUES (%s, %s) ON CONFLICT (open_id) DO NOTHING", (open_id, now))
                await self._db.execute(
                    "INSERT INTO users (open_id, role, enabled, created_at, updated_at) VALUES (%s, 'admin', TRUE, %s, %s) ON CONFLICT (open_id) DO NOTHING",
                    (open_id, now, now),
                )

    async def owners(self) -> list[str]:
        async with self._lock:
            cursor = await self._db.execute("SELECT open_id FROM owners ORDER BY open_id")
            return [row["open_id"] for row in await cursor.fetchall()]

    async def _member_lock(self, open_id: str) -> None:
        await self._db.execute(
            "SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(%s, 0))",
            (f"octopus:member:{self._schema}:{open_id}",),
        )

    async def ensure_member(self, open_id: str, role: str = "member") -> None:
        """Enroll only once; repeated config seeding cannot undo a revocation."""
        _metadata(open_id, "成员 ID", 256)
        if not isinstance(role, str) or role not in {"admin", "member"}:
            raise ValueError("成员角色必须是 admin 或 member")
        async with self._lock:
            now = _now()
            await self._db.execute(
                "INSERT INTO users (open_id, role, enabled, created_at, updated_at) VALUES (%s, %s, TRUE, %s, %s) ON CONFLICT (open_id) DO NOTHING",
                (open_id, role, now, now),
            )

    async def member(self, open_id: str) -> dict[str, Any] | None:
        _metadata(open_id, "成员 ID", 256)
        async with self._lock:
            cursor = await self._db.execute("SELECT open_id, role, enabled FROM users WHERE open_id = %s", (open_id,))
            return await cursor.fetchone()

    async def members(self) -> list[dict[str, Any]]:
        async with self._lock:
            cursor = await self._db.execute("SELECT open_id, role, enabled FROM users ORDER BY open_id")
            return await cursor.fetchall()

    async def set_member_enabled(self, open_id: str, enabled: bool) -> None:
        _metadata(open_id, "成员 ID", 256)
        if not isinstance(enabled, bool):
            raise ValueError("成员启用状态必须是布尔值")
        async with self._lock:
            async with self._db.transaction():
                await self._member_lock(open_id)
                now = _now()
                cursor = await self._db.execute("UPDATE users SET enabled = %s, updated_at = %s WHERE open_id = %s", (enabled, now, open_id))
                if not cursor.rowcount:
                    raise ValueError("成员不存在")
                if not enabled:
                    # Running work must be interrupted by the bridge: changing a
                    # database state cannot prove its external execution stopped.
                    for table in ("jobs", "controls"):
                        await self._db.execute(
                            sql.SQL("UPDATE {} SET state = 'failed', error = %s, updated_at = %s WHERE message_json->>'sender_id' = %s AND state = 'queued'").format(sql.Identifier(table)),
                            (MEMBER_DISABLED_ERROR, now, open_id),
                        )

    async def audit(self, actor: str, action: str, resource: str = "", decision: str = "allow", job_id: str | None = None) -> None:
        """Record bounded metadata only; callers must not pass prompts or results."""
        _metadata(actor, "审计调用人", 256)
        _metadata(action, "审计动作", 80)
        _metadata(resource, "审计资源", 512, empty=True)
        _metadata(decision, "审计结果", 32)
        if job_id is not None:
            _metadata(job_id, "审计任务 ID", 256)
        async with self._lock:
            await self._db.execute(
                "INSERT INTO audit_events (actor, action, resource, decision, job_id, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
                (actor, action, resource, decision, job_id, _now()),
            )

    async def purge_history(self, days: int = 30, audit_days: int = 90) -> dict[str, int]:
        """Delete expired terminal payloads, keeping active work and its dedup IDs."""
        _positive_limit(days, "记录保留天数", 3650)
        _positive_limit(audit_days, "审计保留天数", 3650)
        cutoff = _now() - days * 86_400_000
        audit_cutoff = _now() - audit_days * 86_400_000
        counts: dict[str, int] = {}
        async with self._lock:
            async with self._db.transaction():
                for table in ("jobs", "controls"):
                    cursor = await self._db.execute(
                        sql.SQL("DELETE FROM {} WHERE state IN ('done', 'failed') AND updated_at < %s").format(sql.Identifier(table)),
                        (cutoff,),
                    )
                    counts[table] = cursor.rowcount
                cursor = await self._db.execute(
                    """DELETE FROM inbound_ids WHERE created_at < %s
                       AND NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.id = inbound_ids.id)
                       AND NOT EXISTS (SELECT 1 FROM controls WHERE controls.id = inbound_ids.id)""",
                    (cutoff,),
                )
                counts["inbound_ids"] = cursor.rowcount
                cursor = await self._db.execute(
                    """DELETE FROM last_results WHERE updated_at < %s
                       AND NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.session_key = last_results.session_key AND state IN ('queued', 'running'))""",
                    (cutoff,),
                )
                counts["last_results"] = cursor.rowcount
                cursor = await self._db.execute("DELETE FROM audit_events WHERE created_at < %s", (audit_cutoff,))
                counts["audit_events"] = cursor.rowcount
        return counts

    async def claim_message(self, id: str) -> bool:
        async with self._lock:
            # Separate from jobs so a claimed inbound message can still be enqueued.
            cursor = await self._db.execute("INSERT INTO inbound_ids (id, created_at) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING", (id, _now()))
            return cursor.rowcount == 1

    async def enqueue_control(self, message: IncomingMessage, *, max_pending: int | None = None) -> bool:
        if max_pending is not None:
            _positive_limit(max_pending, "控制命令排队上限", 1000)
            _metadata(message.sender_id, "成员 ID", 256)
        async with self._lock:
            async with self._db.transaction():
                now = _now()
                if max_pending is not None:
                    await self._member_lock(message.sender_id)
                    cursor = await self._db.execute("SELECT enabled FROM users WHERE open_id = %s", (message.sender_id,))
                    member = await cursor.fetchone()
                    if not member or not member["enabled"]:
                        return False
                    cursor = await self._db.execute(
                        """SELECT COUNT(*) FILTER (WHERE state IN ('queued', 'running')) AS pending,
                                  COUNT(*) FILTER (WHERE created_at >= %s) AS daily
                           FROM controls WHERE message_json->>'sender_id' = %s""",
                        (now - 86_400_000, message.sender_id),
                    )
                    count = await cursor.fetchone()
                    if count and (count["pending"] >= max_pending or count["daily"] >= 1000):
                        return False
                cursor = await self._db.execute(
                    """INSERT INTO controls (id, message_json, state, created_at, updated_at)
                       VALUES (%s, %s, 'queued', %s, %s) ON CONFLICT (id) DO NOTHING""",
                    (message.id, Jsonb(asdict(message)), now, now),
                )
                return cursor.rowcount == 1

    async def next_controls(self) -> list[IncomingMessage]:
        async with self._lock:
            cursor = await self._db.execute("SELECT message_json FROM controls WHERE state = 'queued' ORDER BY sequence LIMIT 100")
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
    return Job(row["id"], row["session_key"], IncomingMessage(**row["message_json"]), row["state"], row["error"], row["reaction_id"])


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
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS reaction_id TEXT",
    "CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(state, sequence)",
    "CREATE INDEX IF NOT EXISTS jobs_session ON jobs(session_key, state)",
    "CREATE INDEX IF NOT EXISTS jobs_actor_created ON jobs ((message_json->>'sender_id'), created_at)",
    "CREATE TABLE IF NOT EXISTS owners (open_id TEXT PRIMARY KEY, created_at BIGINT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS users (
        open_id TEXT PRIMARY KEY,
        role TEXT NOT NULL CHECK (role IN ('admin', 'member')),
        enabled BOOLEAN NOT NULL DEFAULT TRUE,
        created_at BIGINT NOT NULL,
        updated_at BIGINT NOT NULL
    )""",
    """INSERT INTO users (open_id, role, enabled, created_at, updated_at)
        SELECT open_id, 'admin', TRUE, created_at, created_at FROM owners ON CONFLICT (open_id) DO NOTHING""",
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
    "CREATE INDEX IF NOT EXISTS controls_actor ON controls ((message_json->>'sender_id'), state)",
    """CREATE TABLE IF NOT EXISTS last_results (
        session_key TEXT PRIMARY KEY REFERENCES sessions(key),
        text TEXT NOT NULL,
        status TEXT NOT NULL,
        updated_at BIGINT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS audit_events (
        id BIGSERIAL PRIMARY KEY,
        actor TEXT NOT NULL CHECK (length(actor) BETWEEN 1 AND 256),
        action TEXT NOT NULL CHECK (length(action) BETWEEN 1 AND 80),
        resource TEXT NOT NULL CHECK (length(resource) <= 512),
        decision TEXT NOT NULL CHECK (length(decision) BETWEEN 1 AND 32),
        job_id TEXT CHECK (length(job_id) BETWEEN 1 AND 256),
        created_at BIGINT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS audit_events_created ON audit_events (created_at)",
)
