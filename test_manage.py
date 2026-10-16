import base64
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
        lines = output.call_args.args[0].splitlines()
        self.assertIn("  FlareSolverr 运行中", lines)
        self.assertIn("  监控 运行中", lines)
        self.assertIn("  管理页 已停止", lines)
        self.assertTrue(any(line.startswith("  cloudscraper ") for line in lines))

    def test_cloudscraper_status_reports_library_availability(self):
        interpreter = self.directory / ".venv" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.write_text("")

        def fake_run(args, **kwargs):
            self.assertEqual(args[1:], ["-c", "import cloudscraper; print(getattr(cloudscraper, '__version__', 'installed'))"])
            return SimpleNamespace(returncode=0, stdout="1.2.71\n")

        with patch.object(self.manage, "INSTALL_DIR", self.directory), patch.object(self.manage.subprocess, "run", side_effect=fake_run):
            self.assertEqual(self.manage._cloudscraper_state(), "installed")

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

    def test_web_server_restart_uses_delayed_systemd_reboot(self):
        with patch.object(self.manage, "run") as run:
            result = self.manage.web_action({"action": "server-restart"})
        run.assert_called_once_with("systemd-run", "--quiet", "--on-active=5s", "/usr/bin/systemctl", "reboot")
        self.assertIn("5 秒", result["message"])

    def test_browser_backup_is_removed_after_reading_archive(self):
        archive = self.directory / "host-watch-20261016-120000-abcd.tar.gz"
        archive.write_bytes(b"backup bytes")
        with patch.object(self.manage, "backup", return_value=archive):
            result = self.manage.backup_for_download()
        self.assertEqual(base64.b64decode(result["content"]), b"backup bytes")
        self.assertFalse(archive.exists())

    def test_backup_schedule_rejects_invalid_time_or_enabled_value(self):
        with patch.object(self.manage, "run") as run:
            for value in ({"enabled": "true", "time": "03:00"}, {"enabled": True, "time": "24:00"},
                          {"enabled": True, "time": "03:60"}, {"enabled": True, "time": "3:00"},
                          {"enabled": True, "time": "03:00\nExecStart=/bin/sh"}, {}, []):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    self.manage.save_backup_schedule(value)
            run.assert_not_called()

    def test_backup_schedule_writes_daily_timer_and_preserves_on_reapply(self):
        settings_path = self.directory / "schedule.json"
        systemd = self.directory / "units"
        systemd.mkdir()
        with patch.object(self.manage, "BACKUP_SETTINGS", settings_path), patch.object(self.manage, "SYSTEMD_DIR", systemd), patch.object(self.manage, "run") as run, patch.object(self.manage, "backup_schedule_status", return_value={"enabled": True, "time": "04:25"}):
            self.assertEqual(self.manage.read_backup_schedule(), {"enabled": False, "time": "03:00"})
            result = self.manage.web_action({"action": "backup-schedule", "enabled": True, "time": "04:25"})
            self.assertTrue(result["ok"])
            self.assertEqual(json.loads(settings_path.read_text()), {"enabled": True, "time": "04:25"})
            self.assertEqual(settings_path.stat().st_mode & 0o777, 0o600)
            timer = (systemd / self.manage.BACKUP_TIMER).read_text()
            self.assertIn("OnCalendar=*-*-* 04:25:00", timer)
            self.assertIn("Asia/Shanghai", timer)
            self.assertIn("Persistent=true", timer)
            self.assertIn(("systemctl", "restart", self.manage.BACKUP_TIMER), [call.args for call in run.call_args_list])
            self.assertIn("manage.py scheduled-backup", (systemd / self.manage.BACKUP_SERVICE).read_text())
            # The installer reapplies this file, without resetting the chosen time.
            self.manage.apply_backup_schedule(self.manage.read_backup_schedule())
            self.assertEqual((systemd / self.manage.BACKUP_TIMER).read_text(), timer)
            self.manage.save_backup_schedule({"enabled": False, "time": "05:40"})
            self.assertIn(("systemctl", "disable", "--now", self.manage.BACKUP_TIMER), [call.args for call in run.call_args_list])

    def test_backup_schedule_failure_restores_saved_settings_and_timer(self):
        settings_path = self.directory / "schedule.json"
        settings_path.write_text(json.dumps({"enabled": True, "time": "02:15"}))
        systemd = self.directory / "units"
        systemd.mkdir()
        failures = []
        def failing_restart(*args):
            if args == ("systemctl", "restart", self.manage.BACKUP_TIMER) and not failures:
                failures.append(True)
                raise subprocess.CalledProcessError(1, args)
        with patch.object(self.manage, "BACKUP_SETTINGS", settings_path), patch.object(self.manage, "SYSTEMD_DIR", systemd), patch.object(self.manage, "run", side_effect=failing_restart):
            with self.assertRaisesRegex(ValueError, "已恢复原计划"):
                self.manage.save_backup_schedule({"enabled": True, "time": "06:30"})
            self.assertEqual(json.loads(settings_path.read_text()), {"enabled": True, "time": "02:15"})
            self.assertIn("OnCalendar=*-*-* 02:15:00", (systemd / self.manage.BACKUP_TIMER).read_text())

    def test_backup_schedule_status_reports_next_run_and_timezone(self):
        def fake_run(args, **kwargs):
            if args[0] == "timedatectl":
                return SimpleNamespace(returncode=0, stdout="Asia/Singapore\n")
            if self.manage.BACKUP_TIMER in args:
                return SimpleNamespace(returncode=0, stdout="ActiveState=active\nUnitFileState=enabled\nNextElapseUSecRealtime=Sat 2026-10-17 04:25:00 +08\nLastTriggerUSec=Fri 2026-10-16 04:25:00 +08\n")
            return SimpleNamespace(returncode=0, stdout="Result=success\nActiveState=inactive\n")
        with patch.object(self.manage, "read_backup_schedule", return_value={"enabled": True, "time": "04:25"}), patch.object(self.manage.subprocess, "run", side_effect=fake_run):
            result = self.manage.backup_schedule_status()
        self.assertEqual(result["timezone"], "Asia/Shanghai (UTC+08:00)")
        self.assertEqual(result["next_run"], "2026-10-17 04:25:00 北京时间")
        self.assertEqual(result["last_run"], "2026-10-16 04:25:00 北京时间")
        self.assertEqual(result["last_result"], "success")

    def test_backup_schedule_status_converts_utc_and_cst_timestamps(self):
        self.assertEqual(self.manage._systemd_time_beijing("Sat 2026-10-17 04:25:00 UTC"), "2026-10-17 12:25:00 北京时间")
        self.assertEqual(self.manage._systemd_time_beijing("Sat 2026-10-17 04:25:00 CST"), "2026-10-17 04:25:00 北京时间")

    def test_corrupt_backup_schedule_does_not_break_management_status(self):
        with patch.object(self.manage, "read_backup_schedule", side_effect=ValueError("invalid settings")), patch.object(self.manage.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="")):
            result = self.manage.backup_schedule_status()
        self.assertIn("读取备份计划失败", result["error"])

    def test_installer_reapplies_schedule_without_deadlocking_parent_update_lock(self):
        (self.directory / "admin.py").write_text("")
        with patch.object(self.manage, "INSTALL_DIR", self.directory), patch.object(self.manage.os, "geteuid", return_value=0), patch.object(self.manage.Path, "read_text", return_value='ID=debian\nVERSION_ID="12"\n'), patch.object(self.manage, "read_backup_schedule", return_value={"enabled": True, "time": "04:25"}), patch.object(self.manage, "apply_backup_schedule") as apply, patch.object(self.manage.os, "open", side_effect=AssertionError("must not reacquire parent lock")), patch.object(sys, "argv", ["manage.py", "backup-schedule-apply"]):
            self.assertEqual(self.manage.main(), 0)
            apply.assert_called_once_with({"enabled": True, "time": "04:25"})

    def test_scheduled_worker_waits_for_manager_and_obeys_saved_enable_state(self):
        (self.directory / "admin.py").write_text("")
        original_open = self.manage.os.open
        def open_lock(path, *args):
            self.assertEqual(path, "/run/stockwatch-manager.lock")
            return original_open(self.directory / "manager.lock", *args)
        for enabled in (True, False):
            with self.subTest(enabled=enabled), patch.object(self.manage, "INSTALL_DIR", self.directory), patch.object(self.manage.os, "geteuid", return_value=0), patch.object(self.manage.Path, "read_text", return_value='ID=debian\nVERSION_ID="12"\n'), patch.object(self.manage, "read_backup_schedule", return_value={"enabled": enabled, "time": "04:25"}), patch.object(self.manage, "backup") as backup, patch.object(self.manage.os, "open", side_effect=open_lock), patch.object(self.manage.fcntl, "flock") as flock, patch.object(sys, "argv", ["manage.py", "scheduled-backup"]):
                self.assertEqual(self.manage.main(), 0)
                self.assertEqual(flock.call_args.args[1], self.manage.fcntl.LOCK_EX)
                self.assertEqual(backup.call_count, int(enabled))

    def test_real_backup_archive_contains_schedule_and_uses_tempfile_name(self):
        import tarfile
        settings_path = self.directory / "schedule.json"
        settings_path.write_text('{"enabled":true,"time":"04:25"}')
        systemd = self.directory / "units"
        systemd.mkdir()
        (systemd / self.manage.BACKUP_TIMER).write_text("timer")
        install = self.directory / "app"
        install.mkdir()
        data = self.directory / "data"
        data.mkdir()
        with patch.object(self.manage, "BACKUP_SETTINGS", settings_path), patch.object(self.manage, "SYSTEMD_DIR", systemd), patch.object(self.manage, "BACKUP_DIR", self.directory / "backups"), patch.object(self.manage, "INSTALL_DIR", install), patch.object(self.manage, "DATA_DIR", data), patch.object(self.manage, "ADMIN_ENV", self.directory / "absent"), patch.object(self.manage, "DEFAULT_USERS_PATH", self.directory / "no-users"):
            archive = self.manage.backup()
            self.assertRegex(archive.name, r"host-watch-[0-9]{8}-[0-9]{6}-[a-z0-9_]{8}\.tar\.gz")
            with tarfile.open(archive) as contents:
                self.assertIn(str(settings_path).lstrip("/"), contents.getnames())
                self.assertIn(str(systemd / self.manage.BACKUP_TIMER).lstrip("/"), contents.getnames())
            self.assertEqual(archive.stat().st_mode & 0o777, 0o600)

    def test_web_backup_download_returns_one_time_archive(self):
        with patch.object(self.manage, "backup_for_download", return_value={"name": "backup.tar.gz", "content": "Yg=="}):
            result = self.manage.web_action({"action": "backup-download"})
        self.assertEqual(result["content"], "Yg==")

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
