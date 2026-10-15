from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from html import escape
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

import requests

from auth import DEFAULT_USERS_PATH, authentication_required, authorized, users_path
from catalog import CatalogBusy, CatalogService
from affiliates import DEFAULT_AFFILIATES, PROVIDERS, affiliate_purchase_url, normalize_affiliates, normalize_merchant_order
from notifications import purchase_button, stock_message


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.getenv("VMISS_CONFIG", str(ROOT / "config.json")))
STATUS_PATH = Path(os.getenv("VMISS_STATUS", str(CONFIG_PATH.parent / "status.json")))
PAGE_PATH = ROOT / "admin.html"
MAX_BODY = 64 * 1024
MANAGER = Path("/opt/vmiss-monitor/manage.py")
MUTATION_LOCK = threading.Lock()
TEST_LOCK = threading.Lock()
TEST_TASKS = {}
STOPPED_TEST_IDS = {}
BROWSER_CLEANUP_SECONDS = 8
BROWSER_CLEANUP_WARNING = "本地测试进程已结束，但未能确认 FlareSolverr 浏览器释放；请检查服务日志，必要时重启 FlareSolverr，避免继续启动浏览器测试"
CATALOG = CatalogService(lambda: CONFIG_PATH.parent / "catalog.json", lambda: current_config()["fetcher"])


def destroy_test_browser(endpoint: str, session_id: str, creation_uncertain: bool = False) -> str:
    """Only destroy this task's session, never other browser users' sessions."""
    for attempt in range(3):
        try:
            response = requests.post(endpoint, json={"cmd": "sessions.destroy", "session": session_id}, timeout=(2, 5))
            payload = response.json()
            if isinstance(payload, dict) and (payload.get("status") == "ok" or (not creation_uncertain and "doesn't exist" in str(payload.get("message", "")))):
                return ""
        except (requests.RequestException, ValueError):
            pass
        if attempt < 2:
            time.sleep(1)
    return BROWSER_CLEANUP_WARNING


def browser_session_worker() -> None:
    """Keep all potentially blocking browser I/O outside the admin process."""
    value = json.loads(sys.stdin.read(MAX_BODY + 1))
    if value["command"] == "destroy":
        warning = destroy_test_browser(value["endpoint"], value["session"], value["creation_uncertain"])
        # A cancelled creation can still finish remotely after a destroy request.
        if value["creation_uncertain"]:
            warning = BROWSER_CLEANUP_WARNING
        output = {"ok": not warning, "warning": warning}
    else:
        try:
            response = requests.post(value["endpoint"], json={"cmd": "sessions.create", "session": value["session"]}, timeout=(3, 30))
            payload = response.json()
            output = {"ok": bool(response.ok and isinstance(payload, dict) and payload.get("status") == "ok")}
        except (requests.RequestException, ValueError):
            output = {"ok": False}
    print(json.dumps(output))


def kill_test_process(process) -> None:
    if process is not None:
        try:
            process.kill()
        except OSError:
            pass  # It may have exited between publication and cancellation.


def stock_test_worker() -> None:
    from monitor import MonitorError, inspect_product
    value = json.loads(sys.stdin.read(MAX_BODY + 1))
    try:
        with requests.Session() as session:
            session.headers["User-Agent"] = "HostWatch/1.0 (manual stock test)"
            if value["product"].get("_seckill"):
                from seckill import activities
                print(json.dumps({"ok": True, **activities(session, value["fetcher"])}))
                return
            result = inspect_product(session, value["product"], value["fetcher"])
        output = {"ok": True, "available": result.available, "stock_mode": result.stock_mode, "purchase_url": result.url, "name": result.name, **{field: getattr(result, field) for field in ("region", "specs", "traffic", "price")}}
        if result.vendor == "Akile":
            output["matched_product"] = {"area_id": result.area_id, "area_name": result.area_name, "node_name": result.node_name, "plan_name": result.name}
        if result.vendor in {"VPS.SOY", "DMIT"}:
            output["catalog_url"] = result.catalog_url
        if result.vendor == "Isvoro":
            output["matched_product"] = result.match_details
    except MonitorError as exc:
        output = {"ok": False, "error": str(exc)}
    print(json.dumps(output))


class StockTestTask:
    def __init__(self, task_id: str, product: dict, fetcher: dict):
        from monitor import product_fetcher
        self.id = task_id
        self.product = product
        self.fetcher = product_fetcher(fetcher, product)
        self.started = time.monotonic()
        self.cancelled = threading.Event()
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.process = None
        self.output = {"pending": True}
        self.browser = self.fetcher["mode"] == "flaresolverr"
        self.session_id = f"hostwatch-test-{task_id}"

    def snapshot(self) -> dict:
        with self.lock:
            return {"task_id": self.id, "product_id": self.product["id"], **self.output, "stopping": self.cancelled.is_set() and not self.done.is_set()}

    def cancel(self) -> None:
        with self.lock:
            if self.done.is_set() or self.output.get("notifying"):
                return
            self.cancelled.set()
            process = self.process
        # SIGTERM can be ignored; kill only this task's child, outside state locks.
        kill_test_process(process)

    def run_process(self, mode: str, payload: dict, timeout: float, cancellable: bool = True) -> dict:
        if cancellable and self.cancelled.is_set():
            return {"ok": False}
        # Popen can stall under memory pressure. Never hold a state/registry lock
        # while spawning, signalling, waiting or doing network I/O.
        process = subprocess.Popen([sys.executable, str(ROOT / "admin.py"), mode], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
        if cancellable:
            with self.lock:
                self.process = process
            if self.cancelled.is_set():
                kill_test_process(process)
        try:
            stdout, _ = process.communicate(json.dumps(payload), timeout=timeout)
            if cancellable and self.cancelled.is_set():
                return {"ok": False}
            result = json.loads(stdout)
            if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
                raise ValueError("invalid worker result")
            return result
        finally:
            if cancellable:
                with self.lock:
                    if self.process is process:
                        self.process = None
            if process.poll() is None:
                kill_test_process(process)
                try:
                    process.communicate(timeout=2)
                except (subprocess.TimeoutExpired, OSError):
                    pass  # Never block the control API on a stuck child.

    def run(self) -> None:
        from contextlib import nullcontext
        from monitor import MonitorError, browser_request_slot, check_browser_memory
        result = {"ok": False, "error": "测试任务未完成"}
        cleanup_warning = ""
        browser_attempted = False
        browser_created = False
        try:
            with browser_request_slot() if self.browser else nullcontext():
                if self.browser:
                    check_browser_memory(self.fetcher)
                try:
                    if self.browser and not self.cancelled.is_set():
                        browser_attempted = True
                        with self.lock:
                            self.output["phase"] = "starting"
                        try:
                            payload = self.run_process("--browser-session-worker", {"command": "create", "endpoint": self.fetcher["flaresolverr_url"], "session": self.session_id}, 35)
                            if not payload["ok"]:
                                raise ValueError("browser creation failed")
                            browser_created = True
                        except (subprocess.TimeoutExpired, ValueError):
                            raise MonitorError("FlareSolverr 浏览器启动失败或超时，请检查服务内存及日志") from None
                    with self.lock:
                        if self.cancelled.is_set():
                            return
                        self.output["phase"] = "checking"
                    fetcher = dict(self.fetcher)
                    if self.browser:
                        fetcher.update({"_test_browser_session": self.session_id, "_browser_slot_held": True})
                    try:
                        result = self.run_process("--stock-test-worker", {"product": self.product, "fetcher": fetcher}, self.fetcher["timeout_seconds"] + self.fetcher["wait_seconds"] + 40)
                    except subprocess.TimeoutExpired:
                        result = {"ok": False, "error": "测试超时，已终止本地任务"}
                finally:
                    if browser_attempted:
                        with self.lock:
                            self.output["phase"] = "cleaning"
                        try:
                            cleanup = self.run_process("--browser-session-worker", {"command": "destroy", "endpoint": self.fetcher["flaresolverr_url"], "session": self.session_id, "creation_uncertain": not browser_created}, BROWSER_CLEANUP_SECONDS, cancellable=False)
                            cleanup_warning = cleanup.get("warning", "") if cleanup["ok"] else cleanup.get("warning") or BROWSER_CLEANUP_WARNING
                        except (subprocess.TimeoutExpired, OSError, ValueError):
                            cleanup_warning = BROWSER_CLEANUP_WARNING
        except MonitorError as exc:
            result = {"ok": False, "error": str(exc)}
        except (OSError, ValueError):
            result = {"ok": False, "error": "测试进程异常退出，请检查服务器内存及管理页日志"}
        finally:
            if result.get("ok") and result.get("purchase_url"):
                result["url"] = result["purchase_url"]
                try:
                    result["purchase_url"] = affiliate_purchase_url(result["url"], self.product["adapter"], current_config(), self.product)
                except (OSError, ValueError):
                    pass  # The stock result remains available if config was removed.
            with self.lock:
                if self.cancelled.is_set():
                    result = {"ok": False, "cancelled": True, "error": "测试已停止"}
                if cleanup_warning:
                    result["warning"] = cleanup_warning
                self.output = {**result, "pending": True, "notifying": not self.cancelled.is_set(), "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"), "duration_seconds": round(time.monotonic() - self.started, 1)}
            # Stock checking and browser cleanup have finished. Do not hold the
            # task lock across Telegram I/O; status polling must remain responsive.
            if not self.cancelled.is_set():
                notification = send_stock_test_result(self.product, self.snapshot())
            else:
                notification = {"status": "skipped", "message": "测试已停止，未发送 Telegram"}
            with self.lock:
                self.output.update(pending=False, notifying=False, notification=notification)
                self.done.set()


def start_stock_test(submitted: dict) -> dict:
    task_id = str(submitted.get("task_id", ""))
    if not re.fullmatch(r"[a-zA-Z0-9-]{16,64}", task_id):
        raise ValueError("测试任务编号无效")
    if submitted.get("kind") == "isvoro_seckill":
        from seckill import TEST_PRODUCT_ID
        value = {"products": [{"id": TEST_PRODUCT_ID, "adapter": "isvoro", "provider": "Isvoro", "name": "限时秒杀", "_seckill": True}], "fetcher": normalize_fetcher(submitted.get("fetcher", {}))}
    else:
        value = validate_config({"products": [submitted.get("product")], "fetcher": submitted.get("fetcher", {})})
    with TEST_LOCK:
        if task_id in TEST_TASKS:
            raise ValueError("测试任务编号已使用，请重新测试")
        if any(not task.done.is_set() for task in TEST_TASKS.values()):
            raise ValueError("已有商品测试正在执行，请先停止或等待完成")
        for old_id in list(TEST_TASKS)[:-19]:
            del TEST_TASKS[old_id]
        task = StockTestTask(task_id, value["products"][0], value["fetcher"])
        TEST_TASKS[task_id] = task
        stopped_early = task_id in STOPPED_TEST_IDS
    if stopped_early:
        task.cancel()
    threading.Thread(target=task.run, daemon=True).start()
    return task.snapshot()


def stop_stock_test(task_id: str) -> dict:
    if not re.fullmatch(r"[a-zA-Z0-9-]{16,64}", task_id):
        raise ValueError("测试任务编号无效")
    with TEST_LOCK:
        task = TEST_TASKS.get(task_id)
        if task is None:
            # A stop can arrive before a delayed start request. Keep a bounded
            # tombstone so the late start can never launch a browser.
            STOPPED_TEST_IDS[task_id] = True
            for old_id in list(STOPPED_TEST_IDS)[:-20]:
                del STOPPED_TEST_IDS[old_id]
    if task is None:
        return {"task_id": task_id, "pending": False, "ok": False, "cancelled": True, "error": "测试已停止"}
    task.cancel()
    return task.snapshot()


def default_config() -> dict:
    return {
        "enabled": False,
        "interval_seconds": 600,
        "notify_current_stock_on_start": False,
        "isvoro_seckill": False,
        "affiliate_id": DEFAULT_AFFILIATES["vmiss"],
        "affiliates": dict(DEFAULT_AFFILIATES),
        "merchant_order": list(PROVIDERS),
        "watch_order": [],
        "notify_order": [],
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
    provider_modes = value.get("provider_modes", {})
    if not isinstance(provider_modes, dict) or any(key not in (*PROVIDERS, "html") or not isinstance(mode, str) or mode not in {"inherit", "requests", "flaresolverr"} for key, mode in provider_modes.items()):
        raise ValueError("商家请求方式必须是支持商家的 inherit、requests 或 flaresolverr")
    return {
        "mode": mode,
        "flaresolverr_url": endpoint.rstrip("/"),
        "timeout_seconds": timeout_seconds,
        "wait_seconds": wait,
        "provider_modes": dict(provider_modes),
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


def normalize_vps_soy_url(value: object) -> str:
    catalog_url = str(value).strip()
    parsed = urlsplit(catalog_url)
    if parsed.scheme != "https" or parsed.hostname not in {"www.vps.soy", "vps.soy"} or parsed.path.rstrip("/") != "/cart" or parsed.username or parsed.password or parsed.fragment or parsed.port not in {None, 443} or len(catalog_url) > 2048:
        raise ValueError("VPS.SOY 商品页必须是 https://www.vps.soy/cart，可带 fid、gid 分类参数")
    query = parse_qsl(parsed.query, keep_blank_values=True)
    for key in ("fid", "gid"):
        values = [value for name, value in query if name == key]
        if values and (len(values) != 1 or not re.fullmatch(r"[1-9][0-9]{0,9}", values[0])):
            raise ValueError(f"VPS.SOY 的 {key.upper()} 必须是唯一的正整数（最多 10 位），或移除此参数使用默认分类")
    if any(key in {"action", "pid"} for key, _ in query):
        raise ValueError("VPS.SOY 请填写带 fid、gid 的商品分类页，不要填写含 action 或 pid 的下单链接")
    return "https://www.vps.soy/cart" + ("?" + urlencode(sorted(query)) if query else "")


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

    affiliates = normalize_affiliates(value)
    merchant_order = normalize_merchant_order(value.get("merchant_order", []))

    for key in ("enabled", "notify_current_stock_on_start", "isvoro_seckill"):
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
        adapter = str(item.get("adapter", "vmiss" if any(key in item for key in ("category_url", "selector", "gid", "pid")) else "html")).strip().casefold()
        provider = str(item.get("provider", {"vmiss": "VMISS", "vps_soy": "VPS.SOY", "akile": "Akile", "isvoro": "Isvoro", "dmit": "DMIT"}.get(adapter, "自定义网站"))).strip()
        gid = str(item.get("gid", "")).strip()
        pid = str(item.get("pid", "")).strip()
        if "enabled" in item and not isinstance(item["enabled"], bool):
            raise ValueError(f"产品 {name or product_id} 的启用状态必须是布尔值")
        if not product_id or product_id in seen_ids:
            raise ValueError("每个产品都需要唯一 ID")
        if adapter not in {*PROVIDERS, "html"}:
            raise ValueError(f"产品 {name or product_id} 的适配器必须是 vmiss、vps_soy、akile、isvoro、dmit 或 html")
        if not name:
            raise ValueError(f"产品 {product_id} 需要填写商品名称")
        if not provider or len(provider) > 80:
            raise ValueError(f"产品 {name} 的厂商名称必填且不能超过 80 个字符")

        if adapter == "vmiss":
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
        elif adapter == "dmit":
            from dmit import PRICING_URL, normalize_catalog_url
            category_url = normalize_catalog_url(item.get("category_url", PRICING_URL))
            if not re.fullmatch(r"[1-9][0-9]{0,9}", pid) or len(name) > 120 or len(category_url) > 2048:
                raise ValueError(f"产品 {name} 的 DMIT PID 必须是正整数（最多 10 位），名称最多 120 字符")
            normalized_item = {"id": product_id, "name": name, "provider": provider, "adapter": adapter, "category_url": category_url, "pid": pid, "enabled": item.get("enabled", False)}
        elif adapter == "vps_soy":
            catalog_url = normalize_vps_soy_url(item.get("url", "https://www.vps.soy/cart"))
            selector = str(item.get("selector", name)).strip()
            if not selector or len(selector) > 200:
                raise ValueError(f"产品 {name} 需要填写 VPS.SOY 套餐完整名称（最多 200 个字符）")
            normalized_item = {"id": product_id, "name": name, "provider": provider, "adapter": adapter, "url": catalog_url, "selector": selector, "enabled": item.get("enabled", False)}
        elif adapter == "isvoro":
            normalized_item = {"id": product_id, "name": name, "provider": provider, "adapter": adapter, "enabled": item.get("enabled", False)}
            for key in ("type_id", "category_id", "product_id"):
                identifier = str(item.get(key, "")).strip()
                if not re.fullmatch(r"[1-9][0-9]{0,9}", identifier):
                    raise ValueError(f"产品 {name} 需要选择 Isvoro 类型、地区/线路和套餐（{key} 必须是正整数）")
                normalized_item[key] = identifier
            for key in ("type_name", "category_name", "product_name"):
                normalized_item[key] = str(item.get(key, ""))[:200]
        elif adapter == "akile":
            area_mode = str(item.get("area_mode", "auto")).strip().casefold()
            if area_mode not in {"auto", "manual"}:
                raise ValueError(f"产品 {name} 的 Akile 地区识别方式必须是 auto 或 manual")
            area_id = str(item.get("area_id", "")).strip()
            node_name = str(item.get("node_name", "HKL-TW")).strip()
            selector = str(item.get("selector", name)).strip()
            if (area_mode == "manual" or area_id) and not re.fullmatch(r"[1-9][0-9]{0,9}", area_id):
                raise ValueError(f"产品 {name} 的 Akile 地区 ID 必须是正整数（最多 10 位）")
            if not node_name or not selector or max(len(node_name), len(selector), len(name)) > 200:
                raise ValueError(f"产品 {name} 需要填写 Akile 线路及套餐完整名称（最多 200 个字符）")
            normalized_item = {"id": product_id, "name": name, "provider": provider, "adapter": adapter, "area_mode": area_mode, "area_id": area_id, "node_name": node_name, "selector": selector, "enabled": item.get("enabled", False)}
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
        # Legacy selections remain visible and subscribed after migration.
        for flag in ("watch_enabled", "notify_enabled"):
            if flag in item and not isinstance(item[flag], bool):
                raise ValueError(f"产品 {name} 的 {flag} 必须是布尔值")
            legacy = item.get("enabled", False) if not any(k in item for k in ("watch_enabled", "notify_enabled")) else False
            normalized_item[flag] = item.get(flag, legacy)
        normalized_item["enabled"] = normalized_item["watch_enabled"] or normalized_item["notify_enabled"]
        for field in ("remarks", "line_label", "line_telecom", "line_unicom", "line_mobile"):
            text = item.get(field, "")
            if not isinstance(text, str) or len(text) > 500:
                raise ValueError(f"产品 {name} 的备注和线路必须是 500 字以内的文本")
            normalized_item[field] = text.strip()
        snapshot = item.get("snapshot", {})
        if not isinstance(snapshot, dict):
            raise ValueError("商品快照必须是对象")
        normalized_item["snapshot"] = {key: str(snapshot.get(key, ""))[:2048 if key == "url" else 500] for key in ("name", "region", "specs", "traffic", "price", "url", "checked_at")}
        if normalized_item["snapshot"]["url"] and urlsplit(normalized_item["snapshot"]["url"]).scheme != "https":
            raise ValueError("商品购买快照必须是 HTTPS 地址")
        if type(snapshot.get("available")) is int and snapshot["available"] >= 0:
            normalized_item["snapshot"]["available"] = snapshot["available"]
        normalized_item["snapshot"]["stock_mode"] = "availability" if snapshot.get("stock_mode") == "availability" else "quantity"
        normalized_item["request_mode"] = request_mode
        seen_ids.add(product_id)
        normalized.append(normalized_item)
    enabled = value.get("enabled", False)
    if enabled and not any(item["enabled"] for item in normalized) and not value.get("isvoro_seckill", False):
        raise ValueError("启用监控前至少启用一个产品")
    fetcher = normalize_fetcher(value.get("fetcher", {}))
    telegram = normalize_telegram(value.get("telegram", {}))
    product_orders = {}
    for key in ("watch_order", "notify_order"):
        order = value.get(key, [])
        if not isinstance(order, list) or len(order) > 100 or any(not isinstance(identifier, str) for identifier in order) or len(set(order)) != len(order):
            raise ValueError("商品顺序必须是最多 100 项、无重复的商品 ID 列表")
        product_orders[key] = [identifier for identifier in order if identifier in seen_ids]
    return {
        "enabled": enabled,
        "interval_seconds": interval,
        "notify_current_stock_on_start": value.get("notify_current_stock_on_start", False),
        "isvoro_seckill": value.get("isvoro_seckill", False),
        "affiliate_id": affiliates["vmiss"],
        "affiliates": affiliates,
        "merchant_order": merchant_order,
        **product_orders,
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


def send_stock_test_result(product: dict, result: dict) -> dict:
    """Explicit manual delivery, like /api/telegram/test, independent of monitoring."""
    try:
        token, chat_id = telegram_credentials()
        if not token or not chat_id:
            return {"status": "unconfigured", "message": "未发送 Telegram：请先保存机器人 Token 和 Chat ID"}
        matched = result.get("matched_product")
        matched = matched if isinstance(matched, dict) else {}
        link = ""
        if product.get("_seckill") and result.get("ok"):
            from seckill import campaign_message, URL
            settings = current_config()
            for activity in result.get("activities", []):
                message, keyboard = campaign_message(activity, settings, test=True)
                telegram_call(token, "sendMessage", chat_id=chat_id, text=message, parse_mode="HTML", disable_web_page_preview=True, reply_markup=keyboard)
            if not result.get("activities"):
                link = affiliate_purchase_url(URL, "isvoro", settings)
                telegram_call(token, "sendMessage", chat_id=chat_id, text="🧪 HostWatch · Isvoro 秒杀单次测试\n当前暂无秒杀活动。\n单次测试，不修改自动通知记录。", reply_markup=purchase_button(link))
            return {"status": "sent", "message": "秒杀测试结果已发送至 Telegram；未修改自动通知记录"}
        if result.get("ok"):
            if result.get("purchase_url"):
                link = affiliate_purchase_url(result.get("url") or result["purchase_url"], product.get("adapter", ""), current_config(), product)
            message = stock_message(vendor=product.get("provider") or product.get("adapter", ""),
                name=result.get("name") or matched.get("plan_name") or product["name"], available=result["available"], stock_mode=result.get("stock_mode", "quantity"),
                region=result.get("region") or matched.get("area_name") or matched.get("type_name", ""),
                specs=result.get("specs", ""), traffic=result.get("traffic", ""), price=result.get("price", ""),
                line=matched.get("category_name") or matched.get("node_name") or product.get("category_name") or product.get("node_name", ""),
                item=product, checked_at=result["checked_at"], test=True, duration=result["duration_seconds"], warning=result.get("warning", ""), purchase_url=link)
        else:
            message = escape("❌ HostWatch · 商品单次测试\n"
                f"商家：{str(product.get('provider') or product.get('adapter', ''))[:80]}\n"
                f"商品：{str(product['name'])[:200]}\n测试失败：{str(result.get('error', '未知错误'))[:800]}\n"
                f"检查时间：{result['checked_at']}\n耗时：{result['duration_seconds']} 秒")
            if result.get("warning"):
                message += "\n提示：" + escape(str(result["warning"])[:300])
        if matched:
            # Keep the exact match identities available for diagnosing selection errors.
            identities = [(label, matched.get(key)) for key, label in (("type_id", "类型 ID"), ("category_id", "线路 ID"), ("product_id", "套餐 ID"), ("area_id", "地区 ID")) if matched.get(key)]
            message += "\n" + escape(" · ".join(f"{label}={str(value)[:10]}" for label, value in identities))
        if result.get("catalog_url"):
            message += "\n检查分类：" + escape(str(result["catalog_url"])[:400])
        telegram_call(token, "sendMessage", chat_id=chat_id, text=message, parse_mode="HTML", disable_web_page_preview=True, reply_markup=purchase_button(link))
        return {"status": "sent", "message": "测试结果已发送至 Telegram"}
    except TelegramAPIError as exc:
        return {"status": "failed", "message": f"测试结果发送失败：{exc}"}
    except (OSError, ValueError, KeyError, TypeError):
        return {"status": "failed", "message": "测试结果发送失败：请检查已保存的 Telegram 配置和管理页日志"}


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
        elif self.path in {"/admin.js", "/catalog.js"}:
            self._send(200, "text/javascript; charset=utf-8", (ROOT / self.path[1:]).read_bytes())
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
        elif urlsplit(self.path).path == "/api/catalog":
            try:
                query = dict(parse_qsl(urlsplit(self.path).query))
                output = CATALOG.snapshot(query.get("provider", "akile"), query.get("scope", ""))
                self._send(200, "application/json; charset=utf-8", json.dumps(output, ensure_ascii=False).encode("utf-8"))
            except ValueError as exc:
                self._send(400, "application/json; charset=utf-8", json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"))
        elif self.path == "/api/isvoro/categories" or self.path.startswith("/api/isvoro/category/"):
            from monitor import MonitorError, fetch_isvoro_json, isvoro_categories, isvoro_category
            try:
                slug = self.path.rsplit("/", 1)[-1]
                if self.path != "/api/isvoro/categories" and not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", slug):
                    raise MonitorError("Isvoro 分类标识无效")
                with requests.Session() as session:
                    path = "categories" if self.path == "/api/isvoro/categories" else f"categories/{slug}"
                    raw = fetch_isvoro_json(session, path, {"mode": "requests"})
                output = {"categories": isvoro_categories(raw)} if path == "categories" else {"category": isvoro_category(raw)}
                self._send(200, "application/json; charset=utf-8", json.dumps(output, ensure_ascii=False).encode("utf-8"))
            except (MonitorError, ValueError, requests.RequestException) as exc:
                self._send(502, "application/json; charset=utf-8", json.dumps({"error": f"Isvoro 目录读取失败：{exc}"}, ensure_ascii=False).encode("utf-8"))
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
        elif self.path.startswith("/api/products/test/status/"):
            task_id = self.path.rsplit("/", 1)[-1]
            with TEST_LOCK:
                task = TEST_TASKS.get(task_id)
                stopped_early = task_id in STOPPED_TEST_IDS
            if task is None and stopped_early:
                self._send(200, "application/json; charset=utf-8", json.dumps({"task_id": task_id, "pending": False, "ok": False, "cancelled": True, "error": "测试已停止"}, ensure_ascii=False).encode("utf-8"))
                return
            if task is None:
                self._send(404, "application/json; charset=utf-8", b'{"error":"Test task not found"}')
                return
            self._send(200, "application/json; charset=utf-8", json.dumps(task.snapshot(), ensure_ascii=False).encode("utf-8"))
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
        # Stopping and polling tests must never wait behind config/service locks.
        if self.path in {"/api/products/test", "/api/products/test/stop", "/api/catalog/refresh", "/api/catalog/stop"}:
            self._post_action()
            return
        if not MUTATION_LOCK.acquire(blocking=False):
            self._send(409, "application/json; charset=utf-8", json.dumps({"error": "另一个管理操作正在执行，请稍后重试"}, ensure_ascii=False).encode("utf-8"))
            return
        try:
            self._post_action()
        finally:
            MUTATION_LOCK.release()

    def _post_action(self) -> None:
        if self.path not in {"/api/config", "/api/telegram/test", "/api/manage", "/api/fetcher/test", "/api/products/test", "/api/products/test/stop", "/api/catalog/refresh", "/api/catalog/stop"}:
            self.send_error(404)
            return
        try:
            submitted = request_body(self)
            if self.path in {"/api/catalog/refresh", "/api/catalog/stop"}:
                try:
                    output = CATALOG.stop() if self.path.endswith("/stop") else CATALOG.refresh(submitted.get("provider"), submitted.get("scope", ""))
                except CatalogBusy as exc:
                    self._send(409, "application/json; charset=utf-8", json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"))
                    return
                self._send(200, "application/json; charset=utf-8", json.dumps(output, ensure_ascii=False).encode("utf-8"))
                return
            if self.path == "/api/products/test/stop":
                output = stop_stock_test(str(submitted.get("task_id", "")))
                self._send(200, "application/json; charset=utf-8", json.dumps(output, ensure_ascii=False).encode("utf-8"))
                return
            if self.path == "/api/products/test":
                if not submitted.get("task_id"):
                    raise ValueError("请刷新管理页后重试，以使用可停止的商品测试")
                output = start_stock_test(submitted)
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
            elif isinstance(submitted["fetcher"], dict) and "provider_modes" not in submitted["fetcher"]:
                submitted["fetcher"]["provider_modes"] = current["fetcher"].get("provider_modes", {})
            if "affiliates" not in submitted:
                submitted["affiliates"] = dict(current["affiliates"])
                if "affiliate_id" in submitted:
                    submitted["affiliates"]["vmiss"] = str(submitted["affiliate_id"]).strip()
            if "merchant_order" not in submitted:
                submitted["merchant_order"] = current["merchant_order"]
            for key in ("watch_order", "notify_order"):
                if key not in submitted:
                    submitted[key] = current[key]
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
    if sys.argv[1:] == ["--stock-test-worker"]:
        stock_test_worker()
    elif sys.argv[1:] == ["--browser-session-worker"]:
        browser_session_worker()
    else:
        main()
