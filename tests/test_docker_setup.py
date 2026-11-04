"""Exercise Docker bootstrap and explicit reset against disposable data files."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import auth


class DockerSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.users = self.data / "stockwatch-users.json"
        self.root = Path(__file__).resolve().parent.parent
        self.environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith("HOSTWATCH_INITIAL_ADMIN_")
        }
        self.environment.update(
            HOSTWATCH_DATA_DIR=str(self.data),
            ADMIN_USERS_FILE=str(self.users),
            HOSTWATCH_SOURCE_CONFIG=str(self.root / "config/config.example.json"),
            PYTHONUTF8="1",
        )

    def run_script(self, name):
        result = subprocess.run(
            [sys.executable, str(self.root / "docker" / name)],
            env=self.environment, capture_output=True, text=True, encoding="utf-8", timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def assert_login(self, username, password):
        header = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        with patch.dict(os.environ, {"ADMIN_USERS_FILE": str(self.users)}):
            self.assertTrue(auth.authorized(header))

    def test_fresh_install_uses_requested_credentials_and_repeat_preserves_changed_password(self):
        output = self.run_script("bootstrap.py")
        self.assertEqual(set(auth.load_users(self.users)), {"admin"})
        self.assert_login("admin", "admin123456789")
        self.assertIn("admin123456789", output)
        self.assertNotIn("admin123456789", self.users.read_text())
        self.users.write_text(json.dumps({"version": 1, "users": {
            "admin": auth.password_record("changed-password-123"),
        }}))
        before = self.users.read_bytes()
        output = self.run_script("bootstrap.py")
        self.assertEqual(self.users.read_bytes(), before)
        self.assertNotIn("初始密码", output)
        self.assert_login("admin", "changed-password-123")

    def test_upgrade_preserves_legacy_account_and_explicit_reset_keeps_other_users(self):
        self.users.write_text(json.dumps({"version": 1, "users": {
            "pingzi": auth.password_record("legacy-password-123"),
        }}))
        before = self.users.read_bytes()
        self.run_script("bootstrap.py")
        self.assertEqual(self.users.read_bytes(), before)
        self.run_script("reset_admin.py")
        self.assertEqual(set(auth.load_users(self.users)), {"admin", "pingzi"})
        self.assert_login("admin", "admin123456789")
        self.assert_login("pingzi", "legacy-password-123")

    def test_explicit_initial_credentials_override_defaults(self):
        self.environment.update(
            HOSTWATCH_INITIAL_ADMIN_USERNAME="custom-admin",
            HOSTWATCH_INITIAL_ADMIN_PASSWORD="custom-password-123",
        )
        self.run_script("bootstrap.py")
        self.assertEqual(set(auth.load_users(self.users)), {"custom-admin"})
        self.assert_login("custom-admin", "custom-password-123")
