"""Run the daily backup schedule without requiring host systemd."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time

from manage import backup, read_backup_schedule
from time_utils import now_beijing


DATA = Path(os.getenv("HOSTWATCH_DATA_DIR", "/data"))
RUNTIME = DATA / "backup-runtime.json"
LOCK = Path(os.getenv("HOSTWATCH_MANAGER_LOCK", str(DATA / ".manager.lock")))


def publish(**values: object) -> None:
    current = {}
    try:
        current = json.loads(RUNTIME.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    current.update(values)
    temporary = RUNTIME.with_suffix(".tmp")
    temporary.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
    temporary.replace(RUNTIME)


def main() -> None:
    last_key = ""
    while True:
        now = now_beijing()
        try:
            schedule = read_backup_schedule()
            key = f"{now.date()} {schedule['time']}"
            if schedule["enabled"] and now.strftime("%H:%M") == schedule["time"] and key != last_key:
                last_key = key
                publish(backup_active="activating", last_run="", last_result="running")
                try:
                    LOCK.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        import fcntl
                    except ImportError:  # Development smoke tests may run on Windows.
                        fcntl = None
                    with LOCK.open("a+") as lock:
                        if fcntl:
                            fcntl.flock(lock, fcntl.LOCK_EX)
                        path = backup()
                    publish(backup_active="inactive", last_run=now.isoformat(), last_result="success", last_backup=str(path))
                except Exception as exc:  # Keep the worker alive for tomorrow.
                    publish(backup_active="inactive", last_run=now.isoformat(), last_result=f"failed: {exc}")
            if schedule["enabled"]:
                next_run = now.replace(hour=int(schedule["time"][:2]), minute=int(schedule["time"][3:]), second=0, microsecond=0)
                if next_run <= now:
                    from datetime import timedelta
                    next_run += timedelta(days=1)
                publish(next_run=next_run.isoformat())
        except Exception as exc:
            publish(backup_active="inactive", last_result=f"failed: {exc}")
        time.sleep(20)


if __name__ == "__main__":
    main()
