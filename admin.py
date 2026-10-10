from __future__ import annotations

import json
import os
import re
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlencode
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.getenv("VMISS_CONFIG", str(ROOT / "config.json")))
STATUS_PATH = Path(os.getenv("VMISS_STATUS", str(CONFIG_PATH.parent / "status.json")))
PAGE_PATH = ROOT / "admin.html"
MAX_BODY = 64 * 1024


def default_config() -> dict:
    return {"enabled": False, "interval_seconds": 600, "notify_current_stock_on_start": False, "affiliate_id": "", "products": []}


def read_json(path: Path, default: dict) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else default
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return default


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
    return {
        "enabled": enabled,
        "interval_seconds": interval,
        "notify_current_stock_on_start": value.get("notify_current_stock_on_start", False),
        "affiliate_id": affiliate_id,
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
        host = self.headers.get("Host", "").split(":", 1)[0].strip("[]").casefold()
        if host not in {"127.0.0.1", "localhost"}:
            self.send_error(403)
            return False
        return True

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
        if not self._secure():
            return
        if self.path == "/":
            self._send(200, "text/html; charset=utf-8", PAGE_PATH.read_bytes())
        elif self.path == "/admin.js":
            self._send(200, "text/javascript; charset=utf-8", (ROOT / "admin.js").read_bytes())
        elif self.path == "/api/config":
            try:
                raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else default_config()
                data = json.dumps(validate_config(raw), ensure_ascii=False).encode("utf-8")
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self._send(500, "application/json; charset=utf-8", json.dumps({"error": f"配置文件无法读取：{exc}"}, ensure_ascii=False).encode("utf-8"))
                return
            self._send(200, "application/json; charset=utf-8", data)
        elif self.path == "/api/status":
            data = json.dumps(read_json(STATUS_PATH, {"state": "unknown", "products": []}), ensure_ascii=False).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", data)
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if not self._secure():
            return
        if self.path != "/api/config":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_BODY:
            self.send_error(413)
            return
        try:
            value = validate_config(json.loads(self.rfile.read(length)))
            save_config(value)
        except (ValueError, json.JSONDecodeError) as exc:
            self._send(400, "application/json; charset=utf-8", json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"))
            return
        self._send(200, "application/json; charset=utf-8", b'{"ok":true}')

    def log_message(self, format: str, *args) -> None:
        print("%s - %s" % (self.address_string(), format % args))


def main() -> None:
    host = os.getenv("ADMIN_HOST", "127.0.0.1")
    if host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("管理页只允许监听 localhost")
    port = int(os.getenv("ADMIN_PORT", "8765"))
    print(f"VMISS monitor admin listening on http://{host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
