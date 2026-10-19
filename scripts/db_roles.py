"""Provision a restricted bridge login using the trusted maintenance identity.

Run in the Python image with the administrator's POSTGRES_* environment and
FEISHU_APP_ID. Supply the runtime password through a mounted local secret file,
never through an argument or a database URL in shell history. This targets the
dedicated application database; its PUBLIC CREATE/TEMP privileges are revoked.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row

from octopus.config import database_url_from_env
from octopus.store import Store


TABLE_GRANTS = {
    "meta": "SELECT",
    "sessions": "SELECT, INSERT, UPDATE, DELETE",
    "jobs": "SELECT, INSERT, UPDATE, DELETE",
    "owners": "SELECT, INSERT",
    "users": "SELECT, INSERT, UPDATE",
    "inbound_ids": "SELECT, INSERT, DELETE",
    "controls": "SELECT, INSERT, UPDATE, DELETE",
    "last_results": "SELECT, INSERT, UPDATE, DELETE",
    "audit_events": "SELECT, INSERT",
}


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,62}", value) or value.startswith("pg_"):
        raise ValueError("数据库角色和 schema 必须是安全标识符，且不能使用 pg_ 前缀")
    return value


def read_password(path: Path) -> str:
    if path.stat().st_size > 4096:
        raise ValueError("运行账号密码文件过大")
    password = path.read_text().strip()
    _password(password)
    return password


def _password(password: str) -> None:
    if not isinstance(password, str) or not 32 <= len(password) <= 1024 or any(ord(char) < 33 or ord(char) > 126 for char in password):
        raise ValueError("运行账号密码必须是 32 至 1024 个可打印 ASCII 字符，且不能包含空白")


async def provision(
    database_url: str,
    app_id: str,
    password: str,
    *,
    runtime_role: str = "octopus_runtime",
    schema: str = "octopus",
) -> None:
    """Migrate as administrator, then create or refresh one known runtime login.

    Existing roles are accepted only when this schema already records them as
    its managed runtime login. Owners and members of other roles are rejected,
    because NOINHERIT alone would still allow SET ROLE to elevate privileges.
    """
    _identifier(runtime_role)
    _identifier(schema)
    _password(password)
    store = await Store.open(database_url, app_id, schema=schema)
    await store.close()
    async with await AsyncConnection.connect(database_url, autocommit=True, row_factory=dict_row) as connection:
        async with connection.transaction():
            await connection.execute(
                "SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(%s, 0))",
                (f"octopus:db-role:{runtime_role}",),
            )
            cursor = await connection.execute("SELECT current_user AS administrator, current_database() AS database")
            context = await cursor.fetchone()
            assert context is not None
            if runtime_role == context["administrator"]:
                raise ValueError("运行账号不能与维护账号相同")
            meta = sql.Identifier(schema, "meta")
            cursor = await connection.execute(sql.SQL("SELECT value FROM {} WHERE key = 'runtime_role'").format(meta))
            registered = await cursor.fetchone()
            if registered and registered["value"] != runtime_role:
                raise ValueError("此应用已绑定另一个运行账号；请单独安排身份迁移")
            cursor = await connection.execute("SELECT oid FROM pg_catalog.pg_roles WHERE rolname = %s", (runtime_role,))
            existing = await cursor.fetchone()
            if existing:
                if not registered:
                    raise ValueError("指定角色已存在且不由此应用管理，请使用新的专用角色名")
                cursor = await connection.execute(
                    """SELECT 1 FROM pg_catalog.pg_shdepend
                       WHERE refclassid = 'pg_catalog.pg_authid'::pg_catalog.regclass AND refobjid = %s AND deptype = 'o'
                       UNION ALL SELECT 1 FROM pg_catalog.pg_auth_members WHERE member = %s LIMIT 1""",
                    (existing["oid"], existing["oid"]),
                )
                if await cursor.fetchone():
                    raise ValueError("运行账号拥有数据库对象或其他角色成员资格，不能安全复用")
            else:
                await connection.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(runtime_role)))
            # Utility statements do not accept bind parameters for passwords;
            # psycopg's Literal performs SQL quoting without shell interpolation.
            await connection.execute(
                sql.SQL("ALTER ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD {} VALID UNTIL 'infinity'").format(
                    sql.Identifier(runtime_role), sql.Literal(password)
                )
            )
            await connection.execute(sql.SQL("ALTER ROLE {} RESET ALL").format(sql.Identifier(runtime_role)))
            database = sql.Identifier(context["database"])
            role = sql.Identifier(runtime_role)
            namespace = sql.Identifier(schema)
            # PUBLIC grants are inherited by every role, independently of
            # NOINHERIT. The application uses its own database, so remove the
            # default paths by which an otherwise restricted login could do DDL.
            await connection.execute(sql.SQL("REVOKE CREATE, TEMPORARY ON DATABASE {} FROM PUBLIC").format(database))
            await connection.execute(sql.SQL("REVOKE ALL PRIVILEGES ON DATABASE {} FROM {}").format(database, role))
            await connection.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(database, role))
            cursor = await connection.execute("SELECT nspname FROM pg_catalog.pg_namespace WHERE nspname NOT LIKE 'pg_%' AND nspname <> 'information_schema'")
            for row in await cursor.fetchall():
                await connection.execute(sql.SQL("REVOKE CREATE ON SCHEMA {} FROM PUBLIC").format(sql.Identifier(row["nspname"])))
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
    store = await Store.open(database_url, app_id, schema=schema, migrate=False)
    try:
        return await store.purge_history(days=days, audit_days=audit_days)
    finally:
        await store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="使用维护账号配置最小权限运行账号或清理过期记录")
    parser.add_argument("--password-file", type=Path)
    parser.add_argument("--role", default="octopus_runtime")
    parser.add_argument("--schema", default="octopus")
    parser.add_argument("--retention", action="store_true", help="仅清理过期记录，不读取或修改运行账号密码")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--audit-days", type=int, default=90)
    args = parser.parse_args()
    if not args.retention and args.password_file is None:
        parser.error("配置运行账号时必须提供 --password-file；记录清理使用 --retention")
    try:
        app_id = os.environ.get("FEISHU_APP_ID", "")
        if not app_id:
            raise ValueError("请通过 FEISHU_APP_ID 指定独立机器人应用")
        if args.retention:
            counts = asyncio.run(retention(database_url_from_env(), app_id, schema=args.schema, days=args.days, audit_days=args.audit_days))
            print(json.dumps(counts, sort_keys=True))
            return 0
        asyncio.run(provision(database_url_from_env(), app_id, read_password(args.password_file), runtime_role=args.role, schema=args.schema))
    except Exception as exc:
        # Driver errors may include connection strings or SQL password literals.
        # Keep failures actionable without disclosing them in logs.
        print(f"数据库维护失败（{type(exc).__name__}）；检查维护账号、应用绑定、参数和本地密码文件。", file=sys.stderr)
        return 1
    print("数据库迁移和运行账号授权完成；运行账号无 DDL、角色管理或审计修改权限。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
