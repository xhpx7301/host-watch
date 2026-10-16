import base64
import gzip
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


@unittest.skipUnless(sys.platform == "linux", "恢复使用 Linux 的权限及服务接口")
class BackupRestoreTests(unittest.TestCase):
    def setUp(self):
        import manage
        self.manage = manage
        self.validator = manage.validate_restore_config
        self.backup_impl = manage.backup
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.settings = self.root / "schedule.json"
        self.config = self.data / "config.json"
        self.config.write_text('{"enabled":true,"products":[{"name":"current"}]}')
        self.state = self.data / "state.json"
        self.state.write_text('{"current":"record"}')
        self.settings.write_text('{"enabled":false,"time":"03:00"}')
        for mocked in [patch.object(manage, "DATA_DIR", self.data), patch.object(manage, "BACKUP_SETTINGS", self.settings),
                       patch.object(manage, "_systemd_state", return_value="active"),
                       patch.object(manage.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=os.getuid())),
                       patch.object(manage.grp, "getgrnam", return_value=SimpleNamespace(gr_gid=os.getgid()))]:
            mocked.start()
            self.addCleanup(mocked.stop)
        for attr, mocked in [("run", patch.object(manage, "run")),
                             ("backup", patch.object(manage, "backup", return_value=self.root / "before.tar.gz")),
                             ("validate", patch.object(manage, "validate_restore_config", side_effect=lambda config: {**config, "enabled": False}))]:
            setattr(self, attr, mocked.start())
            self.addCleanup(mocked.stop)

    def archive(self, entries=None):
        if entries is None:
            entries = [(str(self.config).lstrip("/"), {"enabled": True, "products": [{"name": "restored"}]}),
                       (str(self.state).lstrip("/"), {"restored": "record"}),
                       (str(self.settings).lstrip("/"), {"enabled": True, "time": "04:25"}),
                       ("etc/stockwatch-users.json", {"users": {"old": {}}}),
                       ("opt/vmiss-monitor/admin.py", "do not install or execute")]
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, value in entries:
                body = value if isinstance(value, bytes) else json.dumps(value).encode()
                member = tarfile.TarInfo(name)
                member.size = len(body)
                archive.addfile(member, io.BytesIO(body))
        return base64.b64encode(buffer.getvalue()).decode()

    def test_import_restores_data_and_schedule_but_never_code_or_accounts(self):
        users = self.root / "users.json"
        users.write_text("keep current admin")
        with patch.object(self.manage, "save_backup_schedule") as schedule:
            result = self.manage.web_action({"action": "backup-import", "content": self.archive()})
        self.assertTrue(result["ok"])
        self.assertEqual(json.loads(self.config.read_text())["products"][0]["name"], "restored")
        self.assertFalse(json.loads(self.config.read_text())["enabled"])
        self.assertEqual(json.loads(self.state.read_text()), {"restored": "record"})
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads((self.data / "status.json").read_text())["state"], "paused")
        self.assertEqual(users.read_text(), "keep current admin")
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["data", "schedule.json", "users.json"])
        self.run.assert_any_call("systemctl", "stop", self.manage.MONITOR)
        self.run.assert_any_call("systemctl", "disable", self.manage.MONITOR)
        self.assertFalse(any(call.args[1] in {"start", "restart"} for call in self.run.call_args_list))
        self.backup.assert_called_once()
        schedule.assert_called_once_with({"enabled": True, "time": "04:25"})

    def test_missing_schedule_keeps_current_plan_and_clears_stale_caches(self):
        catalog = self.data / "catalog.json"
        catalog.write_text('{"stale":true}')
        content = self.archive([(str(self.config).lstrip("/"), {"products": []})])
        with patch.object(self.manage, "save_backup_schedule") as schedule:
            self.manage.restore_backup({"content": content})
        schedule.assert_not_called()
        self.assertEqual(json.loads(catalog.read_text()), {})
        self.assertEqual(json.loads(self.settings.read_text())["time"], "03:00")

    def test_real_exported_archive_can_be_imported(self):
        install = self.root / "app"
        install.mkdir()
        (install / "admin.py").write_text("original program")
        with patch.object(self.manage, "INSTALL_DIR", install), patch.object(self.manage, "BACKUP_DIR", self.root / "backups"), patch.object(self.manage, "ADMIN_ENV", self.root / "no-env"), patch.object(self.manage, "DEFAULT_USERS_PATH", self.root / "no-users"):
            archive = self.backup_impl()
        content = base64.b64encode(archive.read_bytes()).decode()
        self.config.write_text('{"products":[]}')
        self.state.write_text("{}")
        with patch.object(self.manage, "save_backup_schedule"):
            self.manage.restore_backup({"content": content})
        self.assertEqual(json.loads(self.config.read_text())["products"][0]["name"], "current")
        self.assertEqual(json.loads(self.state.read_text()), {"current": "record"})
        self.assertFalse(json.loads(self.config.read_text())["enabled"])
        self.assertEqual((install / "admin.py").read_text(), "original program")

    def test_invalid_archives_never_stop_service_or_change_files(self):
        config_name = str(self.config).lstrip("/")
        invalid = ["not base64", base64.b64encode(b"bad tar").decode(), self.archive([]),
                   self.archive([("../../etc/passwd", {})]), self.archive([("/etc/passwd", {})]),
                   self.archive([(config_name, {}), (config_name, {})]),
                   self.archive([(config_name, b"{broken")]), self.archive([(config_name, [])]),
                   self.archive([(config_name, {}), (str(self.settings).lstrip("/"), {"enabled": True, "time": "25:00"})])]
        for content in invalid:
            with self.subTest(content=content[:40]), self.assertRaises(ValueError):
                self.manage.restore_backup({"content": content})
        self.assertTrue(json.loads(self.config.read_text())["enabled"])
        self.run.assert_not_called()
        self.backup.assert_not_called()

    def test_rejects_link_and_oversized_members(self):
        for kind in [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.REGTYPE]:
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
                member = tarfile.TarInfo(str(self.config).lstrip("/"))
                member.type = kind
                member.linkname = "/etc/passwd"
                if kind == tarfile.REGTYPE:
                    member.size = 65 * 1024 * 1024
                archive.addfile(member)
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.manage.restore_backup({"content": base64.b64encode(buffer.getvalue()).decode()})
        self.run.assert_not_called()

    def test_corrupt_gzip_checksum_and_excessive_decompression_are_rejected(self):
        valid = bytearray(base64.b64decode(self.archive()))
        valid[-8] ^= 1
        with self.assertRaisesRegex(ValueError, "损坏"):
            self.manage.restore_backup({"content": base64.b64encode(valid).decode()})
        with patch.object(self.manage, "MAX_BACKUP_EXPANDED", 1024), self.assertRaisesRegex(ValueError, "解压内容过大"):
            self.manage.restore_backup({"content": base64.b64encode(gzip.compress(b"\0" * 4096)).decode()})
        self.run.assert_not_called()
        self.backup.assert_not_called()

    def test_rejects_target_symlink(self):
        self.state.unlink()
        self.state.symlink_to(self.config)
        with self.assertRaisesRegex(ValueError, "非普通文件"):
            self.manage.restore_backup({"content": self.archive()})
        self.backup.assert_not_called()
        self.run.assert_not_called()

    def test_invalid_config_is_rejected_before_changes(self):
        self.validate.side_effect = ValueError("invalid config")
        with self.assertRaisesRegex(ValueError, "invalid config"):
            self.manage.restore_backup({"content": self.archive()})
        self.run.assert_not_called()
        self.backup.assert_not_called()

    def test_apply_failure_rolls_back_files_schedule_and_running_service(self):
        config_before, state_before = self.config.read_text(), self.state.read_text()
        with patch.object(self.manage, "save_backup_schedule", side_effect=ValueError("timer failed")), patch.object(self.manage, "apply_backup_schedule") as apply:
            with self.assertRaisesRegex(ValueError, "已保留或恢复原数据"):
                self.manage.restore_backup({"content": self.archive()})
        self.assertEqual(self.config.read_text(), config_before)
        self.assertEqual(self.state.read_text(), state_before)
        self.assertFalse((self.data / "status.json").exists())
        apply.assert_called_once_with({"enabled": False, "time": "03:00"})
        self.run.assert_any_call("systemctl", "start", self.manage.MONITOR)

    def test_backup_failure_keeps_original_files_and_restarts_monitor(self):
        self.backup.side_effect = OSError("disk full")
        with self.assertRaisesRegex(ValueError, "disk full"):
            self.manage.restore_backup({"content": self.archive()})
        self.assertTrue(json.loads(self.config.read_text())["enabled"])
        self.run.assert_any_call("systemctl", "start", self.manage.MONITOR)

    def test_validator_uses_current_installed_app_and_disables_monitor(self):
        with patch.object(self.manage.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout='{"enabled":false}')) as run:
            result = self.validator({"enabled": True})
        self.assertFalse(result["enabled"])
        self.assertFalse(json.loads(run.call_args.kwargs["input"])["enabled"])
        self.assertIn("-I", run.call_args.args[0])

    def test_web_worker_accepts_large_import_without_expanding_other_actions(self):
        (self.root / "admin.py").touch()
        real_open, real_read = self.manage.os.open, Path.read_text
        def open_lock(path, flags, mode):
            self.assertEqual(path, "/run/stockwatch-manager.lock")
            return real_open(self.root / "manager.lock", flags, mode)
        def read_release(path, *args, **kwargs):
            return 'ID=debian\nVERSION_ID="12"\n' if str(path) == "/etc/os-release" else real_read(path, *args, **kwargs)
        for action, expected in [("backup-import", 0), ("status", 1)]:
            payload = {"action": action, "content": "a" * 80_000}
            with self.subTest(action=action), patch.object(self.manage, "INSTALL_DIR", self.root), patch.object(self.manage.os, "geteuid", return_value=0), patch.object(Path, "read_text", read_release), patch.object(self.manage.os, "open", side_effect=open_lock), patch.object(sys, "argv", ["manage.py", "web-action"]), patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), patch.object(self.manage, "web_action", return_value={"ok": True}) as worker:
                self.assertEqual(self.manage.main(), expected)
                if expected == 0:
                    worker.assert_called_once_with(payload)
                else:
                    worker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
