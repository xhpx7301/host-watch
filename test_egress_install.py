"""Run both installer branches with fake OS commands and a real local agent."""
import base64
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest


def bash_path():
    if sys.platform == "win32":
        git = shutil.which("git")
        candidate = Path(git).parents[1] / "bin" / "bash.exe" if git else Path("missing")
        return str(candidate) if candidate.is_file() else None
    return shutil.which("bash")


@unittest.skipUnless(bash_path(), "需要 Bash 执行隔离安装测试")
class EgressInstallerTests(unittest.TestCase):
    def install(self, distro):
        root = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory)
            release = sandbox / "os-release"
            release.write_text(f"ID={distro}\n", encoding="utf-8")
            text = (root / "install-egress.sh").read_text(encoding="utf-8")
            # Redirect filesystem targets and OS detection into this sandbox;
            # package/user/service commands below never affect the host.
            text = text.replace("/etc/os-release", release.as_posix())
            for target in ["/opt/hostwatch-egress", "/etc/hostwatch-egress.json", "/etc/hostwatch-egress.env",
                           "/etc/init.d/hostwatch-egress-agent", "/etc/systemd/system/hostwatch-egress-agent.service",
                           "/var/log/hostwatch-egress.log"]:
                path = sandbox / target.lstrip("/")
                path.parent.mkdir(parents=True, exist_ok=True)
                text = text.replace(target, path.as_posix())
            installer = sandbox / "install.sh"
            installer.write_bytes(text.encode())
            archive = sandbox / "source.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                for filename in ["egress_agent.py", "hostwatch-egress-agent.service", "hostwatch-egress-agent.openrc", "uninstall-egress.sh"]:
                    body = (root / filename).read_text(encoding="utf-8").encode()
                    info = tarfile.TarInfo("host-watch-main/" + filename)
                    info.size = len(body)
                    tar.addfile(info, io.BytesIO(body))
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            settings = {"EGRESS_NODE_ID": "installed-node", "EGRESS_NODE_NAME": "默认出口",
                        "EGRESS_TOKEN": "installation-test-token", "EGRESS_LISTEN_PORT": port}
            encoded = base64.b64encode(json.dumps(settings).encode()).decode()
            shell = r'''
set -eu
record() { printf '%s\n' "$*" >> "$TEST_ROOT/actions"; }
id() { if [ "${1:-}" = -u ]; then echo 0; else test -f "$TEST_ROOT/user"; fi; }
getent() { return 0; }
grep() { return 1; }
apt-get() { record "apt-get $*"; }
apk() { record "apk $*"; }
groupadd() { record "groupadd $*"; }
addgroup() { record "addgroup $*"; }
useradd() { record "useradd $*"; touch "$TEST_ROOT/user"; }
adduser() { record "adduser $*"; touch "$TEST_ROOT/user"; }
curl() { for argument do target=$argument; done; cp "$TEST_ARCHIVE" "$target"; }
install() {
  record "install $*"
  local -a args=()
  while [ "$#" -gt 0 ]; do
    case "$1" in -o|-g) shift 2;; *) args+=("$1"); shift;; esac
  done
  command install "${args[@]}"
}
python3() { "$TEST_PYTHON" "$@"; }
systemctl() { record "systemctl $*"; if [ "${1:-}" = restart ]; then touch "$TEST_ROOT/start"; fi; }
rc-update() { record "rc-update $*"; }
rc-service() { record "rc-service $*"; if [ "${2:-}" = restart ]; then touch "$TEST_ROOT/start"; fi; }
set -- "$TEST_PAYLOAD"
# Source twice to verify that rerunning the command updates the same install.
source "$TEST_INSTALLER"
source "$TEST_INSTALLER"
'''
            processes, failures = [], []
            stop = threading.Event()

            def launch():
                try:
                    deadline = time.monotonic() + 15
                    while not (sandbox / "start").exists():
                        if stop.wait(0.02) or time.monotonic() >= deadline:
                            return
                    env = {key: value for key, value in os.environ.items() if not key.startswith("EGRESS_")}
                    env.update(EGRESS_CONFIG=str(sandbox / "etc/hostwatch-egress.json"), EGRESS_PUBLIC_IP="203.0.113.20")
                    processes.append(subprocess.Popen([sys.executable, str(sandbox / "opt/hostwatch-egress/egress_agent.py")],
                                                      env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                except Exception as exc:
                    failures.append(exc)

            thread = threading.Thread(target=launch, daemon=True)
            thread.start()
            env = {**os.environ, "TEST_ROOT": sandbox.as_posix(), "TEST_PYTHON": Path(sys.executable).as_posix(),
                   "TEST_ARCHIVE": archive.as_posix(), "TEST_PAYLOAD": encoded, "TEST_INSTALLER": installer.as_posix(),
                   "PYTHONIOENCODING": "utf-8"}
            try:
                result = subprocess.run([bash_path(), "-c", shell], cwd=directory, env=env,
                                        capture_output=True, text=True, encoding="utf-8", timeout=45)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(failures)
                self.assertEqual((sandbox / "opt/hostwatch-egress/uninstall.sh").read_text(encoding="utf-8"),
                                 (root / "uninstall-egress.sh").read_text(encoding="utf-8"))
                installed = json.loads((sandbox / "etc/hostwatch-egress.json").read_text(encoding="utf-8"))
                self.assertEqual(installed["EGRESS_NODE_ID"], "installed-node")
                self.assertEqual(installed["EGRESS_NODE_NAME"], settings["EGRESS_NODE_NAME"])
                self.assertEqual(installed["EGRESS_LISTEN_PORT"], port)
                self.assertIn('"public_ip": "203.0.113.20"', result.stdout)
                self.assertIn('"token": "installation-test-token"', result.stdout)
                return (sandbox / "actions").read_text(), sandbox.joinpath("etc/hostwatch-egress.env").read_text()
            finally:
                stop.set()
                thread.join(2)
                for process in processes:
                    process.terminate()
                    process.wait(timeout=5)

    def test_debian_installs_packages_service_and_ready_agent(self):
        actions, env = self.install("debian")
        self.assertIn("python3-requests", actions)
        self.assertEqual(actions.count("systemctl restart hostwatch-egress-agent.service"), 2)
        self.assertTrue(env.strip().replace("\\", "/").endswith("/etc/hostwatch-egress.json"))

    def test_alpine_installs_packages_openrc_and_ready_agent(self):
        actions, _ = self.install("alpine")
        self.assertIn("apk add --no-cache ca-certificates curl python3 py3-requests openrc coreutils", actions)
        self.assertEqual(actions.count("rc-update add hostwatch-egress-agent default"), 2)
        self.assertEqual(actions.count("rc-service hostwatch-egress-agent restart"), 2)
        self.assertEqual(actions.count("adduser -S -D -H"), 1)


if __name__ == "__main__":
    unittest.main()
