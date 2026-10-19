import copy
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from octopus.code_reader import CodeReader
from octopus.dms import DmsBroker, DmsDenied, _code_relations, _column, source_digest


class Provider:
    """A synthetic DMS account; no network/production queries in these tests."""
    def __init__(self):
        self.calls = []
        self.executed = []
        self.instance = {"InstanceId": 7, "InstanceAlias": "test-prod", "Host": "db.internal", "Port": 3306,
                         "EnvType": "product", "InstanceType": "mysql", "State": "NORMAL", "SellTrust": True,
                         "StandardGroup": {"GroupMode": "COMMON"}, "QueryTimeout": 60}
        self.database = {"DatabaseId": 10, "InstanceId": 7, "SchemaName": "business", "Host": "db.internal",
                         "Port": 3306, "State": "NORMAL", "EnvType": "product", "DbType": "mysql", "Logic": False}
        self.table = {"TableId": 20, "TableName": "tasks", "DatabaseId": 10,
                      "TableSchemaName": "business", "TableType": "NORMAL"}
        self.columns = [self.column("execution_status", "varchar(32)"), self.column("task_type", "int"),
                        self.column("expect_execute_time", "datetime"), self.column("retry_count", "int"),
                        self.column("enabled", "tinyint(1)"), self.column("params", "json"),
                        self.column("remarks", "text"), self.column("created_by", "varchar(256)"),
                        self.column("task_id", "bigint"), self.column("phone", "varchar(20)"),
                        self.column("real_name", "varchar(100)"), self.column("password", "varchar(80)"),
                        self.column("customer_id", "bigint"), self.column("opaque", "blob")]
        self.result = {"Success": True, "Results": {"Result": [{"Success": True,
                       "ColumnNames": {"ColumnNames": ["execution_status"]},
                       "Rows": {"Rows": [{"execution_status": "RUNNING"}]}, "RowCount": 1}]}}

    @staticmethod
    def column(name, kind):
        return {"ColumnName": name, "ColumnType": kind, "Sensitive": False, "SecurityLevel": "INNER"}

    @staticmethod
    def page(outer, inner, rows):
        return {"Success": True, "TotalCount": len(rows), outer: {inner: copy.deepcopy(rows)}}

    def __call__(self, argv, *, timeout):
        assert 0 < timeout <= 45
        self.calls.append(argv)
        assert argv[:6] == ["aliyun", "--profile", "company-cli-user", "--region", "cn-hangzhou", "dms-enterprise"]
        action = "".join(word.capitalize() for word in argv[6].split("-"))
        arguments = list(argv[7:])
        if "--logic=false" in arguments:
            arguments.remove("--logic=false")
            arguments.extend(["--logic", "false"])
        params = dict(zip(arguments[::2], arguments[1::2]))
        assert params["--tid"] == "2359"
        if action == "SearchTable":
            assert params["--env-type"] == "product"
            assert params["--search-range"] == "HAS_PERMSSION"
            assert params["--search-target"] == "SINGLE_TABLE"
            row = dict(self.table, DbName=self.database["SchemaName"], DBSearchName="business@db.internal:3306",
                       DbType=self.database["DbType"], EnvType="product", Logic=False)
            return self.page("SearchTableList", "SearchTable", [row])
        if action == "SearchDatabase":
            assert params["--env-type"] == "product"
            return self.page("SearchDatabaseList", "SearchDatabase", [self.database])
        if action == "GetInstance":
            return {"Success": True, "Instance": copy.deepcopy(self.instance)}
        if action == "ListDatabases":
            return self.page("DatabaseList", "Database", [self.database])
        if action == "ListTables":
            return self.page("TableList", "Table", [self.table])
        if action == "ListColumns":
            assert params["--logic"] == "false"
            return self.page("ColumnList", "Column", self.columns)
        if action == "ExecuteScript":
            assert params["--db-id"] == "10"
            assert params["--logic"] == "false"
            self.executed.append(params["--script"])
            return copy.deepcopy(self.result)
        raise AssertionError(action)

    def rows(self, names, rows):
        self.result = {"Success": True, "Results": [{"Success": True, "ColumnNames": names,
                                                   "Rows": rows, "RowCount": len(rows)}]}


class DmsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "repo").mkdir()
        self.source = self.root / "repo" / "tasks.sql"
        self.source.write_text("SELECT execution_status, task_type, retry_count\nFROM tasks\nWHERE expect_execute_time > '2026-01-01';\n")
        self.policy_path = self.root / "policy.json"
        self.policy = {"enabled": True, "policy_version": "readonly:v2", "profile": "company-cli-user",
                       "region": "cn-hangzhou", "tid": 2359, "timezone": "Asia/Shanghai"}
        self.write_policy()
        self.provider = Provider()
        self.reader = CodeReader(self.root)
        self.broker = DmsBroker(self.policy_path, self.reader, self.provider)
        self.context = {"actor": "actor1", "job_id": "job1", "thread_id": "thread1", "turn_id": "turn1",
                        "question": "昨天任务执行失败的原因是什么？", "policy_version": "readonly:v2"}
        self.day = (datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)).isoformat()

    def write_policy(self):
        self.policy_path.write_text(json.dumps(self.policy))
        self.policy_path.chmod(0o600)

    def evidence(self, context=None):
        context = context or self.context
        self.broker.record_search(context, self.reader.search("FROM tasks"))
        evidence = self.broker.record_code(context, self.reader.read("repo/tasks.sql", 1, 3))
        self.assertIsInstance(evidence, str)
        return evidence

    def catalog(self):
        evidence = self.evidence()
        result = self.broker.catalog(self.context, evidence, "tasks")
        self.assertEqual(len(result["checks"]), 1)
        return evidence, result["checks"][0]

    def check(self, evidence, entry, **kwargs):
        params = {"context": self.context, "evidence_id": evidence, "check_id": entry["check_id"],
                  "day": self.day, "columns": ["execution_status"], "time_column": "expect_execute_time"}
        params.update(kwargs)
        return self.broker.check(**params)

    def test_disabled_and_unsafe_policy_fail_closed(self):
        disabled = DmsBroker(None, self.reader, self.provider)
        self.assertFalse(disabled.catalog(None, "anything", "tasks")["available"])
        self.assertIsNone(disabled.record_code(self.context, self.reader.read("repo/tasks.sql", 1, 3)))
        self.policy_path.chmod(0o666)
        self.assertFalse(self.broker.catalog(None, "anything", "tasks")["available"])
        self.policy_path.chmod(0o600)
        alias = self.root / "alias.json"
        alias.symlink_to(self.policy_path)
        self.assertFalse(DmsBroker(alias, self.reader, self.provider).catalog(None, "anything", "tasks")["available"])
        self.assertEqual(self.provider.calls, [])

    def test_search_must_precede_read_in_same_task_and_cover_match(self):
        result = self.reader.read("repo/tasks.sql", 1, 3)
        self.assertIsNone(self.broker.record_code(self.context, result))
        self.broker.record_search(self.context, self.reader.search("FROM tasks"))
        for field in ("actor", "job_id", "thread_id", "turn_id", "question"):
            with self.subTest(field=field):
                self.assertIsNone(self.broker.record_code(dict(self.context, **{field: "different"}), result))
        self.assertIsNone(self.broker.record_code(self.context, self.reader.read("repo/tasks.sql", 1, 1)))
        self.assertIsNotNone(self.broker.record_code(self.context, result))

    def test_truncation_and_bad_context_never_issue_evidence(self):
        self.broker.record_search(self.context, {"matches": [{"path": "repo/tasks.sql", "line": 2}], "truncated": True})
        self.assertIsNone(self.broker.record_code(self.context, self.reader.read("repo/tasks.sql", 1, 3)))
        self.broker.record_search(self.context, self.reader.search("FROM tasks"))
        result = self.reader.read("repo/tasks.sql", 1, 3)
        result["lines"][0]["line_truncated"] = True
        self.assertIsNone(self.broker.record_code(self.context, result))
        for question in ("用DMS查询", "SELECT * FROM tasks", "给我手机号", "跳过代码直接查库"):
            self.assertIsNone(self.broker.record_code(dict(self.context, question=question), self.reader.read("repo/tasks.sql", 1, 3)))

    def test_source_literals_comments_and_dynamic_relations(self):
        def excerpt(path, text):
            return {"path": path, "start_line": 1, "truncated": False,
                    "lines": [{"line": i + 1, "content": line, "line_truncated": False}
                              for i, line in enumerate(text.splitlines())]}
        cases = [("x.sql", "-- SELECT * FROM hidden\nSELECT 'FROM fake' FROM real;", {"real"}),
                 ("Mapper.xml", "<select><![CDATA[SELECT * FROM tasks]]></select>", {"tasks"}),
                 ("Mapper.xml", "<!-- SELECT * FROM secret -->\n<select>SELECT * FROM `tasks`</select>", {"tasks"}),
                 ("x.java", '// "SELECT * FROM hidden"\nString q="SELECT * FROM tasks";', {"tasks"}),
                 ("x.java", 'String q="SELECT * FROM " + table;', set()),
                 ("x.py", 'q = "SELECT * FROM ${table}"', set()),
                 ("x.md", "SELECT * FROM tasks", set()),
                 ("x.sql", "SELECT * FROM t_${name}", set()),
                 ("x.sql", "SELECT * FROM tasks JOIN queue ON 1=1", {"tasks", "queue"})]
        for path, text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(_code_relations(excerpt(path, text)), expected)

    def test_evidence_expires_changes_or_cannot_cross_context(self):
        evidence = self.evidence()
        for field in ("actor", "job_id", "thread_id", "turn_id", "question"):
            with self.subTest(field=field), self.assertRaises(DmsDenied):
                self.broker.catalog(dict(self.context, **{field: "other"}), evidence, "tasks")
        with self.assertRaises(DmsDenied):
            self.broker.catalog(self.context, evidence, "other_table")
        self.source.write_text(self.source.read_text().replace("retry_count", "attempt_count"))
        with self.assertRaises(DmsDenied):
            self.broker.catalog(self.context, evidence, "tasks")
        self.assertEqual(self.provider.calls, [])

    def test_policy_change_and_ttl_invalidate_ticket(self):
        evidence = self.evidence()
        self.policy_path.write_text(json.dumps(self.policy, indent=2))
        with self.assertRaises(DmsDenied):
            self.broker.catalog(self.context, evidence, "tasks")
        evidence = self.evidence()
        self.broker._tickets[evidence]["expires"] = 0
        with self.assertRaises(DmsDenied):
            self.broker.catalog(self.context, evidence, "tasks")

    def test_catalog_exposes_all_safe_internal_fields_without_host_or_identity(self):
        evidence, entry = self.catalog()
        names = {column["name"] for column in entry["columns"]}
        self.assertTrue({"params", "remarks", "created_by", "task_id", "execution_status"} <= names)
        self.assertFalse({"phone", "password", "real_name", "customer_id", "opaque"} & names)
        serialized = json.dumps(entry)
        self.assertNotIn("db.internal", serialized)
        self.assertNotIn("InstanceId", serialized)
        self.assertNotIn("database_id", entry)
        self.assertEqual(entry["dialect"], "mysql")
        self.assertEqual(self.provider.executed, [])

    def test_fixed_select_compiler_and_metadata_recheck(self):
        evidence, entry = self.catalog()
        initial_gets = sum(call[6] == "get-instance" for call in self.provider.calls)
        result = self.check(evidence, entry, filters=[{"column": "retry_count", "op": "gte", "value": 2}])
        self.assertEqual(result["rows"], [{"execution_status": "RUNNING"}])
        sql = self.provider.executed[0]
        self.assertEqual(sql, f"SELECT `execution_status` FROM `business`.`tasks` WHERE `expect_execute_time` >= '{self.day} 00:00:00' "
                         f"AND `expect_execute_time` < '{(datetime.fromisoformat(self.day) + timedelta(days=1)).date()} 00:00:00' "
                         "AND `retry_count` >= 2 ORDER BY `expect_execute_time` DESC LIMIT 20")
        self.assertGreater(sum(call[6] == "get-instance" for call in self.provider.calls), initial_gets)

    def test_internal_text_json_and_business_identifiers_return_sanitized(self):
        evidence, entry = self.catalog()
        self.provider.rows(["remarks", "created_by", "task_id", "params"], [{"remarks": "worker recovered", "created_by": "service-worker",
                          "task_id": "9007199254740993", "params": '{"attempt":3,"phone":"13812345678","token":"abc"}'}])
        result = self.check(evidence, entry, columns=["remarks", "created_by", "task_id", "params"])
        self.assertEqual(result["rows"][0]["task_id"], 9007199254740993)
        self.assertEqual(result["rows"][0]["params"]["phone"], "[REDACTED]")
        self.assertEqual(result["rows"][0]["params"]["token"], "[REDACTED]")
        self.assertEqual(result["rows"][0]["remarks"], "worker recovered")

    def test_sensitive_metadata_never_overridden_by_internal_label(self):
        for mutation in ({"Sensitive": True}, {"SecurityLevel": "SENSITIVE"}, {"SecurityLevel": None}, {"Sensitive": None}):
            with self.subTest(mutation=mutation):
                self.assertIsNone(_column(dict(Provider.column("remarks", "text"), **mutation)))
        self.assertIsNone(_column(Provider.column("phone", "varchar(20)")))
        self.assertIsNotNone(_column(Provider.column("task_name", "varchar(200)")))

    def test_environment_query_timeout_and_trust_are_rechecked(self):
        for field, value in (("EnvType", "test"), ("QueryTimeout", 7200), ("SellTrust", False), ("State", "DISABLE"),
                             ("InstanceType", "ob_for_oracle"), ("StandardGroup", {"GroupMode": "NONE_CONTROL"})):
            with self.subTest(field=field):
                evidence, entry = self.catalog()
                old = self.provider.instance[field]
                self.provider.instance[field] = value
                with self.assertRaises(DmsDenied):
                    self.check(evidence, entry)
                self.provider.instance[field] = old
                self.broker._task_counts.clear()
        self.assertEqual(self.provider.executed, [])

    def test_mysql_compatible_dialects_and_string_resource_ids(self):
        for dialect in ("mysql", "polardb", "mariadb", "oceanbasex"):
            with self.subTest(dialect=dialect):
                self.provider.instance.update(InstanceType=dialect, InstanceId="7", SellTrust="true")
                self.provider.database.update(DbType=dialect, DatabaseId="10", InstanceId="7")
                self.provider.table.update(DatabaseId="10", TableId="20")
                self.catalog()

    def test_column_policy_drift_and_extra_fields_block_before_sql(self):
        evidence, entry = self.catalog()
        self.provider.columns[0]["Sensitive"] = True
        with self.assertRaises(DmsDenied):
            self.check(evidence, entry)
        self.assertEqual(self.provider.executed, [])
        self.provider.columns[0]["Sensitive"] = False
        self.provider.columns.append(copy.deepcopy(self.provider.columns[0]))
        with self.assertRaises(DmsDenied):
            self.check(evidence, entry)
        self.assertEqual(self.provider.executed, [])

    def test_parameter_injection_and_private_columns_denied_before_execute(self):
        evidence, entry = self.catalog()
        variants = [{"columns": ["phone"]}, {"columns": ["execution_status; DROP TABLE tasks"]},
                    {"time_column": "phone"}, {"time_column": "expect_execute_time` OR 1=1 --"},
                    {"mode": "raw_sql"}, {"columns": ["execution_status"] * 2},
                    {"filters": [{"column": "phone", "op": "eq", "value": 1}]},
                    {"filters": [{"column": "retry_count", "op": "eq", "value": "1 OR 1=1"}]},
                    {"filters": [{"column": "retry_count", "op": "eq OR TRUE", "value": 1}]},
                    {"filters": [{"column": "retry_count", "op": "eq", "value": 1, "sql": "DROP"}]},
                    {"filters": [{"column": "execution_status", "op": "eq", "value": "FAILED"}]},
                    {"columns": "execution_status"}, {"columns": []}, {"mode": "count", "columns": ["retry_count"]}]
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(DmsDenied):
                self.check(evidence, entry, **variant)
        self.assertEqual(self.provider.executed, [])

    def test_day_today_or_previous_thirty_days(self):
        evidence, entry = self.catalog()
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        for day in ((today + timedelta(days=1)).isoformat(), (today - timedelta(days=31)).isoformat(),
                    self.day + " 01:00:00", "2026-99-40", "yesterday", True):
            with self.subTest(day=day), self.assertRaises(DmsDenied):
                self.check(evidence, entry, day=day)
        self.assertEqual(self.provider.executed, [])

    def test_counts_small_groups_suppressed_and_unapproved_result_columns_denied(self):
        evidence, entry = self.catalog()
        self.provider.rows(["execution_status", "total_count"], [{"execution_status": "NEW", "total_count": "3"},
                                                                  {"execution_status": "FAILED", "total_count": "10"}])
        result = self.check(evidence, entry, mode="counts")
        self.assertEqual(result["rows"], [{"execution_status": "FAILED", "total_count": 10}])
        self.assertTrue(result["small_counts_suppressed"])
        self.assertIn("GROUP BY `execution_status`", self.provider.executed[-1])
        self.provider.rows(["total_count"], [{"total_count": 0}])
        result = self.check(evidence, entry, mode="count", columns=[])
        self.assertEqual(result["rows"], [])
        self.assertNotIn("execution_status", self.provider.executed[-1])

    def test_result_shape_and_values_do_not_leak_provider_errors(self):
        evidence, entry = self.catalog()
        self.provider.rows(["execution_status", "phone"], [{"execution_status": "FAILED", "phone": "13812345678"}])
        with self.assertRaises(DmsDenied) as error:
            self.check(evidence, entry)
        self.assertNotIn("13812345678", str(error.exception))
        self.provider.result = {"Success": False, "ErrorMessage": "SECRET_PASSWORD"}
        with self.assertRaises(DmsDenied) as error:
            self.check(evidence, entry)
        self.assertNotIn("SECRET", str(error.exception))

    def test_budget_atomic_across_simultaneous_check_calls(self):
        evidence, entry = self.catalog()
        results = []
        def run():
            try:
                self.check(evidence, entry)
                results.append("ok")
            except DmsDenied:
                results.append("denied")
        threads = [threading.Thread(target=run) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results.count("ok"), 2)
        self.assertEqual(results.count("denied"), 6)
        self.assertEqual(len(self.provider.executed), 2)

    def test_task_budget_cannot_reset_by_reading_new_evidence(self):
        for _ in range(2):
            evidence, entry = self.catalog()
            self.check(evidence, entry)
        evidence, entry = self.catalog()
        with self.assertRaises(DmsDenied):
            self.check(evidence, entry)
        self.assertEqual(len(self.provider.executed), 2)

    def test_user_table_is_discoverable_and_countable_without_identity_projection(self):
        self.source.write_text("SELECT execution_status FROM users\nWHERE expect_execute_time > '2026-01-01';\n")
        self.provider.table["TableName"] = "users"
        self.broker.record_search(self.context, self.reader.search("FROM users"))
        evidence = self.broker.record_code(self.context, self.reader.read("repo/tasks.sql", 1, 2))
        entry = self.broker.catalog(self.context, evidence, "users")["checks"][0]
        self.provider.rows(["total_count"], [{"total_count": 120}])
        self.assertEqual(self.check(evidence, entry, columns=[], mode="count")["rows"], [{"total_count": 120}])

    def test_postgres_and_sqlserver_use_only_fixed_dialect_quoting(self):
        for dialect, db_type, timestamp in (("postgresql", "postgresql", "timestamp without time zone"),
                                            ("postgresql", "polardb-pg", "timestamp(6) with time zone"),
                                            ("sqlserver", "sqlserver", "datetime2(7)")):
            with self.subTest(dialect=db_type):
                self.provider.instance["InstanceType"] = db_type
                self.provider.database["DbType"] = db_type
                self.provider.columns[2]["ColumnType"] = timestamp
                evidence, entry = self.catalog()
                self.assertEqual(entry["dialect"], dialect)
                self.check(evidence, entry)
                sql = self.provider.executed[-1]
                if dialect == "postgresql":
                    self.assertTrue(sql.startswith('SELECT "execution_status" FROM "business"."tasks"'))
                    self.assertTrue(sql.endswith("LIMIT 20"))
                    self.assertNotIn("`", sql)
                else:
                    self.assertTrue(sql.startswith("SELECT TOP (20) [execution_status] FROM [business].[tasks]"))
                    self.assertNotIn("LIMIT", sql)
                    self.assertNotIn("`", sql)
                self.broker._task_counts.clear()

    def test_sqlserver_count_and_grouping_have_no_limit_or_model_functions(self):
        self.provider.instance["InstanceType"] = "sqlserver"
        self.provider.database["DbType"] = "sqlserver"
        evidence, entry = self.catalog()
        self.provider.rows(["total_count"], [{"total_count": "50"}])
        self.check(evidence, entry, columns=[], mode="count")
        self.assertTrue(self.provider.executed[-1].startswith("SELECT COUNT_BIG(*) AS [total_count] FROM [business].[tasks]"))
        self.assertNotIn("LIMIT", self.provider.executed[-1])
        self.provider.rows(["execution_status", "total_count"], [{"execution_status": "RUNNING", "total_count": "50"}])
        self.check(evidence, entry, mode="counts")
        self.assertIn("SELECT TOP (20) [execution_status], COUNT_BIG(*) AS [total_count]", self.provider.executed[-1])
        self.assertTrue(self.provider.executed[-1].endswith("GROUP BY [execution_status] ORDER BY [execution_status]"))

    def test_unsafe_or_unsupported_candidates_are_reported_and_never_executed(self):
        evidence = self.evidence()
        self.provider.instance["QueryTimeout"] = 7200
        result = self.broker.catalog(self.context, evidence, "tasks")
        self.assertEqual(result["checks"], [])
        self.assertEqual(result["skipped_resources"], 1)
        self.provider.instance["InstanceType"] = "ob_for_oracle"
        self.provider.database["DbType"] = "ob_for_oracle"
        result = self.broker.catalog(self.context, evidence, "tasks")
        self.assertEqual(result["unsupported_engines"], ["ob_for_oracle"])
        self.assertEqual(self.provider.executed, [])

    def test_api_call_budget_and_remaining_timeout_are_enforced(self):
        evidence = self.evidence()
        with mock.patch("octopus.dms.MAX_API_CALLS", 2):
            result = self.broker.catalog(self.context, evidence, "tasks")
        self.assertEqual(len(self.provider.calls), 2)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["checks"], [])
        self.assertEqual(self.provider.executed, [])
        with mock.patch("octopus.dms.OPERATION_TIMEOUT", 0):
            with self.assertRaises(DmsDenied):
                self.broker.catalog(self.context, evidence, "tasks")
        self.assertEqual(len(self.provider.calls), 2)

    def test_today_is_supported_without_hour_or_future_override(self):
        evidence, entry = self.catalog()
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        result = self.check(evidence, entry, day=today)
        self.assertEqual(result["day"], today)
        self.assertIn(today + " 00:00:00", self.provider.executed[-1])

    def test_pro_table_and_database_identity_must_match_exactly(self):
        for field, value in (("InstanceId", 8), ("Host", "different.internal"), ("State", "DISABLED")):
            with self.subTest(field=field):
                evidence, entry = self.catalog()
                old = self.provider.database[field]
                self.provider.database[field] = value
                with self.assertRaises(DmsDenied):
                    self.check(evidence, entry)
                self.provider.database[field] = old
                self.broker._task_counts.clear()
        self.assertEqual(self.provider.executed, [])

    def test_provider_pagination_unknown_or_oversized_shapes_fail_closed(self):
        evidence = self.evidence()
        original = self.provider
        def malformed(argv, **kwargs):
            result = original(argv, **kwargs)
            if argv[6] == "search-table":
                result["TotalCount"] = 101
            return result
        self.broker.runner = malformed
        with self.assertRaises(DmsDenied):
            self.broker.catalog(self.context, evidence, "tasks")
        self.assertEqual(self.provider.executed, [])

    def test_source_and_policy_withdrawal_during_query_block_delivery(self):
        evidence, entry = self.catalog()
        original = self.provider
        def withdraw(argv, **kwargs):
            result = original(argv, **kwargs)
            if argv[6] == "execute-script":
                self.policy["enabled"] = False
                self.write_policy()
            return result
        self.broker.runner = withdraw
        with self.assertRaises(DmsDenied):
            self.check(evidence, entry)
        self.assertEqual(len(self.provider.executed), 1)

    def test_json_depth_size_and_raw_error_values_do_not_escape(self):
        evidence, entry = self.catalog()
        self.provider.rows(["params"], [{"params": {"password": "secret-value", "email": "someone@example.com", "attempt": 1}}])
        result = self.check(evidence, entry, columns=["params"])
        self.assertEqual(result["rows"][0]["params"], {"password": "[REDACTED]", "email": "[REDACTED]", "attempt": 1})
        self.provider.rows(["params"], [{"params": {"large": list(range(101))}}])
        with self.assertRaises(DmsDenied):
            self.check(evidence, entry, columns=["params"])

    def test_sqlserver_uses_actual_schema_and_rejects_schema_drift(self):
        self.provider.instance["InstanceType"] = "sqlserver"
        self.provider.database["DbType"] = "sqlserver"
        self.provider.table["TableSchemaName"] = "dbo"
        evidence, entry = self.catalog()
        self.assertEqual(entry["table_schema"], "dbo")
        self.check(evidence, entry)
        self.assertIn("FROM [dbo].[tasks]", self.provider.executed[-1])
        self.provider.table["TableSchemaName"] = "unverified"
        with self.assertRaises(DmsDenied):
            self.check(evidence, entry)
        self.assertEqual(len(self.provider.executed), 1)

    def test_source_label_disambiguates_and_is_revalidated(self):
        evidence, entry = self.catalog()
        self.assertTrue(entry["source_label"].startswith("test-prod ["))
        self.assertNotIn("db.internal", entry["source_label"])
        self.provider.instance["InstanceAlias"] = "different-prod"
        with self.assertRaises(DmsDenied):
            self.check(evidence, entry)
        self.assertEqual(self.provider.executed, [])

    def test_nfkc_sensitive_json_keys_are_redacted_without_renaming(self):
        evidence, entry = self.catalog()
        self.provider.rows(["params"], [{"params": {"ｒｅａｌ＿ｎａｍｅ": "Synthetic Person", "ｐｈｏｎｅ": "12345"}}])
        result = self.check(evidence, entry, columns=["params"])
        self.assertEqual(result["rows"][0]["params"], {"ｒｅａｌ＿ｎａｍｅ": "[REDACTED]", "ｐｈｏｎｅ": "[REDACTED]"})


if __name__ == "__main__":
    unittest.main()
