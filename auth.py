"""Shared file-based administrator authentication (standard library only)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path


ITERATIONS = 600_000
# Container deployments provide HOSTWATCH_USERS_PATH/ADMIN_USERS_FILE so
# credentials live in a persistent volume instead of the host's /etc.
DEFAULT_USERS_PATH = Path(os.getenv("HOSTWATCH_USERS_PATH", "/etc/stockwatch-users.json"))
_AUTH_CACHE_TTL = 90.0
_AUTH_CACHE_LIMIT = 128
_AUTH_CACHE_KEY = secrets.token_bytes(32)
_AUTH_CACHE_LOCK = threading.Lock()
_AUTH_CACHE = {}


def _credential_cache_key(username: str, password: str, source: str = "") -> str:
    return hmac.new(_AUTH_CACHE_KEY, f"{source}\0{username}\0{password}".encode("utf-8"), hashlib.sha256).hexdigest()


def _cached_credentials(username: str, password: str, source: str = ""):
    key = _credential_cache_key(username, password, source)
    now = time.monotonic()
    with _AUTH_CACHE_LOCK:
        expires = _AUTH_CACHE.get(key)
        if expires and expires > now:
            return True
        if expires:
            _AUTH_CACHE.pop(key, None)
    return None


def _cache_credentials(username: str, password: str, source: str = "") -> None:
    key = _credential_cache_key(username, password, source)
    now = time.monotonic()
    with _AUTH_CACHE_LOCK:
        if len(_AUTH_CACHE) >= _AUTH_CACHE_LIMIT:
            oldest = min(_AUTH_CACHE, key=_AUTH_CACHE.get)
            _AUTH_CACHE.pop(oldest, None)
        _AUTH_CACHE[key] = now + _AUTH_CACHE_TTL


def validate_username(username: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", username):
        raise ValueError("用户名需为 1–64 个英文字母、数字或 _ . @ -")
    return username


def password_record(password: str) -> dict:
    if not 12 <= len(password) <= 256 or any(ord(c) < 32 for c in password):
        raise ValueError("密码需为 12–256 个字符，不能包含控制字符")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), ITERATIONS)
    return {"salt": salt, "iterations": ITERATIONS, "hash": digest.hex()}


def load_users(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("账号文件版本无效")
    users = payload.get("users")
    if not isinstance(users, dict) or not 1 <= len(users) <= 50:
        raise ValueError("账号文件必须包含 1–50 个账号")
    for username, record in users.items():
        validate_username(username)
        if not isinstance(record, dict):
            raise ValueError("账号记录无效")
        if not re.fullmatch(r"[0-9a-f]{32}", str(record.get("salt", ""))):
            raise ValueError("账号 salt 无效")
        if not re.fullmatch(r"[0-9a-f]{64}", str(record.get("hash", ""))):
            raise ValueError("账号 hash 无效")
        iterations = record.get("iterations")
        if type(iterations) is not int or not 100_000 <= iterations <= 1_000_000:
            raise ValueError("账号哈希迭代次数无效")
    return users


def users_path() -> Path | None:
    configured = os.getenv("ADMIN_USERS_FILE", "").strip()
    if configured:
        return Path(configured)
    # Existing deployments automatically adopt the menu-created account file.
    if DEFAULT_USERS_PATH.exists():
        return DEFAULT_USERS_PATH
    return None


def authentication_required() -> bool:
    return users_path() is not None or bool(os.getenv("ADMIN_PASSWORD", ""))


def credentials_authorized(username: str, password: str) -> bool:
    """Validate a username/password pair without requiring an HTTP header."""
    path = users_path()
    if path is None and not os.getenv("ADMIN_PASSWORD", ""):
        return True  # Preserve password-free local SSH-tunnel installations.
    try:
        source = f"{path}:{path.stat().st_mtime_ns}:{path.stat().st_size}" if path is not None else "legacy"
    except OSError:
        source = "missing"
    cached = _cached_credentials(username, password, source)
    if cached is True:
        return True
    try:
        if len(username) > 64 or len(password) > 256:
            return False
        if path is not None:
            users = load_users(path)
            record = users.get(username)
            if record is None:
                return False
            digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(record["salt"]), record["iterations"])
            valid = hmac.compare_digest(digest.hex(), record["hash"])
        else:
            expected = f"{os.getenv('ADMIN_USERNAME', 'admin')}:{os.getenv('ADMIN_PASSWORD', '')}"
            supplied = f"{username}:{password}"
            valid = hmac.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8"))
        if valid:
            _cache_credentials(username, password, source)
        return valid
    except (OSError, ValueError, TypeError, KeyError):
        return False


def authorized(header: str) -> bool:
    path = users_path()
    if path is None and not os.getenv("ADMIN_PASSWORD", ""):
        return True  # Preserve password-free local SSH-tunnel installations.
    try:
        scheme, encoded = header.split(" ", 1)
        if scheme.casefold() != "basic" or len(encoded) > 2048:
            return False
        username, password = base64.b64decode(encoded, validate=True).decode("utf-8").split(":", 1)
        if len(username) > 64 or len(password) > 256:
            return False
        return credentials_authorized(username, password)
    except (UnicodeError, ValueError, TypeError):
        # A missing/unreadable configured account file must never remove auth.
        return False
