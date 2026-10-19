from __future__ import annotations

import asyncio
import io
import json
import os
import secrets
import unittest
import uuid
from contextlib import redirect_stdout
from unittest.mock import patch

from psycopg import AsyncConnection, errors, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from octopus.contracts import IncomingMessage
from octopus.store import Store
from scripts.db_roles import main, provision


@unittest.skipUnless(os.environ.get("OCTOPUS_TEST_DATABASE_URL"), "Set OCTOPUS_TEST_DATABASE_URL to run PostgreSQL role integration tests")
class DatabaseRoleTests(unittest.IsolatedAsyncioTestCase):
    """Use an isolated database as well as roles; never alter the app database ACL."""

    async def asyncSetUp(self) -> None:
        self.admin_url = os.environ["OCTOPUS_TEST_DATABASE_URL"]
        suffix = uuid.uuid4().hex
        self.database = f"octopus_test_db_{suffix}"
        self.role = f"octopus_test_role_{suffix}"
        self.schema = "octopus_test"
        self.password = secrets.token_urlsafe(40)
        async with await AsyncConnection.connect(self.admin_url, autocommit=True) as connection:
            await connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(self.database)))
        self.addAsyncCleanup(self.drop_database_and_role)
        admin_options = conninfo_to_dict(self.admin_url)
        admin_options["dbname"] = self.database
        self.database_url = make_conninfo(**admin_options)
        runtime_options = dict(admin_options, user=self.role, password=self.password)
        self.runtime_url = make_conninfo(**runtime_options)

    async def drop_database_and_role(self) -> None:
        async with await AsyncConnection.connect(self.admin_url, autocommit=True) as connection:
            await connection.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(self.database)))
            await connection.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(self.role)))

    async def provision(self) -> None:
        await provision(self.database_url, "cli_role_test", self.password, runtime_role=self.role, schema=self.schema)

    async def test_runtime_can_operate_bridge_but_cannot_change_schema_roles_or_audit(self) -> None:
        await self.provision()
        await self.provision()  # Refreshing the managed role is idempotent.
        runtime = await Store.open(self.runtime_url, "cli_role_test", schema=self.schema, migrate=False)
        self.addAsyncCleanup(runtime.close)
        await runtime.add_owner("admin")
        await runtime.ensure_member("member")
        await runtime.session("session", "chat", "member")
        request = IncomingMessage("job", "chat", "member", "question", "p2p", 1000)
        self.assertEqual(await runtime.admission("session", request), "accepted")
        self.assertTrue(await runtime.start_job("job"))
        await runtime.save_reaction("job", "reaction")
        await runtime.save_result("session", "answer", "done")
        await runtime.finish_job("job")
        await runtime.audit("member", "query", "code:repository", job_id="job")
        await runtime.set_member_enabled("member", False)
        self.assertEqual(await runtime.last_result("session"), {"text": "answer", "status": "done"})
        self.assertFalse((await runtime.member("member"))["enabled"])
        self.assertTrue(await runtime.acquire_service_lock())
        async with await AsyncConnection.connect(self.runtime_url, autocommit=True) as connection:
            statements = [
                sql.SQL("CREATE TABLE {} (id int)").format(sql.Identifier(self.schema, "forbidden")),
                sql.SQL("CREATE TABLE public.forbidden (id int)"),
                sql.SQL("CREATE TEMP TABLE forbidden (id int)"),
                sql.SQL("CREATE SCHEMA forbidden"),
                sql.SQL("CREATE ROLE forbidden"),
                sql.SQL("ALTER ROLE {} SUPERUSER").format(sql.Identifier(self.role)),
                sql.SQL("ALTER TABLE {} ADD COLUMN forbidden int").format(sql.Identifier(self.schema, "jobs")),
                sql.SQL("UPDATE {} SET decision = 'changed'").format(sql.Identifier(self.schema, "audit_events")),
                sql.SQL("DELETE FROM {}").format(sql.Identifier(self.schema, "audit_events")),
                sql.SQL("TRUNCATE {}").format(sql.Identifier(self.schema, "audit_events")),
                sql.SQL("UPDATE {} SET value = 'cli_wrong_app' WHERE key = 'app_id'").format(sql.Identifier(self.schema, "meta")),
            ]
            for statement in statements:
                with self.assertRaises(errors.InsufficientPrivilege):
                    await connection.execute(statement)
            cursor = await connection.execute(sql.SQL("SELECT decision FROM {}").format(sql.Identifier(self.schema, "audit_events")))
            self.assertEqual(await cursor.fetchall(), [("allow",)])
        with self.assertRaises(errors.InsufficientPrivilege):
            await runtime.purge_history()
        with self.assertRaises(errors.InsufficientPrivilege):
            await Store.open(self.runtime_url, "cli_role_test", schema=self.schema)
        maintenance = await Store.open(self.database_url, "cli_role_test", schema=self.schema)
        self.addAsyncCleanup(maintenance.close)
        self.assertEqual((await maintenance.purge_history())["audit_events"], 0)

    async def test_existing_unmanaged_or_privileged_role_cannot_be_adopted(self) -> None:
        async with await AsyncConnection.connect(self.database_url, autocommit=True) as connection:
            await connection.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(self.role)))
        with self.assertRaisesRegex(ValueError, "不由此应用管理"):
            await self.provision()
        async with await AsyncConnection.connect(self.database_url, autocommit=True) as connection:
            await connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(self.role)))
        await self.provision()
        async with await AsyncConnection.connect(self.database_url, autocommit=True) as connection:
            await connection.execute(sql.SQL("GRANT pg_read_all_data TO {}").format(sql.Identifier(self.role)))
        with self.assertRaisesRegex(ValueError, "其他角色成员资格"):
            await self.provision()
        async with await AsyncConnection.connect(self.database_url, autocommit=True) as connection:
            await connection.execute(sql.SQL("REVOKE pg_read_all_data FROM {}").format(sql.Identifier(self.role)))
            await connection.execute(sql.SQL("CREATE SCHEMA forbidden_owner AUTHORIZATION {}").format(sql.Identifier(self.role)))
        with self.assertRaisesRegex(ValueError, "拥有数据库对象"):
            await self.provision()

    async def test_invalid_identity_or_password_fails_before_migration(self) -> None:
        for role in ("pg_unsafe", "bad;role", "bad-role", "x" * 64, ""):
            with self.assertRaises(ValueError):
                await provision(self.database_url, "cli_role_test", self.password, runtime_role=role, schema=self.schema)
        for password in ("short", "x" * 32 + "\n", "x" * 1025, "中" * 32):
            with self.assertRaises(ValueError):
                await provision(self.database_url, "cli_role_test", password, runtime_role=self.role, schema=self.schema)
        async with await AsyncConnection.connect(self.database_url, autocommit=True) as connection:
            cursor = await connection.execute("SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname = %s", (self.schema,))
            self.assertIsNone(await cursor.fetchone())

    async def test_retention_cli_needs_no_runtime_secret_and_prints_only_deletion_counts(self) -> None:
        await self.provision()
        maintenance = await Store.open(self.database_url, "cli_role_test", schema=self.schema, migrate=False)
        self.addAsyncCleanup(maintenance.close)
        with patch("octopus.store._now", return_value=1000):
            await maintenance.audit("member", "old-query")
            await maintenance.session("session", "chat", "member")
            await maintenance.save_result("session", "sensitive old answer", "done")
        await maintenance.audit("member", "recent-query")
        output = io.StringIO()
        with (
            patch.dict(os.environ, {"FEISHU_APP_ID": "cli_role_test"}),
            patch("sys.argv", ["db_roles.py", "--retention", "--schema", self.schema]),
            patch("scripts.db_roles.database_url_from_env", return_value=self.database_url),
            patch("scripts.db_roles.read_password", side_effect=AssertionError("Must not read runtime password")),
            redirect_stdout(output),
        ):
            self.assertEqual(await asyncio.to_thread(main), 0)
        self.assertEqual(json.loads(output.getvalue()), {"jobs": 0, "controls": 0, "inbound_ids": 0, "last_results": 1, "audit_events": 1})
        self.assertIsNone(await maintenance.last_result("session"))


if __name__ == "__main__":
    unittest.main()
