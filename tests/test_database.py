from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from psycopg import OperationalError, errors

from octopus.database import connect_database


class DatabaseStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_external_database_can_start_after_the_application(self) -> None:
        connection = object()
        connect = AsyncMock(side_effect=[OperationalError("starting"), connection])
        with patch("octopus.database.AsyncConnection.connect", connect), patch(
            "octopus.database.asyncio.sleep", AsyncMock(),
        ):
            self.assertIs(await connect_database("redacted-test-dsn"), connection)
        self.assertEqual(connect.await_count, 2)

    async def test_unavailable_database_stops_at_deadline_without_exposing_dsn(self) -> None:
        async def unavailable(*args, **kwargs):
            await asyncio.sleep(10)

        with patch("octopus.database.AsyncConnection.connect", unavailable):
            with self.assertRaisesRegex(OperationalError, "等待期限"):
                await connect_database("password=never-print-this", timeout=0.01)

    async def test_authorization_failure_is_not_retried(self) -> None:
        connect = AsyncMock(side_effect=errors.InvalidPassword("invalid"))
        with patch("octopus.database.AsyncConnection.connect", connect):
            with self.assertRaises(errors.InvalidPassword):
                await connect_database("redacted-test-dsn")
        connect.assert_awaited_once()

    async def test_cancellation_does_not_retry(self) -> None:
        connect = AsyncMock(side_effect=asyncio.CancelledError())
        with patch("octopus.database.AsyncConnection.connect", connect):
            with self.assertRaises(asyncio.CancelledError):
                await connect_database("redacted-test-dsn")
        connect.assert_awaited_once()
