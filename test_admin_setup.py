"""Fresh-install credentials and legacy reverse-proxy command regressions."""
import base64
from contextlib import ExitStack, redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import auth


@unittest.skipUnless(sys.platform == "linux", "Administrator setup uses Linux ownership and services")
class AdminSetupTests(unittest.TestCase):
    def setUp(self):
        import manage
        self.manage = manage
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.env = self.root / "admin.env"
        self.legacy = self.root / "legacy.env"
        self.users = self.root / "users.json"
        self.env.write_text("# existing settings\nADMIN_ALLOWED_HOSTS=127.0.0.1,localhost\nADMIN_USERNAME=admin\nADMIN_PASSWORD=\nOTHER_SETTING=keep\n")
        self.stack.enter_context(patch.object(manage, "ADMIN_ENV", self.env))
        self.stack.enter_context(patch.object(manage, "LEGACY_ENV", self.legacy))
        self.stack.enter_context(patch.object(manage, "DEFAULT_USERS_PATH", self.users))
        self.stack.enter_context(patch.object(manage.grp, "getgrnam", return_value=SimpleNamespace(gr_gid=os.getgid())))
        self.stack.enter_context(patch.object(manage.os, "fchown"))
        self.run = self.stack.enter_context(patch.object(manage, "run"))
        self.output = self.stack.enter_context(redirect_stdout(io.StringIO()))

    def test_new_install_creates_working_hashed_admin_without_early_restart(self):
        self.assertTrue(self.manage.initialize_admin(restart=False))
        password = re.search(r"随机密码：(\S+)", self.output.getvalue())[1]
        self.assertGreaterEqual(len(password), 24)
        records = auth.load_users(self.users)
        self.assertEqual(set(records), {"admin"})
        self.assertNotIn(password, self.users.read_text())
        self.assertNotIn(password, self.env.read_text())
        self.assertEqual(self.users.stat().st_mode & 0o777, 0o640)
        self.assertIn("OTHER_SETTING=keep", self.env.read_text())
        self.assertIn(f"ADMIN_USERS_FILE={self.users}", self.env.read_text())
        with patch.dict(os.environ, {"ADMIN_USERS_FILE": str(self.users)}):
            self.assertTrue(auth.authorized("Basic " + base64.b64encode(f"admin:{password}".encode()).decode()))
            self.assertFalse(auth.authorized(""))
        self.run.assert_not_called()

    def test_repeat_install_preserves_accounts_and_never_displays_password_again(self):
        self.manage.initialize_admin(restart=False)
        old = self.users.read_bytes(), self.env.read_bytes()
        self.output.seek(0)
        self.output.truncate()
        self.assertFalse(self.manage.initialize_admin())
        self.assertEqual((self.users.read_bytes(), self.env.read_bytes()), old)
        self.assertNotIn("随机密码：", self.output.getvalue())
        self.run.assert_not_called()

    def test_initial_password_is_unique_per_install(self):
        for name in ("one", "two"):
            with patch.object(self.manage, "DEFAULT_USERS_PATH", self.root / (name + ".json")), patch.object(self.manage, "ADMIN_ENV", self.root / (name + ".env")):
                self.manage.initialize_admin(restart=False)
        passwords = re.findall(r"随机密码：(\S+)", self.output.getvalue())
        self.assertEqual(len(passwords), 2)
        self.assertNotEqual(*passwords)

    def test_old_password_in_either_environment_file_is_preserved(self):
        for path in (self.env, self.legacy):
            with self.subTest(path=path):
                original = self.env.read_bytes()
                if path == self.legacy:
                    self.env.write_text("ADMIN_ALLOWED_HOSTS=localhost\n")
                with path.open("a") as stream:
                    stream.write('ADMIN_PASSWORD="legacy long password"\n')
                before = path.read_bytes()
                self.assertFalse(self.manage.initialize_admin())
                self.assertFalse(self.users.exists())
                self.assertEqual(path.read_bytes(), before)
                self.env.write_bytes(original)
                if self.legacy.exists():
                    self.legacy.unlink()
        self.run.assert_not_called()

    def test_custom_account_file_is_preserved_and_missing_custom_file_fails_closed(self):
        custom = self.root / "custom.json"
        custom.write_text(json.dumps({"version": 1, "users": {"owner": auth.password_record("custom existing password")}}))
        self.env.write_text(f'ADMIN_USERS_FILE="{custom}"\n')
        before = custom.read_bytes(), self.env.read_bytes()
        self.assertFalse(self.manage.initialize_admin())
        self.assertEqual((custom.read_bytes(), self.env.read_bytes()), before)
        self.assertFalse(self.users.exists())
        custom.unlink()
        with self.assertRaises(OSError):
            self.manage.configure_admin_domain("hw.example.com")
        self.assertNotIn("hw.example.com", self.env.read_text())
        self.run.assert_not_called()

    def test_legacy_domain_command_initializes_auth_without_changing_domain_settings(self):
        self.env.write_text("# original\nADMIN_ALLOWED_HOSTS=localhost,old.example.com\nADMIN_PASSWORD=\nCUSTOM_VALUE=keep\n")
        self.manage.configure_admin_domain("https://HW.6990699.xyz:443/")
        environment = self.manage.admin_environment()
        self.assertEqual(environment["ADMIN_ALLOWED_HOSTS"], "localhost,old.example.com")
        self.assertIn("CUSTOM_VALUE=keep", self.env.read_text())
        self.assertIn("# original", self.env.read_text())
        self.assertEqual(environment["ADMIN_USERS_FILE"], str(self.users))
        self.run.assert_called_once_with("systemctl", "restart", self.manage.ADMIN)
        original = self.users.read_bytes()
        self.manage.configure_admin_domain("hw.6990699.xyz")
        self.assertEqual(self.users.read_bytes(), original)
        self.assertNotIn("hw.6990699.xyz", self.env.read_text())

    def test_domain_setup_preserves_legacy_authentication(self):
        self.env.write_text('ADMIN_ALLOWED_HOSTS=localhost\nADMIN_PASSWORD="legacy long password"\nADMIN_USERNAME=owner\n')
        self.manage.configure_admin_domain("hw.example.com")
        self.assertIn('ADMIN_PASSWORD="legacy long password"', self.env.read_text())
        self.assertIn("ADMIN_USERNAME=owner", self.env.read_text())
        self.assertFalse(self.users.exists())

    def test_compatibility_command_ignores_old_wildcard_and_does_not_prompt_for_domain(self):
        self.env.write_text('ADMIN_ALLOWED_HOSTS=*\nADMIN_PASSWORD="legacy long password"\n')
        original = self.env.read_bytes()
        with patch("builtins.input", side_effect=AssertionError("No domain input is needed")):
            self.manage.configure_admin_domain()
            self.manage.configure_admin_domain("new.example")
        self.assertEqual(self.env.read_bytes(), original)
        self.assertIn("无需逐个配置", self.output.getvalue())
        self.assertFalse(self.users.exists())

    def test_real_installer_block_initializes_once_without_starting_service(self):
        # Execute the actual installer branch with filesystem/service isolation.
        repository = Path(__file__).resolve().parent
        source = (repository / "install.sh").read_text()
        start = source.index('[[ ! -L "$ADMIN_ENV_FILE" ]]')
        end = source.index('install -d -o "$SERVICE_USER"', start)
        block = source[start:end]
        installed = self.root / "installed"
        installed.mkdir()
        template = self.root / "source"
        template.mkdir()
        (template / "admin.env.example").write_text('ADMIN_ALLOWED_HOSTS=127.0.0.1,localhost\nADMIN_PASSWORD=\n')
        (installed / "manage.py").write_text(
            "import importlib.util, os, sys\nfrom pathlib import Path\nfrom types import SimpleNamespace\n"
            f"sys.path.insert(0, {str(repository)!r})\n"
            f"spec=importlib.util.spec_from_file_location('real_manage', {str(repository / 'manage.py')!r})\n"
            "module=importlib.util.module_from_spec(spec)\nspec.loader.exec_module(module)\n"
            f"module.ADMIN_ENV=Path({str(self.env)!r})\nmodule.LEGACY_ENV=Path({str(self.legacy)!r})\n"
            f"module.DEFAULT_USERS_PATH=Path({str(self.users)!r})\n"
            "module.grp.getgrnam=lambda _: SimpleNamespace(gr_gid=os.getgid())\nmodule.os.fchown=lambda *args: None\n"
            "def forbidden(*args): raise AssertionError('Installer must not start services before unit reload')\n"
            "module.run=forbidden\ninitialize_admin=module.initialize_admin\n")
        self.env.unlink()
        script = '''set -Eeuo pipefail
fail() { printf '%s\\n' "$*" >&2; exit 1; }
chown() { :; }
install() {
  local -a args=()
  while [[ "$#" -gt 0 ]]; do
    case "$1" in -o|-g) shift 2;; *) args+=("$1"); shift;; esac
  done
  command install "${args[@]}"
}
''' + block
        environment = {**os.environ, "ADMIN_ENV_FILE": str(self.env), "INSTALL_DIR": str(installed), "SOURCE_DIR": str(template), "SERVICE_GROUP": "unused"}
        first = subprocess.run(["bash", "-c", script], env=environment, text=True, capture_output=True, timeout=10)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("随机密码：", first.stdout)
        old = self.users.read_bytes(), self.env.read_bytes()
        second = subprocess.run(["bash", "-c", script], env=environment, text=True, capture_output=True, timeout=10)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertNotIn("随机密码：", second.stdout)
        self.assertEqual((self.users.read_bytes(), self.env.read_bytes()), old)


if __name__ == "__main__":
    unittest.main()
