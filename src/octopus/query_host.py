"""Trusted host-side, fixed-function read-only query broker.

The broker accepts fixed queries rather than arbitrary commands or network targets
and does not return provider credentials. This private loopback API authenticates
the bridge; native Codex source reads do not pass through it.
"""
from __future__ import annotations

from datetime import datetime
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import socket
import stat
import subprocess
import threading
import time
import unicodedata
from typing import Any, Callable, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .code_reader import CodeReader, redact
from .dms import DmsBroker
from .privacy import privacy_request_denied, redact_private_text

MAX_REQUEST_BYTES = 65536
MAX_PROVIDER_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 32768
PROVIDER_TIMEOUT = 45.0
MAX_WINDOW_MS = 24 * 60 * 60 * 1000
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_DOCUMENT_TOKEN = re.compile(r"[A-Za-z0-9]{8,128}\Z")
_SECRET_FIELD = re.compile(r"(?:password|passwd|pwd|secret|authorization|cookie|privatekey|accesskey|apikey|token|credential)", re.I)
_PRIVATE_FIELDS = frozenset({
    "phone", "mobile", "phonenumber", "mobilenumber", "telephone", "email", "emailaddress",
    "idcard", "idcardnumber", "identitynumber", "passport", "passportnumber", "bankcard",
    "bankcardnumber", "bankaccount", "realname", "fullname", "firstname", "lastname",
    "homeaddress", "shippingaddress", "deliveryaddress", "recipientaddress", "contactaddress",
    "姓名", "手机号", "电话号码", "身份证", "身份证号", "证件号", "家庭住址", "收货地址", "银行卡号",
})
_ALLOWED_ARGUMENTS = {
    "code_repositories": (set(), set()),
    "code_search": ({"query"}, {"repository", "limit"}),
    "code_read": ({"path"}, {"start_line", "line_count"}),
    "code_evidence": ({"path"}, {"start_line", "line_count"}),
    "business_data_checks": ({"evidence_id", "table"}, {"database_keyword"}),
    "business_data_check": ({"evidence_id", "check_id", "day", "time_column", "columns"}, {"mode", "filters"}),
    "docs_search": ({"query"}, {"page_token", "limit"}),
    "docs_fetch": ({"document"}, {"keyword"}),
    "tls_topics": (set(), {"query", "project_id", "page"}),
    "tls_search": ({"topic_id", "query", "start_time", "end_time"}, {"limit"}),
    "grafana_datasources": (set(), set()),
    "grafana_query": ({"datasource_uid", "query", "start_time", "end_time"}, {"step_seconds"}),
    "grafana_search_dashboards": ({"query"}, set()),
}


class QueryError(Exception):
    """An intentionally generic, safe error suitable for returning to the bridge."""

    def __init__(self, message: str, *, code: Optional[str] = None):
        super().__init__(message)
        self.code = code


def _raise_tls_auth_error(value: Any) -> None:
    # Inspect only the documented classification, never forward provider text,
    # request IDs, login URLs, or credentials from stdout/stderr.
    if (isinstance(value, dict) and value.get("status") == "failed"
            and isinstance(value.get("error"), dict) and value["error"].get("kind") == "auth"):
        raise QueryError("TLS authentication failed; an administrator must renew the host-side TLS login",
                         code="tls_auth_required")


def _string(value: Any, minimum: int = 1, maximum: int = 1024) -> str:
    if (not isinstance(value, str) or not minimum <= len(value) <= maximum
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError("invalid bounded text argument")
    return value


def _integer(value: Any, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("invalid bounded integer argument")
    return value


def _identifier(value: Any) -> str:
    value = _string(value, maximum=128)
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError("invalid resource identifier")
    return value


def _query_context(context: Any) -> None:
    """Trusted bridge envelope, separate from model-controlled arguments."""
    if type(context) is not dict or set(context) != {
        "actor", "job_id", "thread_id", "turn_id", "question", "policy_version"
    }:
        raise ValueError("invalid query context")
    for key in ("actor", "job_id", "thread_id", "turn_id"):
        _identifier(context[key])
    question = context["question"]
    if (context["policy_version"] != "readonly:v2" or not isinstance(question, str)
            or not 1 <= len(question) <= 16000 or "\x00" in question):
        raise ValueError("invalid query context")


def _json(raw: bytes) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON keys are forbidden")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("non-finite JSON values are forbidden")

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeError, RecursionError, OverflowError) as exc:
        raise ValueError("invalid JSON") from None


def _sanitize(value: Any, state: Dict[str, bool], depth: int = 0) -> Any:
    if depth > 12:
        state["truncated"] = True
        return "[TRUNCATED]"
    if isinstance(value, str):
        text = redact_private_text(redact(value))
        if len(text) > 24000:
            state["truncated"] = True
        return text[:24000]
    if isinstance(value, dict):
        result = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 100:
                state["truncated"] = True
                break
            key = str(key)
            safe_key = redact(key)[:256]
            normalized_key = unicodedata.normalize("NFKC", key).casefold()
            private_key = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", normalized_key) in _PRIVATE_FIELDS
            safe_pagination = depth == 0 and key == "page_token" and state.get("preserve_page_token", False)
            pair_name = value.get("Key", value.get("key"))
            if isinstance(pair_name, str):
                pair_name = unicodedata.normalize("NFKC", pair_name).casefold()
            protected_pair = (key in ("Value", "value") and isinstance(pair_name, str)
                              and (_SECRET_FIELD.search(re.sub(r"[^A-Za-z]", "", pair_name))
                                   or re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", pair_name.casefold()) in _PRIVATE_FIELDS))
            if not safe_pagination and (private_key or protected_pair or _SECRET_FIELD.search(re.sub(r"[^A-Za-z]", "", normalized_key))):
                result[safe_key] = "[REDACTED]"
            else:
                result[safe_key] = _sanitize(item, state, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > 200:
            state["truncated"] = True
        return [_sanitize(item, state, depth + 1) for item in value[:200]]
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    return "[UNAVAILABLE]"


def bounded_result(value: Any, *, preserve_page_token: bool = False) -> Dict[str, Any]:
    state = {"truncated": False, "preserve_page_token": preserve_page_token}
    clean = _sanitize(value, state)
    result = {"ok": True, "data": clean, "truncated": state["truncated"]}
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) <= MAX_RESPONSE_BYTES:
        return result
    preview = json.dumps(clean, ensure_ascii=False, allow_nan=False).encode("utf-8")[:14000].decode("utf-8", "ignore")
    result = {"ok": True, "data": {"preview": preview, "format": "truncated_json"}, "truncated": True}
    while len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_RESPONSE_BYTES:
        result["data"]["preview"] = result["data"]["preview"][:len(result["data"]["preview"]) // 2]
    return result


def _subprocess_environment() -> Dict[str, str]:
    keys = ("PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "XDG_CONFIG_HOME",
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy",
            "all_proxy", "no_proxy")
    return {key: os.environ[key] for key in keys if key in os.environ}


def run_cli(argv: list[str], *, tls: bool = False, timeout: Optional[float] = None) -> Any:
    """Run only broker-constructed argv; cap pipes, deadline, and child lifetime."""
    timeout = PROVIDER_TIMEOUT if timeout is None else timeout
    if type(timeout) not in (float, int) or not 0 < timeout <= PROVIDER_TIMEOUT:
        raise ValueError("invalid provider timeout")
    deadline = time.monotonic() + timeout
    executable = shutil.which(argv[0])
    if not executable:
        raise QueryError("query provider is not installed")
    environment = _subprocess_environment()
    if tls:
        environment.update(VE_CALLER_TYPE="ai_agent", VE_CALLER_NAME="codex", VE_SKILL_NAME="volcengine-tls")
    try:
        process = subprocess.Popen([executable, *argv[1:]], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                                   cwd=str(Path.home()), env=environment, start_new_session=True)
    except OSError:
        raise QueryError("query provider could not start") from None
    output = bytearray()
    total = 0
    selector = selectors.DefaultSelector()
    try:
        for stream in (process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise QueryError("query provider timed out; narrow the query")
            for key, _ in selector.select(min(remaining, 0.2)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                total += len(chunk)
                if total > MAX_PROVIDER_BYTES:
                    raise QueryError("query response exceeded the limit; narrow the query")
                if key.fileobj is process.stdout:
                    output.extend(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise QueryError("query provider timed out; narrow the query")
        process.wait(timeout=remaining)
        if process.returncode != 0:
            if tls:
                # volclog emits its structured failure envelope before exiting
                # nonzero. Preserve its safe classification across the broker.
                try:
                    failure = _json(bytes(output))
                except (ValueError, TypeError):
                    pass
                else:
                    _raise_tls_auth_error(failure)
            raise QueryError("query provider failed; check its host-side authorization")
        try:
            return _json(bytes(output))
        except (ValueError, TypeError):
            raise QueryError("query provider returned an invalid response") from None
    except subprocess.TimeoutExpired:
        raise QueryError("query provider timed out; narrow the query") from None
    finally:
        selector.close()
        # Kill descendants too, including when the CLI parent exited but a
        # child still holds one of its inherited pipes open.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        process.stdout.close()
        process.stderr.close()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise QueryError("query provider redirected unexpectedly")


def load_grafana_config(path: Optional[Path] = None) -> tuple[str, str]:
    """Read only the already configured Grafana identity; never return it in results."""
    try:
        import tomllib
        with (path or (Path.home() / ".codex" / "config.toml")).open("rb") as stream:
            config = tomllib.load(stream)
        environment = config["mcp_servers"]["grafana"]["env"]
        base = environment["GRAFANA_URL"]
        token = environment["GRAFANA_SERVICE_ACCOUNT_TOKEN"]
        parsed = urlsplit(base)
        if (not isinstance(base, str) or parsed.scheme != "https" or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or any(ord(c) < 32 for c in base) or not isinstance(token, str)
                or not token or any(ord(c) < 32 for c in token)):
            raise ValueError()
        return base.rstrip("/"), token
    except (ImportError, OSError, ValueError, KeyError, TypeError, AttributeError):
        raise QueryError("Grafana host configuration is unavailable") from None


class QueryHost:
    def __init__(self, code_root: Path, *, runner: Callable[..., Any] = run_cli,
                 grafana_config: Optional[tuple[str, str]] = None,
                 dms_policy_path: Optional[Path] = None):
        self.reader = CodeReader(code_root)
        self.runner = runner
        self._grafana_config = grafana_config
        self._opener = build_opener(_NoRedirect())
        self.dms = DmsBroker(dms_policy_path, self.reader, runner)

    def close(self) -> None:
        self.reader.close()

    def execute(self, tool: str, arguments: Dict[str, Any], *, context: Optional[dict] = None) -> Dict[str, Any]:
        if not isinstance(tool, str) or tool not in _ALLOWED_ARGUMENTS or type(arguments) is not dict:
            raise ValueError("unknown query tool or invalid arguments")
        required, optional = _ALLOWED_ARGUMENTS[tool]
        if not required <= arguments.keys() or arguments.keys() - required - optional:
            raise ValueError("missing or unsupported query arguments")
        if context is not None:
            _query_context(context)
            if privacy_request_denied(context["question"]):
                raise ValueError("private data requests are not permitted")
        if tool == "business_data_checks":
            return bounded_result(self.dms.catalog(context, **arguments))
        if tool == "business_data_check":
            return bounded_result(self.dms.check(context, **arguments))
        if tool == "code_evidence":
            return bounded_result(self.dms.register_code(context, **arguments))
        if tool == "code_read":
            if context is not None:
                # Existing Codex threads retain their original dynamic tool
                # registry. Treat this old name as the same evidence attestation
                # entry point, not as a required path for ordinary source reads.
                return bounded_result(self.dms.register_code(context, **arguments))
            return bounded_result(self._code_read(**arguments))
        if tool == "code_search":
            result = bounded_result(self._code_search(**arguments))
            if context and not result["truncated"]:
                self.dms.record_search(context, result["data"])
            return result
        return bounded_result(getattr(self, "_" + tool)(**arguments), preserve_page_token=tool == "docs_search")

    def _code_repositories(self):
        return self.reader.list_repositories()

    def _code_search(self, query, repository="", limit=30):
        return self.reader.search(query, repository, limit)

    def _code_read(self, path, start_line=1, line_count=120):
        return self.reader.read(path, start_line, line_count)

    def _lark(self, argv):
        data = self.runner(["lark-cli", *argv])
        if not isinstance(data, dict) or data.get("ok") is not True:
            raise QueryError("document query failed; check its host-side authorization")
        return data.get("data", {})

    def _docs_search(self, query, page_token="", limit=10):
        query = _string(query, maximum=30)
        limit = _integer(limit, 1, 20)
        argv = ["drive", "+search", "--query=" + query, "--doc-types=docx,wiki,doc",
                "--page-size=" + str(limit), "--as=user", "--format=json"]
        if page_token:
            argv.append("--page-token=" + _string(page_token, maximum=2048))
        elif not isinstance(page_token, str):
            raise ValueError("invalid pagination token")
        return self._lark(argv)

    def _docs_fetch(self, document, keyword=""):
        document = _string(document, maximum=2048)
        if not _DOCUMENT_TOKEN.fullmatch(document):
            parsed = urlsplit(document)
            hostname = (parsed.hostname or "").lower()
            if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443)
                    or not any(hostname == suffix or hostname.endswith("." + suffix)
                               for suffix in ("feishu.cn", "larksuite.com"))
                    or not re.fullmatch(r"/(docx|doc|wiki)/[A-Za-z0-9]{8,128}/?", parsed.path)
                    or parsed.query or parsed.fragment):
                raise ValueError("document must be a Feishu document URL or token")
        keyword = _string(keyword, minimum=0, maximum=256)
        argv = ["docs", "+fetch", "--doc=" + document, "--as=user", "--doc-format=markdown", "--detail=simple"]
        argv += ["--scope=keyword", "--keyword=" + keyword] if keyword else ["--scope=full"]
        return self._lark(argv)

    def _tls(self, action, payload):
        data = self.runner(["ve", "volclog", "tool", "exec", action, "--input",
                            json.dumps(payload, ensure_ascii=False, separators=(",", ":")), "--output-mode", "stdout"], tls=True)
        _raise_tls_auth_error(data)
        if not isinstance(data, dict) or data.get("status") != "success":
            raise QueryError("TLS query failed; check its host-side authorization and query scope")
        if not isinstance(data.get("summary", {}), dict):
            raise QueryError("TLS returned an invalid response")
        if data.get("summary", {}).get("deliveryMode") not in (None, "stdout"):
            raise QueryError("TLS result is too large; narrow the query")
        return data.get("data", {})

    def _tls_topics(self, query="", project_id="", page=1):
        query = _string(query, minimum=0, maximum=128)
        page = _integer(page, 1, 1000)
        payload = {"PageNumber": page, "PageSize": 30}
        if query:
            if any(ord(c) > 0xffff for c in query):
                raise ValueError("TLS topic search cannot contain four-byte characters")
            payload["FuzzySearchKey"] = query
        if project_id:
            payload["ProjectId"] = _identifier(project_id)
        elif not isinstance(project_id, str):
            raise ValueError("invalid project identifier")
        return self._tls("topic.describe-topics", payload)

    def _tls_search(self, topic_id, query, start_time, end_time, limit=30):
        topic_id = _identifier(topic_id)
        query = _string(query, maximum=4096)
        if "|" in query:
            raise ValueError("TLS SQL and analysis pipelines are not enabled")
        start = _integer(start_time, 1000000000000, 9999999999999)
        end = _integer(end_time, 1000000000000, 9999999999999)
        if not 0 < end - start <= MAX_WINDOW_MS:
            raise ValueError("TLS query window must be positive and at most 24 hours")
        limit = _integer(limit, 1, 100)
        return self._tls("log.search", {"body": {"TopicId": topic_id, "Query": query,
                         "StartTime": start, "EndTime": end, "Limit": limit, "Sort": "desc"}})

    def _grafana(self, path: str, params=None):
        base, token = self._grafana_config or load_grafana_config()
        # Configuration is trusted, but disallow credentials in a base URL and
        # redirects so Authorization cannot migrate to a different destination.
        parsed = urlsplit(base)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise QueryError("Grafana host configuration is unavailable")
        request = Request(base.rstrip("/") + path + ("?" + urlencode(params) if params else ""),
                          headers={"Authorization": "Bearer " + token, "Accept": "application/json"}, method="GET")
        deadline = time.monotonic() + PROVIDER_TIMEOUT
        try:
            with self._opener.open(request, timeout=min(PROVIDER_TIMEOUT, 5.0)) as response:
                raw = bytearray()
                while True:
                    if time.monotonic() >= deadline:
                        raise QueryError("Grafana query timed out; narrow the query")
                    chunk = response.read1(min(65536, MAX_PROVIDER_BYTES + 1 - len(raw)))
                    if not chunk:
                        break
                    raw.extend(chunk)
                    if len(raw) > MAX_PROVIDER_BYTES:
                        raise QueryError("Grafana result is too large; narrow the query")
                return _json(bytes(raw))
        except (OSError, HTTPError, URLError, ValueError):
            raise QueryError("Grafana query failed; check its host-side authorization and query scope") from None

    def _grafana_datasources(self):
        sources = self._grafana("/api/datasources")
        if not isinstance(sources, list):
            raise QueryError("Grafana returned an invalid datasource response")
        return {"datasources": [{key: item.get(key) for key in ("uid", "name", "type")}
                                for item in sources if isinstance(item, dict)],
                "query_supported_type": "prometheus"}

    def _grafana_search_dashboards(self, query):
        return self._grafana("/api/search", {"query": _string(query, maximum=128), "type": "dash-db", "limit": 30})

    def _grafana_query(self, datasource_uid, query, start_time, end_time, step_seconds=60):
        uid = _identifier(datasource_uid)
        query = _string(query, maximum=4096)
        start = _string(start_time, maximum=64)
        end = _string(end_time, maximum=64)
        try:
            dates = [datetime.fromisoformat(value.replace("Z", "+00:00")) for value in (start, end)]
            if any(value.tzinfo is None or value.utcoffset() is None for value in dates):
                raise ValueError()
            window = (dates[1] - dates[0]).total_seconds()
            if not 0 < window <= 86400:
                raise ValueError()
        except (ValueError, OverflowError):
            raise ValueError("Grafana requires timezone-aware ISO times within a 24-hour window") from None
        step = _integer(step_seconds, 15, 3600)
        if window / step > 1440:
            raise ValueError("Grafana query exceeds 1440 points; increase step_seconds")
        metadata = self._grafana("/api/datasources/uid/" + uid)
        if not isinstance(metadata, dict) or metadata.get("type") != "prometheus":
            raise ValueError("only Prometheus datasource queries are enabled")
        return self._grafana("/api/datasources/proxy/uid/" + uid + "/api/v1/query_range",
                             {"query": query, "start": dates[0].timestamp(), "end": dates[1].timestamp(),
                              "step": step, "timeout": "30s"})


class QueryHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 8

    def __init__(self, address, host: QueryHost, token: str):
        if address[0] != "127.0.0.1":
            raise ValueError("query broker can listen only on 127.0.0.1")
        if not isinstance(token, str) or not 32 <= len(token) <= 512 or not token.isascii() or not token.isprintable():
            raise ValueError("query authentication token is invalid")
        self.host = host
        self.token = token
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(address, QueryHandler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        # Never print raw request data, provider stderr, or credential-bearing errors.
        pass


class QueryHandler(BaseHTTPRequestHandler):
    server_version = "OctopusQuery"
    sys_version = ""

    def setup(self):
        self.request.settimeout(10)
        super().setup()

    def log_message(self, format, *args):
        pass

    def _send(self, status, value):
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _authorized(self):
        authorizations = self.headers.get_all("Authorization", [])
        expected = "Bearer " + self.server.token
        if (len(authorizations) != 1 or not authorizations[0].isascii()
                or not hmac.compare_digest(authorizations[0], expected)):
            self._send(401, {"ok": False, "error": "authentication required"})
            return False
        # A web page cannot use this private API through a browser origin.
        if self.headers.get("Origin"):
            self._send(403, {"ok": False, "error": "browser origins are not allowed"})
            return False
        return True

    def do_GET(self):
        if not self._authorized():
            return
        if self.path != "/health":
            self._send(404, {"ok": False, "error": "unknown endpoint"})
            return
        self._send(200, {"ok": True})

    def do_POST(self):
        if not self._authorized():
            return
        if self.path != "/v1/query":
            self._send(404, {"ok": False, "error": "unknown endpoint"})
            return
        sizes = self.headers.get_all("Content-Length", [])
        if (len(sizes) != 1 or len(sizes[0]) > 10 or not sizes[0].isascii() or not sizes[0].isdigit()
                or self.headers.get("Transfer-Encoding") or self.headers.get("Content-Encoding")
                or self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json"):
            self._send(400, {"ok": False, "error": "invalid request framing"})
            return
        size = int(sizes[0])
        if not 1 <= size <= MAX_REQUEST_BYTES:
            self._send(413, {"ok": False, "error": "request size exceeds limit"})
            return
        try:
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError()
            request = _json(raw)
            if (type(request) is not dict or not {"tool", "arguments"} <= request.keys()
                    or request.keys() - {"tool", "arguments", "context"}):
                raise ValueError()
            if "context" in request:
                _query_context(request["context"])
                result = self.server.host.execute(request["tool"], request["arguments"], context=request["context"])
            else:
                result = self.server.host.execute(request["tool"], request["arguments"])
            self._send(200, result)
        except (ValueError, TypeError, FileNotFoundError, RecursionError):
            self._send(400, {"ok": False, "error": "invalid or forbidden query arguments"})
        except QueryError as exc:
            error = {"ok": False, "error": str(exc)}
            if exc.code is not None:
                error["error_code"] = exc.code
            self._send(502, error)
        except (TimeoutError, socket.timeout):
            self._send(408, {"ok": False, "error": "request timed out"})
        except Exception:
            self._send(500, {"ok": False, "error": "query failed"})


def read_token(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 1024:
            raise ValueError("query token file must be private and bounded")
        return os.read(fd, 1024).decode("ascii").strip()
    finally:
        os.close(fd)
