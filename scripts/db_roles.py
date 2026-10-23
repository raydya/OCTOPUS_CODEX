"""Migrate this application's schema and grant an existing restricted login.

The infrastructure service owns role creation, passwords and cross-database
access. Run with octopus_owner, never a cluster administrator. This command
neither reads the runtime password nor creates/alters roles.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys

from psycopg import AsyncConnection, sql

from octopus.config import database_url_from_env
from octopus.database import connect_database
from octopus.store import Store


TABLE_GRANTS = {
    "meta": "SELECT",
    "topics": "SELECT, INSERT, UPDATE, DELETE",
    "topic_routes": "SELECT, INSERT, UPDATE, DELETE",
    "topic_messages": "SELECT, INSERT, UPDATE, DELETE",
    "topic_actors": "SELECT, INSERT, UPDATE, DELETE",
    "topic_pending": "SELECT, INSERT, UPDATE, DELETE",
    "sessions": "SELECT, INSERT, UPDATE, DELETE",
    "jobs": "SELECT, INSERT, UPDATE, DELETE",
    "owners": "SELECT, INSERT",
    "users": "SELECT, INSERT, UPDATE",
    "inbound_ids": "SELECT, INSERT, DELETE",
    "controls": "SELECT, INSERT, UPDATE, DELETE",
    "last_results": "SELECT, INSERT, UPDATE, DELETE",
    "audit_events": "SELECT, INSERT",
    "access_requests": "SELECT, INSERT, UPDATE",
    "access_deliveries": "SELECT, INSERT, UPDATE",
}


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,62}", value) or value.startswith("pg_"):
        raise ValueError("数据库角色和 schema 必须是安全标识符，且不能使用 pg_ 前缀")
    return value


async def _owner_context(connection: AsyncConnection) -> dict:
    cursor = await connection.execute(
        """SELECT current_user AS owner, current_database() AS database,
                  r.rolsuper, r.rolcreatedb, r.rolcreaterole, r.rolreplication, r.rolbypassrls,
                  d.datdba = r.oid AS owns_database,
                  EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members WHERE member = r.oid) AS has_membership
           FROM pg_catalog.pg_roles r JOIN pg_catalog.pg_database d ON d.datname = current_database()
           WHERE r.rolname = current_user"""
    )
    context = await cursor.fetchone()
    if not context or not context["owns_database"] or context["has_membership"] or any(
        context[key] for key in ("rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls")
    ):
        raise ValueError("维护账号必须是本库所有者，不能有集群管理权限或其他角色成员资格")
    return context


async def _runtime_identity(connection: AsyncConnection, runtime_role: str, owner: str) -> None:
    if runtime_role == owner:
        raise ValueError("运行账号不能与维护账号相同")
    cursor = await connection.execute(
        """SELECT oid, rolcanlogin, rolinherit, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls
           FROM pg_catalog.pg_roles WHERE rolname = %s""", (runtime_role,),
    )
    existing = await cursor.fetchone()
    if not existing:
        raise ValueError("运行账号尚未由基础设施创建")
    if not existing["rolcanlogin"] or any(
        existing[key] for key in ("rolinherit", "rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls")
    ):
        raise ValueError("运行账号必须是无管理权限的专用 NOINHERIT LOGIN")
    cursor = await connection.execute(
        """SELECT 1 FROM pg_catalog.pg_shdepend
           WHERE refclassid = 'pg_catalog.pg_authid'::pg_catalog.regclass AND refobjid = %s AND deptype = 'o'
           UNION ALL SELECT 1 FROM pg_catalog.pg_auth_members WHERE member = %s LIMIT 1""",
        (existing["oid"], existing["oid"]),
    )
    if await cursor.fetchone():
        raise ValueError("运行账号拥有数据库对象或其他角色成员资格，不能安全复用")


async def maintain(
    database_url: str,
    app_id: str,
    *,
    runtime_role: str = "octopus_runtime",
    schema: str = "octopus",
) -> None:
    """Migrate using the database owner, then grant the pre-provisioned runtime.

    Infrastructure validates the intended role during first installation. Once
    initialized, meta binds both the Feishu app and runtime role across upgrades.
    """
    _identifier(runtime_role)
    _identifier(schema)
    # Fail before any schema changes if handed administrator/runtime credentials
    # or an unsafe infrastructure-provisioned identity.
    async with await connect_database(database_url) as connection:
        context = await _owner_context(connection)
        await _runtime_identity(connection, runtime_role, context["owner"])
    store = await Store.open(database_url, app_id, schema=schema)
    await store.close()
    async with await connect_database(database_url) as connection:
        async with connection.transaction():
            await connection.execute(
                "SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(%s, 0))",
                (f"octopus:db-role:{runtime_role}",),
            )
            context = await _owner_context(connection)
            await _runtime_identity(connection, runtime_role, context["owner"])
            meta = sql.Identifier(schema, "meta")
            cursor = await connection.execute(sql.SQL("SELECT value FROM {} WHERE key = 'runtime_role'").format(meta))
            registered = await cursor.fetchone()
            if registered and registered["value"] != runtime_role:
                raise ValueError("此应用已绑定另一个运行账号；请单独安排身份迁移")
            database = sql.Identifier(context["database"])
            role = sql.Identifier(runtime_role)
            namespace = sql.Identifier(schema)
            # PUBLIC grants are inherited by every role, independently of
            # NOINHERIT. The application uses its own database, so remove the
            # default paths by which an otherwise restricted login could do DDL.
            await connection.execute(sql.SQL("REVOKE CREATE, TEMPORARY ON DATABASE {} FROM PUBLIC").format(database))
            await connection.execute(sql.SQL("REVOKE ALL PRIVILEGES ON DATABASE {} FROM {}").format(database, role))
            await connection.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(database, role))
            await connection.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            await connection.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC, {}").format(namespace, role))
            await connection.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(namespace, role))
            await connection.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM PUBLIC, {}").format(namespace, role))
            await connection.execute(sql.SQL("REVOKE ALL ON ALL SEQUENCES IN SCHEMA {} FROM PUBLIC, {}").format(namespace, role))
            for table, privileges in TABLE_GRANTS.items():
                await connection.execute(sql.SQL("GRANT {} ON TABLE {} TO {}").format(sql.SQL(privileges), sql.Identifier(schema, table), role))
            await connection.execute(sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}").format(namespace, role))
            await connection.execute(
                sql.SQL("INSERT INTO {} (key, value) VALUES ('runtime_role', %s) ON CONFLICT (key) DO NOTHING").format(meta),
                (runtime_role,),
            )


async def retention(database_url: str, app_id: str, *, schema: str = "octopus", days: int = 30, audit_days: int = 90) -> dict[str, int]:
    """Purge expired data using maintenance credentials without rotating a login."""
    _identifier(schema)
    async with await connect_database(database_url) as connection:
        await _owner_context(connection)
    store = await Store.open(database_url, app_id, schema=schema, migrate=False)
    try:
        return await store.purge_history(days=days, audit_days=audit_days)
    finally:
        await store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="使用本库 owner 迁移、授权已有运行账号或清理过期记录")
    parser.add_argument("--role", default="octopus_runtime")
    parser.add_argument("--schema", default="octopus")
    parser.add_argument("--retention", action="store_true", help="仅清理过期记录，不读取或修改运行账号密码")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--audit-days", type=int, default=90)
    args = parser.parse_args()
    try:
        app_id = os.environ.get("FEISHU_APP_ID", "")
        if not app_id:
            raise ValueError("请通过 FEISHU_APP_ID 指定独立机器人应用")
        if args.retention:
            counts = asyncio.run(retention(database_url_from_env(), app_id, schema=args.schema, days=args.days, audit_days=args.audit_days))
            print(json.dumps(counts, sort_keys=True))
            return 0
        asyncio.run(maintain(database_url_from_env(), app_id, runtime_role=args.role, schema=args.schema))
    except Exception as exc:
        # Driver errors may include connection strings or SQL password literals.
        # Keep failures actionable without disclosing them in logs.
        print(f"数据库维护失败（{type(exc).__name__}）；检查本库 owner、预建运行账号、应用绑定和参数。", file=sys.stderr)
        return 1
    print("数据库迁移和运行账号授权完成；运行账号无 DDL、角色管理或审计修改权限。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
