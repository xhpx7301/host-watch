"""Beijing-time helpers for persisted and user-visible timestamps."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


BEIJING_TZ = timezone(timedelta(hours=8), name="CST")


def now_beijing() -> datetime:
    return datetime.now(BEIJING_TZ)


def beijing_logging_converter(timestamp: float):
    """Return a logging-compatible time tuple in Beijing time."""
    return datetime.fromtimestamp(timestamp, timezone.utc).astimezone(BEIJING_TZ).timetuple()


def beijing_iso(value: datetime | None = None, *, timespec: str = "seconds") -> str:
    current = value or now_beijing()
    if current.tzinfo is None:
        current = current.replace(tzinfo=BEIJING_TZ)
    return current.astimezone(BEIJING_TZ).isoformat(timespec=timespec)


def format_beijing(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return raw
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            if raw.endswith(" CST"):
                parsed = datetime.strptime(raw[:-4], "%Y-%m-%d %H:%M:%S").replace(tzinfo=BEIJING_TZ)
            elif raw.endswith(" UTC"):
                parsed = datetime.strptime(raw[:-4], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            else:
                return raw
        except ValueError:
            return raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING_TZ)
    return parsed.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S 北京时间")
