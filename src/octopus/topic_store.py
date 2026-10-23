"""Durable business-topic routing within an already authorized chat scope.

The base sessions remain authorization/ingress scopes. Child sessions keep the
same identity fields but carry independent Codex histories. Routing is frozen
before business execution; results never move a participant's active topic.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb

from .code_reader import redact
from .contracts import IncomingMessage
from .privacy import redact_private_text

if TYPE_CHECKING:
    from .store import Job


@dataclass(frozen=True)
class Topic:
    session_key: str
    scope_key: str
    title: str
    summary: str
    data_context_restricted: bool = False
    is_current: bool = False
    is_previous: bool = False


@dataclass(frozen=True)
class Route:
    job_id: str
    scope_key: str
    session_key: str
    prompt: str
    question: str | None
    data_allowed: bool
    action: str
    reason: str
    image_sources: tuple[dict, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "image_sources", tuple(asdict(IncomingMessage(**source))
                                                      for source in self.image_sources))


def _now() -> int:
    return time.time_ns() // 1_000_000


def _text(value: str, maximum: int, name: str, *, empty: bool = False) -> None:
    if (not isinstance(value, str) or len(value) > maximum or "\x00" in value
            or (not empty and not value.strip())):
        raise ValueError(f"Invalid topic {name}")


# These static SQL predicates compare the immutable ingress messages, not the
# mutable business-session assignment. Group roots remain distinct scopes.
_SAME_INGRESS_SCOPE = """
    c.message_json->>'sender_id' = j.message_json->>'sender_id'
    AND c.message_json->>'chat_id' = j.message_json->>'chat_id'
    AND c.message_json->>'chat_type' = j.message_json->>'chat_type'
    AND (c.message_json->>'chat_type' <> 'group' OR
         COALESCE(NULLIF(c.message_json->>'root_id', ''), c.id) =
         COALESCE(NULLIF(j.message_json->>'root_id', ''), j.id))
"""
_SELECTION_CONTROL = "lower(c.message_json->>'text') ~ '^[[:space:]]*/(new|switch)([[:space:]]|$)'"


class TopicStore:
    """Uses Store's connection and lock; methods compose SQL in one transaction."""

    async def _topic_scope(self, scope_key: str, actor_id: str | None = None,
                           message: IncomingMessage | None = None) -> dict:
        cursor = await self._db.execute("SELECT * FROM sessions WHERE key = %s", (scope_key,))
        scope = await cursor.fetchone()
        if not scope:
            raise ValueError("主题所属聊天不存在")
        cursor = await self._db.execute("SELECT scope_key FROM topics WHERE session_key = %s", (scope_key,))
        bound = await cursor.fetchone()
        if bound and bound["scope_key"] != scope_key:
            raise ValueError("子主题不能作为聊天范围")
        if actor_id is not None:
            _text(actor_id, 256, "actor")
            if scope["scope"] == "personal" and scope["sender_id"] != actor_id:
                raise ValueError("主题不属于此用户")
        if message is not None:
            if (scope["chat_id"] != message.chat_id
                    or (scope["scope"] == "topic" and
                        (message.chat_type != "group" or scope["topic_id"] != message.topic_id))
                    or (scope["scope"] == "personal" and
                        (message.chat_type != "p2p" or scope["sender_id"] != message.sender_id))):
                raise ValueError("主题不属于此聊天和用户")
        return scope

    async def _seed_legacy_topic(self, scope_key: str) -> None:
        """Keep a pre-routing thread reachable without copying its history."""
        scope = await self._topic_scope(scope_key)
        if not scope["thread_id"]:
            return
        cursor = await self._db.execute(
            "SELECT message_json FROM jobs WHERE session_key = %s AND state IN ('done', 'failed') ORDER BY sequence DESC LIMIT 1",
            (scope_key,),
        )
        row = await cursor.fetchone()
        question = row["message_json"].get("text", "") if row else ""
        question = redact_private_text(redact(question))[:1200]
        now = _now()
        await self._db.execute(
            """INSERT INTO topics (session_key, scope_key, title, summary, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (session_key) DO NOTHING""",
            (scope_key, scope_key, "此前的对话", question or "已有会话；主题详情未知，请依据明确引用判断。", now, now),
        )

    async def _topic(self, scope_key: str, key: str, actor_id: str | None = None) -> Topic | None:
        cursor = await self._db.execute(
            """SELECT t.session_key, t.scope_key, t.title, t.summary, s.data_context_restricted,
                      COALESCE(a.current_key = t.session_key, FALSE) AS is_current,
                      COALESCE(a.previous_key = t.session_key, FALSE) AS is_previous
               FROM topics t JOIN sessions s ON s.key = t.session_key
               LEFT JOIN topic_actors a ON a.scope_key = t.scope_key AND a.actor_id = %s
               WHERE t.scope_key = %s AND t.session_key = %s""", (actor_id, scope_key, key),
        )
        row = await cursor.fetchone()
        return Topic(**row) if row else None

    async def topic_candidates(self, scope_key: str, actor_id: str, limit: int = 8) -> list[Topic]:
        if type(limit) is not int or not 1 <= limit <= 30:
            raise ValueError("主题候选数量必须在1至30之间")
        async with self._lock:
            async with self._db.transaction():
                await self._topic_scope(scope_key, actor_id)
                await self._seed_legacy_topic(scope_key)
                cursor = await self._db.execute(
                    """SELECT t.session_key, t.scope_key, t.title, t.summary, s.data_context_restricted,
                              COALESCE(a.current_key = t.session_key, FALSE) AS is_current,
                              COALESCE(a.previous_key = t.session_key, FALSE) AS is_previous
                       FROM topics t JOIN sessions s ON s.key = t.session_key
                       LEFT JOIN topic_actors a ON a.scope_key = t.scope_key AND a.actor_id = %s
                       WHERE t.scope_key = %s
                       ORDER BY is_current DESC, is_previous DESC, t.updated_at DESC, t.session_key LIMIT %s""",
                    (actor_id, scope_key, limit),
                )
                return [Topic(**row) for row in await cursor.fetchall()]

    async def topic_by_key(self, scope_key: str, key: str) -> Topic | None:
        async with self._lock:
            return await self._topic(scope_key, key)

    async def topic_for_message(self, scope_key: str, message_id: str) -> str | None:
        async with self._lock:
            async with self._db.transaction():
                await self._seed_legacy_topic(scope_key)
                cursor = await self._db.execute(
                    "SELECT session_key FROM topic_messages WHERE scope_key = %s AND message_id = %s",
                    (scope_key, message_id),
                )
                row = await cursor.fetchone()
                if row:
                    return row["session_key"]
                # Legacy incoming jobs are enough to recover explicit references;
                # unrelated messages in the same group cannot pass this join.
                cursor = await self._db.execute(
                    """SELECT j.session_key FROM jobs j JOIN topics t ON t.session_key = j.session_key
                       WHERE j.id = %s AND t.scope_key = %s AND j.state IN ('running', 'done', 'failed')""",
                    (message_id, scope_key),
                )
                row = await cursor.fetchone()
                return row["session_key"] if row else None

    async def current_topic(self, scope_key: str, actor_id: str) -> str | None:
        async with self._lock:
            await self._topic_scope(scope_key, actor_id)
            cursor = await self._db.execute(
                "SELECT current_key FROM topic_actors WHERE scope_key = %s AND actor_id = %s", (scope_key, actor_id),
            )
            row = await cursor.fetchone()
            return row["current_key"] if row else None

    async def _select_topic(self, scope_key: str, actor_id: str, key: str) -> None:
        if not await self._topic(scope_key, key):
            raise ValueError("主题不属于此聊天")
        await self._db.execute(
            """INSERT INTO topic_actors (scope_key, actor_id, current_key, updated_at) VALUES (%s, %s, %s, %s)
               ON CONFLICT (scope_key, actor_id) DO UPDATE SET
                   previous_key = CASE WHEN topic_actors.current_key IS DISTINCT FROM excluded.current_key
                                       THEN topic_actors.current_key ELSE topic_actors.previous_key END,
                   current_key = excluded.current_key, updated_at = excluded.updated_at""",
            (scope_key, actor_id, key, _now()),
        )

    async def select_topic(self, scope_key: str, actor_id: str, key: str) -> None:
        async with self._lock:
            async with self._db.transaction():
                await self._topic_scope(scope_key, actor_id)
                await self._select_topic(scope_key, actor_id, key)

    async def _new_topic(self, scope_key: str, title: str, summary: str) -> str:
        scope = await self._topic_scope(scope_key)
        key, now = scope_key + ":subject:" + uuid.uuid4().hex, _now()
        await self._db.execute(
            """INSERT INTO sessions (key, chat_id, sender_id, scope, topic_id, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (key, scope["chat_id"], scope["sender_id"], scope["scope"], scope["topic_id"], now, now),
        )
        await self._db.execute(
            """INSERT INTO topics (session_key, scope_key, title, summary, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s)""", (key, scope_key, title, summary, now, now),
        )
        return key

    async def new_topic(self, scope_key: str, actor_id: str, title: str = "新问题", summary: str = "") -> str:
        _text(title, 120, "title")
        _text(summary, 1200, "summary", empty=True)
        async with self._lock:
            async with self._db.transaction():
                await self._topic_scope(scope_key, actor_id)
                key = await self._new_topic(scope_key, title, summary)
                await self._select_topic(scope_key, actor_id, key)
                await self._db.execute("DELETE FROM topic_pending WHERE scope_key = %s AND actor_id = %s", (scope_key, actor_id))
                return key

    async def pending_routes(self) -> list[Job]:
        from .store import _job
        async with self._lock:
            cursor = await self._db.execute(
                f"""SELECT j.* FROM jobs j WHERE j.state = 'queued'
                   AND NOT EXISTS (SELECT 1 FROM topic_routes r WHERE r.job_id = j.id)
                   AND NOT EXISTS (
                       SELECT 1 FROM controls c WHERE c.state IN ('queued', 'running')
                         AND c.routing_order < j.routing_order
                         AND {_SELECTION_CONTROL} AND {_SAME_INGRESS_SCOPE})
                   ORDER BY j.routing_order LIMIT 100"""
            )
            return [_job(row) for row in await cursor.fetchall()]

    async def topic_control_ready(self, control_id: str) -> bool:
        """A selection cannot overtake an earlier question awaiting routing."""
        async with self._lock:
            cursor = await self._db.execute(
                f"""SELECT NOT ({_SELECTION_CONTROL}) OR NOT EXISTS (
                       SELECT 1 FROM jobs j WHERE j.state = 'queued' AND j.routing_order < c.routing_order
                         AND NOT EXISTS (SELECT 1 FROM topic_routes r WHERE r.job_id = j.id)
                         AND {_SAME_INGRESS_SCOPE}) AS ready
                    FROM controls c WHERE c.id = %s""", (control_id,),
            )
            row = await cursor.fetchone()
            return bool(row and row["ready"])

    async def next_routed_jobs(self) -> list[Job]:
        """Read assignment and job together, never mixing pre/post-route keys."""
        from .store import _job
        async with self._lock:
            cursor = await self._db.execute(
                """SELECT * FROM (
                       SELECT j.*, row_number() OVER (
                           PARTITION BY j.message_json->>'sender_id' ORDER BY j.sequence) AS actor_rank
                       FROM jobs j JOIN topic_routes r ON r.job_id = j.id AND r.session_key = j.session_key
                       JOIN sessions s ON s.key = j.session_key
                       WHERE j.state = 'queued' AND s.blocked_reason IS NULL
                         AND NOT EXISTS (
                             SELECT 1 FROM jobs earlier JOIN topic_routes er ON er.job_id = earlier.id
                             WHERE earlier.session_key = j.session_key AND earlier.state = 'queued'
                               AND earlier.sequence < j.sequence)
                   ) pending ORDER BY actor_rank, sequence LIMIT 100"""
            )
            return [_job(row) for row in await cursor.fetchall()]

    async def cancel_pending_routes(self, scope_key: str, actor_id: str) -> int:
        async with self._lock:
            await self._topic_scope(scope_key, actor_id)
            cursor = await self._db.execute(
                """UPDATE jobs j SET state = 'failed', error = '任务已取消', updated_at = %s
                   WHERE j.state = 'queued' AND j.session_key = %s
                     AND j.message_json->>'sender_id' = %s
                     AND NOT EXISTS (SELECT 1 FROM topic_routes r WHERE r.job_id = j.id)""",
                (_now(), scope_key, actor_id),
            )
            return cursor.rowcount

    async def cancel_routed_jobs(self, session_key: str) -> int:
        async with self._lock:
            cursor = await self._db.execute(
                """UPDATE jobs j SET state = 'failed', error = '任务已取消', updated_at = %s
                   WHERE j.state = 'queued' AND j.session_key = %s
                     AND EXISTS (SELECT 1 FROM topic_routes r WHERE r.job_id = j.id AND r.session_key = j.session_key)""",
                (_now(), session_key),
            )
            return cursor.rowcount

    async def _get_route(self, job_id: str) -> Route | None:
        cursor = await self._db.execute(
            """SELECT job_id, scope_key, session_key, prompt, question, data_allowed, action, reason, image_sources
               FROM topic_routes WHERE job_id = %s""", (job_id,),
        )
        row = await cursor.fetchone()
        return Route(**row) if row else None

    async def get_route(self, job_id: str) -> Route | None:
        async with self._lock:
            return await self._get_route(job_id)

    async def topic_last_question(self, scope_key: str, key: str,
                                  actor_id: str | None = None) -> dict | None:
        async with self._lock:
            await self._topic_scope(scope_key, actor_id)
            cursor = await self._db.execute(
                """SELECT r.question, r.data_allowed, r.image_sources, j.id, j.message_json
                   FROM topic_routes r JOIN jobs j ON j.id = r.job_id
                   WHERE r.scope_key = %s AND r.session_key = %s
                     AND (%s::text IS NULL OR j.message_json->>'sender_id' = %s)
                   ORDER BY j.sequence DESC LIMIT 1""", (scope_key, key, actor_id, actor_id),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            message = row["message_json"]
            sources = ((asdict(IncomingMessage(**message)),) if message.get("image_keys") else ())
            sources += tuple(asdict(IncomingMessage(**source)) for source in row["image_sources"])
            return {"question": (row["question"] or message.get("text", ""))[:16000],
                    "data_allowed": row["data_allowed"], "message_id": row["id"],
                    "has_images": bool(sources or message.get("attachment_error")), "image_sources": sources}

    async def _map_topic_message(self, scope_key: str, key: str, message_id: str) -> None:
        _text(message_id, 256, "message ID")
        await self._db.execute(
            """INSERT INTO topic_messages (scope_key, message_id, session_key, created_at) VALUES (%s, %s, %s, %s)
               ON CONFLICT (scope_key, message_id) DO NOTHING""", (scope_key, message_id, key, _now()),
        )
        cursor = await self._db.execute(
            "SELECT session_key FROM topic_messages WHERE scope_key = %s AND message_id = %s", (scope_key, message_id),
        )
        if (await cursor.fetchone())["session_key"] != key:
            raise ValueError("消息已经绑定其他主题")

    async def map_topic_message(self, scope_key: str, key: str, message_id: str) -> None:
        async with self._lock:
            async with self._db.transaction():
                if not await self._topic(scope_key, key):
                    raise ValueError("主题不属于此聊天")
                await self._map_topic_message(scope_key, key, message_id)

    async def freeze_route(self, job_id: str, scope_key: str, target_key: str | None, *, title: str,
                           summary: str, prompt: str, question: str | None, data_allowed: bool,
                           action: str, reason: str, context_ids: tuple[str, ...] = (),
                           image_sources: tuple[dict, ...] = ()) -> Route | None:
        _text(title, 120, "title")
        _text(summary, 1200, "summary", empty=True)
        _text(prompt, 50000, "prompt")
        if question is not None:
            _text(question, 16000, "question")
        _text(reason, 300, "reason", empty=True)
        if type(data_allowed) is not bool or action not in {"new", "continue", "resume"}:
            raise ValueError("Invalid topic decision")
        if not isinstance(context_ids, tuple) or len(context_ids) > 30:
            raise ValueError("Invalid topic context IDs")
        async with self._lock:
            async with self._db.transaction():
                cursor = await self._db.execute("SELECT * FROM jobs WHERE id = %s FOR UPDATE", (job_id,))
                job = await cursor.fetchone()
                if not job:
                    return None
                existing = await self._get_route(job_id)
                if existing:
                    if existing.scope_key != scope_key:
                        raise ValueError("消息已经绑定其他聊天")
                    return existing
                if job["state"] != "queued":
                    return None
                message = IncomingMessage(**job["message_json"])
                await self._topic_scope(scope_key, message.sender_id, message)
                if job["session_key"] != scope_key:
                    raise ValueError("待路由消息不属于此聊天")
                sources = await self._validate_image_sources(scope_key, message, image_sources)
                if sources or message.image_keys:
                    data_allowed = False
                key = target_key
                if key is None:
                    key = await self._new_topic(scope_key, title, summary)
                elif not await self._topic(scope_key, key):
                    raise ValueError("主题不属于此聊天")
                await self._map_topic_message(scope_key, key, job_id)
                for context_id in context_ids:
                    _text(context_id, 256, "context message ID")
                    # An explicit reference may be used to start a different
                    # question. Preserve the source message's original topic;
                    # only previously unmapped context receives this binding.
                    await self._db.execute(
                        """INSERT INTO topic_messages (scope_key, message_id, session_key, created_at)
                           VALUES (%s, %s, %s, %s) ON CONFLICT (scope_key, message_id) DO NOTHING""",
                        (scope_key, context_id, key, _now()),
                    )
                # Restrictions are monotonic inside each business thread. They
                # do not spread to unrelated new threads without shared input.
                if not data_allowed:
                    await self._db.execute("UPDATE sessions SET data_context_restricted = TRUE WHERE key = %s", (key,))
                cursor = await self._db.execute("SELECT data_context_restricted FROM sessions WHERE key = %s", (key,))
                allowed = data_allowed and not (await cursor.fetchone())["data_context_restricted"]
                now = _now()
                await self._db.execute(
                    "UPDATE topics SET title = %s, summary = %s, updated_at = %s WHERE session_key = %s",
                    (title, summary, now, key),
                )
                await self._db.execute("UPDATE jobs SET session_key = %s, updated_at = %s WHERE id = %s", (key, now, job_id))
                await self._db.execute(
                    """INSERT INTO topic_routes (job_id, scope_key, session_key, prompt, question, data_allowed,
                                                  action, reason, created_at, image_sources)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (job_id, scope_key, key, prompt, question, allowed, action, reason, now, Jsonb(sources)),
                )
                await self._select_topic(scope_key, message.sender_id, key)
                await self._db.execute("DELETE FROM topic_pending WHERE scope_key = %s AND actor_id = %s", (scope_key, message.sender_id))
                return await self._get_route(job_id)

    async def _validate_image_sources(self, scope_key: str, current: IncomingMessage,
                                      sources: tuple[dict, ...]) -> tuple[dict, ...]:
        if not isinstance(sources, (list, tuple)) or len(sources) > 4:
            raise ValueError("Invalid topic image sources")
        count = len(current.image_keys)
        if count > 4:
            raise ValueError("每个问题最多支持4张图片（包括待澄清的原图）")
        seen = {current.id}
        normalized = []
        for source in sources:
            if not isinstance(source, dict):
                raise ValueError("Invalid topic image source")
            try:
                message = IncomingMessage(**source)
            except (TypeError, ValueError):
                raise ValueError("Invalid topic image source") from None
            if (message.id in seen or message.sender_id != current.sender_id
                    or type(message.created_at) is not int or message.created_at > current.created_at
                    or not message.image_keys or message.attachment_error
                    or any(not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", key)
                           for key in message.image_keys)):
                raise ValueError("Invalid topic image source identity")
            await self._topic_scope(scope_key, message.sender_id, message)
            cursor = await self._db.execute(
                """SELECT j.message_json FROM jobs j
                   LEFT JOIN topics t ON t.session_key = j.session_key
                   WHERE j.id = %s AND (j.session_key = %s OR t.scope_key = %s)""",
                (message.id, scope_key, scope_key),
            )
            row = await cursor.fetchone()
            if not row or IncomingMessage(**row["message_json"]) != message:
                raise ValueError("Topic image source is not the original accepted message")
            count += len(message.image_keys)
            if count > 4:
                raise ValueError("每个问题最多支持4张图片（包括待澄清的原图）")
            seen.add(message.id)
            normalized.append(asdict(message))
        return tuple(normalized)

    async def get_topic_pending(self, scope_key: str, actor_id: str) -> dict | None:
        async with self._lock:
            await self._topic_scope(scope_key, actor_id)
            cursor = await self._db.execute(
                "SELECT pending FROM topic_pending WHERE scope_key = %s AND actor_id = %s AND created_at > %s",
                (scope_key, actor_id, _now() - 30 * 60_000),
            )
            row = await cursor.fetchone()
            return row["pending"] if row else None

    async def defer_topic_route(self, job_id: str, scope_key: str, pending: dict) -> bool:
        if not isinstance(pending, dict) or len(json.dumps(pending, ensure_ascii=False)) > 65000:
            raise ValueError("Invalid pending topic question")
        async with self._lock:
            async with self._db.transaction():
                cursor = await self._db.execute("SELECT * FROM jobs WHERE id = %s FOR UPDATE", (job_id,))
                job = await cursor.fetchone()
                if not job or job["state"] != "queued" or await self._get_route(job_id):
                    return False
                message = IncomingMessage(**job["message_json"])
                await self._topic_scope(scope_key, message.sender_id, message)
                if job["session_key"] != scope_key:
                    raise ValueError("待澄清消息不属于此聊天")
                await self._db.execute(
                    """INSERT INTO topic_pending (scope_key, actor_id, pending, created_at) VALUES (%s, %s, %s, %s)
                       ON CONFLICT (scope_key, actor_id) DO UPDATE SET pending = excluded.pending, created_at = excluded.created_at""",
                    (scope_key, message.sender_id, Jsonb(pending), _now()),
                )
                await self._db.execute("UPDATE jobs SET state = 'done', updated_at = %s WHERE id = %s", (_now(), job_id))
                return True

    async def reject_topic_route(self, job_id: str, error: str) -> bool:
        async with self._lock:
            cursor = await self._db.execute(
                "UPDATE jobs SET state = 'failed', error = %s, updated_at = %s WHERE id = %s AND state = 'queued'",
                (error, _now(), job_id),
            )
            return cursor.rowcount == 1


TOPIC_SCHEMA = (
    "CREATE SEQUENCE IF NOT EXISTS topic_ingress_order",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS routing_order BIGINT",
    "ALTER TABLE controls ADD COLUMN IF NOT EXISTS routing_order BIGINT",
    "ALTER TABLE jobs ALTER COLUMN routing_order SET DEFAULT nextval('topic_ingress_order')",
    "ALTER TABLE controls ALTER COLUMN routing_order SET DEFAULT nextval('topic_ingress_order')",
    # Backfill only pre-routing records, retaining chronological order across
    # both tables rather than assigning all controls after all old questions.
    """WITH pending AS MATERIALIZED (
           SELECT kind, id FROM (
               SELECT 'job' AS kind, id, created_at, sequence FROM jobs WHERE routing_order IS NULL
               UNION ALL
               SELECT 'control' AS kind, id, created_at, sequence FROM controls WHERE routing_order IS NULL
           ) old ORDER BY created_at, kind, sequence
       ), numbered AS MATERIALIZED (
           SELECT kind, id, nextval('topic_ingress_order') AS routing_order FROM pending
       ), updated_jobs AS (
           UPDATE jobs j SET routing_order = n.routing_order FROM numbered n
           WHERE n.kind = 'job' AND j.id = n.id RETURNING j.id
       )
       UPDATE controls c SET routing_order = n.routing_order FROM numbered n
       WHERE n.kind = 'control' AND c.id = n.id""",
    "ALTER TABLE jobs ALTER COLUMN routing_order SET NOT NULL",
    "ALTER TABLE controls ALTER COLUMN routing_order SET NOT NULL",
    "CREATE INDEX IF NOT EXISTS jobs_routing_order ON jobs(routing_order) WHERE state = 'queued'",
    "CREATE INDEX IF NOT EXISTS controls_routing_order ON controls(routing_order) WHERE state IN ('queued', 'running')",
    """CREATE TABLE IF NOT EXISTS topics (
        session_key TEXT PRIMARY KEY REFERENCES sessions(key),
        scope_key TEXT NOT NULL REFERENCES sessions(key),
        title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 120),
        summary TEXT NOT NULL CHECK (length(summary) <= 1200),
        created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL,
        UNIQUE (scope_key, session_key)
    )""",
    "CREATE INDEX IF NOT EXISTS topics_recent ON topics(scope_key, updated_at DESC)",
    """CREATE TABLE IF NOT EXISTS topic_routes (
        job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
        scope_key TEXT NOT NULL,
        session_key TEXT NOT NULL,
        prompt TEXT NOT NULL, question TEXT,
        data_allowed BOOLEAN NOT NULL,
        action TEXT NOT NULL CHECK (action IN ('new', 'continue', 'resume')),
        reason TEXT NOT NULL, created_at BIGINT NOT NULL,
        FOREIGN KEY (scope_key, session_key) REFERENCES topics(scope_key, session_key)
    )""",
    "ALTER TABLE topic_routes ADD COLUMN IF NOT EXISTS image_sources JSONB NOT NULL DEFAULT '[]'::jsonb",
    """CREATE TABLE IF NOT EXISTS topic_messages (
        scope_key TEXT NOT NULL, message_id TEXT NOT NULL, session_key TEXT NOT NULL,
        created_at BIGINT NOT NULL,
        PRIMARY KEY (scope_key, message_id),
        FOREIGN KEY (scope_key, session_key) REFERENCES topics(scope_key, session_key)
    )""",
    """CREATE TABLE IF NOT EXISTS topic_actors (
        scope_key TEXT NOT NULL REFERENCES sessions(key), actor_id TEXT NOT NULL,
        current_key TEXT NOT NULL, previous_key TEXT, updated_at BIGINT NOT NULL,
        PRIMARY KEY (scope_key, actor_id),
        FOREIGN KEY (scope_key, current_key) REFERENCES topics(scope_key, session_key),
        FOREIGN KEY (scope_key, previous_key) REFERENCES topics(scope_key, session_key)
    )""",
    """CREATE TABLE IF NOT EXISTS topic_pending (
        scope_key TEXT NOT NULL REFERENCES sessions(key), actor_id TEXT NOT NULL,
        pending JSONB NOT NULL, created_at BIGINT NOT NULL,
        PRIMARY KEY (scope_key, actor_id)
    )""",
)
