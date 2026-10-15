from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

import requests

from auth import DEFAULT_USERS_PATH, authentication_required, authorized, users_path


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.getenv("VMISS_CONFIG", str(ROOT / "config.json")))
STATUS_PATH = Path(os.getenv("VMISS_STATUS", str(CONFIG_PATH.parent / "status.json")))
PAGE_PATH = ROOT / "admin.html"
MAX_BODY = 64 * 1024
MANAGER = Path("/opt/vmiss-monitor/manage.py")
MUTATION_LOCK = threading.Lock()


def default_config() -> dict:
    return {
        "enabled": False,
        "interval_seconds": 600,
        "notify_current_stock_on_start": False,
        "affiliate_id": "",
        "fetcher": {
            "mode": "requests",
            "flaresolverr_url": "http://127.0.0.1:8191/v1",
            "timeout_seconds": 60,
            "wait_seconds": 0,
        },
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


def normalize_fetcher(value: object) -> dict:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("请求方式配置必须是对象")
    mode = str(value.get("mode", "requests")).strip().casefold()
    if mode not in {"requests", "flaresolverr"}:
        raise ValueError("请求方式必须是 requests 或 flaresolverr")
    endpoint = str(value.get("flaresolverr_url", "http://127.0.0.1:8191/v1")).strip()
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or len(endpoint) > 2048
    ):
        raise ValueError("FlareSolverr API 地址必须是没有查询参数的 HTTP(S) URL")
    timeout_value = value.get("timeout_seconds", 60)
    if isinstance(timeout_value, bool):
        raise ValueError("FlareSolverr 超时必须是整数") from None
    try:
        timeout_seconds = int(timeout_value)
    except (ValueError, TypeError):
        raise ValueError("FlareSolverr 超时必须是整数") from None
    if str(timeout_seconds) != str(timeout_value).strip():
        raise ValueError("FlareSolverr 超时必须是整数")
    if timeout_seconds < 10 or timeout_seconds > 180:
        raise ValueError("FlareSolverr 超时范围是 10 到 180 秒")
    wait = value.get("wait_seconds", 0)
    if isinstance(wait, bool) or not isinstance(wait, int) or not 0 <= wait <= 30:
        raise ValueError("网页渲染等待必须是 0 到 30 秒的整数")
    return {
        "mode": mode,
        "flaresolverr_url": endpoint.rstrip("/"),
        "timeout_seconds": timeout_seconds,
        "wait_seconds": wait,
    }


def flaresolverr_health(endpoint: str) -> dict:
    """Read readiness without launching a browser or visiting a product website."""
    try:
        response = requests.get(endpoint.removesuffix("/v1") + "/", timeout=(3, 5), allow_redirects=False)
        payload = response.json()
    except (requests.RequestException, ValueError):
        raise ValueError("无法连接 FlareSolverr 或响应无效，请检查服务日志和 API 地址") from None
    if not response.ok or not isinstance(payload, dict) or "flaresolverr" not in str(payload.get("msg", "")).casefold():
        raise ValueError("该地址未返回 FlareSolverr 就绪信息，请检查服务是否启动")
    return {"ok": True, "version": str(payload.get("version", "未知"))[:80], "message": "FlareSolverr 服务已就绪；此测试不会访问商品网站，不代表目标网页一定能通过验证。"}


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
    if interval < 0 or interval > 86400:
        raise ValueError("检查间隔必须是 0 到 86400 秒的整数；0 表示本轮完成后立即检查下一轮")

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
    vmiss_products = False
    for item in products:
        if not isinstance(item, dict):
            raise ValueError("产品配置必须是对象")
        product_id = str(item.get("id", "")).strip()
        name = str(item.get("name", "")).strip()
        adapter = str(item.get("adapter", "vmiss" if any(key in item for key in ("category_url", "selector", "gid", "pid")) else "html")).strip().casefold()
        provider = str(item.get("provider", {"vmiss": "VMISS", "vps_soy": "VPS.SOY", "akile": "Akile"}.get(adapter, "自定义网站"))).strip()
        gid = str(item.get("gid", "")).strip()
        pid = str(item.get("pid", "")).strip()
        if "enabled" in item and not isinstance(item["enabled"], bool):
            raise ValueError(f"产品 {name or product_id} 的启用状态必须是布尔值")
        if not product_id or product_id in seen_ids:
            raise ValueError("每个产品都需要唯一 ID")
        if adapter not in {"vmiss", "html", "vps_soy", "akile"}:
            raise ValueError(f"产品 {name or product_id} 的适配器必须是 vmiss、vps_soy、akile 或 html")
        if not name:
            raise ValueError(f"产品 {product_id} 需要填写商品名称")
        if not provider or len(provider) > 80:
            raise ValueError(f"产品 {name} 的厂商名称必填且不能超过 80 个字符")

        if adapter == "vmiss":
            vmiss_products = True
            category_url = str(item.get("category_url", "")).strip()
            selector = str(item.get("selector", "")).strip()
            parsed = urlsplit(category_url)
            if not selector:
                raise ValueError(f"产品 {name} 需要填写商品选择标识")
            if parsed.scheme != "https" or parsed.hostname != "app.vmiss.com" or parsed.query or parsed.fragment or not parsed.path.startswith("/store/"):
                raise ValueError(f"产品 {name} 的分类页必须是干净的 app.vmiss.com HTTPS URL")
            if bool(gid) == bool(pid):
                raise ValueError(f"产品 {name} 必须且只能填写 gid 或 pid")
            target_id = pid or gid
            if not target_id.isdigit():
                raise ValueError(f"产品 {name} 的 gid/pid 必须是数字")
            if len(selector) > 200 or len(category_url) > 2048 or len(target_id) > 20:
                raise ValueError(f"产品 {name} 的字段长度超限")
            normalized_item = {
                "id": product_id,
                "name": name,
                "provider": provider,
                "adapter": adapter,
                "category_url": category_url,
                "selector": selector,
                "gid": gid,
                "pid": pid,
                "enabled": item.get("enabled", False),
            }
        elif adapter == "vps_soy":
            catalog_url = str(item.get("url", "https://www.vps.soy/cart")).strip()
            parsed = urlsplit(catalog_url)
            if parsed.scheme != "https" or parsed.hostname != "www.vps.soy" or parsed.path.rstrip("/") != "/cart" or parsed.username or parsed.password or parsed.fragment or parsed.port not in {None, 443} or len(catalog_url) > 2048:
                raise ValueError(f"产品 {name} 的 VPS.SOY 商品页必须是 https://www.vps.soy/cart，可带分类查询参数")
            selector = str(item.get("selector", name)).strip()
            if not selector or len(selector) > 200:
                raise ValueError(f"产品 {name} 需要填写 VPS.SOY 套餐完整名称（最多 200 个字符）")
            normalized_item = {"id": product_id, "name": name, "provider": provider, "adapter": adapter, "url": catalog_url, "selector": selector, "enabled": item.get("enabled", False)}
        elif adapter == "akile":
            area_id = str(item.get("area_id", "6")).strip()
            node_name = str(item.get("node_name", "HKL-TW")).strip()
            selector = str(item.get("selector", name)).strip()
            if not re.fullmatch(r"[1-9][0-9]{0,9}", area_id):
                raise ValueError(f"产品 {name} 的 Akile 地区 ID 必须是正整数（最多 10 位）")
            if not node_name or not selector or max(len(node_name), len(selector), len(name)) > 200:
                raise ValueError(f"产品 {name} 需要填写 Akile 线路及套餐完整名称（最多 200 个字符）")
            normalized_item = {"id": product_id, "name": name, "provider": provider, "adapter": adapter, "area_id": area_id, "node_name": node_name, "selector": selector, "enabled": item.get("enabled", False)}
        else:
            check_url = str(item.get("url", item.get("category_url", ""))).strip()
            parsed = urlsplit(check_url)
            host = (parsed.hostname or "").casefold()
            if parsed.scheme != "https" or not host or parsed.username or parsed.password or parsed.fragment:
                raise ValueError(f"产品 {name} 的监控网址必须是有效的 HTTPS URL，不能包含账号或片段")
            if host == "localhost" or host.endswith((".localhost", ".local")):
                raise ValueError(f"产品 {name} 不能监控本机或 .local 地址")
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                address = None
            if address is not None and not address.is_global:
                raise ValueError(f"产品 {name} 不能监控内网或保留 IP 地址")
            stock_selector = str(item.get("stock_selector", "")).strip()
            stock_mode = str(item.get("stock_mode", "quantity")).strip().casefold()
            stock_pattern = str(item.get("stock_pattern", r"([\d,]+)")).strip()
            available_pattern = str(item.get("available_pattern", r"(?i)in stock|available|有货|现货|库存充足")).strip()
            unavailable_pattern = str(item.get("unavailable_pattern", r"(?i)out of stock|unavailable|sold out|缺货|售罄|无货")).strip()
            purchase_url = str(item.get("purchase_url", "")).strip()
            if not stock_selector or len(stock_selector) > 512:
                raise ValueError(f"产品 {name} 需要填写 CSS 库存选择器（最多 512 个字符）")
            if stock_mode not in {"quantity", "availability"}:
                raise ValueError(f"产品 {name} 的库存识别方式必须是 quantity 或 availability")
            if stock_mode == "quantity":
                try:
                    compiled = re.compile(stock_pattern, re.IGNORECASE)
                except re.error as exc:
                    raise ValueError(f"产品 {name} 的数量正则无效：{exc}") from None
                if compiled.groups < 1:
                    raise ValueError(f"产品 {name} 的数量正则必须包含一个捕获组，例如 (\\d+)")
            else:
                for label, pattern in (("有货", available_pattern), ("缺货", unavailable_pattern)):
                    try:
                        re.compile(pattern, re.IGNORECASE)
                    except re.error as exc:
                        raise ValueError(f"产品 {name} 的{label}正则无效：{exc}") from None
            if purchase_url:
                purchase = urlsplit(purchase_url)
                if purchase.scheme != "https" or not purchase.hostname or purchase.username or purchase.password or purchase.fragment:
                    raise ValueError(f"产品 {name} 的购买链接必须是有效 HTTPS URL，不能包含账号或片段")
            if len(name) > 120 or len(check_url) > 2048 or len(stock_pattern) > 500 or len(available_pattern) > 500 or len(unavailable_pattern) > 500 or len(purchase_url) > 2048:
                raise ValueError(f"产品 {name} 的字段长度超限")
            normalized_item = {
                "id": product_id,
                "name": name,
                "provider": provider,
                "adapter": adapter,
                "url": check_url,
                "stock_selector": stock_selector,
                "stock_mode": stock_mode,
                "stock_pattern": stock_pattern,
                "available_pattern": available_pattern,
                "unavailable_pattern": unavailable_pattern,
                "purchase_url": purchase_url,
                "enabled": item.get("enabled", False),
            }
        request_mode = str(item.get("request_mode", "inherit")).strip().casefold()
        if request_mode not in {"inherit", "requests", "flaresolverr"}:
            raise ValueError(f"产品 {name} 的请求方式无效")
        normalized_item["request_mode"] = request_mode
        seen_ids.add(product_id)
        normalized.append(normalized_item)
    enabled = value.get("enabled", False)
    if enabled and not any(item["enabled"] for item in normalized):
        raise ValueError("启用监控前至少启用一个产品")
    if enabled and vmiss_products and not affiliate_id:
        raise ValueError("启用监控前请填写推广 ID（aff）")
    fetcher = normalize_fetcher(value.get("fetcher", {}))
    telegram = normalize_telegram(value.get("telegram", {}))
    return {
        "enabled": enabled,
        "interval_seconds": interval,
        "notify_current_stock_on_start": value.get("notify_current_stock_on_start", False),
        "affiliate_id": affiliate_id,
        "fetcher": fetcher,
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


def manager_action(value: dict) -> dict:
    try:
        result = subprocess.run(
            ["/usr/bin/sudo", "-n", "/usr/bin/python3", str(MANAGER), "web-action"],
            input=json.dumps(value, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("服务器管理命令不可用或超时，请更新 HostWatch 安装后重试") from None
    lines = result.stdout.splitlines()
    try:
        response = json.loads(lines[-1]) if lines else {}
    except json.JSONDecodeError:
        response = {}
    if result.returncode or not isinstance(response, dict) or not response:
        message = response.get("error") if isinstance(response, dict) else None
        detail = message or result.stderr.strip() or "操作失败，请检查安装版本和系统权限"
        raise ValueError(detail[-1000:])
    return response


class Handler(BaseHTTPRequestHandler):
    server_version = "HostWatchAdmin/1.0"

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
        if authorized(self.headers.get("Authorization", "")):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="HostWatch admin", charset="UTF-8"')
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
        elif self.path == "/api/manage":
            try:
                data = json.dumps(manager_action({"action": "status"}), ensure_ascii=False).encode("utf-8")
            except ValueError as exc:
                self._send(503, "application/json; charset=utf-8", json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"))
                return
            self._send(200, "application/json; charset=utf-8", data)
        elif self.path == "/api/manage/logs":
            try:
                data = json.dumps(manager_action({"action": "logs"}), ensure_ascii=False).encode("utf-8")
            except ValueError as exc:
                self._send(503, "application/json; charset=utf-8", json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"))
                return
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
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            self._send(415, "application/json; charset=utf-8", b'{"error":"Content-Type must be application/json"}')
            return
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.send_error(403)
            return
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc.casefold() != self.headers.get("Host", "").casefold():
            self.send_error(403)
            return
        if not MUTATION_LOCK.acquire(blocking=False):
            self._send(409, "application/json; charset=utf-8", json.dumps({"error": "另一个管理操作正在执行，请稍后重试"}, ensure_ascii=False).encode("utf-8"))
            return
        try:
            self._post_action()
        finally:
            MUTATION_LOCK.release()

    def _post_action(self) -> None:
        if self.path not in {"/api/config", "/api/telegram/test", "/api/manage", "/api/fetcher/test", "/api/products/test"}:
            self.send_error(404)
            return
        try:
            submitted = request_body(self)
            if self.path == "/api/products/test":
                # Validate only the selected draft; other unfinished products and
                # Telegram settings must not prevent a standalone stock test.
                from monitor import MonitorError, inspect_product
                product = submitted.get("product")
                value = validate_config({"products": [product], "fetcher": submitted.get("fetcher", {})})
                started = time.monotonic()
                try:
                    with requests.Session() as session:
                        session.headers["User-Agent"] = "HostWatch/1.0 (manual stock test)"
                        result = inspect_product(session, value["products"][0], value["fetcher"])
                    output = {"ok": True, "available": result.available, "stock_mode": result.stock_mode}
                except MonitorError as exc:
                    output = {"ok": False, "error": str(exc)}
                output.update({"checked_at": datetime.now().astimezone().isoformat(timespec="seconds"), "duration_seconds": round(time.monotonic() - started, 1)})
                self._send(200, "application/json; charset=utf-8", json.dumps(output, ensure_ascii=False).encode("utf-8"))
                return
            if self.path == "/api/fetcher/test":
                fetcher = normalize_fetcher(submitted)
                data = json.dumps(flaresolverr_health(fetcher["flaresolverr_url"]), ensure_ascii=False).encode("utf-8")
                self._send(200, "application/json; charset=utf-8", data)
                return
            if self.path == "/api/manage":
                if submitted.get("action") in {"admin-add", "admin-passwd", "admin-delete"} and users_path() not in {None, DEFAULT_USERS_PATH}:
                    raise ValueError("当前使用自定义账号文件，请使用终端管理该文件中的管理员")
                if submitted.get("action") == "monitor-start":
                    validate_config({**current_config(), "enabled": True})
                data = json.dumps(manager_action(submitted), ensure_ascii=False).encode("utf-8")
                self._send(200, "application/json; charset=utf-8", data)
                return
            if self.path == "/api/telegram/test":
                token, chat_id = telegram_credentials(
                    str(submitted.get("bot_token", "")),
                    str(submitted.get("chat_id", "")),
                )
                if not token or not chat_id:
                    raise ValueError("请填写 Telegram bot token 和 chat ID")
                telegram_call(token, "sendMessage", chat_id=chat_id, text="✅ HostWatch Telegram 配置测试成功")
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
            if "fetcher" not in submitted:
                submitted["fetcher"] = current["fetcher"]
            # Saving settings never toggles monitoring. Start/stop are explicit
            # management actions against the persisted configuration.
            submitted["enabled"] = current["enabled"]
            value = validate_config(submitted)
            save_config(value)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            status = 502 if isinstance(exc, TelegramAPIError) else (413 if "请求体大小" in str(exc) else 400)
            self._send(status, "application/json; charset=utf-8", json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"))
            return
        try:
            service = manager_action({"action": "monitor-apply"})
        except ValueError as exc:
            service = {"ok": False, "error": str(exc)}
        self._send(200, "application/json; charset=utf-8", json.dumps({"ok": True, "config": public_config(value), "service": service}, ensure_ascii=False).encode("utf-8"))

    def log_message(self, format: str, *args) -> None:
        print("%s - %s" % (self.address_string(), format % args))


def main() -> None:
    host = os.getenv("ADMIN_HOST", "127.0.0.1")
    if host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("管理页只允许监听 localhost")
    hosts = allowed_hosts()
    if "*" in hosts:
        raise SystemExit("ADMIN_ALLOWED_HOSTS 不允许使用通配符；请填写精确域名")
    if any(item not in {"127.0.0.1", "localhost"} for item in hosts) and not authentication_required():
        raise SystemExit("允许域名访问时必须设置 ADMIN_PASSWORD 或使用 hw add-user 创建管理页账号")
    port = int(os.getenv("ADMIN_PORT", "8765"))
    print(f"HostWatch admin listening on http://{host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
