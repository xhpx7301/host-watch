import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
import ssl
from urllib.error import URLError, HTTPError
from unittest.mock import patch, MagicMock


@unittest.skipUnless(sys.platform == "linux", "菜单使用 Linux 的文件权限及服务接口")
class ManagementTests(unittest.TestCase):
    def setUp(self):
        import manage
        self.manage = manage
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def test_atomic_write_preserves_original_when_replace_fails(self):
        target = self.directory / "config"
        target.write_text("original")
        with patch.object(self.manage.os, "replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                self.manage.atomic_write(target, "replacement")
        self.assertEqual(target.read_text(), "original")
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()), ["config"])

    def test_rejects_symlink_destination(self):
        original = self.directory / "original"
        original.write_text("safe")
        link = self.directory / "link"
        link.symlink_to(original)
        with self.assertRaises(ValueError):
            self.manage.atomic_write(link, "overwrite")
        self.assertEqual(original.read_text(), "safe")

    def test_prevents_last_admin_deletion(self):
        with patch.object(self.manage, "read_accounts", return_value={"alice": {}}), patch("builtins.input", return_value="alice"), patch.object(self.manage, "save_accounts") as save:
            with self.assertRaisesRegex(ValueError, "最后一个"):
                self.manage.delete_account()
            save.assert_not_called()

    def test_activating_accounts_preserves_hosts_and_removes_legacy_secret(self):
        env = self.directory / "admin.env"
        env.write_text('# configuration\nADMIN_ALLOWED_HOSTS=127.0.0.1,stock.example.com\nADMIN_PASSWORD="old secret"\nADMIN_USERNAME=old\n')
        with patch.object(self.manage, "ADMIN_ENV", env), patch.object(self.manage.grp, "getgrnam", return_value=SimpleNamespace(gr_gid=123)), patch.object(self.manage.os, "fchown") as chown, patch.object(self.manage, "run") as run:
            self.manage.activate_accounts()
        contents = env.read_text()
        self.assertIn("ADMIN_ALLOWED_HOSTS=127.0.0.1,stock.example.com", contents)
        self.assertIn("ADMIN_USERS_FILE=/etc/stockwatch-users.json", contents)
        self.assertNotIn("old secret", contents)
        self.assertNotIn("ADMIN_USERNAME=", contents)
        self.assertEqual(env.stat().st_mode & 0o777, 0o640)
        self.assertEqual(chown.call_args.args[1:], (0, 123))
        run.assert_called_once_with("systemctl", "restart", self.manage.ADMIN)

    def test_password_confirmation_mismatch_never_returns_password(self):
        with patch.object(self.manage.getpass, "getpass", side_effect=["first password", "other password"]):
            with self.assertRaisesRegex(ValueError, "不一致"):
                self.manage.ask_password()

    def test_start_requires_enabled_config_before_systemctl(self):
        (self.directory / "config.json").write_text(json.dumps({"enabled": False}))
        with patch.object(self.manage, "DATA_DIR", self.directory), patch.object(self.manage, "run") as run:
            with self.assertRaises(ValueError):
                self.manage.start_monitor()
            run.assert_not_called()

    def test_menu_status_shows_flaresolverr_monitor_and_admin_states(self):
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            if args[:3] == ["systemctl", "is-active", self.manage.MONITOR]:
                return SimpleNamespace(returncode=0, stdout="active\n")
            if args[:3] == ["systemctl", "is-active", self.manage.ADMIN]:
                return SimpleNamespace(returncode=3, stdout="inactive\n")
            return SimpleNamespace(returncode=0, stdout="running\n")

        with patch.object(self.manage.shutil, "which", return_value="/usr/bin/docker"), patch.object(self.manage.subprocess, "run", side_effect=fake_run), patch("builtins.print") as output:
            self.manage.print_menu_status()
        line = output.call_args.args[0]
        self.assertIn("服务 运行中", line)
        self.assertIn("监控 运行中", line)
        self.assertIn("管理页 已停止", line)

    def test_web_apply_restarts_running_monitor_to_read_new_config(self):
        (self.directory / "config.json").write_text(json.dumps({"enabled": True}))
        with patch.object(self.manage, "DATA_DIR", self.directory), patch.object(self.manage, "run") as run:
            self.manage.web_action({"action": "monitor-apply"})
        self.assertEqual([call.args for call in run.call_args_list], [
            ("systemctl", "enable", self.manage.MONITOR), ("systemctl", "restart", self.manage.MONITOR)])

    def test_web_apply_disabled_config_stops_monitor(self):
        (self.directory / "config.json").write_text(json.dumps({"enabled": False}))
        with patch.object(self.manage, "DATA_DIR", self.directory), patch.object(self.manage, "run") as run:
            self.manage.web_action({"action": "monitor-apply"})
        run.assert_called_once_with("systemctl", "disable", "--now", self.manage.MONITOR)

    def test_web_rejects_arbitrary_actions_and_update_unit_names(self):
        for payload in ({"action": "shell", "command": "id"}, {"action": "update-status", "unit": "ssh.service"}):
            with self.subTest(payload=payload), patch.object(self.manage, "run") as run:
                with self.assertRaises(ValueError):
                    self.manage.web_action(payload)
                run.assert_not_called()

    def test_web_cannot_delete_last_administrator(self):
        with patch.object(self.manage, "read_accounts", return_value={"alice": {}}), patch.object(self.manage, "save_accounts") as save:
            with self.assertRaisesRegex(ValueError, "最后一个"):
                self.manage.web_action({"action": "admin-delete", "username": "alice"})
            save.assert_not_called()

    def test_web_password_confirmation_failure_keeps_accounts(self):
        with patch.object(self.manage, "read_accounts", return_value={"alice": {}}), patch.object(self.manage, "save_accounts") as save:
            with self.assertRaises(ValueError):
                self.manage.web_action({"action": "admin-passwd", "username": "alice", "password": "password long enough", "confirmation": "different"})
            save.assert_not_called()

    def test_web_update_runs_in_independent_systemd_unit(self):
        with patch.object(self.manage, "run") as run:
            result = self.manage.web_action({"action": "update"})
        self.assertRegex(result["update_unit"], r"^host-watch-web-update-[0-9]{14}-[0-9a-f]{8}\.service$")
        self.assertEqual(run.call_args.args[0], "systemd-run")
        self.assertEqual(run.call_args.args[-1], "web-update-worker")

    def test_web_start_stop_persists_enabled_state_with_service_owner(self):
        config = self.directory / "config.json"
        config.write_text(json.dumps({"enabled": False}))
        with patch.object(self.manage, "DATA_DIR", self.directory), patch.object(self.manage, "run"), patch.object(self.manage.grp, "getgrnam", return_value=SimpleNamespace(gr_gid=123)), patch.object(self.manage.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=456)), patch.object(self.manage.os, "fchown") as chown:
            self.manage.web_action({"action": "monitor-start"})
            self.assertTrue(json.loads(config.read_text())["enabled"])
            self.manage.web_action({"action": "monitor-stop"})
            self.assertFalse(json.loads(config.read_text())["enabled"])
        self.assertEqual(chown.call_args.args[1:], (456, 123))

    def test_update_network_error_keeps_services_untouched(self):
        with patch.object(self.manage, "urlopen", side_effect=OSError("offline")) as download, patch.object(self.manage.time, "sleep"), patch.object(self.manage, "run") as run, patch.object(self.manage, "backup") as backup:
            with self.assertRaises(OSError):
                self.manage.update()
            self.assertEqual(download.call_count, 3)
            run.assert_not_called()
            backup.assert_not_called()

    def test_download_recovers_from_handshake_timeout_and_official_fallback(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"#!/usr/bin/env bash\nexit 0\n"
        with patch.object(self.manage, "urlopen", side_effect=[URLError(TimeoutError("handshake timed out")), URLError(TimeoutError()), response]) as download, patch.object(self.manage.time, "sleep"):
            self.assertTrue(self.manage.download_installer().startswith(b"#!/"))
        self.assertEqual(download.call_args.args[0], self.manage.INSTALLER_FALLBACK_URL)

    def test_certificate_http404_and_invalid_script_are_not_retried(self):
        for error in [URLError(ssl.SSLCertVerificationError("invalid cert")), HTTPError(self.manage.INSTALLER_URL, 404, "Not found", {}, None)]:
            with self.subTest(error=error), patch.object(self.manage, "urlopen", side_effect=error) as download, self.assertRaises(OSError):
                self.manage.download_installer()
            download.assert_called_once()
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"<html>error</html>"
        with patch.object(self.manage, "urlopen", return_value=response) as download, self.assertRaises(ValueError):
            self.manage.download_installer()
        download.assert_called_once()

    def test_update_only_restores_previously_active_monitor(self):
        for active in (True, False):
            response = MagicMock()
            response.__enter__.return_value.read.return_value = b"#!/usr/bin/env bash\nexit 0\n"
            with self.subTest(active=active), patch.object(self.manage, "urlopen", return_value=response), patch.object(self.manage, "backup", return_value=self.directory / "backup.tar.gz"), patch.object(self.manage.subprocess, "run", return_value=SimpleNamespace(returncode=0 if active else 3)), patch.object(self.manage, "run") as run:
                self.manage.update()
                calls = [call.args for call in run.call_args_list]
                self.assertEqual(("systemctl", "start", self.manage.MONITOR) in calls, active)

    def test_install_failure_does_not_restart_monitor(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"#!/usr/bin/env bash\nexit 1\n"

        def fail_install(*args):
            if args[0] == "bash" and len(args) == 2:
                raise subprocess.CalledProcessError(1, args)

        with patch.object(self.manage, "urlopen", return_value=response), patch.object(self.manage, "backup", return_value=self.directory / "backup.tar.gz"), patch.object(self.manage.subprocess, "run", return_value=SimpleNamespace(returncode=0)), patch.object(self.manage, "run", side_effect=fail_install) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                self.manage.update()
            self.assertNotIn(("systemctl", "start", self.manage.MONITOR), [call.args for call in run.call_args_list])


if __name__ == "__main__":
    unittest.main()
