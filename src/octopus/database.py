"""Bounded startup connection retry for the independently managed PostgreSQL."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from psycopg import AsyncConnection, OperationalError
from psycopg.rows import dict_row


async def connect_database(database_url: str, *, timeout: float = 30) -> AsyncConnection[dict[str, Any]]:
    """Wait for PostgreSQL availability, without retrying SQL or leaking DSNs."""
    if timeout <= 0:
        raise ValueError("数据库连接等待时间必须大于零")
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OperationalError("PostgreSQL 在连接等待期限内未就绪") from None
        try:
            return await asyncio.wait_for(
                AsyncConnection.connect(
                    database_url, autocommit=True, row_factory=dict_row, connect_timeout=5,
                ),
                timeout=remaining,
            )
        except OperationalError as exc:
            # Invalid credentials/authorization cannot recover by waiting.
            if exc.sqlstate and exc.sqlstate.startswith("28"):
                raise
        except TimeoutError:
            raise OperationalError("PostgreSQL 在连接等待期限内未就绪") from None
        remaining = deadline - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(min(2, remaining))
