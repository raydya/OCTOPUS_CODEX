"""Source-bound, production-only DMS access through a small SELECT compiler.

The model supplies identifiers and numeric predicates, never SQL, an instance,
connection, account, or CLI arguments. Provider metadata is not a privacy proof:
column names/types and every returned value are independently restricted here.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timedelta
from functools import wraps
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time
import unicodedata
from zoneinfo import ZoneInfo

POLICY_VERSION = "readonly:v2"
EVIDENCE_TTL = 600
MAX_TICKETS = 2048
MAX_TASKS = 8192
MAX_CHECKS_PER_TASK = 2
MYSQL_TYPES = frozenset({"mysql", "polardb", "mariadb", "oceanbasex"})
POSTGRES_TYPES = frozenset({"postgresql", "polardb-pg"})
SQLSERVER_TYPES = frozenset({"sqlserver"})
SUPPORTED_TYPES = MYSQL_TYPES | POSTGRES_TYPES | SQLSERVER_TYPES
OPERATION_TIMEOUT = 50
MAX_API_CALLS = 40
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_ID = re.compile(r"[1-9][0-9]{0,19}\Z")
_SENSITIVE = re.compile(
    r"password|passwd|secret|token|credential|phone|mobile|telephone|email|address|identity|idcard|passport|"
    r"bank|accountnumber|accountid|userid|customerid|memberid|employeeid|personid|deviceid|"
    r"imei|imsi|openid|unionid|idfa|oaid|macaddress|ipaddress|realname|fullname|firstname|lastname|birthday|birthdate|"
    r"latitude|longitude|fingerprint|salary|wage|姓名|手机|电话|地址|身份证|银行卡", re.I,
)


class DmsDenied(ValueError):
    """Only fixed, non-sensitive diagnostics leave this module."""


def _deny(message="DMS business check is not authorized"):
    raise DmsDenied(message)


def _text(value, minimum=1, maximum=128):
    if (not isinstance(value, str) or not minimum <= len(value) <= maximum
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        _deny("invalid DMS argument or metadata")
    return value


def _number(value, minimum=1, maximum=1000000):
    if type(value) is not int or not minimum <= value <= maximum:
        _deny("invalid DMS argument or metadata")
    return value


def _identifier(value):
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        _deny("invalid DMS identifier")
    return value


def _resource_id(value):
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or not _ID.fullmatch(value):
        _deny("invalid DMS resource identifier")
    return value


def _snake(name):
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower()


def _sensitive(name):
    snake = _snake(unicodedata.normalize("NFKC", name))
    normalized = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]", "", snake)
    return bool(_SENSITIVE.search(normalized) or re.search(
        r"(?:^|_)(?:uid|ip|pwd|tel|dob)(?:_|$)", snake))


def _shape(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        _deny("invalid DMS argument structure")


def _json(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                _deny("duplicate policy fields are forbidden")
            result[key] = value
        return result

    def bad_constant(value):
        _deny("invalid policy number")

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=bad_constant)
    except (ValueError, TypeError, RecursionError, UnicodeError):
        _deny("invalid DMS policy JSON")


def source_digest(result):
    """Hash complete code_read lines using canonical JSON, not repository HEAD."""
    if type(result) is not dict or result.get("truncated") is not False:
        _deny("complete code evidence is required")
    path = _text(result.get("path"), maximum=2048)
    if path.startswith("/") or "\\" in path or any(p in ("", ".", "..") for p in path.split("/")):
        _deny("relative code evidence is required")
    start = _number(result.get("start_line"), maximum=1000000)
    lines = result.get("lines")
    if not isinstance(lines, list) or not 1 <= len(lines) <= 200:
        _deny("complete code evidence is required")
    clean = []
    for offset, line in enumerate(lines):
        if (type(line) is not dict or line.get("line") != start + offset
                or line.get("line_truncated") is not False or not isinstance(line.get("content"), str)):
            _deny("complete code evidence is required")
        clean.append({"line": start + offset, "content": line["content"]})
    payload = {"path": path, "start_line": start, "lines": clean}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _list_field(response, outer, item):
    value = response.get(outer)
    if isinstance(value, dict) and set(value) == {item}:
        value = value[item]
    if not isinstance(value, list) or len(value) > 1000 or not all(type(row) is dict for row in value):
        _deny("DMS metadata or result shape is not verified")
    return value


def _strip_comments(text, sql=False):
    """Keep quoted strings intact while excluding source/SQL/XML comments."""
    output, i = [], 0
    while i < len(text):
        if text.startswith("<!--", i) or text.startswith("/*", i):
            opening, closing = ("<!--", "-->") if text.startswith("<!--", i) else ("/*", "*/")
            end = text.find(closing, i + len(opening))
            if end < 0:
                break
            output.append("\n" * text[i:end + len(closing)].count("\n") + " ")
            i = end + len(closing)
        elif text.startswith("--", i) if sql else (text.startswith("//", i) or text[i] == "#"):
            end = text.find("\n", i)
            if end < 0:
                break
            output.append("\n")
            i = end + 1
        elif text[i] in "'\"`":
            quote = text[i]
            triple = text.startswith(quote * 3, i) and quote != "`"
            marker = quote * (3 if triple else 1)
            end = i + len(marker)
            while end < len(text):
                if text[end] == "\\":
                    end += 2
                elif text.startswith(marker, end):
                    end += len(marker)
                    if not triple and end < len(text) and text[end] == quote:
                        end += 1
                        continue
                    break
                else:
                    end += 1
            output.append(text[i:end])
            i = end
        else:
            output.append(text[i])
            i += 1
    return "".join(output)


def _sql_relations(sql):
    # This only identifies evidence; it never authorizes executing source SQL.
    # String literals are blanked so SELECT 'FROM private_table' is not proof.
    sql = _strip_comments(sql, sql=True)
    sql = re.sub(r"'(?:''|\\.|[^'\\])*'|\"(?:\"\"|\\.|[^\"\\])*\"", "''", sql)
    relations = set()
    identifier = r"(?:`[A-Za-z_][A-Za-z0-9_]{0,63}`|[A-Za-z_][A-Za-z0-9_]{0,63})"
    for statement in sql.split(";"):
        if not re.search(r"\bselect\b", statement, re.I):
            continue
        for match in re.finditer(r"\b(?:from|join)\s+(" + identifier + r"(?:\s*\.\s*" + identifier + r")?)(?=\s|,|\)|$)", statement, re.I):
            if not re.search(r"\bselect\b", statement[:match.start()], re.I):
                continue
            table = re.split(r"\s*\.\s*", match.group(1))[-1].strip("`")
            relations.add(table)
    return frozenset(relations)


def _code_relations(result):
    path = Path(result["path"])
    text = "\n".join(line["content"] for line in result["lines"])
    suffix = path.suffix.lower()
    if suffix in (".sql", ".xml"):
        if suffix == ".xml":
            # MyBatis SQL text, not XML comments/attribute examples.
            text = re.sub(r"<!--[\s\S]*?-->", " ", text)
            text = text.replace("<![CDATA[", " ").replace("]]>", " ")
            text = re.sub(r"<[^>]*>", " ", text)
        return _sql_relations(text)
    if suffix not in (".py", ".java", ".kt", ".go", ".js", ".ts", ".rb", ".php", ".cs", ".scala"):
        return frozenset()
    text = _strip_comments(text)
    relations = set()
    # Require SELECT and FROM in one static literal. Dynamic/concatenated SQL
    # needing execution to resolve is deliberately not accepted as evidence.
    literal = re.compile(r'"""([\s\S]*?)"""|\'\'\'([\s\S]*?)\'\'\'|"((?:\\.|[^"\\])*)"|\'((?:\\.|[^\'\\])*)\'')
    for match in literal.finditer(text):
        value = next(group for group in match.groups() if group is not None)
        if "${" in value or "#{" in value or re.search(r"\b(?:from|join)\s*[^\s]*[{}]", value, re.I):
            continue
        relations.update(_sql_relations(value.replace("\\n", "\n").replace("\\t", "\t")))
    return frozenset(relations)


def _bounded_operation(method):
    @wraps(method)
    def invoke(self, *args, **kwargs):
        self._local.budget = {"deadline": time.monotonic() + OPERATION_TIMEOUT, "remaining": MAX_API_CALLS}
        try:
            return method(self, *args, **kwargs)
        finally:
            self._local.budget = None
    return invoke


def _dialect(db_type):
    if db_type in MYSQL_TYPES:
        return "mysql"
    if db_type in POSTGRES_TYPES:
        return "postgresql"
    if db_type in SQLSERVER_TYPES:
        return "sqlserver"
    _deny("database engine is not supported by the read-only compiler")


def _source_label(instance, database_id):
    from .code_reader import redact
    from .privacy import redact_private_text
    alias = instance.get("InstanceAlias")
    if not isinstance(alias, str) or not alias.strip():
        alias = "production"
    alias = redact_private_text(redact(alias[:128]))
    alias = " ".join(alias.split())
    marker = hashlib.sha256((str(instance["InstanceId"]) + ":" + str(database_id)).encode()).hexdigest()[:8]
    return alias + " [" + marker + "]"


def _column(column):
    """Return only independently safe field kinds; unknown metadata is excluded."""
    name = column.get("ColumnName")
    if not isinstance(name, str) or not _IDENTIFIER.fullmatch(name) or _sensitive(name):
        return None
    if (column.get("Sensitive") is not False or column.get("SecurityLevel") != "INNER"
            or column.get("FunctionType") not in (None, "")):
        return None
    ctype = column.get("ColumnType")
    if not isinstance(ctype, str) or len(ctype) > 2048:
        return None
    ctype = ctype.strip().lower()
    snake = _snake(name)
    result = {"name": name, "type": ctype}
    if re.fullmatch(r"date|smalldatetime|(?:datetime|datetime2|datetimeoffset|timestamp)(?:\([0-7]\))?(?: (?:with|without) time zone)?", ctype):
        result["kind"] = "time"
    elif re.fullmatch(r"bool(?:ean)?|tinyint\(1\)(?: unsigned)?|bit(?:\(1\))?", ctype):
        result["kind"] = "boolean"
    elif re.fullmatch(r"(?:tinyint|smallint|mediumint|int|integer|bigint|int2|int4|int8|smallserial|serial|bigserial)(?:\([0-9]{1,3}\))?(?: unsigned)?", ctype):
        result["kind"] = "integer"
    elif re.fullmatch(r"(?:decimal|numeric)\([0-9]{1,2},\s*[0-9]{1,2}\)|real|double precision|money|smallmoney|(?:float|double)(?:\([0-9]{1,2}(?:,[0-9]{1,2})?\))?", ctype):
        result["kind"] = "number"
    elif (re.fullmatch(r"(?:n?(?:var)?char|character(?: varying)?)\((?:[0-9]{1,5}|max)\)|(?:tiny|medium|long|n)?text|jsonb?|uuid|uniqueidentifier", ctype)
            or ctype.startswith("enum(") and ctype.endswith(")")):
        result["kind"] = "text"
    else:
        return None
    return result


class DmsBroker:
    def __init__(self, policy_path, reader, runner):
        self.policy_path = Path(policy_path) if policy_path is not None else None
        self.reader = reader
        self.runner = runner
        self._lock = threading.RLock()
        self._local = threading.local()
        self._tickets = OrderedDict()
        self._searches = OrderedDict()
        self._checks = OrderedDict()
        # Never evict used task counters to admit replay. Capacity fails closed.
        self._task_counts = {}

    def _policy(self):
        fd = -1
        try:
            path = self.policy_path
            if path is None:
                _deny("DMS business checks are disabled")
            if not path.is_absolute() or ".." in path.parts:
                _deny("DMS policy path must be absolute")
            fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            for part in path.parts[1:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            child = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                    or info.st_mode & 0o022 or not info.st_mode & 0o200 or info.st_size > 131072):
                _deny("DMS policy must be an owner-managed regular file")
            raw = os.read(fd, 131073)
            if len(raw) > 131072:
                _deny("DMS policy exceeds its size limit")
            policy = _json(raw)
            if type(policy) is not dict or policy.get("enabled") is not True:
                _deny("DMS business checks are disabled")
            _shape(policy, ("enabled", "policy_version", "profile", "region", "tid", "timezone"))
            if (policy["policy_version"] != POLICY_VERSION or policy["profile"] != "company-cli-user"
                    or policy["region"] != "cn-hangzhou" or policy["timezone"] != "Asia/Shanghai"):
                _deny("DMS policy does not match the fixed account and region")
            _resource_id(policy["tid"])
            return policy, hashlib.sha256(raw).hexdigest()
        except (OSError, UnicodeError):
            _deny("DMS business checks are disabled or their policy is unavailable")
        finally:
            if fd >= 0:
                os.close(fd)

    def _context(self, context):
        _shape(context, ("actor", "job_id", "thread_id", "turn_id", "policy_version", "question"))
        if context["policy_version"] != POLICY_VERSION:
            _deny("DMS task policy is stale")
        values = [_text(context[key], maximum=256) for key in ("actor", "job_id", "thread_id", "turn_id")]
        question = context["question"]
        if not isinstance(question, str) or not 1 <= len(question) <= 16000 or "\x00" in question:
            _deny("invalid original business question")
        try:
            from .privacy import dms_request_allowed
            allowed = dms_request_allowed(question)
        except Exception:
            _deny("DMS request privacy policy is unavailable")
        if allowed is not True:
            _deny("this request is not eligible for database access")
        return tuple(values) + (POLICY_VERSION, hashlib.sha256(question.encode()).hexdigest())

    def _prune(self, now):
        for collection in (self._tickets, self._checks, self._searches):
            for key in [key for key, entry in collection.items() if entry["expires"] <= now]:
                del collection[key]

    def record_search(self, context, result):
        try:
            _, fingerprint = self._policy()
            binding = self._context(context)
            if type(result) is not dict or result.get("truncated") is not False:
                return
            matches = result.get("matches")
            if not isinstance(matches, list) or len(matches) > 100:
                return
            found = set()
            for match in matches:
                if type(match) is not dict or match.get("line_truncated") is True:
                    return
                found.add((_text(match.get("path"), maximum=2048), _number(match.get("line"), maximum=1000000)))
            with self._lock:
                now = time.monotonic()
                self._prune(now)
                old = self._searches.get(binding)
                if not old and len(self._searches) >= MAX_TICKETS:
                    return
                previous = old["matches"] if old and old["policy"] == fingerprint else frozenset()
                combined = list(previous) + sorted(found - set(previous))
                self._searches[binding] = {"policy": fingerprint, "expires": now + EVIDENCE_TTL,
                                           "matches": frozenset(combined[:100])}
        except (DmsDenied, KeyError, TypeError, ValueError):
            return

    def record_code(self, context, result):
        try:
            _, fingerprint = self._policy()
            binding = self._context(context)
            digest = source_digest(result)
            tables = _code_relations(result)
            if not tables:
                return None
            with self._lock:
                now = time.monotonic()
                self._prune(now)
                search = self._searches.get(binding)
                if (not search or search["policy"] != fingerprint
                        or not any(path == result["path"] and result["start_line"] <= line < result["start_line"] + len(result["lines"])
                                   for path, line in search["matches"])):
                    return None
                if len(self._tickets) + len(self._checks) >= MAX_TICKETS:
                    return None
                token = secrets.token_urlsafe(24)
                self._tickets[token] = {"context": binding, "policy": fingerprint, "expires": now + EVIDENCE_TTL,
                                        "source": {"path": result["path"], "start_line": result["start_line"],
                                                   "line_count": len(result["lines"]), "sha256": digest},
                                        "tables": tables}
            self._read_source(self._tickets[token])
            return token
        except (DmsDenied, KeyError, TypeError, ValueError):
            return None

    def _read_source(self, ticket):
        source = ticket["source"]
        try:
            result = self.reader.read(source["path"], source["start_line"], source["line_count"])
            if source_digest(result) != source["sha256"] or _code_relations(result) != ticket["tables"]:
                _deny("code evidence changed")
        except DmsDenied:
            raise
        except Exception:
            _deny("code evidence is unavailable")

    def _eligible(self, context, evidence_id):
        policy, fingerprint = self._policy()
        binding = self._context(context)
        _text(evidence_id, minimum=20, maximum=128)
        with self._lock:
            self._prune(time.monotonic())
            ticket = self._tickets.get(evidence_id)
            if not ticket or ticket["context"] != binding or ticket["policy"] != fingerprint:
                _deny("valid code evidence for this task is required")
        self._read_source(ticket)
        return policy, binding, ticket

    def _api(self, policy, action, **params):
        allowed = {"SearchTable", "SearchDatabase", "GetInstance", "ListDatabases", "ListTables", "ListColumns", "ExecuteScript"}
        if action not in allowed:
            _deny("unsupported DMS operation")
        cli_action = re.sub(r"(?<!^)(?=[A-Z])", "-", action).lower()
        argv = ["aliyun", "--profile", "company-cli-user", "--region", "cn-hangzhou", "dms-enterprise", cli_action,
                "--tid", _resource_id(policy["tid"])]
        for key, value in params.items():
            if key == "logic":
                argv.append("--logic=false")
            else:
                argv.extend(["--" + key, str(value)])
        budget = getattr(self._local, "budget", None)
        if not budget or budget["remaining"] <= 0:
            _deny("DMS metadata call budget is exhausted")
        remaining = budget["deadline"] - time.monotonic()
        if remaining <= 0:
            _deny("DMS operation deadline is exhausted")
        budget["remaining"] -= 1
        try:
            result = self.runner(argv, timeout=min(remaining, 45 if action == "ExecuteScript" else 15))
        except Exception:
            _deny("DMS provider is unavailable or unauthorized")
        if type(result) is not dict or result.get("Success") is not True:
            _deny("DMS provider is unavailable or unauthorized")
        return result

    def _pages(self, policy, action, outer, item, maximum=10, **params):
        rows = []
        for page in range(1, maximum + 1):
            response = self._api(policy, action, **params, **{"page-number": page, "page-size": 100})
            batch = _list_field(response, outer, item)
            total = response.get("TotalCount")
            if type(total) is not int or not 0 <= total <= 1000000 or len(batch) > 100:
                _deny("DMS metadata pagination is not verified")
            rows.extend(batch)
            if page * 100 >= total:
                return rows, False
            if len(batch) != 100:
                _deny("DMS metadata pagination is incomplete")
        return rows, True

    def _find(self, policy, action, outer, item, predicate, **params):
        rows, truncated = self._pages(policy, action, outer, item, **params)
        matches = [row for row in rows if predicate(row)]
        if truncated or len(matches) != 1:
            _deny("DMS resource identity is absent or ambiguous")
        return matches[0]

    def _instance(self, policy, host, port, sid=None):
        _text(host, maximum=253)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host):
            _deny("invalid DMS host metadata")
        _number(port, maximum=65535)
        params = {"host": host, "port": port}
        if sid:
            params["sid"] = _text(sid, maximum=128)
        instance = self._api(policy, "GetInstance", **params).get("Instance")
        if (type(instance) is not dict or instance.get("EnvType") != "product"
                or instance.get("InstanceType") not in SUPPORTED_TYPES or instance.get("State") != "NORMAL"
                or not (instance.get("SellTrust") is True or instance.get("SellTrust") == "true")
                or type(instance.get("StandardGroup")) is not dict or instance["StandardGroup"].get("GroupMode") != "COMMON"
                or type(instance.get("QueryTimeout")) is not int or not 1 <= instance["QueryTimeout"] <= 60
                or instance.get("Host") != host or type(instance.get("Port")) is not int or instance["Port"] != port):
            _deny("instance is not a verified pro resource under safe query controls")
        _resource_id(instance.get("InstanceId"))
        return instance

    def _metadata(self, policy, target):
        instance = self._instance(policy, target["host"], target["port"], target.get("sid"))
        if (_resource_id(instance["InstanceId"]) != target["instance_id"] or instance["InstanceType"] != target["db_type"]
                or _source_label(instance, target["database_id"]) != target["source_label"]):
            _deny("DMS instance identity changed")
        database = self._find(policy, "ListDatabases", "DatabaseList", "Database",
                              lambda row: str(row.get("DatabaseId")) == target["database_id"],
                              **{"instance-id": target["instance_id"], "search-key": target["db_name"]})
        if (str(database.get("InstanceId")) != target["instance_id"] or database.get("SchemaName") != target["db_name"]
                or database.get("EnvType") != "product" or database.get("State") != "NORMAL"
                or database.get("DbType") != target["db_type"] or database.get("Host") != target["host"]
                or type(database.get("Port")) is not int or database["Port"] != target["port"]):
            _deny("database identity is not verified as pro")
        table = self._find(policy, "ListTables", "TableList", "Table",
                           lambda row: row.get("TableName") == target["table"] and str(row.get("TableId")) == target["table_id"],
                           **{"database-id": target["database_id"], "search-name": target["table"]})
        schema = _identifier(table.get("TableSchemaName"))
        if (str(table.get("DatabaseId")) != target["database_id"]
                or table.get("TableType") != "NORMAL" or str(table.get("TableId")) != target["table_id"]
                or target["db_type"] in MYSQL_TYPES and schema != target["db_name"]
                or target.get("table_schema") is not None and schema != target["table_schema"]):
            _deny("table identity is not verified")
        # DMS PostgreSQL Database.SchemaName is a schema (e.g. public), whereas
        # SQLServer Database.SchemaName is the DB and TableSchemaName is dbo.
        # Pin the actual table metadata and always qualify SQL with this schema.
        target["table_schema"] = schema
        columns = _list_field(self._api(policy, "ListColumns", **{"table-id": target["table_id"], "logic": "false"}),
                              "ColumnList", "Column")
        names, safe = set(), {}
        for column in columns:
            name = column.get("ColumnName")
            if not isinstance(name, str) or name in names:
                _deny("column identity is ambiguous")
            names.add(name)
            clean = _column(column)
            if clean:
                safe[name] = clean
        return safe

    def _discover(self, policy, table, database_keyword):
        candidates, truncated = self._pages(policy, "SearchTable", "SearchTableList", "SearchTable", maximum=3,
                                            **{"env-type": "product", "search-range": "HAS_PERMSSION",
                                               "search-target": "SINGLE_TABLE", "search-key": table})
        targets, seen, skipped, unsupported = [], set(), 0, set()
        attempts = 0
        for row in candidates:
            if row.get("TableName") != table:
                continue
            if row.get("EnvType") != "product" or row.get("Logic") is not False:
                skipped += 1
                continue
            if row.get("DbType") not in SUPPORTED_TYPES:
                unsupported.add(str(row.get("DbType"))[:64])
                skipped += 1
                continue
            db_name = _identifier(row.get("DbName"))
            if database_keyword and database_keyword.casefold() not in db_name.casefold():
                continue
            dbid = _resource_id(row.get("DatabaseId"))
            table_id = _resource_id(row.get("TableId"))
            identity = (dbid, table_id)
            if identity in seen:
                continue
            seen.add(identity)
            if len(targets) >= 5 or attempts >= 10:
                truncated = True
                break
            attempts += 1
            try:
                lookup = _text(row.get("DBSearchName") or db_name, maximum=512)
                database = self._find(policy, "SearchDatabase", "SearchDatabaseList", "SearchDatabase",
                                      lambda item: str(item.get("DatabaseId")) == dbid and item.get("SchemaName") == db_name,
                                      **{"search-key": lookup, "search-range": "HAS_PERMSSION", "search-target": "SINGLE_DB", "env-type": "product"})
                if (database.get("EnvType") != "product" or database.get("Logic") is not False
                        or database.get("DbType") != row["DbType"]):
                    _deny("discovered database is not verified as pro")
                instance = self._instance(policy, database.get("Host"), database.get("Port"), database.get("Sid"))
                target = {"database_id": dbid, "db_name": db_name, "table_id": _resource_id(row.get("TableId")),
                          "table": table, "host": database["Host"], "port": database["Port"],
                          "instance_id": _resource_id(instance["InstanceId"]), "db_type": row["DbType"],
                          "sid": database.get("Sid"), "source_label": _source_label(instance, dbid)}
                columns = self._metadata(policy, target)
                targets.append((target, columns))
            except DmsDenied:
                skipped += 1
                budget = self._local.budget
                if budget["remaining"] <= 0 or time.monotonic() >= budget["deadline"]:
                    truncated = True
                    break
        return targets, truncated, skipped, sorted(unsupported)

    @_bounded_operation
    def catalog(self, context, evidence_id, table, database_keyword=""):
        try:
            self._policy()
        except DmsDenied:
            return {"checks": [], "available": False, "reason": "业务数据检查尚未配置，继续基于代码回答"}
        policy, binding, ticket = self._eligible(context, evidence_id)
        table = _identifier(table)
        _text(database_keyword, minimum=0, maximum=64)
        if database_keyword and not re.fullmatch(r"[A-Za-z0-9_-]+", database_keyword):
            _deny("invalid database search keyword")
        if table not in ticket["tables"]:
            _deny("table must occur in this task's searched and read code SQL")
        targets, truncated, skipped, unsupported = self._discover(policy, table, database_keyword)
        self._eligible(context, evidence_id)
        checks = []
        with self._lock:
            self._prune(time.monotonic())
            for target, columns in targets:
                if len(self._tickets) + len(self._checks) >= MAX_TICKETS:
                    _deny("DMS evidence capacity is exhausted")
                token = secrets.token_urlsafe(24)
                self._checks[token] = {"context": binding, "policy": ticket["policy"], "evidence_id": evidence_id,
                                        "expires": ticket["expires"], "target": target, "columns": columns}
                checks.append({"check_id": token, "db_name": target["db_name"], "table": table, "table_schema": target["table_schema"],
                               "source_label": target["source_label"], "dialect": _dialect(target["db_type"]),
                               "columns": [{key: value for key, value in column.items() if key != "values"}
                                           for column in columns.values()]})
        return {"available": True, "checks": checks, "truncated": truncated, "timezone": policy["timezone"],
                "day_rule": "one calendar day from today or the previous 30 days", "max_checks_per_task": MAX_CHECKS_PER_TASK,
                "skipped_resources": skipped, "unsupported_engines": unsupported}

    @staticmethod
    def _day(policy, day):
        if not isinstance(day, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", day):
            _deny("day must identify one calendar day from today or the previous 30 days")
        try:
            selected = datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError:
            _deny("invalid business day")
        today = datetime.now(ZoneInfo(policy["timezone"])).date()
        if not today - timedelta(days=30) <= selected <= today:
            _deny("day must identify one calendar day from today or the previous 30 days")
        return selected.isoformat() + " 00:00:00", (selected + timedelta(days=1)).isoformat() + " 00:00:00"

    @staticmethod
    def _query(columns, time_column, mode, filters, metadata):
        if (type(columns) is not list or len(columns) > 5 or any(not isinstance(name, str) for name in columns)
                or len(set(columns)) != len(columns) or mode not in ("rows", "count", "counts")
                or (mode == "count") != (len(columns) == 0) or "total_count" in columns):
            _deny("invalid business projection")
        time_column = _identifier(time_column)
        if time_column not in metadata or metadata[time_column]["kind"] != "time":
            _deny("a non-sensitive date or timestamp column is required")
        for name in columns:
            _identifier(name)
            if name not in metadata:
                _deny("projection is not a permitted business status field")
        if type(filters) is not list or len(filters) > 3:
            _deny("invalid business filters")
        clean = []
        for item in filters:
            _shape(item, ("column", "op", "value"))
            name = _identifier(item["column"])
            if (name not in metadata or metadata[name]["kind"] not in ("boolean", "integer", "number")
                    or item["op"] not in ("eq", "ne", "lt", "lte", "gt", "gte")):
                _deny("filters must use non-sensitive numeric business fields")
            value = item["value"]
            if type(value) is bool:
                if metadata[name]["kind"] != "boolean":
                    _deny("boolean predicate type mismatch")
                value = int(value)
            else:
                _number(value, minimum=-1000000000, maximum=1000000000)
                if metadata[name]["kind"] == "boolean" and value not in (0, 1):
                    _deny("boolean predicate type mismatch")
            clean.append({"column": name, "op": item["op"], "value": value})
        return {"columns": columns, "time_column": time_column, "mode": mode, "filters": clean}

    @staticmethod
    def _sql(table, query, start, end, dialect="mysql", table_schema=None):
        if dialect == "mysql":
            quote = lambda name: "`" + _identifier(name) + "`"
        elif dialect == "postgresql":
            quote = lambda name: '"' + _identifier(name) + '"'
        elif dialect == "sqlserver":
            quote = lambda name: "[" + _identifier(name) + "]"
        else:
            _deny("unsupported read-only query dialect")
        timestamp = quote(query["time_column"])
        tail = f" FROM {quote(table_schema)}.{quote(table)} WHERE {timestamp} >= '{start}' AND {timestamp} < '{end}'"
        operators = {"eq": "=", "ne": "<>", "lt": "<", "lte": "<=", "gt": ">", "gte": ">="}
        for item in query["filters"]:
            tail += f" AND {quote(item['column'])} {operators[item['op']]} {item['value']}"
        aggregate = ("COUNT_BIG(*)" if dialect == "sqlserver" else "COUNT(*)") + " AS " + quote("total_count")
        if query["mode"] == "count":
            return "SELECT " + aggregate + tail + ("" if dialect == "sqlserver" else " LIMIT 1")
        names = ", ".join(quote(name) for name in query["columns"])
        prefix = "SELECT TOP (20) " if dialect == "sqlserver" else "SELECT "
        limit = "" if dialect == "sqlserver" else " LIMIT 20"
        if query["mode"] == "counts":
            return prefix + names + ", " + aggregate + tail + " GROUP BY " + names + " ORDER BY " + names + limit
        return prefix + names + tail + " ORDER BY " + timestamp + " DESC" + limit

    @staticmethod
    def _value(value, column):
        kind = column["kind"]
        if value is None:
            return None
        if kind in ("text", "time"):
            if not isinstance(value, (str, dict, list)):
                _deny("database returned an unexpected business field type")
            from .code_reader import redact
            from .privacy import redact_private_text
            def clean(item, depth=0):
                if depth > 6:
                    return "[TRUNCATED]"
                if isinstance(item, str):
                    return redact_private_text(redact(item[:2048]))
                if type(item) is dict:
                    if len(item) > 100:
                        _deny("database JSON field exceeds its bounds")
                    return {str(key)[:128]: "[REDACTED]" if _sensitive(str(key)) else clean(val, depth + 1)
                            for key, val in item.items()}
                if type(item) is list:
                    if len(item) > 100:
                        _deny("database JSON field exceeds its bounds")
                    return [clean(val, depth + 1) for val in item]
                if item is None or type(item) in (bool, int):
                    return item
                if type(item) is float and abs(item) <= 1e100:
                    return item
                _deny("database JSON field contains an invalid value")
            if isinstance(value, str) and column["type"] in ("json", "jsonb"):
                value = _json(value)
            return clean(value)
        if kind == "boolean":
            if type(value) is bool:
                return value
            if type(value) is int and value in (0, 1) or type(value) is str and value in ("0", "1"):
                return bool(int(value))
            _deny("database returned an invalid boolean")
        if kind == "integer":
            if type(value) is str and re.fullmatch(r"-?(?:0|[1-9][0-9]{0,18})", value):
                value = int(value)
            if type(value) is not int or not -(2**63) <= value <= 2**64 - 1:
                _deny("database returned an invalid business number")
            return value
        if kind == "number":
            if type(value) not in (int, float, str) or type(value) is str and not re.fullmatch(r"-?(?:0|[1-9][0-9]{0,9})(?:\.[0-9]{1,8})?", value):
                _deny("database returned an invalid business number")
            try:
                number = Decimal(str(value))
                if not number.is_finite() or abs(number) > 1000000000:
                    _deny("database returned an invalid business number")
                return float(number)
            except InvalidOperation:
                _deny("database returned an invalid business number")
        _deny("database returned an unsupported field")

    @classmethod
    def _rows(cls, response, query, metadata):
        results = _list_field(response, "Results", "Result")
        if len(results) != 1 or results[0].get("Success") is not True:
            _deny("database result is not a successful single query")
        result = results[0]
        names = result.get("ColumnNames")
        if isinstance(names, dict) and set(names) == {"ColumnNames"}:
            names = names["ColumnNames"]
        expected = list(query["columns"])
        if query["mode"] in ("count", "counts"):
            expected.append("total_count")
        if names != expected:
            _deny("database result columns do not match the permitted projection")
        rows = _list_field(result, "Rows", "Rows")
        if type(result.get("RowCount")) is not int or result["RowCount"] != len(rows) or len(rows) > 20:
            _deny("database result exceeds its verified row bounds")
        if query["mode"] == "count" and len(rows) != 1:
            _deny("invalid aggregate result")
        clean, suppressed = [], False
        for row in rows:
            if set(row) != set(expected):
                _deny("database result contains unapproved fields")
            output = {name: cls._value(row[name], metadata[name]) for name in query["columns"]}
            if "total_count" in expected:
                value = row["total_count"]
                if type(value) is str and re.fullmatch(r"0|[1-9][0-9]{0,14}", value):
                    value = int(value)
                if type(value) is not int or not 0 <= value <= 100000000000000:
                    _deny("database result contains an invalid count")
                if value < 10:
                    suppressed = True
                    continue
                output["total_count"] = value
            clean.append(output)
        return clean, suppressed

    @_bounded_operation
    def check(self, context, evidence_id, check_id, day, columns, time_column, mode="rows", filters=None):
        policy, binding, ticket = self._eligible(context, evidence_id)
        _text(check_id, minimum=20, maximum=128)
        with self._lock:
            selected = self._checks.get(check_id)
            if (not selected or selected["context"] != binding or selected["policy"] != ticket["policy"]
                    or selected["evidence_id"] != evidence_id):
                _deny("check is not bound to this task's code evidence")
        start, end = self._day(policy, day)
        query = self._query(columns, time_column, mode, [] if filters is None else filters, selected["columns"])
        task = binding[:2]
        with self._lock:
            count = self._task_counts.get(task, 0)
            if count >= MAX_CHECKS_PER_TASK or task not in self._task_counts and len(self._task_counts) >= MAX_TASKS:
                _deny("this task's database query budget is exhausted")
            self._task_counts[task] = count + 1
        metadata = self._metadata(policy, selected["target"])
        if metadata != selected["columns"]:
            _deny("database column policy changed; rediscover the catalog")
        self._query(columns, time_column, mode, query["filters"], metadata)
        self._eligible(context, evidence_id)
        response = self._api(policy, "ExecuteScript", **{"db-id": selected["target"]["database_id"], "logic": "false",
                             "script": self._sql(selected["target"]["table"], query, start, end, _dialect(selected["target"]["db_type"]), selected["target"]["table_schema"])})
        self._eligible(context, evidence_id)
        rows, suppressed = self._rows(response, query, metadata)
        return {"check_id": check_id, "db_name": selected["target"]["db_name"], "table": selected["target"]["table"],
                "table_schema": selected["target"]["table_schema"], "source_label": selected["target"]["source_label"],
                "day": day, "timezone": policy["timezone"], "mode": mode, "rows": rows,
                "small_counts_suppressed": suppressed, "limit": 20, "privacy_note": "小计数抑制不构成匿名性保证"}
