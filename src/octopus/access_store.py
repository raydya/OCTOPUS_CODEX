"""Durable, metadata-only access requests and notification outbox.

This mixin uses Store's connection and member locks. The short access-workflow
lock serializes request state transitions and the pending cap across processes;
member locks then serialize authorization with manual membership changes.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from .contracts import IncomingMessage


ACCESS_TTL_MS = 7 * 86_400_000
ACCESS_COOLDOWN_MS = 86_400_000
ACCESS_RETRY_MS = 30_000
ACCESS_PENDING_LIMIT = 200
_REQUEST_COLUMNS = (
    "id, requester_id, chat_id, message_id, status, created_at, expires_at, decided_by, decided_at"
)


def _valid_uuid(value: str) -> bool:
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


class AccessStore:
    async def _access_lock(self) -> None:
        await self._db.execute(
            "SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(%s, 0))",
            (f"octopus:access:{self._schema}",),
        )

    async def _access_member_locks(self, *identities: str) -> None:
        for identity in sorted(set(identities)):
            await self._member_lock(identity)

    async def _access_request(self, request_id: str) -> dict[str, Any] | None:
        cursor = await self._db.execute(
            f"SELECT {_REQUEST_COLUMNS} FROM access_requests WHERE id = %s", (request_id,)
        )
        return await cursor.fetchone()

    async def _access_admin(self, actor: str) -> bool:
        cursor = await self._db.execute(
            "SELECT 1 FROM users WHERE open_id = %s AND role = 'admin' AND enabled", (actor,)
        )
        return bool(await cursor.fetchone())

    async def _access_audit(self, actor: str, action: str, request_id: str, decision: str, now: int) -> None:
        await self._db.execute(
            """INSERT INTO audit_events (actor, action, resource, decision, created_at)
               VALUES (%s, %s, %s, %s, %s)""",
            (actor, action, request_id, decision, now),
        )

    async def _access_notify(self, request_id: str, recipient: str, kind: str, now: int) -> None:
        await self._db.execute(
            """INSERT INTO access_deliveries
               (id, request_id, recipient_id, kind, not_before, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (request_id, recipient_id, kind) DO NOTHING""",
            (str(uuid4()), request_id, recipient, kind, now, now, now),
        )

    async def _access_close(
        self, request: dict[str, Any], status: str, actor: str | None, now: int
    ) -> dict[str, Any]:
        await self._db.execute(
            """UPDATE access_requests SET status = %s, decided_by = %s, decided_at = %s
               WHERE id = %s AND status = 'pending'""",
            (status, actor, now, request["id"]),
        )
        await self._access_notify(request["id"], request["requester_id"], "decision", now)
        # A delayed submission acknowledgement must never claim an already
        # resolved request is still awaiting review.
        await self._db.execute(
            """UPDATE access_deliveries SET completed = TRUE, updated_at = %s
               WHERE request_id = %s AND kind = 'requested' AND NOT completed""",
            (now, request["id"]),
        )
        # Wake sent review cards for their terminal update, including a card
        # whose previous update encountered a transient transport failure.
        await self._db.execute(
            """UPDATE access_deliveries SET not_before = %s, updated_at = %s
               WHERE request_id = %s AND kind = 'review' AND NOT completed""",
            (now, now, request["id"]),
        )
        await self._access_audit(actor or "system", "access.decide", request["id"], status, now)
        return dict(request, status=status, decided_by=actor, decided_at=now)

    async def _access_expire(self, now: int) -> None:
        cursor = await self._db.execute(
            f"""SELECT {_REQUEST_COLUMNS} FROM access_requests
                WHERE status = 'pending' AND expires_at <= %s ORDER BY id FOR UPDATE""",
            (now,),
        )
        for request in await cursor.fetchall():
            await self._access_close(request, "expired", None, now)

    async def _access_cancel_inactive_reviews(self, now: int) -> None:
        # Revocation immediately prevents sending or updating a review card.
        # Once its request is terminal there is no remaining action for that
        # recipient, so retaining an unfinishable outbox row would indefinitely
        # prevent normal retention. No message is sent to the revoked admin.
        await self._db.execute(
            """UPDATE access_deliveries d SET completed = TRUE, updated_at = %s
               WHERE d.kind = 'review' AND NOT d.completed
                 AND EXISTS (SELECT 1 FROM access_requests r
                             WHERE r.id = d.request_id AND r.status <> 'pending')
                 AND NOT EXISTS (SELECT 1 FROM users u
                                 WHERE u.open_id = d.recipient_id AND u.enabled AND u.role = 'admin')""",
            (now,),
        )

    async def submit_access_request(self, message: IncomingMessage) -> tuple[str, dict | None]:
        from .store import _metadata, _now

        if message.chat_type != "p2p":
            raise ValueError("权限申请仅支持私聊")
        for value, name in (
            (message.id, "消息 ID"), (message.chat_id, "聊天 ID"), (message.sender_id, "申请人 ID")
        ):
            _metadata(value, name, 256)
        async with self._lock:
            async with self._db.transaction():
                await self._access_lock()
                now = _now()
                await self._access_expire(now)
                cursor = await self._db.execute(
                    f"SELECT {_REQUEST_COLUMNS} FROM access_requests WHERE message_id = %s", (message.id,)
                )
                duplicate = await cursor.fetchone()
                if duplicate:
                    return "duplicate", duplicate if duplicate["requester_id"] == message.sender_id else None
                cursor = await self._db.execute(
                    "SELECT open_id FROM users WHERE role = 'admin' AND enabled ORDER BY open_id"
                )
                admin_ids = [row["open_id"] for row in await cursor.fetchall()]
                await self._access_member_locks(message.sender_id, *admin_ids)
                cursor = await self._db.execute(
                    "SELECT enabled FROM users WHERE open_id = %s", (message.sender_id,)
                )
                member = await cursor.fetchone()
                if member:
                    return ("member" if member["enabled"] else "disabled"), None
                cursor = await self._db.execute(
                    f"""SELECT {_REQUEST_COLUMNS} FROM access_requests
                        WHERE requester_id = %s AND status = 'pending'""",
                    (message.sender_id,),
                )
                pending = await cursor.fetchone()
                if pending:
                    return "pending", pending
                cursor = await self._db.execute(
                    f"""SELECT {_REQUEST_COLUMNS} FROM access_requests
                        WHERE requester_id = %s AND status = 'declined' AND decided_at > %s
                        ORDER BY decided_at DESC LIMIT 1""",
                    (message.sender_id, now - ACCESS_COOLDOWN_MS),
                )
                declined = await cursor.fetchone()
                if declined:
                    return "cooldown", declined
                # Recheck after acquiring member locks: a concurrent revocation
                # cannot leave a new request addressed only to a disabled admin.
                active_admins = [actor for actor in admin_ids if await self._access_admin(actor)]
                if not active_admins:
                    return "no_admin", None
                cursor = await self._db.execute("SELECT COUNT(*) AS count FROM access_requests WHERE status = 'pending'")
                if (await cursor.fetchone())["count"] >= ACCESS_PENDING_LIMIT:
                    return "busy", None
                request_id = str(uuid4())
                await self._db.execute(
                    """INSERT INTO access_requests
                       (id, requester_id, chat_id, message_id, status, created_at, expires_at)
                       VALUES (%s, %s, %s, %s, 'pending', %s, %s)""",
                    (request_id, message.sender_id, message.chat_id, message.id, now, now + ACCESS_TTL_MS),
                )
                await self._access_notify(request_id, message.sender_id, "requested", now)
                for admin in active_admins:
                    await self._access_notify(request_id, admin, "review", now)
                await self._access_audit(message.sender_id, "access.request", request_id, "pending", now)
                return "created", await self._access_request(request_id)

    async def decide_access_request(
        self, request_id: str, actor: str, decision: str, *, card_id: str | None = None
    ) -> tuple[str, dict | None]:
        from .store import _metadata, _now

        if not _valid_uuid(request_id) or decision not in {"accept", "decline"}:
            return "invalid", None
        _metadata(actor, "审批人 ID", 256)
        if card_id is not None:
            try:
                _metadata(card_id, "审批消息 ID", 256)
            except ValueError:
                return "forbidden", None
        async with self._lock:
            async with self._db.transaction():
                await self._access_lock()
                request = await self._access_request(request_id)
                if request is None:
                    return "missing", None
                await self._access_member_locks(actor, request["requester_id"])
                if not await self._access_admin(actor):
                    return "forbidden", None
                if card_id is not None:
                    cursor = await self._db.execute(
                        """SELECT 1 FROM access_deliveries WHERE request_id = %s AND kind = 'review'
                           AND recipient_id = %s AND message_id = %s""",
                        (request_id, actor, card_id),
                    )
                    if not await cursor.fetchone():
                        return "forbidden", None
                if request["status"] != "pending":
                    return "already_decided", request
                now = _now()
                if request["expires_at"] <= now:
                    return "expired", await self._access_close(request, "expired", None, now)
                cursor = await self._db.execute(
                    "SELECT 1 FROM users WHERE open_id = %s", (request["requester_id"],)
                )
                if await cursor.fetchone():
                    return "member_changed", await self._access_close(request, "member_changed", actor, now)
                if decision == "accept":
                    await self._db.execute(
                        """INSERT INTO users (open_id, role, enabled, created_at, updated_at)
                           VALUES (%s, 'member', TRUE, %s, %s)""",
                        (request["requester_id"], now, now),
                    )
                status = "accepted" if decision == "accept" else "declined"
                return "decided", await self._access_close(request, status, actor, now)

    async def pending_access_requests(self, actor: str) -> list[dict]:
        from .store import _metadata, _now

        _metadata(actor, "审批人 ID", 256)
        async with self._lock:
            async with self._db.transaction():
                await self._access_lock()
                await self._access_member_locks(actor)
                if not await self._access_admin(actor):
                    raise PermissionError("仅启用的管理员可查看申请")
                await self._access_expire(_now())
                cursor = await self._db.execute(
                    f"""SELECT {_REQUEST_COLUMNS} FROM access_requests
                        WHERE status = 'pending' ORDER BY created_at, id LIMIT 50"""
                )
                return await cursor.fetchall()

    async def access_deliveries(self, limit: int = 20) -> list[dict]:
        from .store import _now, _positive_limit

        _positive_limit(limit, "通知批次", 200)
        async with self._lock:
            async with self._db.transaction():
                await self._access_lock()
                now = _now()
                await self._access_expire(now)
                await self._access_cancel_inactive_reviews(now)
                cursor = await self._db.execute(
                    """SELECT d.id AS delivery_id, d.request_id, d.recipient_id, d.kind, d.message_id
                       FROM access_deliveries d JOIN access_requests r ON r.id = d.request_id
                       WHERE NOT d.completed AND d.not_before <= %s
                         AND (d.kind <> 'requested' OR r.status = 'pending')
                         AND (d.kind <> 'review' OR d.message_id IS NULL OR r.status <> 'pending')
                         AND ((d.kind = 'review' AND EXISTS
                               (SELECT 1 FROM users u WHERE u.open_id = d.recipient_id AND u.enabled AND u.role = 'admin'))
                              OR (d.kind <> 'review' AND d.recipient_id = r.requester_id))
                       ORDER BY d.created_at, d.id LIMIT %s""",
                    (now, limit),
                )
                rows = await cursor.fetchall()
                for row in rows:
                    row["request"] = await self._access_request(row["request_id"])
                return rows

    async def complete_access_delivery(
        self, delivery_id: str, message_id: str, *, terminal: bool = False
    ) -> None:
        from .store import _metadata, _now

        if not _valid_uuid(delivery_id) or not isinstance(terminal, bool):
            raise ValueError("通知 ID 或完成状态无效")
        _metadata(message_id, "通知消息 ID", 256)
        async with self._lock:
            async with self._db.transaction():
                await self._access_lock()
                cursor = await self._db.execute(
                    """SELECT d.message_id, d.kind, r.status FROM access_deliveries d
                       JOIN access_requests r ON r.id = d.request_id WHERE d.id = %s""", (delivery_id,)
                )
                delivery = await cursor.fetchone()
                if delivery is None:
                    raise ValueError("通知不存在")
                if delivery["message_id"] is not None and delivery["message_id"] != message_id:
                    raise ValueError("通知已绑定其他消息")
                if terminal and delivery["kind"] == "review" and delivery["status"] == "pending":
                    raise ValueError("待审批通知不能提前完成")
                await self._db.execute(
                    """UPDATE access_deliveries SET message_id = %s,
                       completed = completed OR kind <> 'review' OR %s, updated_at = %s WHERE id = %s""",
                    (message_id, terminal, _now(), delivery_id),
                )

    async def retry_access_delivery(self, delivery_id: str) -> None:
        from .store import _now

        if not _valid_uuid(delivery_id):
            raise ValueError("通知 ID 无效")
        async with self._lock:
            now = _now()
            await self._db.execute(
                "UPDATE access_deliveries SET not_before = %s, updated_at = %s WHERE id = %s AND NOT completed",
                (now + ACCESS_RETRY_MS, now, delivery_id),
            )

    async def _purge_access(self, cutoff: int) -> dict[str, int]:
        """Called inside maintenance's transaction, never by the runtime role."""
        from .store import _now

        await self._access_lock()
        await self._access_cancel_inactive_reviews(_now())
        cursor = await self._db.execute(
            """SELECT id FROM access_requests r WHERE status <> 'pending' AND decided_at < %s
               AND NOT EXISTS (SELECT 1 FROM access_deliveries d WHERE d.request_id = r.id AND NOT d.completed)""",
            (cutoff,),
        )
        request_ids = [row["id"] for row in await cursor.fetchall()]
        cursor = await self._db.execute(
            "DELETE FROM access_deliveries WHERE request_id = ANY(%s)", (request_ids,)
        )
        deleted_deliveries = cursor.rowcount
        cursor = await self._db.execute("DELETE FROM access_requests WHERE id = ANY(%s)", (request_ids,))
        return {"access_requests": cursor.rowcount, "access_deliveries": deleted_deliveries}


ACCESS_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS access_requests (
        id TEXT PRIMARY KEY,
        requester_id TEXT NOT NULL CHECK (length(requester_id) BETWEEN 1 AND 256),
        chat_id TEXT NOT NULL CHECK (length(chat_id) BETWEEN 1 AND 256),
        message_id TEXT NOT NULL UNIQUE CHECK (length(message_id) BETWEEN 1 AND 256),
        status TEXT NOT NULL CHECK (status IN ('pending', 'accepted', 'declined', 'expired', 'member_changed')),
        created_at BIGINT NOT NULL,
        expires_at BIGINT NOT NULL,
        decided_by TEXT CHECK (length(decided_by) BETWEEN 1 AND 256),
        decided_at BIGINT
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS access_one_pending ON access_requests (requester_id) WHERE status = 'pending'",
    "CREATE INDEX IF NOT EXISTS access_expiry ON access_requests (expires_at) WHERE status = 'pending'",
    "CREATE INDEX IF NOT EXISTS access_applicant_history ON access_requests (requester_id, created_at)",
    """CREATE TABLE IF NOT EXISTS access_deliveries (
        id TEXT PRIMARY KEY,
        request_id TEXT NOT NULL REFERENCES access_requests(id) ON DELETE CASCADE,
        recipient_id TEXT NOT NULL CHECK (length(recipient_id) BETWEEN 1 AND 256),
        kind TEXT NOT NULL CHECK (kind IN ('requested', 'review', 'decision')),
        message_id TEXT CHECK (length(message_id) BETWEEN 1 AND 256),
        completed BOOLEAN NOT NULL DEFAULT FALSE,
        not_before BIGINT NOT NULL,
        created_at BIGINT NOT NULL,
        updated_at BIGINT NOT NULL,
        UNIQUE (request_id, recipient_id, kind)
    )""",
    "CREATE INDEX IF NOT EXISTS access_outbox_due ON access_deliveries (not_before) WHERE NOT completed",
    "CREATE UNIQUE INDEX IF NOT EXISTS access_sent_message ON access_deliveries (message_id) WHERE message_id IS NOT NULL",
)
