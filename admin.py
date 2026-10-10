from __future__ import annotations

import base64
import hmac
import json
import os
import re
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

import requests


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.getenv("VMISS_CONFIG", str(ROOT / "config.json")))
STATUS_PATH = Path(os.getenv("VMISS_STATUS", str(CONFIG_PATH.parent / "status.json")))
PAGE_PATH = ROOT / "admin.html"
MAX_BODY = 64 * 1024


def default_config() -> dict:
    return {
        "enabled": False,
        "interval_seconds": 600,
        "notify_current_stock_on_start": False,
        "affiliate_id": "",
        "telegram": {"bot_token": "", "chat_id": "", "disabled": False},
        "products": [],
    }


def read_json(path: Path, default: dict) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else default
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return default


def normalize_telegram(value: object) -> dict:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("Telegram 配置必须是对象")
    token = str(value.get("bot_token", "")).strip()
    chat_id = str(value.get("chat_id", "")).strip()
    disabled = value.get("disabled", False)
    if not isinstance(disabled, bool):
        raise ValueError("Telegram disabled 必须是布尔值")
    if len(token) > 256 or any(char.isspace() for char in token):
        raise ValueError("Telegram bot token 格式无效")
    if chat_id and (len(chat_id) > 128 or not re.fullmatch(r"(?:-?\d{1,32}|@[A-Za-z0-9_]{5,64})", chat_id)):
        raise ValueError("Telegram chat_id 格式无效")
    return {"bot_token": token, "chat_id": chat_id, "disabled": disabled}


def effective_telegram(value: object) -> tuple[str, str]:
    if not isinstance(value, dict):
        value = {}
    telegram = normalize_telegram(value.get("telegram", {}))
    if telegram["disabled"]:
        return "", ""
    token = telegram["bot_token"] or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = telegram["chat_id"] or os.getenv("TELEGRAM_CHAT_ID", "").strip()
    return token, chat_id


def public_config(value: dict) -> dict:
    normalized = validate_config(value)
    token, chat_id = effective_telegram(normalized)
    stored_chat_id = normalized["telegram"]["chat_id"]
    if not normalized["telegram"]["disabled"] and not stored_chat_id:
        stored_chat_id = chat_id
    normalized["telegram"] = {
        "configured": bool(token and chat_id),
        "bot_token_configured": bool(token),
        "chat_id": stored_chat_id,
        "disabled": normalized["telegram"]["disabled"],
    }
    return normalized


def allowed_hosts() -> set[str]:
    raw = os.getenv("ADMIN_ALLOWED_HOSTS", "").strip() or "127.0.0.1,localhost"
    return {item.strip().casefold() for item in raw.split(",") if item.strip()}


def validate_config(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("配置必须是 JSON 对象")
    interval_value = value.get("interval_seconds", 600)
    if isinstance(interval_value, bool):
        raise ValueError("检查间隔必须是整数") from None
    try:
        interval = int(interval_value)
    except (ValueError, TypeError):
        raise ValueError("检查间隔必须是整数") from None
    if str(interval) != str(interval_value).strip():
        raise ValueError("检查间隔必须是整数")
    if interval < 300 or interval > 86400:
        raise ValueError("检查间隔范围是 300 到 86400 秒")

    affiliate_id = str(value.get("affiliate_id", "")).strip()
    if affiliate_id and not re.fullmatch(r"\d{1,20}", affiliate_id):
        raise ValueError("推广 ID 格式无效")

    for key in ("enabled", "notify_current_stock_on_start"):
        if key in value and not isinstance(value[key], bool):
            raise ValueError(f"{key} 必须是布尔值")

    products = value.get("products", [])
    if not isinstance(products, list) or len(products) > 100:
        raise ValueError("产品列表无效或超过 100 项")
    normalized = []
    seen_ids = set()
    for item in products:
        if not isinstance(item, dict):
            raise ValueError("产品配置必须是对象")
        product_id = str(item.get("id", "")).strip()
        name = str(item.get("name", "")).strip()
        category_url = str(item.get("category_url", "")).strip()
        selector = str(item.get("selector", "")).strip()
        gid = str(item.get("gid", "")).strip()
        pid = str(item.get("pid", "")).strip()
        if "enabled" in item and not isinstance(item["enabled"], bool):
            raise ValueError(f"产品 {name or product_id} 的启用状态必须是布尔值")
        parsed = urlsplit(category_url)
        if not product_id or product_id in seen_ids:
            raise ValueError("每个产品都需要唯一 ID")
        if not name or not selector:
            raise ValueError(f"产品 {name or product_id} 需要填写名称和商品标识")
        if parsed.scheme != "https" or parsed.hostname != "app.vmiss.com" or parsed.query or parsed.fragment or not parsed.path.startswith("/store/"):
            raise ValueError(f"产品 {name} 的分类页必须是干净的 app.vmiss.com HTTPS URL")
        if bool(gid) == bool(pid):
            raise ValueError(f"产品 {name} 必须且只能填写 gid 或 pid")
        target_id = pid or gid
        if not target_id.isdigit():
            raise ValueError(f"产品 {name} 的 gid/pid 必须是数字")
        if len(name) > 120 or len(selector) > 200 or len(category_url) > 2048 or len(target_id) > 20:
            raise ValueError(f"产品 {name} 的字段长度超限")
        seen_ids.add(product_id)
        normalized.append({
            "id": product_id,
            "name": name,
            "category_url": category_url,
            "selector": selector,
            "gid": gid,
            "pid": pid,
            "enabled": item.get("enabled", False),
        })
    enabled = value.get("enabled", False)
    if enabled and not any(item["enabled"] for item in normalized):
        raise ValueError("启用监控前至少启用一个产品")
    if enabled and not affiliate_id:
        raise ValueError("启用监控前请填写推广 ID（aff）")
    telegram = normalize_telegram(value.get("telegram", {}))
    return {
        "enabled": enabled,
        "interval_seconds": interval,
        "notify_current_stock_on_start": value.get("notify_current_stock_on_start", False),
        "affiliate_id": affiliate_id,
        "telegram": telegram,
        "products": normalized,
    }


def save_config(value: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="config-", suffix=".tmp", dir=CONFIG_PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, CONFIG_PATH)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


class TelegramAPIError(ValueError):
    pass


def telegram_call(token: str, method: str, **payload) -> dict:
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/{method}",
            json=payload,
            timeout=(10, 25),
        )
        result = response.json()
    except (requests.RequestException, ValueError):
        raise TelegramAPIError(f"Telegram {method} 请求失败，请检查服务器网络和 bot 配置") from None
    if not isinstance(result, dict):
        raise TelegramAPIError(f"Telegram {method} 返回了无法识别的响应")
    if not response.ok or not result.get("ok"):
        description = str(result.get("description", ""))
        if token:
            description = description.replace(token, "[已隐藏]")
        if method == "getUpdates" and "webhook" in description.casefold():
            raise TelegramAPIError("读取最近聊天失败：此 bot 已配置 webhook，Telegram 不允许同时使用 getUpdates。请关闭 webhook，或直接手动填写 Chat ID。")
        if description:
            raise TelegramAPIError(f"Telegram {method} 失败：{description[:240]}")
        raise TelegramAPIError(f"Telegram {method} 失败，请检查 bot 配置")
    return result


def telegram_chats(token: str) -> list[dict]:
    result = telegram_call(token, "getUpdates", allowed_updates=["message", "channel_post"])
    chats: dict[str, dict] = {}
    updates = result.get("result", [])
    if not isinstance(updates, list):
        raise TelegramAPIError("Telegram getUpdates 返回了无法识别的聊天列表")
    for update in updates:
        if not isinstance(update, dict):
            continue
        for key in ("message", "channel_post"):
            message = update.get(key, {})
            if not isinstance(message, dict):
                continue
            chat = message.get("chat", {})
            if not isinstance(chat, dict):
                continue
            if chat and "id" in chat:
                chat_id = str(chat["id"])
                chats[chat_id] = {
                    "id": chat_id,
                    "type": str(chat.get("type", "unknown")),
                    "name": str(chat.get("title") or chat.get("username") or chat.get("first_name") or "(未命名)"),
                }
    return list(chats.values())


def current_config() -> dict:
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else default_config()
    return validate_config(raw)


def telegram_credentials(token_override: str = "", chat_id_override: str = "") -> tuple[str, str]:
    config = current_config()
    telegram = config["telegram"]
    token = token_override.strip() or telegram["bot_token"] or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = chat_id_override.strip() or telegram["chat_id"] or os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if len(token) > 256 or any(char.isspace() for char in token):
        raise ValueError("Telegram bot token 格式无效")
    if chat_id and (len(chat_id) > 128 or not re.fullmatch(r"(?:-?\d{1,32}|@[A-Za-z0-9_]{5,64})", chat_id)):
        raise ValueError("Telegram chat_id 格式无效")
    return token, chat_id


def request_body(handler: BaseHTTPRequestHandler) -> dict:
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        raise ValueError("Content-Length 无效") from None
    if length <= 0 or length > MAX_BODY:
        raise ValueError("请求体大小无效")
    value = json.loads(handler.rfile.read(length))
    if not isinstance(value, dict):
        raise ValueError("请求体必须是 JSON 对象")
    return value


class Handler(BaseHTTPRequestHandler):
    server_version = "VMISSMonitorAdmin/1.0"

    def _secure(self) -> bool:
        try:
            if not ip_address(self.client_address[0]).is_loopback:
                self.send_error(403)
                return False
        except ValueError:
            self.send_error(403)
            return False
        host_header = self.headers.get("Host", "")
        try:
            host = urlsplit(f"//{host_header}").hostname or ""
        except ValueError:
            host = ""
        hosts = allowed_hosts()
        if host.casefold() not in hosts:
            self.send_error(403)
            return False
        return True

    def _authorized(self) -> bool:
        password = os.getenv("ADMIN_PASSWORD", "")
        if not password:
            return True
        username = os.getenv("ADMIN_USERNAME", "admin")
        expected = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        supplied = self.headers.get("Authorization", "")
        if supplied.startswith("Basic ") and hmac.compare_digest(supplied[6:], expected):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="VMISS monitor admin", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _guard(self) -> bool:
        return self._secure() and self._authorized()

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if not self._guard():
            return
        if self.path == "/":
            self._send(200, "text/html; charset=utf-8", PAGE_PATH.read_bytes())
        elif self.path == "/admin.js":
            self._send(200, "text/javascript; charset=utf-8", (ROOT / "admin.js").read_bytes())
        elif self.path == "/api/config":
            try:
                raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else default_config()
                data = json.dumps(public_config(raw), ensure_ascii=False).encode("utf-8")
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self._send(500, "application/json; charset=utf-8", json.dumps({"error": f"配置文件无法读取：{exc}"}, ensure_ascii=False).encode("utf-8"))
                return
            self._send(200, "application/json; charset=utf-8", data)
        elif self.path == "/api/status":
            data = json.dumps(read_json(STATUS_PATH, {"state": "unknown", "products": []}), ensure_ascii=False).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", data)
        elif self.path == "/api/telegram/chats":
            try:
                token, _ = telegram_credentials()
                if not token:
                    raise ValueError("请先在管理页保存 Telegram bot token")
                chats = telegram_chats(token)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                status = 502 if isinstance(exc, TelegramAPIError) else 400
                self._send(status, "application/json; charset=utf-8", json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"))
                return
            self._send(200, "application/json; charset=utf-8", json.dumps({"ok": True, "chats": chats}, ensure_ascii=False).encode("utf-8"))
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if not self._guard():
            return
        if self.path not in {"/api/config", "/api/telegram/test"}:
            self.send_error(404)
            return
        try:
            submitted = request_body(self)
            if self.path == "/api/telegram/test":
                token, chat_id = telegram_credentials(
                    str(submitted.get("bot_token", "")),
                    str(submitted.get("chat_id", "")),
                )
                if not token or not chat_id:
                    raise ValueError("请填写 Telegram bot token 和 chat ID")
                telegram_call(token, "sendMessage", chat_id=chat_id, text="✅ VMISS Telegram 配置测试成功")
                self._send(200, "application/json; charset=utf-8", b'{"ok":true}')
                return

            current_raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else default_config()
            current = validate_config(current_raw)
            submitted_telegram = submitted.get("telegram", {})
            if not isinstance(submitted_telegram, dict):
                raise ValueError("Telegram 配置必须是对象")
            submitted_token = str(submitted_telegram.get("bot_token", "")).strip()
            submitted_chat_id = str(submitted_telegram.get("chat_id", "")).strip()
            clear_telegram = submitted_telegram.get("clear", False)
            if not isinstance(clear_telegram, bool):
                raise ValueError("Telegram clear 必须是布尔值")
            submitted_disabled = submitted_telegram.get("disabled", current["telegram"]["disabled"])
            if not isinstance(submitted_disabled, bool):
                raise ValueError("Telegram disabled 必须是布尔值")
            if clear_telegram:
                submitted["telegram"] = {"bot_token": "", "chat_id": "", "disabled": True}
            else:
                current_token = current["telegram"]["bot_token"] or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
                current_chat_id = current["telegram"]["chat_id"] or os.getenv("TELEGRAM_CHAT_ID", "").strip()
                submitted["telegram"] = {
                    "bot_token": submitted_token or current_token,
                    "chat_id": submitted_chat_id or current_chat_id,
                    "disabled": submitted_disabled,
                }
            value = validate_config(submitted)
            save_config(value)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            status = 502 if isinstance(exc, TelegramAPIError) else (413 if "请求体大小" in str(exc) else 400)
            self._send(status, "application/json; charset=utf-8", json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"))
            return
        self._send(200, "application/json; charset=utf-8", json.dumps({"ok": True, "config": public_config(value)}, ensure_ascii=False).encode("utf-8"))

    def log_message(self, format: str, *args) -> None:
        print("%s - %s" % (self.address_string(), format % args))


def main() -> None:
    host = os.getenv("ADMIN_HOST", "127.0.0.1")
    if host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("管理页只允许监听 localhost")
    hosts = allowed_hosts()
    if "*" in hosts:
        raise SystemExit("ADMIN_ALLOWED_HOSTS 不允许使用通配符；请填写精确域名")
    if any(item not in {"127.0.0.1", "localhost"} for item in hosts) and not os.getenv("ADMIN_PASSWORD", ""):
        raise SystemExit("允许域名访问时必须设置 ADMIN_PASSWORD")
    port = int(os.getenv("ADMIN_PORT", "8765"))
    print(f"VMISS monitor admin listening on http://{host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
