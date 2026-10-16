"""Exercise dependency deployment with fake system commands; no Docker/root needed."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


class InstalledRuntimeTests(unittest.TestCase):
    def test_installed_modules_import_without_repository_files(self):
        root = Path(__file__).resolve().parent
        installer = (root / "install.sh").read_text(encoding="utf-8")
        block = re.search(
            r'^install -o root -g root -m 0644 \\\n(.*?)"\$INSTALL_DIR/"',
            installer, re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(block, "Cannot find application installation command")
        filenames = re.findall(r'"\$SOURCE_DIR/([^"\n]+)"', block.group(1))
        self.assertIn("admin.py", filenames)
        self.assertIn("monitor.py", filenames)
        with tempfile.TemporaryDirectory() as directory:
            for filename in filenames:
                shutil.copy2(root / filename, Path(directory) / filename)
            modules = [Path(name).stem for name in filenames if name.endswith(".py")
                       and (name != "manage.py" or sys.platform == "linux")]
            # Isolated mode excludes PYTHONPATH and the checkout; only deployed
            # files and installed third-party dependencies can satisfy imports.
            result = subprocess.run(
                [sys.executable, "-I", "-c",
                 "import importlib, pathlib, sys; "
                 "root = pathlib.Path(sys.argv[1]); sys.path.insert(0, str(root)); "
                 "modules = [importlib.import_module(name) for name in sys.argv[2:]]; "
                 "assert all(pathlib.Path(module.__file__).parent == root for module in modules)",
                 directory, *modules],
                cwd=directory, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


@unittest.skipUnless(sys.platform == "linux", "安装器使用 Bash")
class InstallerTests(unittest.TestCase):
    def deploy(self, scenario, enabled="1"):
        with tempfile.TemporaryDirectory() as directory:
            script = r'''
set -Eeuo pipefail
log() { :; }
fail() { printf '%s\n' "$*" >&2; exit 1; }
record() { printf '%s\n' "$*" >> "$TEMP_DIR/actions"; }
apt-get() { record "apt $*"; }
command() {
  if [[ "$SCENARIO" == no-docker && "$*" == '-v docker' ]]; then return 1; fi
  builtin command "$@"
}
systemctl() { record "systemctl $*"; }
sleep() { :; }
docker() {
  record "docker $*"
  case "$1 ${2:-}" in
    'container inspect') [[ "$SCENARIO" == existing || "$SCENARIO" == foreign ]]; return $?;;
    'inspect --format') [[ "$SCENARIO" == foreign ]] && echo false || echo true;;
    'pull ghcr.io/flaresolverr/flaresolverr:latest') [[ "$SCENARIO" != pull-failed ]]; return $?;;
    'run -d'|'start host-watch-flaresolverr') touch "$TEMP_DIR/started";;
  esac
  return 0
}
curl() {
  local target
  while (($#)); do
    if [[ "$1" == '-o' ]]; then target="$2"; shift; fi
    shift
  done
  if [[ "$SCENARIO" == ready || -f "$TEMP_DIR/started" && "$SCENARIO" != timeout ]]; then
    printf '{"msg":"FlareSolverr is ready!","version":"test"}' > "$target"
  else
    return 1
  fi
}
source "$HELPER"
deploy_flaresolverr
'''
            env = {**os.environ, "TEMP_DIR": directory, "SCENARIO": scenario, "HOSTWATCH_INSTALL_FLARESOLVERR": enabled, "HELPER": str(Path(__file__).with_name("install-flaresolverr.sh"))}
            result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=10)
            log = Path(directory, "actions")
            return result, log.read_text() if log.exists() else ""

    def test_new_install_creates_loopback_only_container_with_restart_and_healthcheck(self):
        result, actions = self.deploy("new")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("docker pull ghcr.io/flaresolverr/flaresolverr:latest", actions)
        self.assertIn("--restart unless-stopped", actions)
        self.assertIn("-p 127.0.0.1:8191:8191", actions)

    def test_existing_ready_service_is_not_changed(self):
        result, actions = self.deploy("ready")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actions, "")

    def test_missing_docker_is_installed(self):
        result, actions = self.deploy("no-docker")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("apt install -y docker.io", actions)

    def test_owned_stopped_container_is_started_without_recreation(self):
        result, actions = self.deploy("existing")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("docker start host-watch-flaresolverr", actions)
        self.assertNotIn("docker run", actions)
        self.assertNotIn("docker pull", actions)

    def test_conflicting_container_is_not_modified(self):
        result, actions = self.deploy("foreign")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("docker start", actions)
        self.assertNotIn("docker run", actions)

    def test_pull_failure_does_not_create_container(self):
        result, actions = self.deploy("pull-failed")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("docker run", actions)

    def test_readiness_timeout_reports_failure(self):
        result, _ = self.deploy("timeout")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("初始化超时", result.stderr)

    def test_explicit_opt_out_does_not_touch_docker(self):
        result, actions = self.deploy("new", "0")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(actions, "")


if __name__ == "__main__":
    unittest.main()
