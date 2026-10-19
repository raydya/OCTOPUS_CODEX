from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from octopus.queries import QueryClient, TOOL_SPECS


class QueryClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_task_context_crosses_authenticated_boundary_outside_model_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "token"
            token.write_text("x" * 40)
            client = QueryClient("http://127.0.0.1:4501", str(token))
            opener = Mock()
            opener.open.return_value = io.BytesIO(b'{"ok": true, "data": {}}')
            context = {"actor": "alice", "job_id": "job1", "thread_id": "thread1", "turn_id": "turn1",
                       "question": "业务问题" * 3500, "policy_version": "readonly:v2"}
            with patch("octopus.queries.build_opener", return_value=opener):
                result = await client.call("code_read", {"path": "service/dao.py"}, context=context)
            self.assertTrue(result["ok"])
            request = opener.open.call_args.args[0]
            self.assertEqual(json.loads(request.data), {
                "tool": "code_read", "arguments": {"path": "service/dao.py"}, "context": context})
            self.assertLess(len(request.data), 64000)
            self.assertEqual(request.headers["Authorization"], "Bearer " + "x" * 40)

    def test_model_cannot_select_sql_or_connection_or_supply_authorization(self):
        specs = {spec["name"]: spec["inputSchema"] for spec in TOOL_SPECS}
        self.assertEqual(set(specs["business_data_check"]["properties"]),
                         {"evidence_id", "check_id", "day", "time_column", "columns", "mode", "filters"})
        self.assertEqual(set(specs["business_data_checks"]["properties"]), {"evidence_id", "table", "database_keyword"})
        self.assertFalse(specs["business_data_check"]["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
