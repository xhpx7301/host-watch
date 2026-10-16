"""Exercise node removal in an isolated filesystem with fake service/user tools."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_egress_install import bash_path


@unittest.skipUnless(bash_path(), "需要 Bash 执行隔离卸载测试")
class EgressUninstallerTests(unittest.TestCase):
    def uninstall(self, distro, fail_stop=False, symlink=False):
        source = Path(__file__).with_name("uninstall-egress.sh").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory)
            targets = ["/opt/hostwatch-egress", "/etc/hostwatch-egress.json", "/etc/hostwatch-egress.env",
                       "/etc/systemd/system/hostwatch-egress-agent.service", "/etc/init.d/hostwatch-egress-agent",
                       "/var/log/hostwatch-egress.log", "/run/hostwatch-egress-agent.pid",
                       "/etc/runlevels/default/hostwatch-egress-agent", "/etc/os-release", "/etc/group"]
            for target in targets:
                path = sandbox / target.lstrip("/")
                path.parent.mkdir(parents=True, exist_ok=True)
                if target == "/opt/hostwatch-egress":
                    path.mkdir()
                    (path / "egress_agent.py").write_text("node program")
                else:
                    path.write_text("node file")
                source = source.replace(target, path.as_posix())
            (sandbox / "etc/os-release").write_text(f"ID={distro}\n")
            (sandbox / "etc/group").write_text("hostwatch-egress:x:999:\n")
            (sandbox / "user").touch()
            # Shared dependencies and the main service must survive removal.
            preserved = [sandbox / "opt/vmiss-monitor/monitor.py", sandbox / "usr/bin/python3",
                         sandbox / "etc/vmiss-monitor.env", sandbox / "var/lib/vmiss-monitor/config.json"]
            for path in preserved:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("preserve me")
            if symlink:
                node_dir = sandbox / "opt/hostwatch-egress"
                (node_dir / "egress_agent.py").unlink()
                node_dir.rmdir()
                try:
                    node_dir.symlink_to(sandbox / "opt/vmiss-monitor", target_is_directory=True)
                except OSError:
                    self.skipTest("当前系统不能创建符号链接")
            script = sandbox / "uninstall.sh"
            script.write_bytes(source.encode("utf-8"))
            shell = r'''
set -eu
record() { printf '%s\n' "$*" >> "$TEST_ROOT/actions"; }
id() { if [ "${1:-}" = -u ]; then echo 0; else test -f "$TEST_ROOT/user"; fi; }
systemctl() { record "systemctl $*"; if [ "$1" = stop ] && [ "$TEST_FAIL_STOP" = 1 ]; then return 1; fi; }
rc-service() { record "rc-service $*"; [ "$TEST_FAIL_STOP" != 1 ]; }
rc-update() { record "rc-update $*"; rm -f "$TEST_ROOT/etc/runlevels/default/hostwatch-egress-agent"; }
userdel() { record "userdel $*"; rm "$TEST_ROOT/user"; }
deluser() { record "deluser $*"; rm "$TEST_ROOT/user"; }
groupdel() { record "groupdel $*"; : > "$TEST_ROOT/etc/group"; }
delgroup() { record "delgroup $*"; : > "$TEST_ROOT/etc/group"; }
source "$TEST_SCRIPT"
source "$TEST_SCRIPT"
'''
            env = {**os.environ, "TEST_ROOT": sandbox.as_posix(), "TEST_SCRIPT": script.as_posix(),
                   "TEST_FAIL_STOP": str(int(fail_stop))}
            result = subprocess.run([bash_path(), "-c", shell], cwd=directory, env=env,
                                    capture_output=True, text=True, encoding="utf-8", timeout=15)
            self.assertEqual(result.returncode == 0, not fail_stop, result.stdout + result.stderr)
            for path in preserved:
                self.assertEqual(path.read_text(), "preserve me")
            for target in targets[:7]:
                self.assertEqual((sandbox / target.lstrip("/")).exists(), fail_stop, target)
            self.assertEqual((sandbox / "user").exists(), fail_stop)
            return (sandbox / "actions").read_text(), result.stdout

    def test_debian_stops_disables_and_removes_node_idempotently(self):
        actions, output = self.uninstall("debian")
        self.assertEqual(actions.count("systemctl stop hostwatch-egress-agent.service"), 1)
        self.assertEqual(actions.count("systemctl disable hostwatch-egress-agent.service"), 1)
        self.assertEqual(actions.count("userdel hostwatch-egress"), 1)
        self.assertEqual(actions.count("groupdel hostwatch-egress"), 1)
        self.assertIn("systemctl daemon-reload", actions)
        self.assertEqual(output.count("节点已卸载"), 2)

    def test_alpine_stops_removes_autostart_and_node_idempotently(self):
        actions, _ = self.uninstall("alpine")
        self.assertEqual(actions.count("rc-service --ifstarted hostwatch-egress-agent stop"), 1)
        self.assertEqual(actions.count("rc-update del hostwatch-egress-agent default"), 1)
        self.assertEqual(actions.count("deluser hostwatch-egress"), 1)
        self.assertEqual(actions.count("delgroup hostwatch-egress"), 1)
        self.assertNotIn("systemctl", actions)

    def test_stop_failure_preserves_installation(self):
        for distro in ["debian", "alpine"]:
            with self.subTest(distro=distro):
                actions, _ = self.uninstall(distro, fail_stop=True)
                self.assertNotIn("userdel", actions)
                self.assertNotIn("deluser", actions)

    def test_node_directory_symlink_does_not_delete_target(self):
        self.uninstall("debian", symlink=True)


if __name__ == "__main__":
    unittest.main()
