from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse, unquote

from octopus.config import Config, ConfigurationError, database_url_from_env


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.token = Path(self.directory.name) / "token"
        self.token.write_text("a" * 64)
        self.secret = Path(self.directory.name) / "app_secret"
        self.secret.write_text("synthetic-new-app-secret")
        self.env = {
            "FEISHU_APP_ID": "cli_new_application",
            "FEISHU_APP_SECRET_FILE": str(self.secret),
            "DATABASE_URL": "postgresql://test:test@db/test",
            "CODEX_WORKSPACE": "/host/workspace/not/in/container",
            "CODEX_URL": "ws://host.docker.internal:4500",
            "CODEX_TOKEN_FILE": str(self.token),
        }

    def test_independent_credentials_and_host_workspace(self):
        with patch.dict(os.environ, {**self.env, "LARK_APP_ID": "cli_old"}, clear=True):
            config = Config.from_env()
        self.assertEqual(config.app_id, "cli_new_application")
        self.assertEqual(config.app_secret, "synthetic-new-app-secret")
        self.assertEqual(config.workspace, "/host/workspace/not/in/container")

    def test_group_mode_requires_exact_bot_identity(self):
        with patch.dict(os.environ, {**self.env, "FEISHU_ALLOW_GROUPS": "true"}, clear=True):
            with self.assertRaisesRegex(ConfigurationError, "FEISHU_BOT_OPEN_ID"):
                Config.from_env()
            os.environ["FEISHU_BOT_OPEN_ID"] = "ou_new_bot"
            self.assertEqual(Config.from_env().bot_open_id, "ou_new_bot")

    def test_remote_plaintext_and_url_credentials_are_rejected(self):
        for url in ("ws://external.example:4500", "wss://user:secret@external.example"):
            with patch.dict(os.environ, {**self.env, "CODEX_URL": url}, clear=True):
                with self.assertRaises(ConfigurationError):
                    Config.from_env()

    def test_password_file_is_encoded_without_leaking_it(self):
        password = Path(self.directory.name) / "pg_password"
        password.write_text("synthetic:p@ss/word")
        with patch.dict(os.environ, {"POSTGRES_PASSWORD_FILE": str(password), "POSTGRES_HOST": "db"}, clear=True):
            url = urlparse(database_url_from_env())
        self.assertEqual(url.hostname, "db")
        self.assertEqual(unquote(url.password), "synthetic:p@ss/word")

    def test_missing_secret_error_contains_only_field_name(self):
        self.secret.write_text("")
        with patch.dict(os.environ, self.env, clear=True):
            with self.assertRaisesRegex(ConfigurationError, "尚未配置：FEISHU_APP_SECRET"):
                Config.from_env()
