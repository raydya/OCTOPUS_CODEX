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
from scripts.db_roles import main, maintain


@unittest.skipUnless(os.environ.get("OCTOPUS_TEST_DATABASE_URL"), "Set OCTOPUS_TEST_DATABASE_URL to run PostgreSQL role integration tests")
class DatabaseRoleTests(unittest.IsolatedAsyncioTestCase):
    """Use an isolated database as well as roles; never alter the app database ACL."""

    async def asyncSetUp(self) -> None:
        self.admin_url = os.environ["OCTOPUS_TEST_DATABASE_URL"]
        suffix = uuid.uuid4().hex
        self.database = f"octopus_test_db_{suffix}"
        self.role = f"octopus_test_role_{suffix}"
        self.owner = f"octopus_test_owner_{suffix}"
        self.schema = "octopus_test"
        self.password = secrets.token_urlsafe(40)
        self.owner_password = secrets.token_urlsafe(40)
        self.addAsyncCleanup(self.drop_database_and_role)
        async with await AsyncConnection.connect(self.admin_url, autocommit=True) as connection:
            for role, password in ((self.owner, self.owner_password), (self.role, self.password)):
                await connection.execute(sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD {}"
                ).format(sql.Identifier(role), sql.Literal(password)))
            await connection.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(self.database), sql.Identifier(self.owner)))
            await connection.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(self.database)))
        admin_options = conninfo_to_dict(self.admin_url)
        admin_options["dbname"] = self.database
        self.database_admin_url = make_conninfo(**admin_options)
        self.database_url = make_conninfo(**dict(admin_options, user=self.owner, password=self.owner_password))
        runtime_options = dict(admin_options, user=self.role, password=self.password)
        self.runtime_url = make_conninfo(**runtime_options)

    async def drop_database_and_role(self) -> None:
        async with await AsyncConnection.connect(self.admin_url, autocommit=True) as connection:
            await connection.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(self.database)))
            await connection.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(self.role)))
            await connection.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(self.owner)))

    async def provision(self) -> None:
        await maintain(self.database_url, "cli_role_test", runtime_role=self.role, schema=self.schema)

    async def test_runtime_can_operate_bridge_but_cannot_change_schema_roles_or_audit(self) -> None:
        await self.provision()
        await self.provision()  # Reapplying grants never rotates a runtime password.
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

    async def test_owner_cannot_manage_cluster_roles_or_accept_admin_credentials(self) -> None:
        async with await AsyncConnection.connect(self.database_url, autocommit=True) as connection:
            with self.assertRaises(errors.InsufficientPrivilege):
                await connection.execute("CREATE ROLE forbidden")
            with self.assertRaises(errors.InsufficientPrivilege):
                await connection.execute(sql.SQL("ALTER ROLE {} CREATEDB").format(sql.Identifier(self.role)))
        with self.assertRaisesRegex(ValueError, "维护账号必须是本库所有者"):
            await maintain(self.database_admin_url, "cli_role_test", runtime_role=self.role, schema=self.schema)

    async def test_runtime_can_manage_access_outbox_but_cannot_erase_requests_or_audit(self) -> None:
        await self.provision()
        runtime = await Store.open(self.runtime_url, "cli_role_test", schema=self.schema, migrate=False)
        self.addAsyncCleanup(runtime.close)
        await runtime.ensure_member("admin", "admin")
        status, request = await runtime.submit_access_request(
            IncomingMessage("access-message", "applicant-chat", "applicant", "not stored", "p2p", 1000)
        )
        self.assertEqual(status, "created")
        review = next(d for d in await runtime.access_deliveries() if d["kind"] == "review")
        await runtime.retry_access_delivery(review["delivery_id"])
        await runtime.complete_access_delivery(review["delivery_id"], "admin-card")
        self.assertEqual((await runtime.decide_access_request(
            request["id"], "admin", "accept", card_id="admin-card"
        ))[0], "decided")
        await runtime.complete_access_delivery(review["delivery_id"], "admin-card", terminal=True)
        self.assertEqual((await runtime.member("applicant"))["role"], "member")
        async with await AsyncConnection.connect(self.runtime_url, autocommit=True) as connection:
            for table in ("access_requests", "access_deliveries", "audit_events"):
                with self.assertRaises(errors.InsufficientPrivilege):
                    await connection.execute(sql.SQL("DELETE FROM {}").format(sql.Identifier(self.schema, table)))
                with self.assertRaises(errors.InsufficientPrivilege):
                    await connection.execute(sql.SQL("TRUNCATE {}").format(sql.Identifier(self.schema, table)))

    async def test_privileged_member_or_object_owner_runtime_cannot_be_adopted(self) -> None:
        async with await AsyncConnection.connect(self.database_admin_url, autocommit=True) as connection:
            await connection.execute(sql.SQL("ALTER ROLE {} CREATEDB").format(sql.Identifier(self.role)))
        with self.assertRaisesRegex(ValueError, "无管理权限"):
            await self.provision()
        async with await AsyncConnection.connect(self.database_admin_url, autocommit=True) as connection:
            await connection.execute(sql.SQL("ALTER ROLE {} NOCREATEDB").format(sql.Identifier(self.role)))
            await connection.execute(sql.SQL("GRANT pg_read_all_data TO {}").format(sql.Identifier(self.role)))
        with self.assertRaisesRegex(ValueError, "其他角色成员资格"):
            await self.provision()
        async with await AsyncConnection.connect(self.database_admin_url, autocommit=True) as connection:
            await connection.execute(sql.SQL("REVOKE pg_read_all_data FROM {}").format(sql.Identifier(self.role)))
            await connection.execute(sql.SQL("CREATE SCHEMA forbidden_owner AUTHORIZATION {}").format(sql.Identifier(self.role)))
        with self.assertRaisesRegex(ValueError, "拥有数据库对象"):
            await self.provision()

    async def test_missing_runtime_or_invalid_identity_fails_before_migration(self) -> None:
        for role in ("pg_unsafe", "bad;role", "bad-role", "x" * 64, ""):
            with self.assertRaises(ValueError):
                await maintain(self.database_url, "cli_role_test", runtime_role=role, schema=self.schema)
        with self.assertRaisesRegex(ValueError, "尚未由基础设施创建"):
            await maintain(self.database_url, "cli_role_test", runtime_role="missing_runtime_" + uuid.uuid4().hex, schema=self.schema)
        async with await AsyncConnection.connect(self.database_url, autocommit=True) as connection:
            cursor = await connection.execute("SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname = %s", (self.schema,))
            self.assertIsNone(await cursor.fetchone())

    async def test_app_and_runtime_binding_survive_maintenance(self) -> None:
        await self.provision()
        with self.assertRaisesRegex(ValueError, "已绑定其他飞书应用"):
            await maintain(self.database_url, "cli_other_app", runtime_role=self.role, schema=self.schema)
        async with await AsyncConnection.connect(self.database_url, autocommit=True) as connection:
            await connection.execute(sql.SQL("UPDATE {} SET value = 'different_runtime' WHERE key = 'runtime_role'").format(
                sql.Identifier(self.schema, "meta")))
        with self.assertRaisesRegex(ValueError, "已绑定另一个运行账号"):
            await self.provision()

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
            redirect_stdout(output),
        ):
            self.assertEqual(await asyncio.to_thread(main), 0)
        self.assertEqual(json.loads(output.getvalue()), {"jobs": 0, "controls": 0, "inbound_ids": 0, "last_results": 1, "audit_events": 1, "access_requests": 0, "access_deliveries": 0})
        self.assertIsNone(await maintenance.last_result("session"))


if __name__ == "__main__":
    unittest.main()
