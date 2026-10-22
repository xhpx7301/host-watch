"""Reset or create the Docker management account without touching other users."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile

# Compose launches this file as ``python docker/reset_admin.py``.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from auth import load_users, password_record, validate_username


DATA = Path(os.getenv("HOSTWATCH_DATA_DIR", "/data"))
USERS = Path(os.getenv("ADMIN_USERS_FILE", str(DATA / "stockwatch-users.json")))
USERNAME = os.getenv("HOSTWATCH_INITIAL_ADMIN_USERNAME", "pingzi").strip() or "pingzi"
PASSWORD = os.getenv("HOSTWATCH_INITIAL_ADMIN_PASSWORD", "pingzi") or "pingzi"


def save_users(users: dict) -> None:
    USERS.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{USERS.name}-", dir=USERS.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"version": 1, "users": users}, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            if hasattr(os, "fchmod"):
                os.fchmod(stream.fileno(), 0o600)
            else:
                os.chmod(temporary, 0o600)
        os.replace(temporary, USERS)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> None:
    username = validate_username(USERNAME)
    try:
        users = load_users(USERS) if USERS.exists() else {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"管理员账号文件无效：{exc}") from None
    if username not in users and len(users) >= 50:
        raise SystemExit("管理员账号已达到 50 个，无法添加 pingzi")
    users[username] = password_record(PASSWORD, allow_short=True)
    save_users(users)
    print(f"Docker 管理员已重置：用户名：{username}，密码：{PASSWORD}")


if __name__ == "__main__":
    main()
