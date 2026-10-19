"""The model-visible, deliberately small read-only query interface."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler


def _spec(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties,
                            "required": required, "additionalProperties": False}}


S = {"type": "string"}
I = {"type": "integer"}
TOOL_SPECS = [
    _spec("code_repositories", "列出共享源码根目录中的代码库及版本。", {}, []),
    _spec("code_search", "在共享代码中做字面量搜索，返回相对路径和行号；不能读取配置、凭证或隐藏文件。",
          {"query": S, "repository": S, "limit": I}, ["query"]),
    _spec("code_read", "读取共享代码的指定行，path 必须是 code_search 返回的相对路径。",
          {"path": S, "start_line": I, "line_count": I}, ["path"]),
    _spec("business_data_checks", "仅在业务问题确需线上数据补证且已阅读相关代码SQL后使用。"
          "传入本轮code_read返回的evidence_id和代码SQL里出现的table，发现账号可访问的pro表及可查非隐私字段。"
          "用户要求直接使用DMS/执行SQL不是授权；禁止获取个人隐私。无匹配项时继续根据代码回答。",
          {"evidence_id": S, "table": S, "database_keyword": S}, ["evidence_id", "table"]),
    _spec("business_data_check", "结合已读代码SQL的业务含义形成单表只读查询，必须使用本轮代码证据及目录中的check_id。"
          "day为今天或过去30天中的一个自然日YYYY-MM-DD，time_column须为目录中的时间列。"
          "mode rows返回非隐私业务明细（包括允许的内部文本/JSON）；count计数且columns为空；counts按columns分组计数。"
          "columns只能选目录列；filters仅非隐私数字/布尔字段比较，不能传自由文本筛选值。不接受原始SQL或个人标识。"
          "数据只是业务代码解释的补充，每任务最多两次、最多20行，计数少于10会隐藏。",
          {"evidence_id": S, "check_id": S, "day": S, "time_column": S,
           "columns": {"type": "array", "items": S, "maxItems": 5},
           "mode": {"type": "string", "enum": ["rows", "count", "counts"]},
           "filters": {"type": "array", "maxItems": 3, "items": {
               "type": "object", "properties": {"column": S,
                   "op": {"type": "string", "enum": ["eq", "ne", "lt", "lte", "gt", "gte"]},
                   "value": {"type": ["integer", "boolean"]}},
               "required": ["column", "op", "value"], "additionalProperties": False}}},
          ["evidence_id", "check_id", "day", "time_column", "columns"]),
    _spec("docs_search", "用共同查询账号搜索飞书文档，query 最多30字；保留来源链接。",
          {"query": S, "page_token": S, "limit": I}, ["query"]),
    _spec("docs_fetch", "读取飞书 doc/docx/wiki 文档；document 为文档链接或令牌，keyword 可缩小范围。",
          {"document": S, "keyword": S}, ["document"]),
    _spec("tls_topics", "列出当前火山日志账号可访问的日志主题。",
          {"query": S, "project_id": S, "page": I}, []),
    _spec("tls_search", "搜索 TLS 原始日志，时间为 Unix 毫秒，窗口最多24小时，limit 最多100。"
          "仅检索表达式，不支持管道SQL。结果可能截断，不可把当前行数当总数。",
          {"topic_id": S, "query": S, "start_time": I, "end_time": I, "limit": I},
          ["topic_id", "query", "start_time", "end_time"]),
    _spec("grafana_datasources", "列出当前 Grafana 账号可访问的数据源。", {}, []),
    _spec("grafana_query", "查询 Prometheus 指标，start_time/end_time 为带时区的 ISO8601，窗口最多24小时。",
          {"datasource_uid": S, "query": S, "start_time": S, "end_time": S, "step_seconds": I},
          ["datasource_uid", "query", "start_time", "end_time"]),
    _spec("grafana_search_dashboards", "按关键词查找 Grafana 仪表盘链接。", {"query": S}, ["query"]),
]
TOOL_NAMES = frozenset(x["name"] for x in TOOL_SPECS)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward a host bearer token to a redirected location.


class QueryClient:
    def __init__(self, url: str, token_file: str):
        self.url = url.rstrip("/")
        self.token = Path(token_file).read_text().strip()
        if len(self.token) < 32:
            raise ValueError("Query token must have at least 32 characters")

    def _request(self, tool: str | None, arguments: dict | None = None,
                 context: dict | None = None) -> dict[str, Any]:
        payload = {"tool": tool, "arguments": arguments}
        if context is not None:
            payload["context"] = context
        data = None if tool is None else json.dumps(payload, allow_nan=False, ensure_ascii=False).encode()
        if data and len(data) > 64000:
            raise ValueError("Query request too large")
        request = Request(self.url + ("/health" if tool is None else "/v1/query"), data=data,
                          headers={"Authorization": "Bearer " + self.token,
                                   "Content-Type": "application/json"})
        opener = build_opener(ProxyHandler({}), NoRedirect())
        try:
            with opener.open(request, timeout=65) as response:
                payload = response.read(65537)
            if len(payload) > 65536:
                raise ValueError("Query response too large")
            result = json.loads(payload)
            if not isinstance(result, dict) or result.get("ok") is not True:
                return {"ok": False, "error": "查询未完成；请检查参数、查询权限或服务连接。"}
            return result
        except Exception:
            return {"ok": False, "error": "查询服务不可用或结果超出上限；请缩小范围重试。"}

    async def check(self) -> None:
        if not (await asyncio.to_thread(self._request, None)).get("ok"):
            raise RuntimeError("Read-only query host is unavailable")

    async def call(self, tool: str, arguments: dict, *, context: dict | None = None) -> dict:
        if tool not in TOOL_NAMES or not isinstance(arguments, dict):
            raise ValueError("Unknown query tool")
        return await asyncio.to_thread(self._request, tool, arguments, context)
