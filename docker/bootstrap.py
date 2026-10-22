"""Initialize the persistent Docker volume exactly once."""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import sys
import tempfile

# When this file is launched as ``python docker/bootstrap.py``, Python puts
# ``/app/docker`` on sys.path instead of the application root.  Add the root
# explicitly so the bootstrap can import the same modules as the services.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from auth import password_record


DATA = Path(os.getenv("HOSTWATCH_DATA_DIR", "/data"))
USERS = Path(os.getenv("ADMIN_USERS_FILE", str(DATA / "stockwatch-users.json")))
CONFIG = DATA / "config.json"
SOURCE_CONFIG = Path(os.getenv("HOSTWATCH_SOURCE_CONFIG", "/app/config.example.json"))
INITIAL_ADMIN_USERNAME = os.getenv("HOSTWATCH_INITIAL_ADMIN_USERNAME", "pingzi").strip() or "pingzi"
INITIAL_ADMIN_PASSWORD = os.getenv("HOSTWATCH_INITIAL_ADMIN_PASSWORD", "pingzi")


def write_json(path: Path, value: dict, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            if hasattr(os, "fchmod"):
                os.fchmod(stream.fileno(), mode)
            else:
                os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    lock_path = DATA / ".bootstrap.lock"
    lock_path.touch(exist_ok=True)
    try:
        import fcntl
    except ImportError:  # Development smoke tests may run on Windows.
        fcntl = None
    with lock_path.open("r+") as lock:
        if fcntl:
            fcntl.flock(lock, fcntl.LOCK_EX)
        if not CONFIG.exists():
            config = json.loads(SOURCE_CONFIG.read_text(encoding="utf-8"))
            target = os.getenv("HOSTWATCH_FLARESOLVERR_URL", "").strip()
            if target:
                config.setdefault("fetcher", {})["flaresolverr_url"] = target
            write_json(CONFIG, config)
        else:
            # Migrate the stock native default when an existing config is
            # first started in Compose; FlareSolverr is addressed by service
            # name on the private Compose network.
            try:
                config = json.loads(CONFIG.read_text(encoding="utf-8"))
                target = os.getenv("HOSTWATCH_FLARESOLVERR_URL", "").strip()
                current = config.get("fetcher", {}).get("flaresolverr_url", "")
                if target and current in {"http://127.0.0.1:8191/v1", "http://localhost:8191/v1"}:
                    config.setdefault("fetcher", {})["flaresolverr_url"] = target
                    write_json(CONFIG, config)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
        if not USERS.exists():
            password = INITIAL_ADMIN_PASSWORD or secrets.token_urlsafe(24)
            # The requested Docker bootstrap credential is intentionally the
            # short, well-known ``pingzi`` pair.  Normal password creation and
            # changes continue to enforce the 12-character minimum.
            write_json(USERS, {"version": 1, "users": {INITIAL_ADMIN_USERNAME: password_record(password, allow_short=True)}})
            print(f"HostWatch Docker 初始管理员：用户名：{INITIAL_ADMIN_USERNAME}", flush=True)
            print(f"HostWatch Docker 初始密码（仅首次显示）：{password}", flush=True)


if __name__ == "__main__":
    main()
