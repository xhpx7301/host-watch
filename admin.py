from __future__ import annotations

import base64
import hmac
import hashlib
import ipaddress
import json
import math
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests

from auth import DEFAULT_USERS_PATH, authentication_required, authorized, credentials_authorized, users_path
from catalog import CatalogBusy, CatalogService
from affiliates import DEFAULT_AFFILIATES, PROVIDERS, affiliate_purchase_url, effective_coupon, normalize_affiliate_accounts, normalize_affiliate_coupons, normalize_affiliate_notes, normalize_affiliate_rates, normalize_affiliates, normalize_merchant_order
from time_utils import beijing_iso, format_beijing
from notifications import purchase_button, stock_message
import egress


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.getenv("VMISS_CONFIG", str(ROOT / "config.json")))
STATUS_PATH = Path(os.getenv("VMISS_STATUS", str(CONFIG_PATH.parent / "status.json")))
RATES_PATH = Path(os.getenv("VMISS_RATES", str(CONFIG_PATH.parent / "exchange-rates.json")))
PAGE_PATH = ROOT / "admin.html"
LOGIN_PAGE_PATH = ROOT / "login.html"
LOGIN_SCRIPT_PATH = ROOT / "login.js"
MAX_BODY = 64 * 1024
MAX_BACKUP_UPLOAD = 20 * 1024 * 1024
MANAGER = Path("/opt/vmiss-monitor/manage.py")
MUTATION_LOCK = threading.Lock()
RATES_LOCK = threading.Lock()
TEST_LOCK = threading.Lock()
TEST_TASKS = {}
STOPPED_TEST_IDS = {}
SECKILL_LOCK = threading.Lock()
SECKILL_TASKS = {}
STOPPED_SECKILL_IDS = {}
SECKILL_TEST_TASKS = {}
STOPPED_SECKILL_TEST_IDS = {}
AKILE_MARKET_LOCK = threading.RLock()
AKILE_MARKET_RUN_LOCK = threading.Lock()
ISVORO_MARKET_RUN_LOCK = threading.Lock()
AKILE_MARKET_WAKE = threading.Event()
AKILE_MARKET_CACHE = {"updated_at": "", "items": [], "error": "", "running": False}
AKILE_MARKET_THREAD = None
ISVORO_MARKET_LOCK = threading.RLock()
ISVORO_MARKET_CACHE = {"updated_at": "", "items": [], "error": "", "running": False}
# BandwagonHost keeps the email-code challenge in the login session. Keep only
# short-lived, in-memory sessions so an administrator can finish that login
# without persisting the code or merchant cookies in configuration backups.
BWH_VERIFICATION_CHALLENGES = {}
BWH_VERIFICATION_TTL = 10 * 60
# Short-lived authentication state for merchants whose login can be checked
# without submitting credentials again.  Values stay in process memory only;
# each task still receives its own requests.Session clone.
MERCHANT_AUTH_SESSIONS = {}
MERCHANT_AUTH_SESSION_TTL = 30 * 24 * 60 * 60
ISVORO_AUTH_SESSION_TTL = 30 * 24 * 60 * 60
BWH_AUTH_SESSION_TTL = 30 * 24 * 60 * 60
MERCHANT_AUTH_CACHE_NAME = "merchant-auth-cache.json"
MERCHANT_AUTH_KEY_NAME = "merchant-auth-cache.key"
MERCHANT_AUTH_CACHE_MAX_BYTES = 4 * 1024 * 1024
REFRESH_LOCK = threading.Lock()
REFRESH_TASKS = {}
BROWSER_CLEANUP_SECONDS = 8
BROWSER_CLEANUP_WARNING = "本地测试进程已结束，但未能确认 FlareSolverr 浏览器释放；请检查服务日志，必要时重启 FlareSolverr，避免继续启动浏览器测试"
ADMIN_SESSION_COOKIE = "hostwatch_session"
ADMIN_SESSION_TTL = 12 * 60 * 60
ADMIN_SESSIONS = {}
ADMIN_SESSION_LOCK = threading.Lock()
CATALOG = CatalogService(lambda: CONFIG_PATH.parent / "catalog.json", lambda: current_config()["fetcher"])
CLOUDSCRAPER_TEST_ID = "cloudscraper-probe"
CLOUDSCRAPER_TEST_TARGETS = {
    "vmiss": ("VMISS", "https://app.vmiss.com/store"),
    "vps_soy": ("VPS.SOY", "https://www.vps.soy/cart"),
    "akile": ("Akile", "https://akile.ai/shop/server?areaId=3"),
    "narwhal": ("独角鲸云", "https://api.fuckip.me/api/v1/plans"),
    "isvoro": ("Isvoro", "https://isvoro.com/buy"),
    "dmit": ("DMIT", "https://www.dmit.io/pages/pricing"),
    "bwh": ("搬瓦工", "https://bwh81.net/order/ecommerce/Los%20Angeles/USCA_9"),
    "baseline": ("公共 HTTPS 基准", "https://example.com/"),
}
EXCHANGE_RATES_URL = "https://api.frankfurter.app/latest?base=CNY"
EXCHANGE_RATE_MAX_AGE = 24 * 60 * 60


def _read_rates_cache() -> dict:
    try:
        value = json.loads(RATES_PATH.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("rates"), dict):
            return {}
        rates = {str(code).upper(): float(rate) for code, rate in value["rates"].items()
                 if re.fullmatch(r"[A-Z]{3}", str(code).upper()) and isinstance(rate, (int, float)) and float(rate) > 0}
        value["rates"] = rates
        return value
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def _write_rates_cache(value: dict) -> None:
    RATES_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="exchange-rates-", suffix=".tmp", dir=RATES_PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
        os.replace(temporary, RATES_PATH)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def exchange_rates() -> dict:
    """Return cached CNY rates, refreshing at most once per day."""
    cached = _read_rates_cache()
    fetched_at = cached.get("fetched_at", 0)
    try:
        fresh = time.time() - float(fetched_at) < EXCHANGE_RATE_MAX_AGE
    except (TypeError, ValueError):
        fresh = False
    if fresh:
        return {"base": "CNY", **cached, "stale": False}
    with RATES_LOCK:
        cached = _read_rates_cache()
        try:
            fresh = time.time() - float(cached.get("fetched_at", 0)) < EXCHANGE_RATE_MAX_AGE
        except (TypeError, ValueError):
            fresh = False
        if fresh:
            return {"base": "CNY", **cached, "stale": False}
        try:
            response = requests.get(EXCHANGE_RATES_URL, timeout=(3, 8))
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("base") != "CNY" or not isinstance(payload.get("rates"), dict):
                raise ValueError("汇率接口返回格式无效")
            rates = {str(code).upper(): float(rate) for code, rate in payload["rates"].items()
                     if re.fullmatch(r"[A-Z]{3}", str(code).upper()) and isinstance(rate, (int, float)) and float(rate) > 0}
            if not rates:
                raise ValueError("汇率接口未返回有效数据")
            updated = {"base": "CNY", "rates": rates, "date": str(payload.get("date", "")), "fetched_at": time.time()}
            _write_rates_cache(updated)
            return {**updated, "stale": False}
        except (OSError, ValueError, TypeError, requests.RequestException):
            if cached.get("rates"):
                return {"base": "CNY", **cached, "stale": True, "error": "实时汇率暂时不可用，继续使用上次成功更新的汇率"}
            return {"base": "CNY", "rates": {}, "stale": True, "error": "实时汇率暂时不可用"}


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
            command = {"cmd": "sessions.create", "session": value["session"]}
            if value.get("proxy"):
                command["proxy"] = value["proxy"]
            response = requests.post(value["endpoint"], json=command, timeout=(3, 30))
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
        if value["product"].get("_request_probe"):
            print(json.dumps(cloudscraper_probe(value["product"]["target"], value["fetcher"])))
            return
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
        if result.vendor == "DMIT":
            output["stock_version"] = result.match_details["stock_version"]
        if result.vendor == "Isvoro":
            output["matched_product"] = result.match_details
    except MonitorError as exc:
        output = {"ok": False, "error": str(exc)}
    print(json.dumps(output))


class StockTestTask:
    def __init__(self, task_id: str, product: dict, fetcher: dict):
        from monitor import product_fetcher, fetcher_route
        self.id = task_id
        self.product = product
        self.fetcher = product_fetcher(fetcher, product)
        self.route = fetcher_route(fetcher, product)
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
            return {"task_id": self.id, "product_id": self.product["id"], "route": self.route, **self.output, "stopping": self.cancelled.is_set() and not self.done.is_set()}

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
                            command = {"command": "create", "endpoint": self.fetcher["flaresolverr_url"], "session": self.session_id}
                            if self.fetcher.get("egress_proxy_url"):
                                command["proxy"] = egress.browser_proxy(self.fetcher)
                            payload = self.run_process("--browser-session-worker", command, 35)
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
                        timeout = self.fetcher["timeout_seconds"] + self.fetcher["wait_seconds"] + 40
                        if fetcher["mode"] == "cloudscraper":
                            options = normalize_cloudscraper(fetcher.get("cloudscraper"))
                            timeout = 2 * (options["connect_timeout_seconds"] + options["read_timeout_seconds"]) + options["challenge_delay_seconds"] + 20
                        result = self.run_process("--stock-test-worker", {"product": self.product, "fetcher": fetcher}, timeout)
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
                self.output = {**result, "pending": True, "notifying": not self.cancelled.is_set() and not self.product.get("_request_probe"), "checked_at": beijing_iso(), "duration_seconds": round(time.monotonic() - self.started, 1)}
            # Stock checking and browser cleanup have finished. Do not hold the
            # task lock across Telegram I/O; status polling must remain responsive.
            if self.product.get("_request_probe"):
                notification = {"status": "skipped", "message": "网页访问测试不发送 Telegram，不修改库存记录"}
            elif not self.cancelled.is_set():
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
    if submitted.get("kind") == "cloudscraper_probe":
        target = submitted.get("target", "dmit")
        if not isinstance(target, str) or target not in CLOUDSCRAPER_TEST_TARGETS:
            raise ValueError("请选择支持的 cloudscraper 测试网站")
        fetcher = normalize_fetcher(submitted.get("fetcher", {}))
        fetcher.update(mode="cloudscraper", provider_modes={})
        value = {"products": [{"id": CLOUDSCRAPER_TEST_ID, "adapter": target, "name": "cloudscraper 访问测试", "target": target, "_request_probe": True}], "fetcher": fetcher}
    elif submitted.get("kind") == "isvoro_seckill":
        from seckill import TEST_PRODUCT_ID
        value = {"products": [{"id": TEST_PRODUCT_ID, "adapter": "isvoro", "provider": "Isvoro", "name": "限时秒杀", "_seckill": True}], "fetcher": normalize_fetcher(submitted.get("fetcher", {}))}
    else:
        value = validate_config({"products": [submitted.get("product")], "fetcher": submitted.get("fetcher", {})})
    # Test inputs may change request mode, but proxy secrets/routes always come
    # from saved server settings rather than the redacted browser draft.
    value["fetcher"]["egress"] = current_config().get("egress", {})
    from monitor import MonitorError
    try:
        task = StockTestTask(task_id, value["products"][0], value["fetcher"])
    except MonitorError as exc:
        raise ValueError(str(exc)) from None
    with TEST_LOCK:
        if task_id in TEST_TASKS:
            raise ValueError("测试任务编号已使用，请重新测试")
        if any(not task.done.is_set() for task in TEST_TASKS.values()):
            raise ValueError("已有商品测试正在执行，请先停止或等待完成")
        for old_id in list(TEST_TASKS)[:-19]:
            del TEST_TASKS[old_id]
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


ORDER_PROTOCOL = "hostwatch-unpaid-v1"

# Optional purchase preferences passed to a merchant's unpaid-order adapter.
# ``payment_mode`` remains the safety contract (always manual); these fields
# describe the invoice that the user will pay later on the merchant site.
ORDER_BILLING_CYCLES = {"monthly", "quarterly", "annually"}
ORDER_PAYMENT_METHODS = {"alipay", "credit_card", "usdt"}
BWH_PAYMENT_METHODS = {
    "alipay": "payssionalipaycn",
    "credit_card": "roudiappstripecheckout",
}

MERCHANT_LOGIN_ADAPTERS = {
    "vmiss": {"name": "VMISS", "url": "https://app.vmiss.com/login", "host": "app.vmiss.com", "username_field": "username"},
    "vps_soy": {"name": "VPS.SOY", "url": "https://www.vps.soy/login", "host": "www.vps.soy", "username_field": "email"},
    "narwhal": {"name": "独角鲸云", "url": "https://api.fuckip.me/api/v1/auth/login", "host": "api.fuckip.me", "kind": "api"},
    "bwh": {"name": "搬瓦工", "url": "https://bwh81.net/clientarea.php", "host": "bwh81.net", "username_field": "username"},
    "dmit": {"name": "DMIT", "url": "https://www.dmit.io/clientarea.php", "auth_probe_url": "https://www.dmit.io/clientarea.php?action=details", "host": "www.dmit.io", "username_field": "username"},
}
ISVORO_API_BASE_URL = "https://isvoro.com/api/v1"
ISVORO_LOGIN_URL = f"{ISVORO_API_BASE_URL}/auth/login"
ISVORO_PROFILE_URL = f"{ISVORO_API_BASE_URL}/auth/me"
ISVORO_CART_URL = f"{ISVORO_API_BASE_URL}/cart/items"
ISVORO_COUPON_URL = f"{ISVORO_API_BASE_URL}/cart/coupon"
ISVORO_CHECKOUT_URL = f"{ISVORO_API_BASE_URL}/checkout"
ISVORO_GATEWAYS_URL = f"{ISVORO_API_BASE_URL}/checkout/gateways"
ISVORO_MARKET_LIST_URL = f"{ISVORO_API_BASE_URL}/marketplace"
MERCHANT_LOGIN_ADAPTERS["isvoro"] = {
    "name": "Isvoro", "url": ISVORO_LOGIN_URL, "host": "isvoro.com",
    "username_field": "email", "kind": "api",
}
AKILE_LOGIN_URL = "https://api.akile.ai/api/v1/user/login"
AKILE_PROFILE_URL = "https://api.akile.ai/api/v1/user/info"
AKILE_STORE_URL = "https://api.akile.ai/api/v1/store/GetVpsStoreV3"
AKILE_ORDER_URL = "https://api.akile.ai/api/v1/store/makeOrder"
AKILE_ORDER_DETAIL_URL = "https://api.akile.ai/api/v1/order/GetOrderByOrderNo"
AKILE_ORDER_PAGE_BASE = "https://akile.ai/console/finance/order/"
AKILE_MARKET_LIST_URL = "https://api.akile.ai/api/v1/pushshop/GetPushProductList"
AKILE_MARKET_DETAIL_URL = "https://api.akile.ai/api/v1/pushshop/GetPushProduct"
NARWHAL_API_BASE_URL = "https://api.fuckip.me/api/v1"
NARWHAL_LOGIN_URL = f"{NARWHAL_API_BASE_URL}/auth/login"
NARWHAL_PROFILE_URL = f"{NARWHAL_API_BASE_URL}/user/me"
AKILE_DEBIAN_TEMPLATES = {
    "Debian 11": "063c07af-b200-40c0-839c-21864474saw",
    "Debian 12": "063c07af-b200-40c0-839c-1864474saw",
    "Debian 13": "063c07af-b200-40c0-839c-164474saw",
}
VMISS_HOST = "app.vmiss.com"
VMISS_BASE_URL = "https://app.vmiss.com/"
VMISS_INVOICES_URL = "https://app.vmiss.com/clientarea.php?action=invoices"
# WHMCS can make a newly-created invoice visible a few seconds after the
# checkout response.  Poll only the read-only invoice list; never retry the
# order POST itself.
VMISS_INVOICE_POLL_ATTEMPTS = 6
VMISS_INVOICE_POLL_DELAY = 0.6
VMISS_INVOICE_MAX_CANDIDATES = 24
VMISS_PAYMENT_METHODS = {"alipay": "motionpayalipay", "credit_card": "stripe", "usdt": "cryptomusgateway"}
MERCHANT_LOGIN_IDS = {
    "vmiss": "vmiss", "vps.soy": "vps_soy", "vps_soy": "vps_soy",
    "搬瓦工": "bwh", "bwh": "bwh", "bandwagonhost": "bwh",
    "akile": "akile", "isvoro": "isvoro", "dmit": "dmit", "narwhal": "narwhal", "独角鲸云": "narwhal",
}


def merchant_login_id(provider: object) -> str:
    value = str(provider or "").strip().casefold()
    return MERCHANT_LOGIN_IDS.get(value, "")


def _same_merchant_url(url: str, adapter: dict) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme == "https" and parsed.hostname == adapter["host"] and parsed.port in {None, 443} and not parsed.username and not parsed.password


def _merchant_session_key(adapter_id: str, account: dict, fetcher: dict | None = None) -> tuple[str, str, str, str, str]:
    """Keep auth state separate by merchant, account and egress identity."""
    route = str((fetcher or {}).get("egress_node_id") or (fetcher or {}).get("egress_proxy_url") or "direct")
    password_hash = hashlib.sha256(str(account.get("password", "")).encode("utf-8")).hexdigest()
    return (str(adapter_id), str(account.get("id", "")),
            str(account.get("username", "")).strip().casefold(), route, password_hash)


def _close_bwh_session(session) -> None:
    try:
        session.close()
    except (AttributeError, OSError):
        pass


def _copy_bwh_session(source, target) -> None:
    """Copy only the authenticated HTTP state into a task-owned session."""
    target.headers.update(dict(getattr(source, "headers", {}) or {}))
    target.cookies.update(getattr(source, "cookies", {}) or {})
    target.proxies = dict(getattr(source, "proxies", {}) or {})


def _copy_merchant_session(source, target) -> None:
    """Copy reusable authentication state, never share a mutable Session."""
    proxies = dict(getattr(target, "proxies", {}) or {})
    _copy_bwh_session(source, target)
    target.proxies = proxies
    if hasattr(source, "_hostwatch_akile_user_id"):
        target._hostwatch_akile_user_id = source._hostwatch_akile_user_id


def _merchant_auth_cache_paths() -> tuple[Path, Path]:
    return CONFIG_PATH.parent / MERCHANT_AUTH_CACHE_NAME, CONFIG_PATH.parent / MERCHANT_AUTH_KEY_NAME


def _merchant_auth_fernet(create: bool = False):
    from cryptography.fernet import Fernet

    cache_path, key_path = _merchant_auth_cache_paths()
    if key_path.is_symlink():
        raise ValueError("认证缓存密钥文件不能是符号链接")
    if not key_path.exists():
        if not create:
            return None
        key_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="merchant-auth-key-", suffix=".tmp", dir=key_path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(Fernet.generate_key())
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            try:
                os.link(temporary, key_path)
            except FileExistsError:
                pass
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    try:
        key = key_path.read_bytes()
    except OSError:
        return None
    try:
        return Fernet(key)
    except (TypeError, ValueError):
        if not create:
            return None
        key_path.unlink(missing_ok=True)
        return _merchant_auth_fernet(create=True)


def _persistent_session_record(key: tuple[str, ...], record: dict) -> dict:
    session = record.get("session")
    cookies = []
    for cookie in getattr(session, "cookies", []) or []:
        cookies.append({"name": cookie.name, "value": cookie.value, "domain": cookie.domain,
            "path": cookie.path, "expires": cookie.expires, "secure": cookie.secure,
            "rest": dict(cookie._rest or {})})
    return {"key": list(key), "expires_at": record.get("expires_at", 0),
            "headers": dict(getattr(session, "headers", {}) or {}), "cookies": cookies,
            "akile_user_id": getattr(session, "_hostwatch_akile_user_id", None)}


def _persist_merchant_sessions() -> None:
    cache_path, _ = _merchant_auth_cache_paths()
    if cache_path.is_symlink():
        raise ValueError("认证缓存文件不能是符号链接")
    fernet = _merchant_auth_fernet(create=bool(MERCHANT_AUTH_SESSIONS))
    if fernet is None:
        return
    now = time.time()
    records = [_persistent_session_record(key, record) for key, record in MERCHANT_AUTH_SESSIONS.items()
               if record.get("expires_at", 0) > now]
    if not records:
        cache_path.unlink(missing_ok=True)
        return
    encrypted = fernet.encrypt(json.dumps({"version": 1, "sessions": records}, ensure_ascii=False,
                                           allow_nan=False, separators=(",", ":")).encode("utf-8"))
    if len(encrypted) > MERCHANT_AUTH_CACHE_MAX_BYTES:
        raise ValueError("认证缓存超过大小限制，未写入磁盘")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="merchant-auth-cache-", suffix=".tmp", dir=cache_path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encrypted)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, cache_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_merchant_auth_cache() -> int:
    """Restore encrypted sessions after restart; each caller still revalidates them."""
    cache_path, _ = _merchant_auth_cache_paths()
    if cache_path.is_symlink() or not cache_path.exists():
        return 0
    try:
        raw = cache_path.read_bytes()
    except OSError:
        return 0
    try:
        if not raw or len(raw) > MERCHANT_AUTH_CACHE_MAX_BYTES:
            raise ValueError()
        fernet = _merchant_auth_fernet()
        if fernet is None:
            return 0
        payload = json.loads(fernet.decrypt(raw).decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("sessions"), list):
            raise ValueError()
        restored = {}
        for record in payload["sessions"]:
            if not isinstance(record, dict):
                continue
            key = record.get("key")
            expires_at = record.get("expires_at")
            headers, cookies = record.get("headers"), record.get("cookies")
            if (not isinstance(key, list) or len(key) != 5 or any(not isinstance(part, str) for part in key)
                    or not isinstance(expires_at, (int, float)) or expires_at <= time.time()
                    or not isinstance(headers, dict) or not isinstance(cookies, list)):
                continue
            session = requests.Session()
            session.trust_env = False
            session.headers.update({str(name): str(value) for name, value in headers.items()
                                    if isinstance(name, str) and isinstance(value, (str, int, float))})
            for cookie in cookies:
                if not isinstance(cookie, dict) or not isinstance(cookie.get("name"), str) or not isinstance(cookie.get("value"), str):
                    continue
                session.cookies.set(cookie["name"], cookie["value"], domain=str(cookie.get("domain") or ""),
                    path=str(cookie.get("path") or "/"), secure=bool(cookie.get("secure", False)),
                    expires=cookie.get("expires") if isinstance(cookie.get("expires"), int) else None,
                    rest=cookie.get("rest") if isinstance(cookie.get("rest"), dict) else {})
            user_id = record.get("akile_user_id")
            if isinstance(user_id, int) and user_id > 0:
                session._hostwatch_akile_user_id = user_id
            restored[tuple(key)] = {"session": session, "expires_at": float(expires_at)}
        with SECKILL_LOCK:
            old = list(MERCHANT_AUTH_SESSIONS.values())
            MERCHANT_AUTH_SESSIONS.clear()
            MERCHANT_AUTH_SESSIONS.update(restored)
        for record in old:
            _close_bwh_session(record.get("session"))
        return len(restored)
    except Exception:
        # Corrupt, undecryptable, or incompatible caches are discarded without
        # logging their encrypted contents; normal login/import remains usable.
        try:
            cache_path.unlink(missing_ok=True)
        except OSError:
            pass
        return 0


def _store_merchant_session(adapter_id: str, account: dict, fetcher: dict | None, session) -> None:
    if not isinstance(session, requests.Session):
        return
    cached = requests.Session()
    cached.trust_env = False
    _copy_merchant_session(session, cached)
    key = _merchant_session_key(adapter_id, account, fetcher)
    with SECKILL_LOCK:
        old = MERCHANT_AUTH_SESSIONS.pop(key, None)
        ttl = ISVORO_AUTH_SESSION_TTL if adapter_id == "isvoro" else BWH_AUTH_SESSION_TTL if adapter_id == "bwh" else MERCHANT_AUTH_SESSION_TTL
        MERCHANT_AUTH_SESSIONS[key] = {"session": cached, "expires_at": time.time() + ttl}
        _persist_merchant_sessions()
    if old:
        _close_bwh_session(old.get("session"))


def _get_merchant_session(adapter_id: str, account: dict, fetcher: dict | None = None):
    key = _merchant_session_key(adapter_id, account, fetcher)
    with SECKILL_LOCK:
        value = MERCHANT_AUTH_SESSIONS.get(key)
        if not value:
            return None
        if value.get("expires_at", 0) <= time.time():
            MERCHANT_AUTH_SESSIONS.pop(key, None)
            session = value.get("session")
        else:
            return value.get("session")
    _close_bwh_session(session)
    with SECKILL_LOCK:
        _persist_merchant_sessions()
    return None


def _renew_merchant_session(adapter_id: str, account: dict, fetcher: dict | None, session) -> None:
    """Extend a cached session only after its authentication was revalidated."""
    key = _merchant_session_key(adapter_id, account, fetcher)
    with SECKILL_LOCK:
        value = MERCHANT_AUTH_SESSIONS.get(key)
        if value and value.get("session") is session:
            ttl = ISVORO_AUTH_SESSION_TTL if adapter_id == "isvoro" else BWH_AUTH_SESSION_TTL if adapter_id == "bwh" else MERCHANT_AUTH_SESSION_TTL
            value["expires_at"] = time.time() + ttl
            _persist_merchant_sessions()


def _remove_merchant_session(adapter_id: str, account: dict, fetcher: dict | None = None) -> None:
    key = _merchant_session_key(adapter_id, account, fetcher)
    with SECKILL_LOCK:
        value = MERCHANT_AUTH_SESSIONS.pop(key, None)
        _persist_merchant_sessions()
    if value:
        _close_bwh_session(value.get("session"))


def _clear_merchant_sessions_for_account(account: dict) -> int:
    """Remove every in-memory merchant session for one saved account."""
    adapter_id = merchant_login_id(account.get("provider"))
    account_id = str(account.get("id", ""))
    username = str(account.get("username", "")).strip().casefold()
    removed = []
    with SECKILL_LOCK:
        for key in list(MERCHANT_AUTH_SESSIONS):
            if key[0] == adapter_id and key[1] == account_id and key[2] == username:
                removed.append(MERCHANT_AUTH_SESSIONS.pop(key))
        _persist_merchant_sessions()
    for value in removed:
        _close_bwh_session(value.get("session"))
    return len(removed)


def _merchant_authenticated(session, adapter: dict) -> bool:
    """Confirm a cached HTML login still reaches an authenticated page."""
    try:
        probe_url = adapter.get("auth_probe_url") or adapter["url"]
        page = session.get(probe_url, timeout=(5, 20), allow_redirects=False,
                           headers={**MERCHANT_BROWSER_HEADERS, "Referer": adapter["url"], "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1"})
    except (requests.RequestException, OSError):
        return False
    if not 200 <= page.status_code < 300 or not _same_merchant_url(page.url or probe_url, adapter):
        return False
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(page.content or page.text or "", "html.parser")
    if _merchant_additional_verification_required(page, soup, adapter):
        return False
    return bool(soup.find("a", href=re.compile(r"logout|clientarea\.php\?action=details", re.I)))


def _merchant_cached_session_reusable(fetcher: dict | None) -> bool:
    """Trust a short-lived browser-solved session without a second challenge probe."""
    return str((fetcher or {}).get("mode", "")).strip().casefold() in {"flaresolverr", "cloudscraper"}


def _login_form(response, username_field: str):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(response.content or response.text or "", "html.parser")
    for form in soup.find_all("form"):
        fields = {str(node.get("name", "")): node for node in form.find_all("input")}
        if username_field in fields and "password" in fields:
            return soup, form, fields
    return soup, None, {}


MERCHANT_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    # Do not advertise Brotli unless the runtime can decode it. requests may
    # otherwise expose compressed bytes to the HTML parser on minimal installs.
    "Accept-Encoding": "gzip, deflate",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
}


def _response_detail(response) -> str:
    ray = str(getattr(response, "headers", {}).get("CF-Ray", "")).strip()
    return f"（Cloudflare Ray ID: {ray[:100]}）" if re.fullmatch(r"[A-Za-z0-9-]{1,100}", ray) else ""


def _is_cloudflare_challenge(response) -> bool:
    """Recognize a Cloudflare challenge before attempting to parse a login form."""
    headers = getattr(response, "headers", {}) or {}
    if str(headers.get("cf-mitigated", "")).casefold() == "challenge":
        return True
    try:
        raw_body = getattr(response, "text", "") or ""
        body = raw_body.casefold() if isinstance(raw_body, str) else ""
    except (AttributeError, TypeError):
        body = ""
    return "just a moment" in body and "cloudflare" in body


def _merchant_additional_verification_required(page, soup, adapter: dict) -> bool:
    """Return true for an authenticated-looking page that still needs a code."""
    if adapter.get("name") != "搬瓦工":
        return False
    path = urlsplit(str(getattr(page, "url", "") or "")).path.casefold()
    if path.endswith("/browser_auth.php"):
        return True
    body = soup.get_text(" ", strip=True).casefold()
    return any(marker in body for marker in (
        "browser_auth_code", "email verification code", "email verification required",
        "邮箱验证码", "邮箱验证", "browser verification",
    ))


def _merchant_form_login(session, account: dict, adapter: dict, fetcher: dict | None = None) -> dict:
    from urllib.parse import urljoin
    from types import SimpleNamespace

    # A FlareSolverr page is rendered in a separate browser process. Copy its
    # short-lived clearance state into this dedicated login session so the
    # subsequent credential POST uses the same merchant-facing identity.
    if (fetcher or {}).get("mode") == "flaresolverr":
        try:
            from monitor import MonitorError, fetch_page
            browser_fetcher = dict(fetcher)
            browser_fetcher["_copy_browser_state"] = True
            body, final_url = fetch_page(session, adapter["url"], browser_fetcher, adapter["host"])
            response = SimpleNamespace(status_code=200, url=final_url, content=body.encode("utf-8"), text=body, headers={})
        except (MonitorError, OSError, requests.RequestException, ValueError) as exc:
            return {"status": "unknown", "message": f"{adapter['name']} 登录页访问失败：{str(exc)[:220]}"}
    elif (fetcher or {}).get("mode") == "cloudscraper":
        try:
            import cloudscraper
            options = (fetcher or {}).get("cloudscraper") or {}
            with cloudscraper.create_scraper(interpreter="native", delay=int(options.get("challenge_delay_seconds", 5)), solveDepth=1, doubleDown=False, debug=False) as scraper:
                egress.apply_session(scraper, fetcher)
                response = scraper.get(adapter["url"], timeout=(5, 25), allow_redirects=False, headers={**MERCHANT_BROWSER_HEADERS, "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1"})
                session.cookies.update(scraper.cookies)
                session.headers.update(scraper.headers)
        except ImportError:
            return {"status": "unknown", "message": "cloudscraper 依赖未安装，无法使用该请求方式检查登录"}
        except (requests.RequestException, RuntimeError, ValueError):
            return {"status": "unknown", "message": f"{adapter['name']} cloudscraper 登录页访问失败，请检查请求日志"}
    else:
        session.headers.update(MERCHANT_BROWSER_HEADERS)
        response = session.get(adapter["url"], timeout=(5, 20), allow_redirects=False,
                               headers={**MERCHANT_BROWSER_HEADERS, "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1"})

    if not 200 <= response.status_code < 300:
        if _is_cloudflare_challenge(response):
            return {"status": "unknown", "message": f"{adapter['name']} 登录页被 Cloudflare 安全挑战拦截（HTTP {response.status_code}）{_response_detail(response)}；挑战发生在登录表单之前，通常是当前出口 IP 风控或请求未完成浏览器 JavaScript。请为该商家选择 FlareSolverr，若仍失败请更换出口节点/IP；Chrome 无痕能打开不代表服务器出口也能通过"}
        return {"status": "unknown", "message": f"{adapter['name']} 登录页 GET 返回 HTTP {response.status_code}{_response_detail(response)}；脚本请求被官网拒绝，请检查出口节点/IP或浏览器请求方式"}
    soup, form, fields = _login_form(response, adapter["username_field"])
    if form is None:
        return {"status": "unknown", "message": "官网登录页结构已变化或需要安全验证，请到官网检查"}
    action = urljoin(response.url or adapter["url"], form.get("action") or response.url or adapter["url"])
    if not _same_merchant_url(action, adapter):
        return {"status": "unknown", "message": "官网登录表单跳转地址不符合商家域名，请到官网检查"}
    payload = {key: node.get("value", "") for key, node in fields.items() if key and node.get("type", "").casefold() == "hidden"}
    # These WHMCS-style login pages expose a first-party "Remember Me"
    # checkbox. Ask the vendor to issue its persistent login cookie for
    # password-based authentication; this is distinct from renewing a cookie.
    if adapter.get("name") in {"VMISS", "DMIT", "搬瓦工"}:
        remember_field = next((key for key, node in fields.items()
                               if key and key.casefold() in {"rememberme", "remember_me", "remember"}
                               and node.get("type", "").casefold() == "checkbox"), "")
        if remember_field:
            payload[remember_field] = next((node.get("value", "on") for key, node in fields.items()
                                            if key == remember_field), "on") or "on"
    payload[adapter["username_field"]] = account["username"]
    payload["password"] = account["password"]
    origin = f"{urlsplit(action).scheme}://{urlsplit(action).netloc}"
    submitted = session.post(action, data=payload, timeout=(5, 20), allow_redirects=False,
                             headers={**MERCHANT_BROWSER_HEADERS, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                                      "Content-Type": "application/x-www-form-urlencoded", "Origin": origin,
                                      "Referer": response.url or adapter["url"], "Sec-Fetch-Site": "same-origin",
                                      "Sec-Fetch-User": "?1"})
    if submitted.status_code == 401:
        return {"status": "invalid", "message": "官网拒绝登录：用户名或密码错误（HTTP 401）"}
    if submitted.status_code == 403:
        if _is_cloudflare_challenge(submitted):
            return {"status": "unknown", "message": f"{adapter['name']} 登录 POST 被 Cloudflare 安全挑战拦截（HTTP 403）{_response_detail(submitted)}；请使用 FlareSolverr 或更换出口节点/IP"}
        return {"status": "unknown", "message": f"{adapter['name']} 登录 POST 被官网拒绝（HTTP 403）{_response_detail(submitted)}；可能是安全策略、验证码或出口 IP，不代表账号密码错误"}
    if 300 <= submitted.status_code < 400:
        # Some WHMCS installations use a short same-site redirect chain after
        # login (for example dologin.php -> post-login -> browser_auth.php).
        # Follow only a bounded chain on the merchant origin; never follow a
        # payment or cross-origin redirect.
        checked = submitted
        for _ in range(6):
            location = checked.headers.get("Location", "")
            base_url = checked.url if isinstance(getattr(checked, "url", None), str) and checked.url else action
            destination = urljoin(base_url, location)
            if not location or not _same_merchant_url(destination, adapter):
                return {"status": "unknown", "message": "官网登录跳转无法安全确认，请到官网检查"}
            checked = session.get(destination, timeout=(5, 20), allow_redirects=False)
            if not 300 <= checked.status_code < 400:
                break
        if not 200 <= checked.status_code < 300:
            return {"status": "unknown", "message": "官网登录结果无法读取，请到官网检查"}
        page = checked
    elif 200 <= submitted.status_code < 300:
        page = submitted
    else:
        return {"status": "unknown", "message": f"官网登录返回 HTTP {submitted.status_code}，请到官网检查"}
    soup, form, _ = _login_form(page, adapter["username_field"])
    if _merchant_additional_verification_required(page, soup, adapter):
        return {"status": "unknown", "verification_required": True,
                "message": "搬瓦工要求邮箱验证码，已向账户邮箱发送验证码，请在此处提交最新验证码完成登录",
                "_verification_session": session, "_verification_url": str(getattr(page, "url", "") or adapter["url"])}
    if form is not None:
        body = soup.get_text(" ", strip=True).casefold()
        if any(marker in body for marker in ("incorrect", "invalid", "incorrect password", "用户名或密码", "密码错误", "账号或密码")):
            return {"status": "invalid", "message": "官网提示账户认证失败，请检查用户名和密码"}
        if any(marker in body for marker in ("captcha", "验证码", "two-factor", "two factor", "二次验证", "安全验证")):
            return {"status": "unknown", "message": "官网要求验证码或二次验证，请先到官网完成验证"}
        return {"status": "invalid", "message": "官网仍显示登录表单，账户认证未通过"}
    authenticated = soup.find("a", href=re.compile(r"logout|clientarea\.php\?action=details", re.I))
    if authenticated:
        return {"status": "valid", "message": "官网登录成功（不代表具有购买资格或库存）"}
    return {"status": "unknown", "message": "官网未返回可确认的登录状态，请到官网检查"}


def _merchant_submit_verification(session, account: dict, adapter: dict, challenge_url: str, code: str) -> dict:
    """Submit a BandwagonHost browser-auth email code in the original session."""
    from urllib.parse import urljoin

    page = session.get(challenge_url, timeout=(5, 20), allow_redirects=False,
                       headers={**MERCHANT_BROWSER_HEADERS, "Referer": adapter["url"], "Sec-Fetch-Site": "same-origin"})
    if not 200 <= page.status_code < 300 or not _same_merchant_url(page.url or challenge_url, adapter):
        return {"status": "unknown", "message": "搬瓦工验证码会话已失效，请重新检查账户以获取最新验证码"}
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(page.content or page.text or "", "html.parser")
    form = next((candidate for candidate in soup.find_all("form")
                 if candidate.find("input", attrs={"name": re.compile(r"browser_auth_code|verification_code|code", re.I)})), None)
    if form is None:
        authenticated = soup.find("a", href=re.compile(r"logout|clientarea\.php\?action=details", re.I))
        if authenticated:
            return {"status": "valid", "ok": True, "message": "搬瓦工官网登录成功（不代表具有购买资格或库存）"}
        return {"status": "unknown", "message": "搬瓦工验证码页面已变化，请重新检查账户"}
    fields = {str(node.get("name", "")): node for node in form.find_all("input") if node.get("name")}
    code_field = next((name for name in fields if re.search(r"browser_auth_code|verification_code|code", name, re.I)), "")
    if not code_field:
        return {"status": "unknown", "message": "搬瓦工验证码字段无法识别，请重新检查账户"}
    action = urljoin(page.url or challenge_url, form.get("action") or page.url or challenge_url)
    if not _same_merchant_url(action, adapter):
        return {"status": "unknown", "message": "搬瓦工验证码提交地址不符合商家域名，请重新检查账户"}
    payload = {key: node.get("value", "") for key, node in fields.items()
               if key != code_field and node.get("type", "").casefold() == "hidden"}
    payload[code_field] = code
    origin = f"{urlsplit(action).scheme}://{urlsplit(action).netloc}"
    submitted = session.post(action, data=payload, timeout=(5, 20), allow_redirects=False,
                             headers={**MERCHANT_BROWSER_HEADERS, "Content-Type": "application/x-www-form-urlencoded",
                                      "Origin": origin, "Referer": page.url or challenge_url, "Sec-Fetch-Site": "same-origin"})
    if submitted.status_code in {401, 403}:
        return {"status": "invalid", "message": "搬瓦工验证码无效或会话被拒绝，请确认使用最新邮件中的验证码"}
    if not 200 <= submitted.status_code < 400:
        return {"status": "unknown", "message": f"搬瓦工验证码提交返回 HTTP {submitted.status_code}，请重新检查账户"}
    checked = submitted
    for _ in range(6):
        location = checked.headers.get("Location", "")
        if not location:
            break
        destination = urljoin(checked.url or action, location)
        if not _same_merchant_url(destination, adapter):
            return {"status": "unknown", "message": "搬瓦工验证码后的跳转无法安全确认，请到官网检查"}
        checked = session.get(destination, timeout=(5, 20), allow_redirects=False)
        if not 300 <= checked.status_code < 400:
            break
    if not 200 <= checked.status_code < 300:
        return {"status": "unknown", "message": "搬瓦工验证码后的登录结果无法读取，请到官网检查"}
    checked_soup = BeautifulSoup(checked.content or checked.text or "", "html.parser")
    if checked_soup.find("a", href=re.compile(r"logout|clientarea\.php\?action=details", re.I)):
        return {"status": "valid", "ok": True, "message": "搬瓦工账户认证正常（邮箱验证码已提交）"}
    body = checked_soup.get_text(" ", strip=True).casefold()
    if any(marker in body for marker in ("invalid", "incorrect", "无效", "错误")):
        return {"status": "invalid", "message": "搬瓦工验证码无效，请确认使用最新邮件中的验证码"}
    if _merchant_additional_verification_required(checked, checked_soup, adapter):
        return {"status": "invalid", "message": "搬瓦工验证码未通过，请确认使用最新邮件中的验证码"}
    return {"status": "unknown", "message": "搬瓦工未返回可确认的登录状态，请到官网检查"}


def _isvoro_profile_data(payload: object) -> dict | None:
    if not isinstance(payload, dict) or payload.get("code") not in {0, "0"}:
        return None
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("user"), dict):
        data = data["user"]
    return data if isinstance(data, dict) else None


def _session_cookie(session, name: str) -> str:
    try:
        cookies = getattr(session, "cookies", ())
        for cookie in cookies:
            if getattr(cookie, "name", "") == name:
                return str(getattr(cookie, "value", "") or "")
    except TypeError:
        return ""
    return ""


def _isvoro_profile_authenticated(session, account: dict) -> bool:
    try:
        response = session.get(ISVORO_PROFILE_URL, timeout=(5, 20), allow_redirects=False,
                               headers={"Accept": "application/json", "Referer": "https://isvoro.com/"})
        if not 200 <= response.status_code < 300:
            return False
        profile = _isvoro_profile_data(response.json())
    except (requests.RequestException, OSError, ValueError, TypeError, AttributeError):
        return False
    if profile is None:
        return False
    email = str(profile.get("email") or profile.get("username") or "").strip().casefold()
    expected = str(account.get("username", "")).strip().casefold()
    return bool(email and expected and email == expected)


def _isvoro_refresh_session(session) -> bool:
    """Refresh an expired access cookie with Isvoro's longer-lived refresh cookie."""
    csrf = _session_cookie(session, "pmt_csrf")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if csrf:
        headers["X-CSRF-Token"] = csrf
    try:
        response = session.post(f"{ISVORO_API_BASE_URL}/auth/refresh", timeout=(5, 20), allow_redirects=False, headers=headers)
    except (requests.RequestException, OSError):
        return False
    return 200 <= response.status_code < 300


def _isvoro_authenticated(session, account: dict) -> bool:
    """Validate an Isvoro session and refresh its access cookie when needed."""
    if _isvoro_profile_authenticated(session, account):
        return True
    return _isvoro_refresh_session(session) and _isvoro_profile_authenticated(session, account)


def _import_merchant_session_cookie(session, raw_cookie: object, domain: str) -> bool:
    """Import a manually copied merchant Cookie header into a task session.

    The value is transient: callers must never put it into the persisted account
    configuration. The caller supplies an allowlisted merchant domain, so the
    pasted header cannot grant access to an unrelated host.
    """
    value = str(raw_cookie or "").strip()
    if value.lower().startswith("cookie:"):
        value = value[7:].strip()
    if not value or len(value) > 16_384 or any(ord(char) < 32 for char in value):
        return False
    found = False
    for part in value.split(";"):
        name, separator, cookie_value = part.strip().partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z0-9_!#$%&'*+.^`|~-]{1,128}", name.strip()):
            continue
        cookie_value = cookie_value.strip()
        if len(cookie_value) > 8192 or any(ord(char) < 32 for char in cookie_value):
            return False
        session.cookies.set(name.strip(), cookie_value, domain=domain, path="/")
        found = True
    return found


def _import_isvoro_session_cookie(session, raw_cookie: object) -> bool:
    return _import_merchant_session_cookie(session, raw_cookie, "isvoro.com")


def _import_bwh_session_cookie(session, raw_cookie: object) -> bool:
    return _import_merchant_session_cookie(session, raw_cookie, "bwh81.net")


def _import_dmit_session_cookie(session, raw_cookie: object) -> bool:
    # DMIT uses www.dmit.io for its client area; keep imported credentials
    # scoped to the merchant's own domain and its subdomains.
    return _import_merchant_session_cookie(session, raw_cookie, ".dmit.io")


def _merchant_cookie_names(raw_cookie: object) -> list[str]:
    value = str(raw_cookie or "").strip()
    if value.lower().startswith("cookie:"):
        value = value[7:].strip()
    names = []
    for part in value.split(";"):
        name, separator, _ = part.strip().partition("=")
        if separator and re.fullmatch(r"[A-Za-z0-9_!#$%&'*+.^`|~-]{1,128}", name.strip()):
            names.append(name.strip())
    return names


def _isvoro_login(session, account: dict) -> dict:
    """Log in through Isvoro's API when a captcha token is available."""
    payload = {"email": account["username"], "password": account["password"]}
    captcha_token = str(account.get("_captcha_token", "")).strip()
    captcha_provider = str(account.get("_captcha_provider", "hcaptcha")).strip()
    if captcha_token:
        payload.update(captcha_token=captcha_token, captcha_provider=captcha_provider or "hcaptcha")
    try:
        response = session.post(ISVORO_LOGIN_URL, json=payload, timeout=(5, 20), allow_redirects=False,
                                headers={"Accept": "application/json", "Content-Type": "application/json", "Referer": "https://isvoro.com/login"})
        data = response.json()
    except (requests.RequestException, OSError, ValueError, TypeError, AttributeError):
        return {"status": "unknown", "message": "Isvoro 登录请求失败，请检查出口节点或官网状态"}
    message = str(data.get("message", "")) if isinstance(data, dict) else ""
    if response.status_code in {401, 403} or any(marker in message.casefold() for marker in ("captcha", "hcaptcha", "人机", "验证")):
        return {"status": "unknown", "captcha_required": True,
                "message": "Isvoro 要求人机验证；请先在官网手动完成 hCaptcha，再导入该浏览器的 Cookie"}
    if not 200 <= response.status_code < 300 or _isvoro_profile_data(data) is None:
        return {"status": "invalid" if response.status_code in {400, 422} else "unknown",
                "message": "Isvoro 账户认证失败，请检查邮箱和密码" if response.status_code in {400, 422} else "Isvoro 登录响应无法确认，请到官网检查"}
    if not _isvoro_authenticated(session, account):
        return {"status": "unknown", "message": "Isvoro 登录成功但无法确认账户身份，请到官网检查"}
    return {"status": "valid", "message": "Isvoro 账户认证正常（不代表具有购买资格或库存）"}


def _akile_login(session, account: dict) -> dict:
    response = session.post(AKILE_LOGIN_URL, json={"email": account["username"], "password": account["password"],
        "token": "", "email_code": "", "new_password": "", "confirm": "", "verifyCode": ""},
        timeout=(5, 20), allow_redirects=False)
    if response.status_code in {401, 403}:
        return {"status": "invalid", "message": "Akile 拒绝登录，账户或密码错误；也可能需要官网安全验证"}
    if not 200 <= response.status_code < 300:
        return {"status": "unknown", "message": f"Akile 登录 API 返回 HTTP {response.status_code}，请到官网检查"}
    try:
        payload = response.json()
    except ValueError:
        return {"status": "unknown", "message": "Akile 登录 API 响应无法识别，请到官网检查"}
    if not isinstance(payload, dict):
        return {"status": "unknown", "message": "Akile 登录 API 响应格式无效，请到官网检查"}
    message = str(payload.get("status_msg", "")).casefold()
    if payload.get("status_code") not in {0, 200, "0", "200"}:
        if any(marker in message for marker in ("password", "username", "email", "密码", "账号", "账户")):
            return {"status": "invalid", "message": "Akile 账户认证失败，请检查邮箱和密码"}
        if any(marker in message for marker in ("two", "verification", "passkey", "captcha", "二步", "验证", "验证码")):
            return {"status": "unknown", "message": "Akile 要求邮箱验证、二步验证、Passkey 或验证码，请到官网完成"}
        return {"status": "unknown", "message": "Akile 未确认账户登录，请到官网检查"}
    data = payload.get("data")
    if not isinstance(data, dict):
        return {"status": "unknown", "message": "Akile 未返回账户登录凭据，请到官网检查"}
    if data.get("email_verification_required") or data.get("passkey_required") or not data.get("token"):
        return {"status": "unknown", "message": "Akile 要求邮箱验证、Passkey 或额外验证，请到官网完成"}
    token = str(data["token"])
    if len(token) > 4096 or any(ord(char) < 33 for char in token):
        return {"status": "unknown", "message": "Akile 返回的登录凭据格式无效，请到官网检查"}
    profile_response = session.get(AKILE_PROFILE_URL, headers={"Authorization": token}, timeout=(5, 20), allow_redirects=False)
    if not 200 <= profile_response.status_code < 300:
        return {"status": "unknown", "message": "Akile 登录成功但无法读取账户身份，请到官网检查"}
    try:
        profile_payload = profile_response.json()
    except ValueError:
        return {"status": "unknown", "message": "Akile 账户信息响应无法识别，请到官网检查"}
    profile = profile_payload.get("data") if isinstance(profile_payload, dict) else None
    email = str(profile.get("email", "")).strip().casefold() if isinstance(profile, dict) else ""
    if not isinstance(profile_payload, dict) or profile_payload.get("status_code") not in {0, 200, "0", "200"} or not email or email != account["username"].strip().casefold():
        return {"status": "unknown", "message": "Akile 返回的账户身份未能与填写邮箱匹配，请到官网检查"}
    user_id = profile.get("id") or profile.get("user_id") or profile.get("uid") if isinstance(profile, dict) else None
    if type(user_id) is int and user_id > 0:
        session._hostwatch_akile_user_id = user_id
    elif isinstance(user_id, str) and re.fullmatch(r"[1-9][0-9]{0,18}", user_id.strip()):
        session._hostwatch_akile_user_id = int(user_id.strip())
    else:
        # Keep login usable for account checks, but the native order adapter
        # will refuse to submit until the API returns the required user ID.
        session._hostwatch_akile_user_id = None
    session.headers["Authorization"] = token
    return {"status": "valid", "message": "Akile 账户认证正常（不代表具有购买资格或库存）"}


def _narwhal_profile_data(payload: object) -> dict | None:
    if not isinstance(payload, dict) or payload.get("code") not in {0, "0"}:
        return None
    data = payload.get("data")
    return data if isinstance(data, dict) else None


def _narwhal_login(session, account: dict) -> dict:
    """Log in to Narwhal Cloud and verify the returned account identity."""
    try:
        response = session.post(NARWHAL_LOGIN_URL,
                                json={"email": account["username"], "password": account["password"]},
                                timeout=(5, 20), allow_redirects=False,
                                headers={"Accept": "application/json", "Content-Type": "application/json"})
        payload = response.json()
    except (requests.RequestException, OSError, ValueError, TypeError, AttributeError):
        return {"status": "unknown", "message": "独角鲸云登录请求失败，请检查出口节点或官网状态"}
    if response.status_code in {401, 403}:
        return {"status": "invalid", "message": "独角鲸云账户认证失败，请检查邮箱和密码"}
    if not 200 <= response.status_code < 300 or _narwhal_profile_data(payload) is None:
        message = str(payload.get("msg", "")) if isinstance(payload, dict) else ""
        if any(marker in message.casefold() for marker in ("password", "email", "credential", "密码", "邮箱", "账户")):
            return {"status": "invalid", "message": "独角鲸云账户认证失败，请检查邮箱和密码"}
        return {"status": "unknown", "message": "独角鲸云登录响应无法确认，请到官网检查"}
    data = _narwhal_profile_data(payload) or {}
    token = data.get("token")
    if not isinstance(token, str) or not 1 <= len(token) <= 4096 or any(ord(char) < 33 for char in token):
        return {"status": "unknown", "message": "独角鲸云未返回有效登录凭据，请到官网检查"}
    session.headers["Authorization"] = f"Bearer {token}"
    if not _narwhal_authenticated(session, account):
        return {"status": "unknown", "message": "独角鲸云登录成功但无法确认账户身份，请到官网检查"}
    return {"status": "valid", "message": "独角鲸云账户认证正常（不代表具有购买资格或库存）"}


def _narwhal_authenticated(session, account: dict) -> bool:
    token = str(getattr(session, "headers", {}).get("Authorization", "")).strip()
    if not token.startswith("Bearer ") or len(token) > 4096 or any(ord(char) < 33 for char in token[7:]):
        return False
    try:
        response = session.get(NARWHAL_PROFILE_URL, headers={"Authorization": token},
                               timeout=(5, 20), allow_redirects=False)
        if not 200 <= response.status_code < 300:
            return False
        payload = response.json()
    except (requests.RequestException, OSError, ValueError, TypeError, AttributeError):
        return False
    profile = _narwhal_profile_data(payload)
    email = str(profile.get("email", "")).strip().casefold() if isinstance(profile, dict) else ""
    return bool(email and email == str(account.get("username", "")).strip().casefold())


def _akile_authenticated(session, account: dict) -> bool:
    """Validate a cached Akile token and refresh the user id used by orders."""
    token = str(getattr(session, "headers", {}).get("Authorization", "")).strip()
    if not token or len(token) > 4096 or any(ord(char) < 33 for char in token):
        return False
    try:
        response = session.get(AKILE_PROFILE_URL, headers={"Authorization": token},
                               timeout=(5, 20), allow_redirects=False)
        if not 200 <= response.status_code < 300:
            return False
        payload = response.json()
    except (requests.RequestException, OSError, ValueError, TypeError, AttributeError):
        return False
    profile = payload.get("data") if isinstance(payload, dict) else None
    email = str(profile.get("email", "")).strip().casefold() if isinstance(profile, dict) else ""
    if (not isinstance(payload, dict) or payload.get("status_code") not in {0, 200, "0", "200"}
            or not email or email != str(account.get("username", "")).strip().casefold()):
        return False
    user_id = profile.get("id") or profile.get("user_id") or profile.get("uid") if isinstance(profile, dict) else None
    if type(user_id) is int and user_id > 0:
        session._hostwatch_akile_user_id = user_id
    elif isinstance(user_id, str) and re.fullmatch(r"[1-9][0-9]{0,18}", user_id.strip()):
        session._hostwatch_akile_user_id = int(user_id.strip())
    else:
        session._hostwatch_akile_user_id = None
    return True


def order_json(response) -> dict:
    # Never follow checkout/payment redirects or mistake a login HTML page for success.
    if not 200 <= response.status_code < 300:
        raise ValueError(f"接口返回 HTTP {response.status_code}（不跟随跳转）")
    try:
        data = response.json()
    except ValueError:
        raise ValueError("接口未返回 JSON，普通登录/结算网页尚未适配") from None
    if not isinstance(data, dict):
        raise ValueError("接口响应格式无效")
    return data


def _isvoro_payment_gateway(session, payment_method: str) -> int:
    response = session.get(ISVORO_GATEWAYS_URL, timeout=(5, 20), allow_redirects=False,
                           headers={"Accept": "application/json"})
    payload = order_json(response)
    gateways = payload.get("data")
    if not isinstance(gateways, list):
        raise ValueError("Isvoro 支付方式响应无效，未提交订单")
    wanted = str(payment_method or "alipay").strip().casefold()
    for gateway in gateways:
        if not isinstance(gateway, dict) or gateway.get("enabled") is False:
            continue
        name = str(gateway.get("name") or "").casefold()
        extension = str(gateway.get("extension") or "").casefold()
        if wanted in {"alipay", "支付宝"} and ("支付宝" in name or extension in {"antom", "alipay"}):
            try:
                return int(gateway["id"])
            except (KeyError, TypeError, ValueError):
                break
    raise ValueError("Isvoro 未提供可用的支付宝支付方式，未提交订单")


def _isvoro_plan(product, item: dict) -> tuple[int, dict]:
    details = getattr(product, "match_details", None) or {}
    plans = [plan for plan in details.get("plans", []) if isinstance(plan, dict) and plan.get("hidden") is not True]
    if not plans:
        raise ValueError("Isvoro 商品没有可用计费周期，未提交订单")
    cycle = str(item.get("billing_cycle") or "monthly").strip().casefold()
    wanted = {"monthly": (1, "month"), "quarterly": (3, "month"), "annually": (1, "year")}.get(cycle)
    selected = next((plan for plan in plans if wanted and int(plan.get("billing_period", 0) or 0) == wanted[0]
                     and str(plan.get("billing_unit", "")).casefold() == wanted[1]), None) if wanted else None
    selected = selected or next((plan for plan in plans if int(plan.get("billing_period", 0) or 0) == 1
                                 and str(plan.get("billing_unit", "")).casefold() == "month"), plans[0])
    try:
        return int(selected["id"]), selected
    except (KeyError, TypeError, ValueError):
        raise ValueError("Isvoro 商品计划 ID 无效，未提交订单") from None


def isvoro_order(session, item: dict, account: dict, product, url: str, key: str | None = None) -> dict:
    """Create and verify one Isvoro unpaid invoice without initiating payment."""
    csrf = _session_cookie(session, "pmt_csrf")
    request_headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if csrf:
        request_headers["X-CSRF-Token"] = csrf
    product_id = str((getattr(product, "match_details", None) or {}).get("product_id") or getattr(product, "id", "")).strip()
    if not re.fullmatch(r"[1-9][0-9]{0,18}", product_id):
        raise ValueError("Isvoro 商品 ID 无效，未提交订单")
    plan_id, plan = _isvoro_plan(product, item)
    checkout_fields = (getattr(product, "match_details", None) or {}).get("checkout_fields") or []
    os_field = next((field for field in checkout_fields if isinstance(field, dict) and field.get("name") == "os"), None)
    os_value = "22"
    if os_field and os_field.get("required"):
        options = os_field.get("options") or []
        if not any(str(option.get("value")) == os_value for option in options if isinstance(option, dict)):
            raise ValueError("Isvoro 商品不支持 Debian12，未提交订单")
    cart_payload = {"product_id": int(product_id), "plan_id": plan_id, "quantity": 1,
                    "checkout_config": {"os": os_value}}
    added = order_json(session.post(ISVORO_CART_URL, json=cart_payload, timeout=(5, 20), allow_redirects=False,
                                    headers=request_headers))
    if added.get("code") not in {0, "0"}:
        raise ValueError(str(added.get("message") or "Isvoro 加入购物车失败，未提交订单"))
    coupon = str(item.get("coupon_code") or "").strip()
    if coupon:
        coupon_response = order_json(session.post(ISVORO_COUPON_URL, json={"code": coupon}, timeout=(5, 20), allow_redirects=False,
                                                  headers=request_headers))
        if coupon_response.get("code") not in {0, "0"}:
            raise ValueError("Isvoro 优惠码应用失败，未提交订单")
    gateway_id = _isvoro_payment_gateway(session, str(item.get("payment_method") or "alipay"))
    checked_out = order_json(session.post(ISVORO_CHECKOUT_URL, json={"gateway_id": gateway_id}, timeout=(5, 20), allow_redirects=False,
                                          headers=request_headers))
    if checked_out.get("code") not in {0, "0"}:
        raise ValueError(str(checked_out.get("message") or "Isvoro 结算失败，未提交订单"))
    data = checked_out.get("data") if isinstance(checked_out.get("data"), dict) else {}
    invoice_id = data.get("invoice_id") or data.get("id") or data.get("order_id")
    if not isinstance(invoice_id, (str, int)) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(invoice_id)):
        raise ValueError("Isvoro 结算响应缺少账单号，未确认订单")
    invoice_id = str(invoice_id)
    invoice_url = f"https://isvoro.com/invoices/{invoice_id}"
    return {"order_id": invoice_id, "invoice_url": invoice_url, "payment_status": "unpaid",
            "plan_id": plan_id, "os": "Debian12", "payment_method": "alipay"}


def _dmit_form_values(form) -> dict:
    from bs4 import BeautifulSoup
    values = {}
    for node in form.find_all(["input", "select", "textarea"]):
        name = str(node.get("name", "")).strip()
        if not name:
            continue
        if node.name == "select":
            option = node.find("option", selected=True) or node.find("option")
            if option is not None:
                values[name] = str(option.get("value", ""))
            continue
        typ = str(node.get("type", "text")).casefold()
        if typ in {"submit", "button", "image", "file", "radio", "checkbox"}:
            if typ in {"checkbox", "radio"} and node.has_attr("checked"):
                values[name] = str(node.get("value", "on"))
            continue
        values[name] = str(node.get("value", ""))
    return values


class DmitOrderFailure(ValueError):
    """DMIT flow failure with an explicit marker for final checkout submission."""

    def __init__(self, message: str, checkout_request_sent: bool = False):
        super().__init__(message)
        self.checkout_request_sent = checkout_request_sent


def _dmit_page_errors(response) -> str:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(response.content or response.text or "", "html.parser")
    def visible(node):
        current = node
        while current is not None and getattr(current, "name", None):
            classes = {str(value).casefold() for value in (current.get("class") or [])}
            style = re.sub(r"\s+", "", str(current.get("style", "")).casefold())
            if ("hidden" in classes or "hide" in classes or current.get("hidden") is not None
                    or str(current.get("aria-hidden", "")).casefold() == "true"
                    or "display:none" in style or "visibility:hidden" in style):
                return False
            current = current.parent
        return True
    errors = [" ".join(node.get_text(" ", strip=True).split()) for node in
              soup.select(".alert-danger, .alert-error, .error, .validation-error, .field-error") if visible(node)]
    errors = [value for value in errors if value and value.casefold() not in {"error", "errors"}]
    return "；".join(dict.fromkeys(errors))[:320]


def _dmit_invoice_id(response_url: str, body_text: str) -> str:
    """Extract a DMIT invoice ID from redirects, HTML links, or JSON."""
    parsed = urlsplit(str(response_url or ""))
    invoice_id = (parse_qs(parsed.query).get("id") or parse_qs(parsed.query).get("invoiceid") or [""])[0]
    if re.fullmatch(r"[1-9][0-9]{2,20}", str(invoice_id)):
        return str(invoice_id)
    body = str(body_text or "")
    for pattern in (
        r"(?:Invoice|账单)\s*(?:#|No\.?|编号)?\s*([0-9]{3,20})",
        r"viewinvoice\.php\?[^\"'\s>]*\bid=([1-9][0-9]{2,20})",
        r"[\"']invoice(?:id|_id)[\"']\s*[:=]\s*[\"']?([1-9][0-9]{2,20})",
    ):
        match = re.search(pattern, body, re.I)
        if match:
            return match.group(1)
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        data = None
    if isinstance(data, dict):
        for key in ("invoiceid", "invoice_id", "invoice", "id"):
            value = str(data.get(key, ""))
            if re.fullmatch(r"[1-9][0-9]{2,20}", value):
                return value
    return ""


def _dmit_response_diagnostic(response) -> str:
    """Return a compact, non-sensitive shape summary for failed DMIT checkout."""
    from bs4 import BeautifulSoup
    raw = getattr(response, "content", b"") or getattr(response, "text", "") or ""
    soup = BeautifulSoup(raw, "html.parser")
    title = " ".join(soup.title.get_text(" ", strip=True).split())[:80] if soup.title else ""
    forms = []
    for form in soup.find_all("form")[:8]:
        action = urlsplit(urljoin(getattr(response, "url", "") or "https://www.dmit.io/", str(form.get("action") or "")))
        fields = {str(node.get("name", "")).strip() for node in form.find_all(["input", "select", "textarea"]) if node.get("name")}
        form_type = ("invoice" if "viewinvoice.php" in action.path else
                     "checkout" if "checkout" in action.query or "submit" in fields else
                     "login" if {"username", "password"}.issubset(fields) else "other")
        safe_fields = sorted(name for name in fields if name.casefold() in {
            "paymentmethod", "accepttos", "accept_terms", "tos", "submit", "custtype", "token"
        })
        form_id = re.sub(r"[^A-Za-z0-9_-]", "", str(form.get("id") or ""))[:32]
        forms.append(form_type + (f"#{form_id}" if form_id else "")
                     + (f"[{','.join(safe_fields)}]" if safe_fields else ""))
    parsed = urlsplit(str(getattr(response, "url", "") or ""))
    query_keys = ",".join(sorted(parse_qs(parsed.query).keys()))
    def visible(node):
        current = node
        while current is not None and getattr(current, "name", None):
            classes = {str(value).casefold() for value in (current.get("class") or [])}
            style = re.sub(r"\s+", "", str(current.get("style", "")).casefold())
            if ("hidden" in classes or "hide" in classes or current.get("hidden") is not None
                    or str(current.get("aria-hidden", "")).casefold() == "true"
                    or "display:none" in style or "visibility:hidden" in style):
                return False
            current = current.parent
        return True
    feedback = []
    for node in soup.select(".alert, [role='alert'], .help-block, .text-danger, .field-error, .validation-error")[:10]:
        if not visible(node):
            continue
        message = " ".join(node.get_text(" ", strip=True).split())
        if message and message.casefold() not in {"error", "errors"}:
            feedback.append(message[:100])
    errors = "；".join(dict.fromkeys(feedback))[:240]
    if not errors:
        errors = _dmit_page_errors(response)
    return (f"HTTP {getattr(response, 'status_code', 0)} {parsed.path or '/'}"
            + (f"?keys={query_keys}" if query_keys else "")
            + (f" title={title!r}" if title else "")
            + f" forms={','.join(forms) or 'none'}"
            + (f" feedback={errors!r}" if errors else " visible_errors=no"))[:420]


def _dmit_checkout_payload_diagnostic(payload: dict) -> str:
    """Summarize only non-secret checkout controls for a failed invoice response."""
    allowed = ("paymentmethod", "accepttos", "accept_terms", "tos", "custtype", "submit")
    fields = []
    for name in allowed:
        if name in payload:
            value = re.sub(r"[^A-Za-z0-9_-]", "", str(payload[name]))[:32]
            fields.append(f"{name}={value or '<empty>'}")
    if "phonenumber" in payload:
        fields.append("phonenumber=" + ("blank" if not str(payload["phonenumber"]).strip() else "filled"))
    return ",".join(fields) or "no_safe_fields"


def _dmit_generated_root_password() -> str:
    """Match DMIT's browser-generated password policy for the hidden field."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%^&*"
    parts = [secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ"), secrets.choice("abcdefghijkmnopqrstuvwxyz"),
             secrets.choice("23456789"), secrets.choice("!@#$%^&*")]
    parts.extend(secrets.choice(alphabet) for _ in range(12))
    secrets.SystemRandom().shuffle(parts)
    return "".join(parts)


def _dmit_generated_hostname() -> str:
    """Match DMIT's browser-generated Linux hostname format."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    return "DMIT-" + "".join(secrets.choice(alphabet) for _ in range(10))


DMIT_OS_VALUES = {
    "debian 13": "472",
    "debian13": "472",
    "debian 12": "471",
    "debian12": "471",
    "debian 11": "272",
    "debian11": "272",
    "debian 10": "623",
    "debian10": "623",
}
DMIT_OS_LABELS = {value: label for label, value in {
    "Debian 13": "472", "Debian 12": "471", "Debian 11": "272", "Debian 10": "623",
}.items()}


def _dmit_config_values(form, item: dict) -> tuple[dict, str]:
    """Build the DMIT product configuration from the rendered form.

    The VM template IDs are product configuration values, not stable product
    IDs.  Prefer the form's current selection (DMIT currently defaults to
    Debian 13), while honoring an explicitly requested Debian version when
    that option is present in the form.
    """
    config = _dmit_form_values(form)
    requested = re.sub(r"\s+x64$", "", str((item or {}).get("operating_system", "")).strip(), flags=re.I).casefold()
    options = {}
    for node in form.select('[data-name="configoption[29]"][data-value][data-version]'):
        version = str(node.get("data-version", "")).strip().casefold()
        value = str(node.get("data-value", "")).strip()
        if value and version:
            options[f"debian {version}"] = value
            options[f"debian{version}"] = value
    selected = str(config.get("configoption[29]", "")).strip()
    if requested:
        selected = options.get(requested) or DMIT_OS_VALUES.get(requested) or selected
    if not selected:
        selected = DMIT_OS_VALUES["debian 13"]
    config["configoption[29]"] = selected
    # The current DMIT form uses customfield[2278] for the optional SSH key.
    # Keep it empty so DMIT generates a key after the order, matching the
    # browser flow used for the verified manual order.
    config["customfield[2278]"] = ""
    config.pop("customfield[1025]", None)
    return config, DMIT_OS_LABELS.get(selected, "Debian 13")


def _dmit_checkout_values(form) -> dict:
    values = _dmit_form_values(form)
    # Match browser form submission semantics: disabled controls are not sent.
    # A few DMIT forms render disabled billing-profile selects (notably
    # `country`) with a populated value. Keep duplicate names when at least one
    # enabled control with that name remains, as with the two `cccvv` controls.
    controls = form.find_all(["input", "select", "textarea"])
    for node in controls:
        name = str(node.get("name", "")).strip()
        if name and node.has_attr("disabled") and not any(
                str(other.get("name", "")).strip() == name and not other.has_attr("disabled")
                for other in controls):
            values.pop(name, None)
    # WHMCS renders blank billing-profile controls on checkout even for an
    # authenticated customer. Sending those blanks overwrites the account's
    # saved defaults and makes DMIT reject the invoice. Omit empty profile
    # values so the logged-in account remains the source of truth.
    for name in ("companyname", "address1", "address2", "city", "postcode", "state",
                 "country", "loginemail", "loginpassword"):
        if not str(values.get(name, "")).strip():
            values.pop(name, None)
    # Preserve repeated successful controls exactly as browser form encoding
    # does. DMIT currently renders a hidden `nostore` field plus a checked
    # `nostore` checkbox; collapsing them changes the checkout request.
    repeated = {}
    for node in controls:
        name = str(node.get("name", "")).strip()
        if not name or node.has_attr("disabled"):
            continue
        typ = str(node.get("type", "text")).casefold()
        if typ in {"submit", "button", "image", "file"}:
            continue
        if typ in {"radio", "checkbox"} and not node.has_attr("checked"):
            continue
        if node.name == "select":
            selected = node.find_all("option", selected=True) or node.find_all("option")[:1]
            entries = [str(option.get("value", "")) for option in selected]
        else:
            entries = [str(node.get("value", ""))]
        repeated.setdefault(name, []).extend(entries)
    for name, entries in repeated.items():
        if len(entries) > 1:
            values[name] = entries
    return values


def _dmit_checkout_form(soup):
    """Choose the actual checkout form when the page also has promo forms."""
    candidates = []
    for index, form in enumerate(soup.find_all("form")):
        action = str(form.get("action") or "").casefold()
        if "checkout" not in action:
            continue
        names = {str(node.get("name", "")).strip().casefold()
                 for node in form.find_all(["input", "select", "textarea"]) if node.get("name")}
        score = (4 * ("paymentmethod" in names)
                 + 3 * bool(names & {"accepttos", "accept_terms", "tos"})
                 + 2 * ("custtype" in names)
                 + 1 * ("submit" in names)
                 + 1 * (str(form.get("id") or "").casefold() == "frmcheckout"))
        candidates.append((score, -index, form))
    return max(candidates, default=(0, 0, None))[2]


def _dmit_accept_terms(form, payload: dict) -> bool:
    """Submit agreement checkboxes with the exact HTML value the browser uses."""
    accepted = False
    for node in form.find_all("input", attrs={"type": "checkbox"}):
        name = str(node.get("name", "")).strip()
        if name.casefold() not in {"accepttos", "accept_terms", "tos"}:
            continue
        # An unchecked checkbox is absent from normal form submission, but we
        # intentionally check it to mirror clicking the agreement in-browser.
        # Preserve an explicit value (for example "1"); HTML defaults to "on"
        # only when the value attribute is missing.
        payload[name] = str(node.get("value", "")) if node.has_attr("value") else "on"
        accepted = True
    return accepted


def _dmit_confirm_cart_item(body: str, product_name: str) -> bool:
    """Confirm that the configured product is actually present in the cart."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(str(body or ""), "html.parser")
    page_text = " ".join(soup.get_text(" ", strip=True).split()).casefold()
    if re.search(r"your shopping cart is empty|购物车为空|購物車為空", page_text, re.I):
        return False
    expected = " ".join(str(product_name or "").split()).casefold()
    return bool(expected and expected in page_text)


def _dmit_browser_response(solution):
    from types import SimpleNamespace
    body = str(solution.get("response", ""))
    return SimpleNamespace(content=body.encode("utf-8"), text=body,
                           status_code=int(solution.get("status", 0)),
                           url=str(solution.get("url", "")), headers=solution.get("headers", {}) or {})


def _dmit_order_flaresolverr(item: dict, account: dict, product, url: str, fetcher: dict, key: str) -> dict:
    """Log in and complete DMIT checkout inside one FlareSolverr browser session."""
    from bs4 import BeautifulSoup
    from monitor import (browser_request_slot, flaresolverr_session_create,
                         flaresolverr_session_destroy, flaresolverr_session_request)

    pid = str(getattr(product, "id", "") or "").split(":")[-1]
    if not re.fullmatch(r"[1-9][0-9]{0,9}", pid):
        pid = str(urlsplit(url).query.split("pid=")[-1]).split("&", 1)[0]
    if not re.fullmatch(r"[1-9][0-9]{0,9}", pid):
        raise DmitOrderFailure("DMIT 商品 PID 无效；最终结算请求尚未发送")
    session_id = ""
    try:
        # Web-triggered browser work must fail fast when FlareSolverr is busy;
        # keeping the HTTP request queued makes the whole admin page appear
        # frozen for tens of seconds.
        with browser_request_slot(wait=False):
            session_id = flaresolverr_session_create(fetcher)
            login = flaresolverr_session_request(fetcher, session_id, MERCHANT_LOGIN_ADAPTERS["dmit"]["url"])
            login_page = _dmit_browser_response(login)
            login_soup = BeautifulSoup(login_page.content, "html.parser")
            login_form = next((candidate for candidate in login_soup.find_all("form")
                               if candidate.find("input", attrs={"name": "username"})
                               and candidate.find("input", attrs={"name": "password"})), None)
            if login_form is not None:
                login_action = urljoin(login_page.url, login_form.get("action") or login_page.url)
                if not _same_merchant_url(login_action, MERCHANT_LOGIN_ADAPTERS["dmit"]):
                    raise DmitOrderFailure("DMIT 登录表单跳转地址不符合商家域名；最终结算请求尚未发送")
                payload = _dmit_form_values(login_form)
                payload["username"] = str(account.get("username", ""))
                payload["password"] = str(account.get("password", ""))
                for node in login_form.find_all("input", attrs={"type": "checkbox"}):
                    name = str(node.get("name", ""))
                    if name.casefold() in {"rememberme", "remember_me", "remember"}:
                        payload[name] = str(node.get("value") or "on")
                posted = flaresolverr_session_request(fetcher, session_id, login_action, "post",
                    urlencode(payload), {"Referer": login_page.url, "Origin": "https://www.dmit.io",
                                         "Content-Type": "application/x-www-form-urlencoded"})
                posted_page = _dmit_browser_response(posted)
                if not 200 <= posted_page.status_code < 300:
                    raise DmitOrderFailure("DMIT 浏览器登录失败；最终结算请求尚未发送")
            authenticated = flaresolverr_session_request(fetcher, session_id,
                MERCHANT_LOGIN_ADAPTERS["dmit"]["auth_probe_url"])
            auth_page = _dmit_browser_response(authenticated)
            auth_text = BeautifulSoup(auth_page.content, "html.parser").get_text(" ", strip=True).casefold()
            if not 200 <= auth_page.status_code < 300 or "logout" not in auth_text and "clientarea.php?action=details" not in auth_text:
                raise DmitOrderFailure("DMIT 浏览器会话未通过账户验证；最终结算请求尚未发送")

            product_name = str(getattr(product, "name", "") or "").strip()
            cart_url = f"https://www.dmit.io/cart.php?a=add&pid={pid}"
            cart = _dmit_browser_response(flaresolverr_session_request(fetcher, session_id, cart_url))
            soup = BeautifulSoup(cart.content, "html.parser")
            form = soup.select_one("form#frmConfigureProduct, form[action*='a=confproduct']")
            if form is None:
                raise DmitOrderFailure("DMIT 商品配置页结构已变化；最终结算请求尚未发送")
            action = urljoin(cart.url, form.get("action") or cart.url)
            if not _same_merchant_url(action, MERCHANT_LOGIN_ADAPTERS["dmit"]):
                raise DmitOrderFailure("DMIT 配置提交地址不符合商家域名；最终结算请求尚未发送")
            config, dmit_os = _dmit_config_values(form, item)
            config.update({"configure": "true", "billingcycle": "monthly",
                           "configoption[118]": "0", "rootpw": _dmit_generated_root_password()})
            hostname = str(config.get("hostname", "")).strip()
            if not hostname:
                hostname = _dmit_generated_hostname()
                config["hostname"] = hostname
            if form.find("input", attrs={"name": "beta_ack"}) is not None:
                config["beta_ack"] = "1"
            # DMIT's order form has no HTML action. Its frontend submits an
            # AJAX request to cart.php with a=confproduct; posting directly to
            # the rendered page only reloads the form and never adds the item.
            configured = _dmit_browser_response(flaresolverr_session_request(fetcher, session_id,
                "https://www.dmit.io/cart.php", "post", "ajax=1&a=confproduct&" + urlencode(config),
                {"Referer": cart.url, "Origin": "https://www.dmit.io",
                 "Content-Type": "application/x-www-form-urlencoded", "X-Requested-With": "XMLHttpRequest"}))
            if not 200 <= configured.status_code < 300 or not _same_merchant_url(configured.url or action, MERCHANT_LOGIN_ADAPTERS["dmit"]):
                detail = _dmit_page_errors(configured)
                raise DmitOrderFailure("DMIT 商品配置请求被拒绝；最终结算请求尚未发送" + (f"：{detail}" if detail else ""))
            ajax_error_text = BeautifulSoup(configured.content or configured.text or "", "html.parser").get_text(" ", strip=True)
            if ajax_error_text:
                detail = _dmit_page_errors(configured)
                raise DmitOrderFailure("DMIT 商品配置未通过校验；最终结算请求尚未发送：" + (detail or ajax_error_text[:320]))
            config_errors = _dmit_page_errors(configured)
            if config_errors:
                raise DmitOrderFailure(f"DMIT 商品配置未通过校验；最终结算请求尚未发送：{config_errors}")
            cart_view = _dmit_browser_response(flaresolverr_session_request(
                fetcher, session_id, "https://www.dmit.io/cart.php?a=view"))
            if (not 200 <= cart_view.status_code < 300
                    or not _same_merchant_url(cart_view.url or "", MERCHANT_LOGIN_ADAPTERS["dmit"])):
                raise DmitOrderFailure("DMIT 已提交商品配置，但购物车无法读取；最终结算请求尚未发送")
            if not _dmit_confirm_cart_item(cart_view.text, product_name):
                raise DmitOrderFailure("DMIT 购物车未显示本次商品；最终结算请求尚未发送")
            checkout = _dmit_browser_response(flaresolverr_session_request(
                fetcher, session_id, "https://www.dmit.io/cart.php?a=checkout"))
            if not 200 <= checkout.status_code < 300 or not _same_merchant_url(checkout.url or "", MERCHANT_LOGIN_ADAPTERS["dmit"]):
                raise DmitOrderFailure("DMIT 结算页无法读取；最终结算请求尚未发送")
            checkout_soup = BeautifulSoup(checkout.content, "html.parser")
            checkout_form = _dmit_checkout_form(checkout_soup)
            if checkout_form is None:
                raise DmitOrderFailure("DMIT 结算页结构已变化；最终结算请求尚未发送")
            checkout_action = urljoin(checkout.url, checkout_form.get("action") or checkout.url)
            payload = _dmit_checkout_values(checkout_form)
            payload.update({"submit": "true", "custtype": "existing"})
            gateway = str(payload.get("paymentmethod", "")).strip()
            if not gateway:
                raise DmitOrderFailure("DMIT 结算页未显示默认付款方式；最终结算请求尚未发送")
            _dmit_accept_terms(checkout_form, payload)
            submitted = _dmit_browser_response(flaresolverr_session_request(fetcher, session_id, checkout_action, "post",
                urlencode(payload, doseq=True), {"Referer": checkout.url, "Origin": "https://www.dmit.io",
                                     "Content-Type": "application/x-www-form-urlencoded"}))
            if not 200 <= submitted.status_code < 300 or not _same_merchant_url(submitted.url or checkout_action, MERCHANT_LOGIN_ADAPTERS["dmit"]):
                raise DmitOrderFailure("DMIT 最终结算请求返回异常；结果待核对", True)
            text = BeautifulSoup(submitted.content, "html.parser").get_text(" ", strip=True)
            invoice_id = _dmit_invoice_id(submitted.url or "", text)
            if not invoice_id:
                diagnostic = _dmit_response_diagnostic(submitted)
                payload_diagnostic = _dmit_checkout_payload_diagnostic(payload)
                raise DmitOrderFailure(f"DMIT 结算页未返回账单号；请求字段 {payload_diagnostic}；{diagnostic}；结果待核对", True)
            invoice_url = f"https://www.dmit.io/viewinvoice.php?id={invoice_id}"
            invoice = _dmit_browser_response(flaresolverr_session_request(fetcher, session_id, invoice_url))
            if not 200 <= invoice.status_code < 300:
                raise DmitOrderFailure("DMIT 账单详情读取失败；结果待核对", True)
            invoice_text = BeautifulSoup(invoice.content, "html.parser").get_text(" ", strip=True)
            if not re.search(r"\b(?:unpaid|pending)\b|未付款|待付款|未付", invoice_text, re.I):
                raise DmitOrderFailure("DMIT 账单详情未显示待付款状态；结果待核对", True)
            if product_name and product_name.casefold() not in invoice_text.casefold():
                raise DmitOrderFailure("DMIT 账单详情中的商品与本次商品不匹配；结果待核对", True)
            return {"order_id": str(invoice_id), "invoice_url": invoice_url, "payment_status": "unpaid",
                    "os": dmit_os, "payment_method": gateway, "hostname": hostname, "extra_disk_gb": 0}
    finally:
        if session_id:
            flaresolverr_session_destroy(fetcher, session_id)


def _dmit_flaresolverr_account_check(account: dict, fetcher: dict) -> dict:
    """Verify DMIT credentials in a browser session when normal HTTP is blocked."""
    from bs4 import BeautifulSoup
    from monitor import (browser_request_slot, flaresolverr_session_create,
                         flaresolverr_session_destroy, flaresolverr_session_request)
    session_id = ""
    try:
        # Return a busy result immediately instead of holding the browser
        # request open while another FlareSolverr task finishes.
        with browser_request_slot(wait=False):
            session_id = flaresolverr_session_create(fetcher)
            login = flaresolverr_session_request(fetcher, session_id, MERCHANT_LOGIN_ADAPTERS["dmit"]["url"])
            page = _dmit_browser_response(login)
            soup = BeautifulSoup(page.content, "html.parser")
            form = next((candidate for candidate in soup.find_all("form")
                         if candidate.find("input", attrs={"name": "username"})
                         and candidate.find("input", attrs={"name": "password"})), None)
            if form is not None:
                action = urljoin(page.url, form.get("action") or page.url)
                if not _same_merchant_url(action, MERCHANT_LOGIN_ADAPTERS["dmit"]):
                    return {"status": "unknown", "message": "DMIT 登录表单跳转地址不符合商家域名，请到官网检查"}
                payload = _dmit_form_values(form)
                payload.update({"username": str(account.get("username", "")), "password": str(account.get("password", ""))})
                for node in form.find_all("input", attrs={"type": "checkbox"}):
                    name = str(node.get("name", ""))
                    if name.casefold() in {"rememberme", "remember_me", "remember"}:
                        payload[name] = str(node.get("value") or "on")
                posted = _dmit_browser_response(flaresolverr_session_request(fetcher, session_id, action, "post",
                    urlencode(payload), {"Referer": page.url, "Origin": "https://www.dmit.io",
                                         "Content-Type": "application/x-www-form-urlencoded"}))
                if not 200 <= posted.status_code < 300:
                    return {"status": "unknown", "message": "DMIT 浏览器登录请求失败，请到官网检查"}
            checked = _dmit_browser_response(flaresolverr_session_request(fetcher, session_id,
                MERCHANT_LOGIN_ADAPTERS["dmit"]["auth_probe_url"]))
            text = BeautifulSoup(checked.content, "html.parser").get_text(" ", strip=True).casefold()
            if 200 <= checked.status_code < 300 and ("logout" in text or "clientarea.php?action=details" in text):
                return {"status": "valid", "ok": True, "message": "DMIT 已通过 FlareSolverr 浏览器认证（不代表具有购买资格或库存）"}
            return {"status": "unknown", "message": "DMIT 浏览器会话未通过账户验证，请到官网检查"}
    finally:
        if session_id:
            flaresolverr_session_destroy(fetcher, session_id)


def _dmit_order(session, item: dict, account: dict, product, url: str, fetcher: dict | None = None, key: str | None = None) -> dict:
    """Configure a DMIT cart item and submit checkout with its default gateway."""
    from bs4 import BeautifulSoup
    from monitor import fetch_page
    pid = str(getattr(product, "id", "") or "").split(":")[-1]
    if not re.fullmatch(r"[1-9][0-9]{0,9}", pid):
        pid = str(urlsplit(url).query.split("pid=")[-1]).split("&", 1)[0]
    if not re.fullmatch(r"[1-9][0-9]{0,9}", pid):
        raise ValueError("DMIT 商品 PID 无效，未提交订单")
    cart_url = f"https://www.dmit.io/cart.php?a=add&pid={pid}"
    browser_fetcher = dict(fetcher or {})
    if browser_fetcher.get("mode") == "flaresolverr":
        browser_fetcher["_copy_browser_state"] = True
        # FlareSolverr solves the page in a fresh anonymous browser. Preserve
        # the authenticated DMIT session and its browser fingerprint when its
        # clearance cookies are copied back for the follow-up POST; otherwise
        # the anonymous WHMCS session replaces the logged-in one and DMIT
        # rejects the configuration request with HTTP 403.
        session._hostwatch_preserve_user_agent = True
        preserve = [cookie.name for cookie in session.cookies
                    if str(cookie.name).casefold() == "cf_app_sid"
                    or str(cookie.name).casefold().startswith("whmcsp")]
        session._hostwatch_preserve_cookie_names = preserve
    body, final_url = fetch_page(session, cart_url, browser_fetcher, "www.dmit.io")
    soup = BeautifulSoup(body, "html.parser")
    form = soup.select_one("form#frmConfigureProduct, form[action*='a=confproduct']")
    if form is None:
        raise ValueError("DMIT 商品配置页结构已变化，未提交订单")
    action = urljoin(final_url, form.get("action") or final_url)
    if not _same_merchant_url(action, MERCHANT_LOGIN_ADAPTERS["dmit"]):
        raise ValueError("DMIT 配置提交地址不符合商家域名，未提交订单")
    config, dmit_os = _dmit_config_values(form, item)
    config.update({"configure": "true", "billingcycle": "monthly", "configoption[118]": "0",
                   "rootpw": _dmit_generated_root_password()})
    hostname = str(config.get("hostname", "")).strip()
    if not hostname:
        hostname = _dmit_generated_hostname()
        config["hostname"] = hostname
    # DMIT's Beta plan form blocks its first submit in JavaScript until the
    # customer acknowledges the Beta notice. The browser's confirmation sets
    # this hidden field; mirror that affirmative step for this explicitly
    # requested order flow.
    if form.find("input", attrs={"name": "beta_ack"}) is not None:
        config["beta_ack"] = "1"
    product_name = str(getattr(product, "name", "") or "").strip()
    try:
        configured = session.post(action, data=config, timeout=(5, 25), allow_redirects=True,
                                  headers={"Referer": final_url, "Content-Type": "application/x-www-form-urlencoded"})
    except requests.RequestException:
        raise DmitOrderFailure("DMIT 商品配置请求网络失败；最终结算请求尚未发送") from None
    if not 200 <= configured.status_code < 300:
        detail = _dmit_page_errors(configured)
        raise DmitOrderFailure(f"DMIT 商品配置请求返回 HTTP {configured.status_code}；尚未发送最终结算请求" + (f"：{detail}" if detail else ""))
    if not _same_merchant_url(configured.url or action, MERCHANT_LOGIN_ADAPTERS["dmit"]):
        raise DmitOrderFailure("DMIT 商品配置请求跳转离开官网；尚未发送最终结算请求")
    config_errors = _dmit_page_errors(configured)
    if config_errors:
        raise DmitOrderFailure(f"DMIT 商品配置未通过校验；尚未发送最终结算请求：{config_errors}")
    try:
        cart_body, cart_view_url = fetch_page(session, "https://www.dmit.io/cart.php?a=view", browser_fetcher, "www.dmit.io")
    except Exception:
        raise DmitOrderFailure("DMIT 已提交商品配置，但购物车无法读取；最终结算请求尚未发送") from None
    if not _same_merchant_url(cart_view_url, MERCHANT_LOGIN_ADAPTERS["dmit"]):
        raise DmitOrderFailure("DMIT 购物车跳转离开官网；最终结算请求尚未发送")
    if not _dmit_confirm_cart_item(cart_body, product_name):
        raise DmitOrderFailure("DMIT 购物车未显示本次商品；最终结算请求尚未发送")
    checkout = session.get("https://www.dmit.io/cart.php?a=checkout", timeout=(5, 25), allow_redirects=True,
                           headers={"Referer": cart_view_url})
    if not 200 <= checkout.status_code < 300 or not _same_merchant_url(checkout.url or "", MERCHANT_LOGIN_ADAPTERS["dmit"]):
        raise ValueError("DMIT 结算页无法读取，未提交订单")
    checkout_soup = BeautifulSoup(checkout.content or checkout.text or "", "html.parser")
    checkout_form = _dmit_checkout_form(checkout_soup)
    if checkout_form is None:
        raise ValueError("DMIT 结算页结构已变化，未提交订单")
    checkout_action = urljoin(checkout.url, checkout_form.get("action") or checkout.url)
    payload = _dmit_checkout_values(checkout_form)
    payload.update({"submit": "true", "custtype": "existing"})
    gateway = str(payload.get("paymentmethod", "")).strip()
    if not gateway:
        raise ValueError("DMIT 结算页未显示默认付款方式，未提交最终订单")
    _dmit_accept_terms(checkout_form, payload)
    try:
        submitted = session.post(checkout_action, data=payload, timeout=(5, 25), allow_redirects=True,
                                 headers={"Referer": checkout.url, "Content-Type": "application/x-www-form-urlencoded"})
    except requests.RequestException:
        raise DmitOrderFailure("DMIT 最终结算请求的响应未收到；账单是否创建无法确认，请先检查官网账单列表", True) from None
    if not 200 <= submitted.status_code < 300:
        raise DmitOrderFailure(f"DMIT 最终结算请求已发出，但返回 HTTP {submitted.status_code}，未确认账单", True)
    if not _same_merchant_url(submitted.url or checkout_action, MERCHANT_LOGIN_ADAPTERS["dmit"]):
        raise DmitOrderFailure("DMIT 最终结算请求已发出，但跳转离开官网，未确认账单", True)
    text = BeautifulSoup(submitted.content or submitted.text or "", "html.parser").get_text(" ", strip=True)
    invoice_id = _dmit_invoice_id(submitted.url or "", text)
    if not invoice_id:
        diagnostic = _dmit_response_diagnostic(submitted)
        payload_diagnostic = _dmit_checkout_payload_diagnostic(payload)
        raise DmitOrderFailure(f"DMIT 最终结算请求已发出，但结算页未返回账单号；请求字段 {payload_diagnostic}；{diagnostic}；结果待核对", True)
    invoice_url = f"https://www.dmit.io/viewinvoice.php?id={invoice_id}"
    try:
        invoice = session.get(invoice_url, timeout=(5, 25), allow_redirects=False,
                              headers={"Referer": submitted.url or checkout_action})
    except requests.RequestException:
        raise DmitOrderFailure("DMIT 结算请求已发出并返回账单号，但读取账单详情失败；结果待核对", True) from None
    if not 200 <= invoice.status_code < 300 or not _same_merchant_url(invoice.url or invoice_url, MERCHANT_LOGIN_ADAPTERS["dmit"]):
        raise DmitOrderFailure(f"DMIT 结算请求已发出并返回账单号，但账单详情返回 HTTP {invoice.status_code}；结果待核对", True)
    invoice_text = BeautifulSoup(invoice.content or invoice.text or "", "html.parser").get_text(" ", strip=True)
    if not re.search(r"\b(?:unpaid|pending)\b|未付款|待付款|未付", invoice_text, re.I):
        raise DmitOrderFailure("DMIT 账单详情未显示待付款状态；结果待核对", True)
    if product_name and product_name.casefold() not in invoice_text.casefold():
        raise DmitOrderFailure("DMIT 账单详情中的商品与本次商品不匹配；结果待核对", True)
    return {"order_id": str(invoice_id), "invoice_url": invoice_url, "payment_status": "unpaid",
            "os": dmit_os, "payment_method": gateway, "hostname": hostname, "extra_disk_gb": 0}


def _vmiss_url(url: str) -> str:
    parsed = urlsplit(str(url or ""))
    if parsed.scheme != "https" or parsed.hostname != VMISS_HOST or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("VMISS 请求地址必须是 app.vmiss.com 的 HTTPS 地址")
    return url


def _vmiss_get(session, url: str):
    """Follow only bounded same-host VMISS redirects for page navigation."""
    current = _vmiss_url(url)
    for _ in range(6):
        response = session.get(current, timeout=(5, 25), allow_redirects=False)
        if 300 <= response.status_code < 400:
            location = str(getattr(response, "headers", {}).get("Location", "")).strip()
            if not location:
                raise ValueError("VMISS 页面跳转缺少目标地址")
            current = _vmiss_url(urljoin(current, location))
            continue
        if not 200 <= response.status_code < 300:
            raise ValueError(f"VMISS 页面返回 HTTP {response.status_code}")
        return response
    raise ValueError("VMISS 页面跳转次数过多")


def _vmiss_form_data(form) -> list[tuple[str, str]]:
    """Collect successful controls from a WHMCS form without submitting buttons."""
    data = []
    for control in form.find_all(["input", "select", "textarea"]):
        name = str(control.get("name") or "").strip()
        if not name or control.has_attr("disabled"):
            continue
        tag = control.name.casefold()
        if tag == "input":
            kind = str(control.get("type") or "text").casefold()
            if kind in {"submit", "button", "reset", "file"} or kind in {"checkbox", "radio"} and not control.has_attr("checked"):
                continue
            data.append((name, str(control.get("value") or "")))
        elif tag == "select":
            options = control.find_all("option")
            selected = [option for option in options if option.has_attr("selected")]
            if not selected and options:
                selected = [options[0]]
            for option in selected:
                data.append((name, str(option.get("value") or "")))
        else:
            data.append((name, control.get_text()))
    return data


def _vmiss_replace(data: list[tuple[str, str]], name: str, value: str) -> list[tuple[str, str]]:
    result = [(key, item) for key, item in data if key != name]
    result.append((name, value))
    return result


def _vmiss_form_action(form, page_url: str) -> str:
    action = urljoin(page_url, str(form.get("action") or page_url))
    return _vmiss_url(action)


def _vmiss_invoice_url(response, fallback: str = "") -> str:
    location = str(getattr(response, "headers", {}).get("Location", "")).strip()
    candidates = [location, str(getattr(response, "url", "") or ""), str(fallback or "")]
    body = str(getattr(response, "text", "") or "")
    body_links = list(dict.fromkeys(re.findall(
        r"(?:https://app\.vmiss\.com)?/(?:viewinvoice|invoice)\.php\?(?:[^\"'<>\s]*&)?(?:id|invoiceid)=[0-9]+",
        body, re.I)))
    # A client invoice list contains many viewinvoice links.  Do not pick the
    # first historical invoice just because it appeared in the response body;
    # the list reconciliation below matches the requested product instead.
    if len(body_links) == 1:
        candidates.extend(body_links)
    for candidate in candidates:
        if not candidate:
            continue
        try:
            target = _vmiss_url(urljoin(response.url or VMISS_BASE_URL, candidate))
        except ValueError:
            continue
        parsed = urlsplit(target)
        if parsed.path.casefold() in {"/viewinvoice.php", "/invoice.php"} and re.fullmatch(r"[1-9][0-9]{0,18}", (parse_qs(parsed.query).get("id") or parse_qs(parsed.query).get("invoiceid") or [""])[0]):
            return target
    return ""


def _vmiss_invoice_id(value: object) -> str:
    """Extract a VMISS invoice number from a detail URL or confirmation page."""
    raw = str(value or "")
    parsed = urlsplit(raw)
    query = parse_qs(parsed.query)
    for key in ("id", "invoiceid", "invoice_id"):
        candidate = query.get(key, [""])[0]
        if re.fullmatch(r"[1-9][0-9]{0,18}", candidate or ""):
            return candidate
    match = re.search(r"\bVMISS[-\s#:]*([1-9][0-9]{5,18})\b", raw, re.I)
    return match.group(1) if match else ""


def _vmiss_order_detail_url(response, fallback: str = "") -> str:
    """Return a same-host page that looks like the post-order detail view."""
    try:
        target = _vmiss_url(str(getattr(response, "url", "") or fallback))
    except ValueError:
        return ""
    parsed = urlsplit(target)
    query = parse_qs(parsed.query)
    body = str(getattr(response, "text", "") or "")
    detail_marker = re.search(r"(?:view\s*invoice|invoice\s*[#№]|\bVMISS[-\s#:]*[1-9][0-9]{5,18}\b)", body, re.I)
    if parsed.path.casefold() == "/clientarea.php" and str(query.get("action", [""])[0]).casefold() in {"invoices", "invoice"}:
        return ""
    if parsed.path.casefold() == "/cart.php" and str(query.get("a", [""])[0]).casefold() in {"checkout", "view"} and not detail_marker:
        return ""
    if not detail_marker and not re.search(r"(?:账单|未付款|待付款|unpaid|pending)", body, re.I):
        return ""
    return target


def _vmiss_invoice_status(text: str) -> str:
    """Classify visible VMISS invoice status without treating ``unpaid`` as paid."""
    body = " ".join(str(text or "").split()).casefold()
    if re.search(r"\b(?:cancelled|canceled|paid|refunded|void)\b|已取消|已付款|已支付|已退款", body):
        return "not_unpaid"
    if re.search(r"\b(?:unpaid|pending)\b|未付款|未支付|待付款", body):
        return "unpaid"
    return "unknown"


def _vmiss_normalize_invoice_text(value: object) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(value or "").casefold())


def _vmiss_confirm_invoice_page(page, invoice_url: str, item: dict, product) -> dict:
    """Verify one already-loaded VMISS invoice/order detail page."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(page.text, "html.parser")
    text = soup.get_text(" ", strip=True)
    status = _vmiss_invoice_status(text)
    status_nodes = soup.select('[class*="status"], [class*="label"], [data-status], [data-state]')
    if status_nodes:
        marked_text = []
        for node in status_nodes:
            marked_text.append(node.get_text(" ", strip=True))
            marked_text.extend(str(node.get(key, "")) for key in ("class", "id", "data-status", "data-state"))
        marked_status = _vmiss_invoice_status(" ".join(marked_text))
        if marked_status != "unknown":
            status = marked_status
    if status != "unpaid":
        raise ValueError("VMISS 账单页未确认未付款状态，已停止且不会重复下单")
    normalized_text = _vmiss_normalize_invoice_text(text)
    product_name = str(getattr(product, "name", "") or "").strip()
    if not product_name and isinstance(product, dict):
        product_name = str(product.get("name", "")).strip()
    if product_name and _vmiss_normalize_invoice_text(product_name) not in normalized_text:
        raise ValueError(f"VMISS 账单项目未匹配本次商品：{product_name[:120]}")
    operating_system = str(item.get("operating_system") or "Debian 12").strip()
    if operating_system and _vmiss_normalize_invoice_text(operating_system) not in normalized_text:
        raise ValueError(f"VMISS 账单项目未确认系统：{operating_system[:80]}")
    coupon = str(item.get("coupon_code") or "").strip()
    if coupon and _vmiss_normalize_invoice_text(coupon) not in normalized_text:
        raise ValueError(f"VMISS 账单项目未确认优惠码：{coupon[:80]}")
    return {"status": "unpaid", "product": product_name, "operating_system": operating_system, "coupon_code": coupon}


def _vmiss_confirm_invoice(session, invoice_url: str, item: dict, product) -> dict:
    """Read one invoice page and verify status plus the requested order details."""
    page = _vmiss_get(session, invoice_url)
    return _vmiss_confirm_invoice_page(page, invoice_url, item, product)


def _vmiss_invoice_status_text(node) -> str:
    """Include status attributes used by different VMISS/WHMCS themes."""
    if node is None:
        return ""
    values = [node.get_text(" ", strip=True)]
    for key, value in getattr(node, "attrs", {}).items():
        if key in {"class", "id", "data-status", "data-state", "status", "title"}:
            if isinstance(value, (list, tuple)):
                values.extend(str(item) for item in value)
            else:
                values.append(str(value))
    return " ".join(values)


def _vmiss_link_status(link) -> str:
    """Read an invoice row status without depending on one WHMCS theme."""
    # The normal table layout puts the status in the same <tr>.  Some themes
    # use a div row or put the status in a child with a class/data attribute,
    # so inspect a few ancestors and status-marked descendants as well.
    ancestors = []
    current = link
    for _ in range(5):
        current = getattr(current, "parent", None)
        if current is None:
            break
        ancestors.append(current)
        if getattr(current, "name", "") == "tr":
            break
    for node in ancestors:
        marked = node.select('[class*="status"], [class*="label"], [data-status], [data-state]')
        sources = marked or [node]
        status = _vmiss_invoice_status(" ".join(_vmiss_invoice_status_text(item) for item in sources))
        if status != "unknown":
            return status
    return _vmiss_invoice_status(" ".join(_vmiss_invoice_status_text(item) for item in ancestors[:2]))


def _vmiss_unpaid_invoice_candidates(session, *, include_unknown: bool = False,
                                     list_url: str = "") -> list[str]:
    """Return visible invoice URLs from the client list.

    ``include_unknown`` is used immediately after checkout.  A few VMISS
    themes render the status with a CSS/data attribute and the surrounding
    text contains no literal ``Unpaid`` label.  The invoice detail page still
    performs the authoritative status check, so inspecting these candidates is
    safe and lets the order flow recover from that presentation difference.
    """
    from bs4 import BeautifulSoup

    page = _vmiss_get(session, list_url or VMISS_INVOICES_URL)
    soup = BeautifulSoup(page.text, "html.parser")
    candidates = {}
    for link in soup.select('a[href*="viewinvoice.php"]')[:VMISS_INVOICE_MAX_CANDIDATES]:
        try:
            invoice_url = _vmiss_url(urljoin(page.url or VMISS_INVOICES_URL, str(link.get("href") or "")))
        except ValueError:
            continue
        invoice_id = parse_qs(urlsplit(invoice_url).query).get("id", [""])[0]
        if not re.fullmatch(r"[1-9][0-9]{0,18}", invoice_id):
            continue
        status = _vmiss_link_status(link)
        if status == "unpaid" or include_unknown and status == "unknown":
            candidates[invoice_id] = invoice_url
    return list(candidates.values())


def _vmiss_unpaid_invoice_from_list(session) -> str:
    """Find a single visible unpaid invoice from the client list."""
    candidates = _vmiss_unpaid_invoice_candidates(session)
    return candidates[0] if len(candidates) == 1 else ""


def vmiss_order(session, item: dict, account: dict, product, url: str, key: str, progress=None) -> dict:
    """Create one VMISS WHMCS order and stop at its unpaid invoice.

    VMISS has no HostWatch JSON order endpoint. This adapter follows the site's
    own product configuration and checkout forms, explicitly selects an invoice
    payment method, disables account credit, and never follows a payment-gateway
    redirect.
    """
    from bs4 import BeautifulSoup

    progress = progress or (lambda message: None)
    if item.get("quantity", 1) != 1:
        raise ValueError("VMISS 原生下单暂只支持单台订单")
    page = _vmiss_get(session, url)
    soup = BeautifulSoup(page.text, "html.parser")
    config = soup.select_one("form#frmConfigureProduct")
    if config is None:
        raise ValueError("VMISS 商品页未返回配置表单，未创建订单")
    data = _vmiss_form_data(config)
    data = _vmiss_replace(data, "ajax", "1")
    data = _vmiss_replace(data, "a", "confproduct")
    cycle = str(item.get("billing_cycle") or "monthly").strip().casefold()
    if cycle not in ORDER_BILLING_CYCLES:
        raise ValueError("VMISS 付款周期无效")
    data = _vmiss_replace(data, "billingcycle", cycle)
    operating_system = str(item.get("operating_system") or "Debian 12").strip()
    os_select = next((select for select in config.find_all("select") if str(select.get("name") or "").startswith("configoption[")), None)
    if os_select is None:
        raise ValueError("VMISS 配置页未返回系统选项，未创建订单")
    os_option = next((option for option in os_select.find_all("option") if " ".join(option.get_text(" ", strip=True).split()).casefold() == operating_system.casefold()), None)
    if os_option is None:
        raise ValueError(f"VMISS 配置页没有系统选项：{operating_system}")
    data = _vmiss_replace(data, str(os_select["name"]), str(os_option.get("value") or ""))
    hostname = next((str(control.get("value") or "").strip() for control in config.select('input[name="hostname"]')), "")
    if not hostname:
        hostname = "hostwatch-" + key[:10]
    data = _vmiss_replace(data, "hostname", hostname)
    if not any(name == "rootpw" for name, _ in data):
        data.append(("rootpw", "N/A"))
    config_response = session.post("https://app.vmiss.com/cart.php", data=data,
        timeout=(5, 25), allow_redirects=False)
    if not 200 <= config_response.status_code < 300 or str(config_response.text or "").strip():
        raise ValueError("VMISS 商品配置未通过，未创建订单")

    checkout = _vmiss_get(session, "https://app.vmiss.com/cart.php?a=checkout")
    checkout_soup = BeautifulSoup(checkout.text, "html.parser")
    coupon = str(item.get("coupon_code") or "").strip()
    if coupon:
        promo = next((form for form in checkout_soup.find_all("form") if form.select_one('input[name="promocode"]')), None)
        if promo is None:
            raise ValueError("VMISS 结算页未返回优惠码表单，未创建订单")
        promo_data = _vmiss_form_data(promo)
        promo_data = _vmiss_replace(promo_data, "promocode", coupon)
        promo_data = _vmiss_replace(promo_data, "validatepromo", "Validate Code")
        promo_response = session.post(_vmiss_form_action(promo, checkout.url), data=promo_data,
            timeout=(5, 25), allow_redirects=False)
        if 300 <= promo_response.status_code < 400:
            location = str(promo_response.headers.get("Location", ""))
            checkout = _vmiss_get(session, urljoin(promo_response.url or checkout.url, location))
        elif 200 <= promo_response.status_code < 300:
            checkout = promo_response
        else:
            raise ValueError("VMISS 优惠码提交失败，未创建订单")
        promo_text = BeautifulSoup(checkout.text, "html.parser").get_text(" ", strip=True).casefold()
        if any(marker in promo_text for marker in ("invalid promotion", "promo code is invalid", "not valid", "优惠码无效")):
            raise ValueError("VMISS 优惠码无效，未创建订单")
        checkout_soup = BeautifulSoup(checkout.text, "html.parser")

    order_form = checkout_soup.select_one("form#frmCheckout")
    if order_form is None:
        raise ValueError("VMISS 结算页未返回订单表单，未创建订单")
    order_data = _vmiss_form_data(order_form)
    order_data = [(name, value) for name, value in order_data if name not in {
        "loginemail", "loginpassword", "password", "password2", "ccnumber", "ccexpirydate", "cccvv", "ccdescription", "ccinfo", "marketingoptin"
    }]
    if order_form.select_one('input[name="custtype"]'):
        order_data = _vmiss_replace(order_data, "custtype", "existing")
    order_data = _vmiss_replace(order_data, "applycredit", "0")
    payment_method = str(item.get("payment_method") or "alipay").strip().casefold()
    gateway = VMISS_PAYMENT_METHODS.get(payment_method)
    if not gateway:
        raise ValueError("VMISS 付款方式不受支持")
    order_data = _vmiss_replace(order_data, "paymentmethod", gateway)
    order_data = _vmiss_replace(order_data, "accepttos", "on")
    progress("正在提交 VMISS 下单请求")
    order_response = session.post(_vmiss_form_action(order_form, checkout.url), data=order_data,
        headers={"Idempotency-Key": key}, timeout=(5, 30), allow_redirects=False)
    progress("VMISS 下单响应已返回，正在定位待付款账单")
    invoice_url = _vmiss_invoice_url(order_response, checkout.url)
    response_invoice_url = bool(invoice_url)
    verification = None
    direct_detail_error = None
    if not invoice_url and 200 <= order_response.status_code < 300:
        # Some VMISS deployments return the completed order/invoice detail as
        # the POST response itself (the browser would display this page after
        # following the redirect).  Verify that page directly before touching
        # the invoice list.
        detail_url = _vmiss_order_detail_url(order_response, checkout.url)
        if detail_url:
            try:
                verification = _vmiss_confirm_invoice_page(order_response, detail_url, item, product)
                invoice_url = _vmiss_invoice_url(order_response, detail_url)
                if not invoice_url:
                    invoice_id = _vmiss_invoice_id(getattr(order_response, "text", ""))
                    invoice_url = f"{VMISS_BASE_URL}viewinvoice.php?id={invoice_id}" if invoice_id else detail_url
                progress("已从 VMISS 下单响应定位订单详情并直接核验")
            except (requests.RequestException, ValueError) as exc:
                direct_detail_error = str(exc).strip() or exc.__class__.__name__
    if not invoice_url and 300 <= order_response.status_code < 400:
        location = str(order_response.headers.get("Location", "")).strip()
        if location:
            try:
                destination = _vmiss_url(urljoin(order_response.url or checkout.url, location))
                destination_path = urlsplit(destination).path.casefold()
                destination_query = parse_qs(urlsplit(destination).query)
                destination_action = str(destination_query.get("a", [""])[0]).casefold()
                should_read_destination = destination_path in {"/viewinvoice.php", "/invoice.php"} or (
                    destination_path == "/cart.php" and destination_action in {"complete", "orderconfirmation"}
                ) or bool(destination_query.get("id") or destination_query.get("invoiceid"))
                if should_read_destination:
                    invoice_page = _vmiss_get(session, destination)
                    linked_invoice_url = _vmiss_invoice_url(invoice_page, destination)
                    detail_url = _vmiss_order_detail_url(invoice_page, destination)
                    if linked_invoice_url:
                        try:
                            if urlsplit(invoice_page.url or destination).path.casefold() in {"/viewinvoice.php", "/invoice.php"}:
                                verification = _vmiss_confirm_invoice_page(invoice_page, linked_invoice_url, item, product)
                            else:
                                verification = _vmiss_confirm_invoice(session, linked_invoice_url, item, product)
                            invoice_url = linked_invoice_url
                            progress("已从 VMISS 订单跳转定位账单详情并直接核验")
                        except (requests.RequestException, ValueError) as exc:
                            direct_detail_error = str(exc).strip() or exc.__class__.__name__
                    elif detail_url:
                        try:
                            verification = _vmiss_confirm_invoice_page(invoice_page, detail_url, item, product)
                            invoice_id = _vmiss_invoice_id(getattr(invoice_page, "text", ""))
                            invoice_url = f"{VMISS_BASE_URL}viewinvoice.php?id={invoice_id}" if invoice_id else detail_url
                            progress("已从 VMISS 订单跳转定位订单详情并直接核验")
                        except (requests.RequestException, ValueError) as exc:
                            direct_detail_error = str(exc).strip() or exc.__class__.__name__
            except ValueError:
                pass
    if invoice_url and verification is None:
        try:
            verification = _vmiss_confirm_invoice(session, invoice_url, item, product)
        except (requests.RequestException, ValueError) as exc:
            # If the response did not provide an authoritative invoice URL,
            # let the read-only list reconciliation locate the new invoice.
            direct_detail_error = str(exc).strip() or exc.__class__.__name__
            if not response_invoice_url:
                invoice_url = ""
    if not invoice_url:
        progress("订单响应未直接返回账单链接，正在读取 VMISS 账单列表")
        # The invoice row can lag behind the successful checkout response.
        # Re-read only the list and invoice details; the order submission above
        # is deliberately never repeated.
        matches = []
        reasons = []
        candidates = []
        for attempt in range(VMISS_INVOICE_POLL_ATTEMPTS):
            # Keep the first request identical to the normal client URL.  A
            # cache-busting query is used only for later reads so an upstream
            # proxy cannot keep returning the pre-order invoice list.
            list_url = VMISS_INVOICES_URL
            if attempt:
                list_url = f"{VMISS_INVOICES_URL}&hostwatch_refresh={int(time.time() * 1000)}"
            try:
                candidates = _vmiss_unpaid_invoice_candidates(session, include_unknown=True, list_url=list_url)
            except (requests.RequestException, ValueError) as exc:
                reasons.append(str(exc).strip() or exc.__class__.__name__)
                candidates = []
            progress(f"账单列表找到 {len(candidates)} 张待核验账单，正在检查账单详情")
            matches = []
            for candidate in candidates:
                candidate_id = parse_qs(urlsplit(candidate).query).get("id", [""])[0]
                progress(f"已找到 VMISS 待付款账单 {candidate_id or '未知'}（订单已创建），正在检查订单详情")
                try:
                    candidate_verification = _vmiss_confirm_invoice(session, candidate, item, product)
                except (requests.RequestException, ValueError) as exc:
                    reasons.append(str(exc).strip() or exc.__class__.__name__)
                    continue
                matches.append((candidate, candidate_verification))
            if matches:
                break
            if attempt + 1 < VMISS_INVOICE_POLL_ATTEMPTS:
                progress("新账单尚未出现在列表，稍后重试核验（不会重复下单）")
                time.sleep(VMISS_INVOICE_POLL_DELAY)
        if len(matches) == 1:
            invoice_url, verification = matches[0]
            progress(f"VMISS 账单 {parse_qs(urlsplit(invoice_url).query).get('id', ['未知'])[0]} 检查正常")
        elif not candidates:
            detail = f"；直接订单详情核验失败：{direct_detail_error[:120]}" if direct_detail_error else ""
            raise ValueError(f"VMISS 账单列表没有明确的未付款账单，未确认订单{detail}")
        elif not matches:
            detail = reasons[0][:180] if reasons else "账单详情均不匹配"
            raise ValueError(f"VMISS 账单列表找到 {len(candidates)} 张未付款账单，但没有匹配本次商品：{detail}")
        else:
            raise ValueError(f"VMISS 账单列表找到 {len(matches)} 张匹配本次商品的未付款账单，无法唯一确认")
    if not invoice_url:
        if 300 <= order_response.status_code < 400:
            raise ValueError("VMISS 订单提交后跳转到支付或未知页面，已停止且未确认订单")
        raise ValueError("VMISS 未返回待付款发票，未确认订单")
    invoice_id = _vmiss_invoice_id(invoice_url)
    if not invoice_id and verification is not None:
        invoice_id = _vmiss_invoice_id(getattr(order_response, "text", ""))
    if not invoice_id:
        raise ValueError("VMISS 已返回订单详情，但无法提取账单号，未确认订单")
    if verification is None:
        if direct_detail_error and response_invoice_url:
            raise ValueError(direct_detail_error)
        progress(f"已创建 VMISS 待付款订单，账单 {invoice_id} 已定位；正在检查订单详情")
        verification = _vmiss_confirm_invoice(session, invoice_url, item, product)
        progress(f"VMISS 账单 {invoice_id} 检查正常")
    return {"order_id": f"vmiss-invoice-{invoice_id}", "invoice_url": invoice_url, "payment_status": "unpaid",
            "invoice_verification": verification}


def _order_exception_detail(exc: Exception) -> str:
    """Keep order diagnostics useful without copying request secrets into logs."""
    if isinstance(exc, requests.RequestException):
        return f"{exc.__class__.__name__}（网络请求异常）"
    detail = str(exc).strip()
    detail = re.sub(r"https?://\S+", "[地址已隐藏]", detail, flags=re.IGNORECASE)
    detail = re.sub(r"(?i)(password|passwd|token|secret|authorization|cookie)[^；。,\s]*", r"\1=[已隐藏]", detail)
    return detail[:240] or exc.__class__.__name__


def akile_order_payload(item: dict, product, url: str) -> dict:
    """Build the same unpaid Akile order payload used by the shop page.

    This function only prepares the order request.  The caller must POST it to
    ``makeOrder`` and must never call Akile's payment endpoint.
    """
    operating_system = str(item.get("operating_system") or "Debian 12").strip()
    template_uuid = AKILE_DEBIAN_TEMPLATES.get(operating_system)
    if not template_uuid:
        raise ValueError("Akile 自动下单仅支持 Debian 11、Debian 12 或 Debian 13")
    billing_cycle = str(item.get("billing_cycle") or "monthly").strip().casefold()
    if billing_cycle != "monthly":
        raise ValueError("Akile 自动下单目前固定使用 1 个月周期")
    details = product.match_details if product and isinstance(product.match_details, dict) else {}
    plan = details.get("plan")
    if not isinstance(plan, dict):
        raise ValueError("Akile 商品目录未返回完整套餐信息，未提交订单")
    quantity = item.get("quantity", 1)
    if quantity != 1:
        raise ValueError("Akile 原生下单暂只支持单台订单")
    if item.get("akile_protocol_agreed") is not True or item.get("akile_broadcast_ip_ack") is not True:
        raise ValueError("Akile 服务协议和广播 IP 条款必须自动确认")
    data = {
        "order_data": [
            {"key": "password", "name": "密码", "value": "开通后服务器管理页面查看"},
            {"key": "template_uuid", "name": "模版", "value": template_uuid},
            {"key": "cycle", "name": "周期", "value": "1"},
            {"key": "system_name", "name": "系统名称", "value": operating_system},
            {"key": "area_name", "name": "区域名称", "value": str(details.get("area_name") or product.area_name or "")},
            {"key": "node_name", "name": "节点名称", "value": str(details.get("node_name") or product.node_name or "")},
            {"key": "plan_info", "name": "套餐详情", "value": json.dumps(plan, ensure_ascii=False, separators=(",", ":"))},
        ],
        "order_plan_id": details.get("plan_id") or product.card_id or product.id,
        "order_type": "1",
    }
    if not isinstance(data["order_plan_id"], int):
        try:
            data["order_plan_id"] = int(data["order_plan_id"])
        except (TypeError, ValueError):
            raise ValueError("Akile 套餐 ID 无效，未提交订单") from None
    return data


def akile_order_page_url(order_id: object) -> str:
    """Return the official Akile order page for a confirmed order number."""
    value = str(order_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("Akile 订单号格式无效，未生成付款链接")
    return f"{AKILE_ORDER_PAGE_BASE}{value}"


def akile_order_result(response, quantity: int = 1) -> dict:
    """Validate Akile's makeOrder response without following payment links."""
    data = order_json(response)
    if str(data.get("status_code")) not in {"0", "200"}:
        raise ValueError(str(data.get("status_msg") or "Akile 下单接口未确认订单"))
    body = data.get("data")
    if not isinstance(body, dict):
        raise ValueError("Akile 下单响应缺少订单信息")
    order_no = body.get("order_no") or body.get("order_id")
    if not isinstance(order_no, (str, int)) or not str(order_no).strip():
        raise ValueError("Akile 下单响应缺少订单号")
    order_id = str(order_no).strip()[:128]
    return {"order_id": order_id, "order_url": akile_order_page_url(order_id), "quantity": quantity}


def akile_confirm_order(session, order_id: str, item: dict | None = None, product=None) -> dict:
    """Read Akile's official order detail and require the same unpaid order."""
    response = session.get(AKILE_ORDER_DETAIL_URL, params={"orderNo": order_id},
                           timeout=(5, 20), allow_redirects=False)
    data = order_json(response)
    if str(data.get("status_code")) not in {"0", "200"}:
        raise ValueError(str(data.get("status_msg") or "Akile 订单详情接口未确认订单"))
    detail = data.get("data")
    if not isinstance(detail, dict):
        raise ValueError("Akile 订单详情缺少订单信息")
    returned_id = detail.get("order_no") or detail.get("order_id")
    if str(returned_id or "").strip() != str(order_id).strip():
        raise ValueError("Akile 订单详情与本次订单号不匹配")
    status = detail.get("order_status")
    if str(status) != "1":
        labels = {"2": "已支付", "3": "已关闭", "4": "已作废"}
        raise ValueError(f"Akile 订单状态不是待付款：{labels.get(str(status), str(status or '未知'))}")
    if detail.get("order_type") not in {None, 1, "1"}:
        raise ValueError("Akile 订单类型与 VPS 商品不匹配")
    return {"status": "unpaid", "order_id": str(order_id).strip(),
            "order_url": akile_order_page_url(order_id), "order_status": 1}


def akile_market_cycle_days(cycle: object) -> int:
    # Akile server_cycle is the renewal period in months (1 = month, 12 = year).
    if isinstance(cycle, bool):
        return 0
    return {"1": 30, "3": 90, "6": 180, "12": 365, "24": 730, "36": 1095}.get(str(cycle), 0)


def _monthly_price(amount: float, period: object, unit: object = "month") -> tuple[float | None, int]:
    """Return the recurring price normalized to one month and month count."""
    if isinstance(period, bool):
        return None, 0
    try:
        number = int(period)
    except (TypeError, ValueError):
        return None, 0
    unit = str(unit or "month").casefold()
    months = number if unit == "month" else number * 12 if unit == "year" else 0
    return (amount / months, months) if months > 0 else (None, 0)


def akile_market_valuation(row: dict, now: float | None = None) -> dict:
    """Compare asking price with the renewal amount normalized to one month."""
    product_id = str(row.get("id", ""))
    if not re.fullmatch(r"[1-9][0-9]{0,18}", product_id):
        raise ValueError("Akile 交易市场商品 ID 无效")
    try:
        fields = [row.get(key) for key in ("price", "server_price", "due_time")]
        if any(isinstance(value, bool) for value in fields):
            raise ValueError()
        price, renewal, due_time = map(float, fields)
        cycle_days = akile_market_cycle_days(row.get("server_cycle"))
        cycle_months = int(row.get("server_cycle")) if cycle_days else 0
        if not all(math.isfinite(value) and value >= 0 for value in (price, renewal, due_time)):
            raise ValueError()
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Akile 商品价格或到期时间无效") from None
    price, renewal = price / 100, renewal / 100
    monthly_price = renewal / cycle_months if cycle_months else None
    remaining_days = max(0, (due_time - (time.time() if now is None else now)) / 86400)
    fair_value = renewal * remaining_days / cycle_days if cycle_days else None
    delta = fair_value - price if fair_value is not None else None
    rate = delta / monthly_price * 100 if delta is not None and monthly_price and monthly_price > 0 else None
    return {"id": product_id, "name": str(row.get("name") or row.get("plan_name") or "")[:160],
        "plan_name": str(row.get("plan_name") or "")[:160],
        "region": str(row.get("area_name") or "")[:80], "node": str(row.get("node_name") or "")[:120],
        **{key: row.get(key) for key in ("cpu", "memory", "disk", "bandwidth", "flow", "flow_used", "ipv4_num", "ipv6_num", "status")},
        "is_bid": row.get("is_bid") is not False, "detail": str(row.get("detail") or "")[:240],
        "price": price, "renewal": renewal, "cycle": row.get("server_cycle"), "cycle_months": cycle_months, "cycle_days": cycle_days,
        "due_time": due_time, "remaining_days": remaining_days, "fair_value": fair_value, "monthly_price": monthly_price,
        "delta": delta, "rate": rate, "updated_at": beijing_iso(),
        "valuation_error": "未知续费周期" if monthly_price is None else "已到期或月付价格为零，无法估值" if rate is None else ""}


def akile_market_list(session: requests.Session, *, area: str = "", name: str = "", page_size: int = 30,
                      max_items: int = 600) -> list[dict]:
    """Deduplicate Akile's unstable ordering of equally priced listings."""
    rows, seen, page, expected, scanned = [], set(), 1, None, 0
    session._hostwatch_market_incomplete = False
    while scanned < max_items:
        payload = order_json(session.post(AKILE_MARKET_LIST_URL,
            json={"area_name": area[:80], "name": name[:120], "page_num": page, "page_size": page_size},
            timeout=(5, 20), allow_redirects=False))
        if str(payload.get("status_code")) not in {"0", "200"}:
            raise ValueError("Akile 市场读取失败，请检查登录或访问限制")
        batch, total = payload.get("list"), payload.get("total")
        if type(total) is not int or total < 0 or total > max_items:
            raise ValueError("Akile 市场数量异常或超过读取上限，请缩小监控范围")
        if expected is not None and total != expected:
            raise ValueError("市场分页期间上架数量变化，本轮不触发，请稍后刷新")
        expected = total
        if total == 0 and batch in (None, []):
            return []
        if not isinstance(batch, list) or not batch:
            raise ValueError("Akile 市场分页数据不完整，本轮不触发")
        before = len(rows)
        for row in batch:
            if not isinstance(row, dict):
                raise ValueError("Akile 市场分页商品格式异常")
            identifier = str(row.get("id", ""))
            if identifier not in seen:
                seen.add(identifier)
                rows.append(row)
        if len(rows) == before:
            raise ValueError("Akile 市场分页未取得新商品，请稍后刷新")
        scanned += len(batch)
        if scanned >= total:
            if scanned != total:
                raise ValueError("Akile 市场分页数量不一致")
            session._hostwatch_market_incomplete = len(rows) != total
            return rows
        page += 1
    raise ValueError("Akile 市场读取超出上限")


def akile_market_detail(session: requests.Session, product_id: str) -> dict:
    if not re.fullmatch(r"[1-9][0-9]{0,18}", str(product_id)):
        raise ValueError("Akile 交易市场商品 ID 无效")
    payload = order_json(session.get(AKILE_MARKET_DETAIL_URL, params={"id": product_id},
        timeout=(5, 20), allow_redirects=False))
    if (str(payload.get("status_code")) not in {"0", "200"} or not isinstance(payload.get("data"), dict)
            or str(payload["data"].get("id")) != str(product_id)):
        raise ValueError("Akile 商品详情无法确认或已下架")
    return payload["data"]


def normalize_akile_market(value: object, accounts: list[dict]) -> dict:
    value = {} if value is None else value
    if not isinstance(value, dict):
        raise ValueError("Akile 交易市场配置必须是对象")
    enabled = value.get("enabled", False)
    if type(enabled) is not bool:
        raise ValueError("Akile 市场启用状态必须是布尔值")
    interval = value.get("interval_seconds", 60)
    if type(interval) is not int or not 30 <= interval <= 3600:
        raise ValueError("Akile 市场刷新间隔必须是 30 到 3600 秒")
    account_id = str(value.get("account_id", ""))
    account_ids = {row["id"] for row in accounts if row.get("provider") == "akile"}
    if account_id and account_id not in account_ids:
        raise ValueError("请为交易市场选择已保存的 Akile 网站账户")
    rows = value.get("items", [])
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError("Akile 市场规则最多 100 项")
    normalized, seen = [], set()
    for raw in rows:
        if not isinstance(raw, dict):
            raise ValueError("Akile 市场规则无效")
        identifier = str(raw.get("id", ""))
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", identifier) or identifier in seen:
            raise ValueError("Akile 市场规则 ID 必须唯一")
        seen.add(identifier)
        rule = {"id": identifier}
        for key, allowed, default in (("target", {"listing", "plan"}, "listing"),
                ("direction", {"discount", "premium"}, "discount"),
                ("action", {"notify", "unpaid_order"}, "notify"), ("match", {"any", "all"}, "any")):
            rule[key] = str(raw.get(key, default))
            if rule[key] not in allowed:
                raise ValueError("Akile 市场规则类型无效")
        rule["enabled"] = raw.get("enabled", True)
        if type(rule["enabled"]) is not bool:
            raise ValueError("Akile 市场规则启用状态必须是布尔值")
        for key, limit in (("product_id", 19), ("name", 160), ("plan_name", 160), ("region", 80), ("node", 120)):
            rule[key] = str(raw.get(key, "")).strip()
            if len(rule[key]) > limit:
                raise ValueError("Akile 市场规则字段过长")
        market_info = raw.get("market_info")
        if market_info is not None:
            if not isinstance(market_info, dict):
                raise ValueError("Akile 市场商品信息无效")
            normalized_info = {}
            for key in ("cpu", "memory", "disk", "bandwidth", "flow"):
                field = market_info.get(key)
                if field is None:
                    continue
                if isinstance(field, (bool, dict, list)):
                    raise ValueError("Akile 市场商品配置无效")
                field = str(field).strip()
                if len(field) > 80:
                    raise ValueError("Akile 市场商品配置字段过长")
                if field:
                    normalized_info[key] = field
            renewal = market_info.get("renewal")
            if renewal is not None:
                if (isinstance(renewal, bool) or not isinstance(renewal, (int, float))
                        or not math.isfinite(renewal) or not 0 <= renewal <= 1000000):
                    raise ValueError("Akile 市场续费金额无效")
                normalized_info["renewal"] = float(renewal)
            cycle = market_info.get("cycle")
            if cycle is not None:
                if isinstance(cycle, (bool, dict, list)) or len(str(cycle).strip()) > 12:
                    raise ValueError("Akile 市场续费周期无效")
                if str(cycle).strip():
                    normalized_info["cycle"] = str(cycle).strip()
            if normalized_info:
                rule["market_info"] = normalized_info
        if rule["target"] == "listing" and not re.fullmatch(r"[1-9][0-9]{0,18}", rule["product_id"]):
            raise ValueError("监控单条挂单必须填写有效的市场商品 ID")
        if rule["target"] == "plan" and not all(rule[key] for key in ("plan_name", "region", "node")):
            raise ValueError("监控同套餐需指定完整套餐名、地区和节点")
        for key in ("rate_threshold", "amount_threshold"):
            number = raw.get(key)
            if number is not None and (isinstance(number, bool) or not isinstance(number, (int, float))
                    or not math.isfinite(number) or not 0 <= number <= 1000000):
                raise ValueError("折价/溢价阈值必须是非负有限数字，留空不使用")
            rule[key] = float(number) if number is not None else None
        if rule["rate_threshold"] is None and rule["amount_threshold"] is None:
            raise ValueError("每条市场规则至少设置一个比例或金额阈值")
        normalized.append(rule)
    if enabled and (not account_id or not any(row["enabled"] for row in normalized)):
        raise ValueError("启用交易市场前请选择 Akile 账户并添加启用的规则")
    return {"enabled": enabled, "interval_seconds": interval, "account_id": account_id, "items": normalized}


def akile_market_rule_matches(rule: dict, item: dict) -> bool:
    if (item.get("valuation_error") or item.get("remaining_days", 0) <= 0
            or item.get("is_bid") is not False or type(item.get("status")) is not int or item["status"] != 0):
        return False
    if rule["target"] == "listing":
        if rule["product_id"] != item["id"]:
            return False
    elif any(rule[key] != item[key] for key in ("plan_name", "region", "node")):
        return False
    results = []
    for key, actual in (("rate_threshold", item["rate"]), ("amount_threshold", item["delta"])):
        if rule.get(key) is not None:
            # Discount is a minimum saving; premium is a maximum extra cost.
            results.append(actual >= rule[key] if rule["direction"] == "discount" else -actual <= rule[key])
    return bool(results) and (all(results) if rule.get("match") == "all" else any(results))


def _normalize_market_config(value: object, accounts: list[dict], provider: str, label: str) -> dict:
    """Normalize the shared read-only marketplace rule format for a provider."""
    value = {} if value is None else value
    if not isinstance(value, dict):
        raise ValueError(f"{label}交易市场配置必须是对象")
    enabled = value.get("enabled", False)
    if type(enabled) is not bool:
        raise ValueError(f"{label}市场启用状态必须是布尔值")
    interval = value.get("interval_seconds", 600 if provider == "isvoro" else 60)
    # Isvoro's previous default was 60 seconds; move that legacy default to
    # the safer 10-minute cadence while preserving other explicit intervals.
    if provider == "isvoro" and interval == 60:
        interval = 600
    if type(interval) is not int or not 30 <= interval <= 3600:
        raise ValueError(f"{label}市场刷新间隔必须是 30 到 3600 秒")
    account_id = str(value.get("account_id", ""))
    account_ids = {row["id"] for row in accounts if row.get("provider") == provider}
    if account_id and account_id not in account_ids:
        raise ValueError(f"请为交易市场选择已保存的 {label} 网站账户")
    rows = value.get("items", [])
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError(f"{label}市场规则最多 100 项")
    normalized, seen = [], set()
    for raw in rows:
        if not isinstance(raw, dict):
            raise ValueError(f"{label}市场规则无效")
        identifier = str(raw.get("id", ""))
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", identifier) or identifier in seen:
            raise ValueError(f"{label}市场规则 ID 必须唯一")
        seen.add(identifier)
        rule = {"id": identifier}
        for key, allowed, default in (("target", {"listing", "plan"}, "listing"),
                ("direction", {"discount", "premium"}, "discount"),
                ("action", {"notify"}, "notify"), ("match", {"any", "all"}, "any")):
            rule[key] = str(raw.get(key, default))
            if rule[key] not in allowed:
                raise ValueError(f"{label}市场规则类型无效")
        rule["enabled"] = raw.get("enabled", True)
        if type(rule["enabled"]) is not bool:
            raise ValueError(f"{label}市场规则启用状态必须是布尔值")
        for key, limit in (("product_id", 40), ("name", 160), ("plan_name", 160), ("region", 80), ("node", 120)):
            rule[key] = str(raw.get(key, "")).strip()
            if len(rule[key]) > limit:
                raise ValueError(f"{label}市场规则字段过长")
        if rule["target"] == "listing" and not re.fullmatch(r"[1-9][0-9]{0,38}", rule["product_id"]):
            raise ValueError(f"监控单条挂单必须填写有效的 {label} 市场商品 ID")
        if rule["target"] == "plan" and not all(rule[key] for key in ("plan_name", "region", "node")):
            raise ValueError("监控同套餐需指定完整套餐名、地区和节点")
        for key in ("rate_threshold", "amount_threshold"):
            number = raw.get(key)
            if number is not None and (isinstance(number, bool) or not isinstance(number, (int, float))
                    or not math.isfinite(number) or not 0 <= number <= 1000000):
                raise ValueError("折价/溢价阈值必须是非负有限数字，留空不使用")
            rule[key] = float(number) if number is not None else None
        if rule["rate_threshold"] is None and rule["amount_threshold"] is None:
            raise ValueError("每条市场规则至少设置一个比例或金额阈值")
        normalized.append(rule)
    if enabled and (not account_id or not any(row["enabled"] for row in normalized)):
        raise ValueError(f"启用交易市场前请选择 {label} 账户并添加启用的规则")
    return {"enabled": enabled, "interval_seconds": interval, "account_id": account_id, "items": normalized}


def normalize_isvoro_market(value: object, accounts: list[dict]) -> dict:
    return _normalize_market_config(value, accounts, "isvoro", "Isvoro")


def _isvoro_market_cycle_days(period: object, unit: object) -> int:
    if isinstance(period, bool):
        return 0
    try:
        period = int(period)
    except (TypeError, ValueError):
        return 0
    unit = str(unit or "month").casefold()
    if unit == "month":
        return {1: 30, 3: 90, 6: 180, 12: 365, 24: 730, 36: 1095}.get(period, 0)
    if unit == "year":
        return period * 365 if 1 <= period <= 10 else 0
    return 0


def _isvoro_market_timestamp(value: object) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        from datetime import datetime, timezone
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def isvoro_market_valuation(row: dict, now: float | None = None) -> dict:
    product_id = str(row.get("id", ""))
    if not re.fullmatch(r"[1-9][0-9]{0,38}", product_id):
        raise ValueError("Isvoro 交易市场商品 ID 无效")
    server = row.get("server_info") if isinstance(row.get("server_info"), dict) else {}
    service = row.get("service") if isinstance(row.get("service"), dict) else {}
    product = service.get("product") if isinstance(service.get("product"), dict) else {}
    try:
        price = float(row.get("price"))
        renewal = float(server.get("renewal_price"))
        expires_at = _isvoro_market_timestamp(server.get("expires_at") or row.get("expires_at"))
        period = server.get("billing_period", service.get("billing_period", 1))
        unit = server.get("billing_unit", service.get("billing_unit", "month"))
        cycle_days = _isvoro_market_cycle_days(period, unit)
        monthly_price, cycle_months = _monthly_price(renewal, period, unit) if cycle_days else (None, 0)
        renewal_currency = str(server.get("renewal_currency") or row.get("currency_code") or "CNY").strip().upper()
        currency = str(row.get("currency_code") or "CNY").strip().upper()
        if (not all(math.isfinite(value) and value >= 0 for value in (price, renewal, expires_at))
                or expires_at <= 0 or renewal_currency != currency):
            raise ValueError()
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Isvoro 商品价格或到期时间无效") from None
    remaining_days = max(0, (expires_at - (time.time() if now is None else now)) / 86400)
    fair_value = renewal * remaining_days / cycle_days if cycle_days else None
    delta = fair_value - price if fair_value is not None else None
    rate = delta / monthly_price * 100 if delta is not None and monthly_price and monthly_price > 0 else None
    config = server.get("config_summary") or server.get("network_summary") or ""
    return {"id": product_id, "name": str(server.get("hostname") or product.get("name") or server.get("plan_name") or "")[:160],
        "plan_name": str(server.get("plan_name") or product.get("name") or "")[:160],
        "region": str(server.get("location_name") or server.get("region") or "")[:80],
        "node": str(server.get("node") or server.get("location_name") or "")[:120],
        "cpu": (server.get("spec") or {}).get("cores") if isinstance(server.get("spec"), dict) else "",
        "memory": (server.get("spec") or {}).get("memory_mb") if isinstance(server.get("spec"), dict) else "",
        "disk": (server.get("spec") or {}).get("disk_gb") if isinstance(server.get("spec"), dict) else "",
        "bandwidth": (server.get("spec") or {}).get("bandwidth_mbps") if isinstance(server.get("spec"), dict) else "",
        "flow": (server.get("spec") or {}).get("traffic_gb") if isinstance(server.get("spec"), dict) else "",
        "status": 0 if str(row.get("status", "active")).casefold() in {"active", "available", ""} else 1,
        "is_bid": False, "detail": config[:240], "price": price, "renewal": renewal,
        "cycle": period, "cycle_unit": unit, "cycle_months": cycle_months, "cycle_days": cycle_days, "due_time": expires_at,
        "remaining_days": remaining_days, "fair_value": fair_value, "monthly_price": monthly_price, "delta": delta, "rate": rate,
        "currency_code": currency[:12], "updated_at": beijing_iso(),
        "valuation_error": "未知续费周期" if monthly_price is None else "月付价格为零，无法估值" if rate is None else ""}


def isvoro_market_list(session: requests.Session, *, query: str = "", location_id: object = "", page_size: int = 200,
                       max_items: int = 1000) -> list[dict]:
    rows, page, expected = [], 1, None
    while len(rows) < max_items:
        params = {"page": page, "per_page": page_size}
        if query:
            params["q"] = str(query)[:120]
        if str(location_id).strip():
            params["location_id"] = str(location_id).strip()
        payload = order_json(session.get(ISVORO_MARKET_LIST_URL, params=params, timeout=(5, 20), allow_redirects=False,
                                         headers={"Accept": "application/json"}))
        if payload.get("code") not in {0, "0"}:
            raise ValueError("Isvoro 市场读取失败，请检查登录或访问限制")
        batch, meta = payload.get("data"), payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        total = meta.get("total", len(batch) if isinstance(batch, list) else None)
        if not isinstance(batch, list) or not isinstance(total, int) or total < 0 or total > max_items:
            raise ValueError("Isvoro 市场分页数据异常或超过读取上限")
        if expected is not None and total != expected:
            raise ValueError("Isvoro 市场分页期间上架数量变化，本轮不触发")
        expected = total
        rows.extend(row for row in batch if isinstance(row, dict))
        if not batch or len(rows) >= total:
            if len(rows) != total:
                raise ValueError("Isvoro 市场分页数据不完整，本轮不触发")
            return rows
        page += 1
    raise ValueError("Isvoro 市场读取超出上限")


def _bwh_canonical_url(url: str) -> str:
    parsed = urlsplit(str(url or "").strip())
    if parsed.scheme != "https" or (parsed.hostname or "").casefold() not in {"bwh81.net", "www.bwh81.net", "bandwagonhost.com", "www.bandwagonhost.com"}:
        raise ValueError("搬瓦工购买链接域名无效")
    return urlunsplit(("https", "bwh81.net", parsed.path or "/", parsed.query, ""))


def _bwh_form_values(form) -> dict:
    """Collect hidden and selected form values without submitting payment fields."""
    values = {}
    for node in form.find_all(["input", "select", "textarea"]):
        name = str(node.get("name", "")).strip()
        if not name:
            continue
        if node.name == "select":
            option = node.find("option", selected=True) or node.find("option")
            if option is not None:
                values[name] = option.get("value", "")
            continue
        input_type = str(node.get("type", "text")).casefold()
        if input_type in {"submit", "button", "image", "file", "radio", "checkbox"}:
            if input_type == "checkbox" and node.has_attr("checked"):
                values[name] = node.get("value", "on")
            continue
        values[name] = node.get("value", "")
    return values


def _bwh_invoice_id_from_page(response) -> str:
    """Extract a pending BWH invoice number from confirmation-page variants."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(response.content or response.text or "", "html.parser")
    text = soup.get_text(" ", strip=True)
    if not re.search(r"\b(?:unpaid|pending)\b|未付款|待付款", text, re.I):
        return ""
    for node in soup.find_all("a", href=True):
        match = re.search(r"/(?:billing|invoice)(?:/|\.php\?[^\s#>]*(?:id|invoiceid)=)([0-9]+)", str(node.get("href", "")), re.I)
        if match:
            return match.group(1)
    match = re.search(r"(?:Invoice|账单)\s*(?:[#№:]|No\.?|编号)?\s*([0-9]{1,20})", text, re.I)
    return match.group(1) if match else ""


def bwh_order(session, item: dict, account: dict, product, url: str, key: str | None = None) -> dict:
    """Create one BandwagonHost invoice and stop at its unpaid billing page."""
    from bs4 import BeautifulSoup
    from urllib.parse import parse_qs

    if item.get("quantity", 1) != 1:
        raise ValueError("搬瓦工官网结算适配目前只支持单台订单")
    cart_url = _bwh_canonical_url(url)
    parsed = urlsplit(cart_url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    cycle = str(item.get("billing_cycle") or (query.get("billingcycle") or [""])[0]).strip().casefold()
    if cycle not in ORDER_BILLING_CYCLES:
        raise ValueError("搬瓦工付款周期必须是月付、季付、半年付或年付")
    query["billingcycle"] = [cycle]
    cart_url = urlunsplit(("https", "bwh81.net", parsed.path, urlencode(query, doseq=True), ""))
    response = session.get(cart_url, timeout=(5, 20), allow_redirects=True, headers={**MERCHANT_BROWSER_HEADERS, "Sec-Fetch-Site": "same-origin"})
    if not 200 <= response.status_code < 300 or not _same_merchant_url(response.url or cart_url, MERCHANT_LOGIN_ADAPTERS["bwh"]):
        raise ValueError("搬瓦工商品配置页无法读取，未提交订单")
    soup = BeautifulSoup(response.content or response.text or "", "html.parser")
    config_form = next((form for form in soup.find_all("form") if form.find("select", attrs={"name": "configoption[17]"})), None)
    if config_form is None:
        raise ValueError("搬瓦工商品配置页结构已变化，未提交订单")
    config_action = urljoin(response.url, config_form.get("action") or response.url)
    if not _same_merchant_url(config_action, MERCHANT_LOGIN_ADAPTERS["bwh"]):
        raise ValueError("搬瓦工配置提交地址不符合商家域名，未提交订单")
    config = _bwh_form_values(config_form)
    config["configure"] = "true"
    config["billingcycle"] = cycle
    config_response = session.post(config_action, data=config, timeout=(5, 20), allow_redirects=True,
                                   headers={**MERCHANT_BROWSER_HEADERS, "Referer": response.url, "Sec-Fetch-Site": "same-origin"})
    if not 200 <= config_response.status_code < 300 or not _same_merchant_url(config_response.url or config_action, MERCHANT_LOGIN_ADAPTERS["bwh"]):
        raise ValueError("搬瓦工商品配置提交失败，未提交订单")
    checkout = session.get("https://bwh81.net/cart.php?a=checkout", timeout=(5, 20), allow_redirects=False,
                           headers={**MERCHANT_BROWSER_HEADERS, "Referer": config_response.url, "Sec-Fetch-Site": "same-origin"})
    if not 200 <= checkout.status_code < 300:
        raise ValueError("搬瓦工结算页无法读取，未提交订单")
    checkout_soup = BeautifulSoup(checkout.content or checkout.text or "", "html.parser")
    checkout_form = next((form for form in checkout_soup.find_all("form") if form.get("action", "").endswith("cart.php?a=checkout")), None)
    if checkout_form is None:
        raise ValueError("搬瓦工结算页结构已变化，未提交订单")
    checkout_action = urljoin(checkout.url, checkout_form.get("action") or checkout.url)
    if not _same_merchant_url(checkout_action, MERCHANT_LOGIN_ADAPTERS["bwh"]):
        raise ValueError("搬瓦工结算提交地址不符合商家域名，未提交订单")
    payload = _bwh_form_values(checkout_form)
    payload.update({"submit": "true", "custtype": "existing", "paymentmethod": BWH_PAYMENT_METHODS.get(str(item.get("payment_method") or "alipay").strip().casefold(), "")})
    if not payload["paymentmethod"]:
        raise ValueError("搬瓦工付款方式不受支持，未提交订单")
    payload["accepttos"] = "on"
    payload["loginemail"] = account.get("username", "")
    # The session is already authenticated. Do not send credit-card fields or
    # account-credit flags; this request only creates the invoice.
    for name in ("ccnumber", "cccvv", "ccinfo", "ccexpirydate", "ccexpirymonth", "ccexpiryyear", "applycredit"):
        payload.pop(name, None)
    submitted = session.post(checkout_action, data=payload, timeout=(5, 20), allow_redirects=False,
                             headers={**MERCHANT_BROWSER_HEADERS, "Referer": checkout.url, "Sec-Fetch-Site": "same-origin",
                                      "Content-Type": "application/x-www-form-urlencoded"})
    checked = submitted
    for _ in range(6):
        location = checked.headers.get("Location", "")
        if not location:
            break
        destination = urljoin(checked.url or checkout_action, location)
        if not _same_merchant_url(destination, MERCHANT_LOGIN_ADAPTERS["bwh"]):
            raise ValueError("搬瓦工下单跳转无法安全确认，未进入支付")
        if "/payment" in urlsplit(destination).path.casefold():
            raise ValueError("搬瓦工下单跳转到了支付页面，已停止")
        checked = session.get(destination, timeout=(5, 20), allow_redirects=False,
                              headers={**MERCHANT_BROWSER_HEADERS, "Referer": checked.url or checkout_action, "Sec-Fetch-Site": "same-origin"})
        if not 300 <= checked.status_code < 400:
            break
    if not 200 <= checked.status_code < 300:
        raise ValueError(f"搬瓦工下单返回 HTTP {checked.status_code}，未确认订单")
    invoice_id = _bwh_invoice_id_from_page(checked)
    if not invoice_id:
        raise ValueError("搬瓦工未返回可确认的未付款账单")
    invoice_soup = BeautifulSoup(checked.content or checked.text or "", "html.parser")
    text = invoice_soup.get_text(" ", strip=True)
    if product:
        product_name = str(getattr(product, "name", "") or "").split(" · ", 1)[0]
        if product_name and product_name.casefold() not in text.casefold():
            raise ValueError("搬瓦工账单商品与本次商品不匹配")
        details = getattr(product, "match_details", None) or {}
        datacenter_id = str(details.get("datacenter_id", "")).strip()
        if datacenter_id and datacenter_id.casefold() not in text.casefold():
            raise ValueError("搬瓦工账单机房与本次商品不匹配")
    invoice_url = f"https://bwh81.net/billing/{invoice_id}"
    return {"order_id": invoice_id, "invoice_url": invoice_url, "payment_status": "unpaid"}


def check_order_account(session, account: dict, fetcher: dict | None = None) -> dict:
    result = {"status": "unknown", "ok": False, "account_id": account.get("id", ""),
        "checked_at": beijing_iso()}
    adapter_id = merchant_login_id(account.get("provider"))
    supplied_cookie = str(account.get("_session_cookie", "")).strip()
    supplied_user_agent = str(account.get("_session_user_agent", "")).strip()
    cached_cookie_session = _get_merchant_session(adapter_id, account, fetcher) if adapter_id in {"bwh", "dmit"} else None
    if not account.get("username") or (not account.get("password") and not (adapter_id in {"bwh", "dmit"} and (supplied_cookie or cached_cookie_session))):
        return {**result, "message": "请填写用户名和密码；DMIT、搬瓦工也可填写用户名并导入有效 Cookie 检查"}
    try:
        if supplied_user_agent and len(supplied_user_agent) <= 512 and "\r" not in supplied_user_agent and "\n" not in supplied_user_agent:
            session.headers["User-Agent"] = supplied_user_agent
        adapter = MERCHANT_LOGIN_ADAPTERS.get(adapter_id)
        if adapter:
            if account.get("login_url") != adapter["url"]:
                return {**result, "message": "商家登录地址与内置适配不一致，请重新选择商家并保存"}
            if adapter_id == "isvoro":
                supplied_cookie = str(account.get("_session_cookie", "")).strip()
                if supplied_cookie:
                    if not _import_isvoro_session_cookie(session, supplied_cookie):
                        return {**result, "message": "Isvoro 会话 Cookie 格式无效，请复制完整 Cookie 后重试"}
                    if _isvoro_authenticated(session, account):
                        _store_merchant_session(adapter_id, account, fetcher, session)
                        return {**result, "ok": True, "status": "valid", "session_reused": True,
                                "message": "Isvoro 已导入并缓存人工登录会话（不代表具有购买资格或库存）"}
                    return {**result, "message": "Isvoro 会话 Cookie 已失效，请重新登录官网并复制最新 Cookie"}
                cached = _get_merchant_session(adapter_id, account, fetcher)
                if cached and _isvoro_authenticated(cached, account):
                    _renew_merchant_session(adapter_id, account, fetcher, cached)
                    _copy_merchant_session(cached, session)
                    return {**result, "ok": True, "status": "valid", "session_reused": True,
                            "message": "Isvoro 已复用认证会话（内存缓存最长 30 天；不代表具有购买资格或库存）"}
                if cached:
                    _remove_merchant_session(adapter_id, account, fetcher)
                checked = _isvoro_login(session, account)
                if checked["status"] == "valid":
                    _store_merchant_session(adapter_id, account, fetcher, session)
                return {**result, "ok": checked["status"] == "valid", **checked}
            if adapter_id == "narwhal":
                cached = _get_merchant_session(adapter_id, account, fetcher)
                if cached and _narwhal_authenticated(cached, account):
                    _renew_merchant_session(adapter_id, account, fetcher, cached)
                    _copy_merchant_session(cached, session)
                    return {**result, "ok": True, "status": "valid", "session_reused": True,
                            "message": "独角鲸云已复用认证会话（内存缓存最长 30 天；不代表具有购买资格或库存）"}
                if cached:
                    _remove_merchant_session(adapter_id, account, fetcher)
                checked = _narwhal_login(session, account)
                if checked["status"] == "valid":
                    _store_merchant_session(adapter_id, account, fetcher, session)
                return {**result, "ok": checked["status"] == "valid", **checked}
            if adapter_id == "bwh" and supplied_cookie:
                if not _import_bwh_session_cookie(session, supplied_cookie):
                    return {**result, "message": "搬瓦工会话 Cookie 格式无效，请复制完整 Cookie 后重试"}
                if _merchant_authenticated(session, adapter):
                    _store_merchant_session(adapter_id, account, fetcher, session)
                    return {**result, "ok": True, "status": "valid", "session_reused": True,
                            "message": "搬瓦工已导入并缓存官网登录会话（最长 30 天；不代表具有购买资格或库存）"}
                return {**result, "message": "搬瓦工会话 Cookie 无效或已过期，请重新登录官网并复制最新 Cookie"}
            if adapter_id == "dmit" and supplied_cookie:
                if not _import_dmit_session_cookie(session, supplied_cookie):
                    return {**result, "message": "DMIT 会话 Cookie 格式无效，请复制完整 Cookie 后重试"}
                if _merchant_authenticated(session, adapter):
                    _store_merchant_session(adapter_id, account, fetcher, session)
                    return {**result, "ok": True, "status": "valid", "session_reused": True,
                            "message": "DMIT 已导入并缓存官网登录会话（最长 30 天；不代表具有购买资格或库存）"}
                cookie_names = ", ".join(_merchant_cookie_names(supplied_cookie)[:20]) or "未识别"
                return {**result, "message": f"DMIT 会话 Cookie 未通过详情页验证（已读取 {len(_merchant_cookie_names(supplied_cookie))} 个 Cookie：{cookie_names}）；可能是 Cookie 不完整、浏览器 User-Agent 不匹配，或 DMIT/Cloudflare 绑定浏览器与出口 IP。请复制完整 Cookie，并填写浏览器 User-Agent 后重试"}
            if adapter_id == "dmit" and str((fetcher or {}).get("mode", "")).casefold() == "flaresolverr" and account.get("password"):
                checked = _dmit_flaresolverr_account_check(account, fetcher)
                return {**result, **checked}
            cached = _get_merchant_session(adapter_id, account, fetcher)
            if cached and (_merchant_cached_session_reusable(fetcher) or _merchant_authenticated(cached, adapter)):
                _renew_merchant_session(adapter_id, account, fetcher, cached)
                _copy_merchant_session(cached, session)
                return {**result, "ok": True, "status": "valid", "session_reused": True,
                        "message": f"{adapter['name']} 已复用认证会话（内存缓存最长 30 天；不代表具有购买资格或库存）"}
            if cached:
                _remove_merchant_session(adapter_id, account, fetcher)
            if adapter_id == "bwh" and not account.get("password"):
                return {**result, "message": "搬瓦工 Cookie 已失效；请重新导入 Cookie，或填写密码后通过邮箱验证码登录"}
            if adapter_id == "dmit" and not account.get("password"):
                return {**result, "message": "DMIT Cookie 已失效；请重新从官网登录并导入 Cookie，或填写密码登录"}
            checked = _merchant_form_login(session, account, adapter, fetcher)
            if checked["status"] == "valid":
                _store_merchant_session(adapter_id, account, fetcher, session)
                return {**result, "ok": True, **checked}
            return {**result, **checked}
        if adapter_id == "akile":
            if account.get("login_url") != AKILE_LOGIN_URL:
                return {**result, "message": "Akile 登录地址与内置适配不一致，请重新选择商家并保存"}
            cached = _get_merchant_session(adapter_id, account, fetcher)
            if cached and _akile_authenticated(cached, account):
                _renew_merchant_session(adapter_id, account, fetcher, cached)
                _copy_merchant_session(cached, session)
                return {**result, "ok": True, "status": "valid", "session_reused": True,
                        "message": "Akile 已复用认证会话（内存缓存最长 30 天；不代表具有购买资格或库存）"}
            if cached:
                _remove_merchant_session(adapter_id, account, fetcher)
            checked = _akile_login(session, account)
            if checked["status"] == "valid":
                _store_merchant_session(adapter_id, account, fetcher, session)
                return {**result, "ok": True, **checked}
            return {**result, **checked}
        if adapter_id:
            return {**result, "message": "该商家的登录含 API 验证、验证码或二次验证，目前无法安全自动确认；请到官网登录"}
        if not account.get("login_url"):
            return {**result, "message": "请填写其他商家提供的 HostWatch 账户检查 JSON 接口"}
        response = session.post(account["login_url"], json={"protocol": ORDER_PROTOCOL,
            "action": "check_account", "username": account["username"], "password": account["password"]},
            timeout=(5, 20), allow_redirects=False)
        if response.status_code in {401, 403}:
            return {**result, "status": "invalid", "message": "账户认证失败或访问受限，请到官网检查"}
        data = order_json(response)
        if data.get("authenticated") is False:
            return {**result, "status": "invalid", "message": "账户认证失败，请检查密码、验证码或二次验证"}
        if data.get("protocol") == ORDER_PROTOCOL and data.get("authenticated") is True and data.get("username") == account["username"]:
            return {**result, "ok": True, "status": "valid", "message": "账户认证正常（不代表具有购买资格或库存）"}
        return {**result, "message": "未取得明确的账户认证凭据；需适配登录接口或到官网检查"}
    except (requests.RequestException, ValueError):
        return {**result, "message": "无法验证账户：网络异常、跳转或接口未适配，请到官网检查"}


def merchant_account_status(account_id: str) -> dict:
    """Probe only an existing in-memory auth session; never submit credentials."""
    config = current_config()
    account = next((row for row in config["vps_seckill"].get("accounts", []) if row.get("id") == account_id), None)
    result = {"account_id": account_id, "status": "unknown", "ok": False, "checked_at": beijing_iso()}
    if not account:
        return {**result, "message": "网站账户不存在"}
    adapter_id = merchant_login_id(account.get("provider"))
    if not adapter_id:
        return {**result, "message": "该商家没有可复用的内置认证会话"}
    from monitor import product_fetcher
    fetcher = product_fetcher(config["fetcher"], {"adapter": adapter_id, "egress_node_id": account.get("egress_node_id", "")})
    session = _get_merchant_session(adapter_id, account, fetcher)
    if not session:
        return {**result, "message": "尚未建立认证缓存，请点击“检查账户”"}
    try:
        if adapter_id == "isvoro":
            valid = _isvoro_authenticated(session, account)
        elif adapter_id == "akile":
            valid = _akile_authenticated(session, account)
        elif adapter_id == "narwhal":
            valid = _narwhal_authenticated(session, account)
        else:
            valid = _merchant_authenticated(session, MERCHANT_LOGIN_ADAPTERS[adapter_id])
        if valid:
            _renew_merchant_session(adapter_id, account, fetcher, session)
            return {**result, "status": "valid", "ok": True, "session_reused": True,
                    "message": f"{MERCHANT_LOGIN_ADAPTERS[adapter_id]['name']} 登录缓存正常"}
        _remove_merchant_session(adapter_id, account, fetcher)
        return {**result, "status": "expired", "message": "登录缓存已失效，请点击“检查账户”重新认证"}
    except (OSError, ValueError, requests.RequestException):
        return {**result, "status": "unknown", "message": "认证状态暂时无法确认，请稍后重试"}


def public_account_check_result(value: dict) -> dict:
    """Remove transient login-session handles before exposing task results."""
    return {key: item for key, item in value.items() if not str(key).startswith("_")}


def _seckill_action_config(submitted: dict) -> dict:
    """Validate a transient browser draft while retaining server-only secrets."""
    current = current_config()
    draft = submitted.get("config")
    if not isinstance(draft, dict):
        return validate_config(current)
    draft = dict(draft)
    current_accounts = {row["id"]: row for row in current["vps_seckill"]["accounts"]}
    seckill = draft.get("vps_seckill")
    if isinstance(seckill, dict):
        seckill = dict(seckill)
        accounts = []
        for raw in seckill.get("accounts", []):
            if not isinstance(raw, dict):
                continue
            account = dict(raw)
            saved = current_accounts.get(str(account.get("id", "")))
            if not account.get("password") and saved:
                account["password"] = saved.get("password", "")
            accounts.append(account)
        seckill["accounts"] = accounts
        draft["vps_seckill"] = seckill
    egress_value = draft.get("egress")
    current_nodes = {row["id"]: row for row in current.get("egress", {}).get("nodes", [])}
    if isinstance(egress_value, dict):
        egress_value = dict(egress_value)
        nodes = []
        for raw in egress_value.get("nodes", []):
            if isinstance(raw, dict):
                node = dict(raw)
                saved = current_nodes.get(str(node.get("id", "")))
                if not node.get("token") and saved:
                    node["token"] = saved.get("token", "")
                nodes.append(node)
        egress_value["nodes"] = nodes
        draft["egress"] = egress_value
    value = validate_config(draft)
    # Proxy credentials are server-only and must not be supplied by the browser.
    value["egress"] = current.get("egress", value["egress"])
    value["fetcher"]["egress"] = value["egress"]
    if not value["telegram"].get("bot_token"):
        value["telegram"]["bot_token"] = current.get("telegram", {}).get("bot_token", "")
    return value


def seckill_account_check(submitted: dict) -> dict:
    from monitor import product_fetcher
    account_override = submitted.get("account")
    transient_session_cookie = str(account_override.get("session_cookie", "")).strip() if isinstance(account_override, dict) else ""
    transient_session_user_agent = str(account_override.get("session_user_agent", "")).strip() if isinstance(account_override, dict) else ""
    transient_captcha_token = str(account_override.get("captcha_token", "")).strip() if isinstance(account_override, dict) else ""
    transient_captcha_provider = str(account_override.get("captcha_provider", "hcaptcha")).strip() if isinstance(account_override, dict) else "hcaptcha"
    current = _seckill_action_config(submitted)
    if isinstance(account_override, dict):
        account_id = str(submitted.get("account_id", ""))
        if str(account_override.get("id", "")) != account_id:
            raise ValueError("网站账户与检查请求不匹配")
        draft = dict(current)
        seckill = dict(current["vps_seckill"])
        accounts = [row for row in seckill["accounts"] if row["id"] != account_id]
        accounts.append(account_override)
        seckill["accounts"] = accounts
        item_override = submitted.get("item")
        item_id = str(submitted.get("item_id", "")).strip()
        if item_id and isinstance(item_override, dict):
            items = [row for row in seckill["items"] if row["id"] != item_id]
            items.append(item_override)
            seckill["items"] = items
        draft["vps_seckill"] = seckill
        current = _seckill_action_config({"config": draft})
    account = next((row for row in current["vps_seckill"]["accounts"] if row["id"] == submitted.get("account_id")), None)
    if not account:
        raise ValueError("网站账户不存在，请先保存账户配置")
    if transient_session_cookie or transient_session_user_agent or transient_captcha_token:
        account = dict(account)
        if transient_session_cookie:
            account["_session_cookie"] = transient_session_cookie
        if transient_session_user_agent:
            account["_session_user_agent"] = transient_session_user_agent
        if transient_captcha_token:
            account["_captcha_token"] = transient_captcha_token
            account["_captcha_provider"] = transient_captcha_provider or "hcaptcha"
    item_id = str(submitted.get("item_id", "")).strip()
    item = next((row for row in current["vps_seckill"]["items"] if row["id"] == item_id), None) if item_id else None
    item_override = submitted.get("item")
    if item_id and isinstance(item_override, dict):
        draft_item = next((row for row in current["vps_seckill"]["items"] if row["id"] == item_id), None)
        if draft_item:
            item = {**draft_item, **item_override}
        else:
            raise ValueError("抢购商品不存在")
    if item and item.get("account_id") != account["id"]:
        raise ValueError("请为该商品选择正在检查的账户")
    if item_id and not item:
        raise ValueError("请先保存商品与账户绑定，再检查该商品的账户")
    if item:
        product = next((row for row in current["products"] if row["id"] == item["product_id"]), {})
        route_item = {**product, "egress_node_id": item["egress_node_id"]}
    else:
        # Account-level checks follow the merchant's saved route and request mode.
        # They intentionally do not require a product to be configured first.
        adapter_id = merchant_login_id(account.get("provider")) or "html"
        route_item = {"adapter": adapter_id, "provider": account.get("provider", ""), "egress_node_id": account.get("egress_node_id", "")}
    fetcher = product_fetcher(current.get("fetcher"), route_item)
    session = requests.Session()
    session.trust_env = False
    egress.apply_session(session, fetcher)
    try:
        result = {**check_order_account(session, account, fetcher), "account_id": account["id"]}
        if item:
            result["item_id"] = item["id"]
        pending_session = result.pop("_verification_session", None)
        pending_url = result.pop("_verification_url", "")
        if result.get("verification_required") and pending_session is session:
            challenge_id = secrets.token_urlsafe(24)
            now = time.time()
            with SECKILL_LOCK:
                for key, value in list(BWH_VERIFICATION_CHALLENGES.items()):
                    if value.get("expires_at", 0) <= now or value.get("account_id") == account["id"]:
                        old = BWH_VERIFICATION_CHALLENGES.pop(key, None)
                        if old and old.get("session"):
                            old["session"].close()
                BWH_VERIFICATION_CHALLENGES[challenge_id] = {
                    "account_id": account["id"], "username": account["username"], "provider": merchant_login_id(account.get("provider")),
                    "session": session, "url": pending_url, "fetcher": fetcher, "expires_at": now + BWH_VERIFICATION_TTL,
                }
            result["challenge_id"] = challenge_id
            result["verification_expires_at"] = int((now + BWH_VERIFICATION_TTL) * 1000)
            return result
        session.close()
        return result
    except Exception:
        session.close()
        raise


def seckill_account_verification(submitted: dict) -> dict:
    challenge_id = str(submitted.get("challenge_id", "")).strip()
    account_id = str(submitted.get("account_id", "")).strip()
    code = str(submitted.get("verification_code", "")).strip()
    if not re.fullmatch(r"[0-9]{4,12}", code):
        raise ValueError("邮箱验证码应为 4 到 12 位数字")
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,128}", challenge_id):
        raise ValueError("邮箱验证码会话无效，请重新检查账户")
    with SECKILL_LOCK:
        challenge = BWH_VERIFICATION_CHALLENGES.get(challenge_id)
        if challenge and challenge.get("expires_at", 0) <= time.time():
            BWH_VERIFICATION_CHALLENGES.pop(challenge_id, None)
            challenge["session"].close()
            challenge = None
    if not challenge or challenge.get("account_id") != account_id:
        raise ValueError("邮箱验证码会话已失效，请重新检查账户")
    current = current_config()
    supplied_account = submitted.get("account")
    saved_account = next((row for row in current["vps_seckill"].get("accounts", []) if row["id"] == account_id), None)
    if isinstance(supplied_account, dict) and str(supplied_account.get("id", "")) == account_id:
        account = dict(saved_account or {})
        account.update({key: value for key, value in supplied_account.items() if key in {"id", "name", "provider", "username", "login_url", "password"}})
        if not account.get("password") and saved_account:
            account["password"] = saved_account.get("password", "")
    else:
        account = saved_account
    if not account or account.get("username") != challenge.get("username") or merchant_login_id(account.get("provider")) != "bwh":
        raise ValueError("网站账户已修改，请重新检查搬瓦工账户")
    adapter = MERCHANT_LOGIN_ADAPTERS["bwh"]
    try:
        checked = _merchant_submit_verification(challenge["session"], account, adapter, challenge["url"], code)
    except (requests.RequestException, ValueError):
        checked = {"status": "unknown", "message": "无法提交搬瓦工验证码：网络异常或会话已失效，请重新检查账户"}
    with SECKILL_LOCK:
        BWH_VERIFICATION_CHALLENGES.pop(challenge_id, None)
    if checked.get("status") == "valid":
        _store_merchant_session("bwh", account, challenge.get("fetcher"), challenge["session"])
    _close_bwh_session(challenge["session"])
    return {"account_id": account_id, "checked_at": beijing_iso(), "ok": checked.get("status") == "valid", **checked}


def seckill_test(submitted: dict) -> dict:
    """Check configured VPS purchase prerequisites without submitting an order."""
    from monitor import MonitorError, inspect_product, product_fetcher
    current = _seckill_action_config(submitted)
    mode = str(submitted.get("mode", current["vps_seckill"]["mode"])).strip().casefold()
    if mode not in {"rapid", "precise", "manual"}:
        raise ValueError("秒杀测试模式无效")
    products = {str(item.get("id")): item for item in current.get("products", [])}
    accounts = {str(item.get("id")): item for item in current["vps_seckill"].get("accounts", [])}
    items = [item for item in current["vps_seckill"].get("items", []) if item.get("enabled", True)]
    if not items:
        raise ValueError("请先从库存监控或库存通知选择至少一个启用的抢购商品")
    results = []
    for item in items:
        product = products.get(item.get("product_id"))
        required_account = mode != "manual"
        label = item.get("label") or (product.get("name") if product else item.get("product_id", "已下架商品"))
        result = {"item_id": item["id"], "label": label, "required_account": required_account,
                  "site_access": {"ok": False, "message": "尚未检查"},
                  "page_recognition": {"ok": False, "message": "尚未检查"},
                  "order_submission": {"ok": not required_account, "required": required_account,
                  "message": "手动模式只准备购买链接，不创建订单" if not required_account else "尚未检查"},
                  "account": {"ok": False, "required": required_account, "status": "unbound", "message": "未绑定账户"}}
        if required_account:
            result["order_submission"] = {"ok": False, "required": True, "message": "尚未检查"}
        if not product:
            result["site_access"] = {"ok": False, "message": "商品已下架，无法访问并识别页面"}
            result["page_recognition"] = {"ok": False, "message": "商品已下架，无法识别页面"}
            results.append(result)
            continue
        routed = {**product, "egress_node_id": item.get("egress_node_id", "")}
        fetcher = product_fetcher(current.get("fetcher"), routed)
        account = accounts.get(item.get("account_id"))
        if required_account:
            if not account:
                result["order_submission"] = {"ok": False, "required": True, "message": "未绑定账户，本次不会创建订单"}
            elif (str((product or {}).get("adapter", "")).casefold() not in {"akile", "vmiss", "bwh", "isvoro", "dmit"}
                  and not str(item.get("submit_url", "")).strip()):
                result["order_submission"] = {"ok": False, "required": True, "message": "未配置商家待付款下单适配，本次不会创建订单"}
            else:
                adapter_id = str((product or {}).get("adapter", "")).casefold()
                result["order_submission"] = {"ok": True, "required": True,
                                                "message": ("已启用 Akile 原生待付款下单适配，启动时会固定使用 Debian 12 和 1 个月周期"
                                                            if adapter_id == "akile" else
                                                            "已启用 VMISS 原生结算适配，启动时会登录、预填配置并创建待付款发票"
                                                            if adapter_id == "vmiss" else
                                                            "已启用搬瓦工原生官网结算适配，认证会话会短期复用并只创建未付款账单"
                                                            if adapter_id == "bwh" else
                                                            "已启用 Isvoro 原生待付款适配，固定使用 Debian12 和支付宝"
                                                            if adapter_id == "isvoro" else
                                                            "已启用 DMIT 原生待付款适配，使用月付、PZ、DMIT 表单默认系统（当前为 Debian 13）、结算页默认付款方式、订单后自动生成 SSH Key 和 0GB 额外硬盘"
                                                            if adapter_id == "dmit" else
                                                            "已配置统一 hostwatch-unpaid-v1 待付款接口，启动时还会校验接口能力")}
        try:
            with requests.Session() as session:
                session.trust_env = False
                session.headers["User-Agent"] = "HostWatch/1.0 (VPS seckill configuration test)"
                egress.apply_session(session, fetcher)
                try:
                    found = inspect_product(session, routed, fetcher)
                    available = found.available if type(found.available) is int else None
                    stock = "有货" if ((found.stock_mode == "availability" and available and available > 0) or (available and available > 0)) else "缺货/未知"
                    result["site_access"] = {"ok": True, "message": "网站访问成功"}
                    result["page_recognition"] = {"ok": True, "message": f"页面识别成功：{found.name} · {stock}"}
                except (MonitorError, requests.RequestException, ValueError) as exc:
                    message = str(exc)[:300] or "访问或识别失败"
                    result["site_access"] = {"ok": False, "message": f"网站访问失败：{message}"}
                    result["page_recognition"] = {"ok": False, "message": f"页面识别失败：{message}"}
                if account:
                    checked = check_order_account(session, account, fetcher)
                    result["account"] = {**public_account_check_result(checked), "required": required_account, "account_id": account["id"]}
        except (OSError, requests.RequestException, ValueError) as exc:
            message = str(exc)[:300] or "检查失败"
            if not result["site_access"]["ok"]:
                result["site_access"] = {"ok": False, "message": f"网站访问失败：{message}"}
            if not result["page_recognition"]["ok"]:
                result["page_recognition"] = {"ok": False, "message": f"页面识别失败：{message}"}
        result["ok"] = bool(result["site_access"]["ok"] and result["page_recognition"]["ok"]
                            and result["order_submission"]["ok"]
                            and (not result["account"]["required"] or result["account"].get("ok")))
        results.append(result)
    return {"ok": all(item["ok"] for item in results), "mode": mode, "checked_at": beijing_iso(), "items": results}


class SeckillTestTask:
    """Run configuration checks in a cancellable background task."""
    def __init__(self, task_id: str, submitted: dict):
        self.id = task_id
        self.submitted = submitted
        self.cancelled = threading.Event()
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.output = {"task_id": task_id, "pending": True, "items": [], "phase": "starting"}

    def snapshot(self) -> dict:
        with self.lock:
            return {**self.output, "stopping": self.cancelled.is_set() and not self.done.is_set()}

    def cancel(self) -> None:
        self.cancelled.set()

    def run(self) -> None:
        try:
            current = _seckill_action_config(self.submitted)
            mode = str(self.submitted.get("mode", current["vps_seckill"]["mode"])).strip().casefold()
            products = {str(item.get("id")): item for item in current.get("products", [])}
            accounts = {str(item.get("id")): item for item in current["vps_seckill"].get("accounts", [])}
            items = [item for item in current["vps_seckill"].get("items", []) if item.get("enabled", True)]
            if not items:
                raise ValueError("请先从库存监控或库存通知选择至少一个启用的抢购商品")
            results = []
            for item in items:
                if self.cancelled.is_set():
                    break
                submitted_one = dict(self.submitted)
                submitted_one["config"] = current
                # Keep the original function's validation and per-item behavior,
                # while checking one item at a time so cancellation is observable.
                draft = dict(current)
                draft["vps_seckill"] = {**current["vps_seckill"], "items": [item]}
                submitted_one["config"] = draft
                result = seckill_test(submitted_one)["items"][0]
                results.append(result)
                with self.lock:
                    self.output.update({"phase": "checking", "items": list(results), "mode": mode})
            with self.lock:
                self.output.update({"ok": not self.cancelled.is_set() and all(item["ok"] for item in results),
                                    "mode": mode, "checked_at": beijing_iso()})
        except (OSError, requests.RequestException, ValueError) as exc:
            with self.lock:
                self.output.update({"ok": False, "error": str(exc), "checked_at": beijing_iso()})
        finally:
            with self.lock:
                if self.cancelled.is_set():
                    self.output.update({"ok": False, "cancelled": True, "error": "测试已停止"})
                self.output["pending"] = False
                self.done.set()


def start_seckill_test(submitted: dict) -> dict:
    task_id = str(submitted.get("task_id", ""))
    if not re.fullmatch(r"[a-zA-Z0-9-]{16,64}", task_id):
        raise ValueError("测试任务编号无效")
    with SECKILL_LOCK:
        if task_id in SECKILL_TEST_TASKS or task_id in STOPPED_SECKILL_TEST_IDS:
            raise ValueError("测试任务编号已使用，请重新测试")
        for old_id in list(SECKILL_TEST_TASKS)[:-19]:
            del SECKILL_TEST_TASKS[old_id]
        for old_id in list(STOPPED_SECKILL_TEST_IDS)[:-20]:
            del STOPPED_SECKILL_TEST_IDS[old_id]
        if any(not task.done.is_set() for task in SECKILL_TEST_TASKS.values()):
            raise ValueError("已有抢购配置测试正在执行，请先停止")
        task = SeckillTestTask(task_id, submitted)
        SECKILL_TEST_TASKS[task_id] = task
    threading.Thread(target=task.run, daemon=True).start()
    return task.snapshot()


def stop_seckill_test(task_id: str) -> dict:
    if not re.fullmatch(r"[a-zA-Z0-9-]{16,64}", task_id):
        raise ValueError("测试任务编号无效")
    with SECKILL_LOCK:
        task = SECKILL_TEST_TASKS.get(task_id)
        if task is None:
            STOPPED_SECKILL_TEST_IDS[task_id] = True
            return {"task_id": task_id, "pending": False, "ok": False, "cancelled": True, "error": "测试已停止"}
    task.cancel()
    return task.snapshot()


class SeckillTask:
    def __init__(self, task_id: str, config: dict, mode: str):
        self.id = task_id
        self.config = config
        self.mode = mode
        self.started = time.monotonic()
        self.cancelled = threading.Event()
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.logs = []
        self.output = {"task_id": task_id, "pending": True, "mode": mode, "logs": self.logs, "items": []}

    def log(self, message: str, level: str = "info", item: dict | None = None) -> None:
        entry = {"at": beijing_iso(), "level": level, "message": message}
        if item:
            entry["item_id"] = item.get("id", "")
        with self.lock:
            self.logs.append(entry)
            del self.logs[:-200]
            self.output["logs"] = list(self.logs)

    def publish_module_status(self, result: str, message: str = "") -> None:
        try:
            from monitor import module_schedule, update_module_status
            update_module_status(STATUS_PATH, "seckill", module_schedule(self.config, "seckill"), result, message, trigger="manual")
        except (OSError, ValueError, TypeError):
            pass

    def snapshot(self) -> dict:
        with self.lock:
            return {**self.output, "logs": list(self.logs), "stopping": self.cancelled.is_set() and not self.done.is_set()}

    def cancel(self) -> None:
        self.cancelled.set()

    def _affiliate_purchase_url(self, item, url, product=None):
        product = product or next((row for row in self.config.get("products", []) if row.get("id") == item.get("product_id")), {})
        provider = product.get("adapter") or product.get("provider") or ""
        return affiliate_purchase_url(url, str(provider).casefold(), self.config, product)

    def _item_label(self, item: dict, product: dict | None = None) -> str:
        product = product or next((row for row in self.config.get("products", []) if row.get("id") == item.get("product_id")), {})
        return str(item.get("label") or product.get("name") or item.get("product_id") or "VPS 商品")

    def _submit(self, session, item, account, url, product=None, fetcher=None, inspected=None):
        quantity = item.get("quantity", 1)
        # This is an explicit unpaid-order adapter contract, never a generic checkout POST.
        # The adapter MUST enforce no charging, including saved cards/account credit.
        if self.cancelled.is_set():
            return {"status": "cancelled", "url": url, "quantity": quantity}
        key = hashlib.sha256(f"{self.id}:{item['id']}".encode()).hexdigest()
        if product and str(product.get("adapter", "")).casefold() == "akile":
            try:
                payload = akile_order_payload(item, inspected, url)
                self.log(f"正在提交 Akile 下单请求，数量 {quantity}", "info", item)
                response = session.post(AKILE_ORDER_URL, json=payload,
                    headers={"Idempotency-Key": key}, timeout=(5, 20), allow_redirects=False)
                order = akile_order_result(response, quantity)
                self.log(f"已创建 Akile 待付款订单 {order['order_id']}，正在检查订单详情", "info", item)
                verification = akile_confirm_order(session, order["order_id"], item, inspected)
                self.log(f"Akile 订单 {order['order_id']} 检查正常：状态为待付款且订单号匹配", "success", item)
            except (requests.RequestException, ValueError) as exc:
                detail = _order_exception_detail(exc)
                self.log(f"已发送一次 Akile 下单请求，但无法确认待付款订单；原因：{detail[:240]}。已停止该商品，勿重复启动，请先到官网核对。未发起付款请求。", "error", item)
                return {"status": "uncertain", "url": url, "quantity": quantity}
            purchase_url = akile_order_page_url(order["order_id"])
            self.log(f"Akile 待付款订单已确认，数量 {quantity}；正在准备订单通知。", "success", item)
            notification = self._notify_unpaid_order(item, order["order_id"], quantity, purchase_url)
            return {"status": "submitted", "order_id": order["order_id"], "url": purchase_url,
                    "quantity": quantity, "payment_status": "unpaid", "order_verification": verification,
                    "telegram_status": notification}
        if product and str(product.get("adapter", "")).casefold() == "vmiss":
            try:
                order = vmiss_order(session, item, account, inspected, url, key,
                                    progress=lambda message: self.log(message, "info", item))
            except (requests.RequestException, ValueError) as exc:
                detail = _order_exception_detail(exc)
                self.log(f"已发送一次 VMISS 下单请求，但无法确认待付款发票；原因：{detail[:240]}。已停止该商品，勿重复启动，请先到官网核对。未发起付款请求。", "error", item)
                return {"status": "uncertain", "url": url, "quantity": quantity}
            self.log(f"VMISS 订单 {order['order_id']} 检查正常：状态为未付款，商品、系统和优惠码已匹配", "success", item)
            self.log(f"VMISS 待付款订单已确认，数量 {quantity}；正在准备订单通知。", "success", item)
            notification = self._notify_unpaid_order(item, order["order_id"], quantity, order["invoice_url"])
            return {"status": "submitted", "order_id": order["order_id"], "url": order["invoice_url"],
                    "quantity": quantity, "payment_status": "unpaid", "telegram_status": notification}
        if product and str(product.get("adapter", "")).casefold() == "bwh":
            try:
                order = bwh_order(session, item, account, inspected, url, key)
            except (requests.RequestException, ValueError) as exc:
                detail = _order_exception_detail(exc)
                self.log(f"已发送一次搬瓦工下单请求，但无法确认未付款账单；原因：{detail[:240]}。已停止该商品，勿重复启动，请先到官网核对。未发起付款请求。", "error", item)
                return {"status": "uncertain", "url": url, "quantity": quantity}
            self.log(f"搬瓦工待付款账单 {order['order_id']} 已确认，数量 {quantity}；正在准备订单通知。", "success", item)
            notification = self._notify_unpaid_order(item, order["order_id"], quantity, order["invoice_url"])
            return {"status": "submitted", "order_id": order["order_id"], "url": order["invoice_url"],
                    "quantity": quantity, "payment_status": "unpaid", "telegram_status": notification}
        if product and str(product.get("adapter", "")).casefold() == "isvoro":
            try:
                order = isvoro_order(session, item, account, inspected, url, key)
                self.log(f"Isvoro 已创建待付款订单 {order['order_id']}（Debian12 / 支付宝），正在准备订单通知", "success", item)
            except (requests.RequestException, ValueError) as exc:
                detail = _order_exception_detail(exc)
                self.log(f"已发送一次 Isvoro 下单请求，但无法确认待付款账单；原因：{detail[:240]}。已停止该商品，勿重复启动，请先到官网核对。未发起付款请求。", "error", item)
                return {"status": "uncertain", "url": url, "quantity": quantity}
            notification = self._notify_unpaid_order(item, order["order_id"], quantity, order["invoice_url"])
            return {"status": "submitted", "order_id": order["order_id"], "url": order["invoice_url"],
                    "quantity": quantity, "payment_status": "unpaid", "telegram_status": notification,
                    "os": order["os"], "payment_method": order["payment_method"]}
        if product and str(product.get("adapter", "")).casefold() == "dmit":
            try:
                order = (_dmit_order_flaresolverr(item, account, inspected, url, fetcher, key)
                         if str((fetcher or {}).get("mode", "")).casefold() == "flaresolverr"
                         else _dmit_order(session, item, account, inspected, url, fetcher, key))
                self.log(f"DMIT 已创建待付款账单 {order['order_id']}（月付 / PZ / {order.get('os', 'DMIT 默认系统')} / 结算页默认付款方式 / SSH Key 订单后生成 / 额外硬盘 0GB），正在准备订单通知", "success", item)
            except DmitOrderFailure as exc:
                detail = str(exc)[:300]
                if exc.checkout_request_sent:
                    self.log(f"DMIT 最终结算请求已发出，但账单核验未通过；原因：{detail}。结果待核对，请先查看官网账单，勿重复启动。未发起付款请求。", "error", item)
                    return {"status": "uncertain", "url": url, "quantity": quantity}
                self.log(f"DMIT 商品配置失败，未发送最终结算请求，也未创建或核验账单；原因：{detail}。修正配置后可重试。", "error", item)
                return {"status": "failed_before_submit", "url": url, "quantity": quantity}
            except (requests.RequestException, ValueError) as exc:
                detail = _order_exception_detail(exc)
                self.log(f"DMIT 抢购流程在账单核验前异常；最终结算请求是否发出无法确认。原因：{detail[:240]}。请先检查官网账单，勿重复启动。未发起付款请求。", "error", item)
                return {"status": "uncertain", "url": url, "quantity": quantity}
            notification = self._notify_unpaid_order(item, order["order_id"], quantity, order["invoice_url"])
            return {"status": "submitted", "order_id": order["order_id"], "url": order["invoice_url"],
                    "quantity": quantity, "payment_status": "unpaid", "telegram_status": notification,
                    "os": order["os"], "payment_method": order["payment_method"], "hostname": order["hostname"],
                    "extra_disk_gb": order["extra_disk_gb"]}
        payload = {"protocol": ORDER_PROTOCOL, "action": "create_unpaid_order",
            "product_id": item["product_id"], "purchase_url": url, "quantity": quantity,
            "coupon_code": item.get("coupon_code", ""), "payment_mode": "manual",
            "auto_pay": False, "idempotency_key": key}
        for field in ("operating_system", "billing_cycle", "payment_method"):
            value = str(item.get(field, "")).strip()
            if value:
                payload[field] = value
        try:
            self.log(f"正在提交待付款下单请求，数量 {quantity}", "info", item)
            response = session.post(item["submit_url"], json=payload,
                headers={"Idempotency-Key": key}, timeout=(5, 20), allow_redirects=False)
            data = order_json(response)
            if (data.get("protocol") != ORDER_PROTOCOL or data.get("status") != "unpaid"
                    or not isinstance(data.get("order_id"), str) or not data["order_id"].strip()
                    or type(data.get("quantity")) is not int or data["quantity"] != quantity):
                raise ValueError("未确认待付款订单")
            self.log(f"已创建待付款订单 {data['order_id']}，订单状态检查正常", "success", item)
        except (requests.RequestException, ValueError) as exc:
            # A lost response may still have created an order. Never retry automatically.
            detail = _order_exception_detail(exc)
            self.log(f"已发送一次下单请求，但无法确认待付款订单；原因：{detail[:240]}。已停止该商品，勿重复启动，请先到官网核对。未发起付款请求。", "error", item)
            return {"status": "uncertain", "url": url, "quantity": quantity}
        purchase_url = self._affiliate_purchase_url(item, url)
        self.log(f"已创建待付款订单，数量 {quantity}；请手动前往官网核对订单并支付。", "success", item)
        notification = self._notify_unpaid_order(item, data["order_id"], quantity, purchase_url)
        # Do not expose or visit arbitrary response links, and never fetch payment_url.
        return {"status": "submitted", "order_id": data["order_id"][:128], "url": purchase_url, "quantity": quantity,
                "payment_status": "unpaid", "telegram_status": notification}

    def _notify_unpaid_order(self, item: dict, order_id: str, quantity: int, url: str) -> str:
        token, chat_id = effective_telegram(self.config)
        if not token or not chat_id:
            self.log("待付款订单已创建，但未发送 Telegram：机器人未配置或通知已关闭。", "warning", item)
            return "unconfigured"
        label = self._item_label(item)
        message = "\n".join([
            "✅ <b>HostWatch · VPS 补货抢购成功</b>",
            f"商品：{escape(str(label)[:160])}",
            f"订单号：<code>{escape(str(order_id)[:128])}</code>",
            f"数量：{quantity}",
            "状态：待付款",
            f"下单时间：{format_beijing(beijing_iso())}",
            "请登录商家官网手动付款，系统不会自动付款。",
        ])
        self.log("正在发送 Telegram 待付款订单通知", "info", item)
        try:
            telegram_call(token, "sendMessage", chat_id=chat_id, text=message, parse_mode="HTML",
                          disable_web_page_preview=True, reply_markup=purchase_button(url, "💳 立即付款"))
        except TelegramAPIError:
            self.log("待付款订单已创建，但 Telegram 通知发送失败；请查看服务日志并到官网核对订单。", "error", item)
            return "failed"
        self.log("待付款订单已通过 Telegram 通知。", "success", item)
        return "sent"

    @staticmethod
    def enough_stock(result, quantity):
        return (type(result.available) is int and result.available > 0
            and (result.stock_mode == "availability" or result.available >= quantity))

    def run(self) -> None:
        from contextlib import ExitStack
        from monitor import inspect_product, product_fetcher, write_inventory_cache
        products = {str(item.get("id")): item for item in self.config.get("products", [])}
        seckill = self.config.get("vps_seckill", {})
        accounts = {str(item.get("id")): item for item in seckill.get("accounts", [])}
        items = [item for item in seckill.get("items", []) if item.get("enabled", True)]
        finished, contexts = {}, {}
        self.publish_module_status("pending", "正在执行抢购任务")
        self.log(f"秒杀任务已启动：{self.mode_label()}，共 {len(items)} 个商品；仅创建待付款订单，需手动到官网支付")
        try:
            with ExitStack() as stack:
                # Separate stock and authenticated sessions per item, even for the same account.
                for item in items:
                    if self.cancelled.is_set():
                        break
                    try:
                        product = products.get(item["product_id"])
                        routed = {**(product or {}), "egress_node_id": item.get("egress_node_id", "")}
                        fetcher = product_fetcher(self.config.get("fetcher"), routed)
                        self.log(f"准备商品：{self._item_label(item, product)} · 商家 {str((product or {}).get('provider') or (product or {}).get('adapter') or '未知')} · 出口 {item.get('egress_node_id') or '直连'}", "info", item)
                        stock_session = stack.enter_context(requests.Session())
                        order_session = stack.enter_context(requests.Session())
                        for session in (stock_session, order_session):
                            session.trust_env = False
                            session.headers["User-Agent"] = "HostWatch/1.0 (VPS seckill; unpaid orders only)"
                            egress.apply_session(session, fetcher)
                        self.log("已建立库存检查会话和下单会话，开始预检查", "info", item)
                        account = accounts.get(item.get("account_id"))
                        native_adapter = str((product or {}).get("adapter", "")).casefold()
                        native_akile = native_adapter == "akile"
                        native_vmiss = native_adapter == "vmiss"
                        native_bwh = native_adapter == "bwh"
                        native_isvoro = native_adapter == "isvoro"
                        native_dmit = native_adapter == "dmit"
                        auto_requested = self.mode != "manual"
                        if auto_requested and not account:
                            raise ValueError("自动模式必须绑定网站账户")
                        auto = bool(auto_requested and (native_akile or native_vmiss or native_bwh or native_isvoro or native_dmit or str(item.get("submit_url", "")).strip()))
                        if auto_requested and not auto:
                            raise ValueError("该商家尚未配置内置或兼容的待付款下单适配")
                        if auto and not native_akile and not native_vmiss and not native_bwh and not native_isvoro and not native_dmit:
                            if not account.get("login_url") or urlsplit(account["login_url"]).netloc != urlsplit(item["submit_url"]).netloc:
                                raise ValueError("账户检查接口与待付款下单接口必须同源，以复用认证会话")
                            capability = order_json(order_session.get(item["submit_url"], timeout=(5, 20), allow_redirects=False))
                            if capability.get("protocol") != ORDER_PROTOCOL or capability.get("order_only") is not True or capability.get("automatic_payment") is not False:
                                raise ValueError("接口未明确支持仅创建待付款订单；普通结算网页不能自动提交")
                        if account:
                            self.log(f"开始账户认证：{account.get('name') or account.get('username')}（优先复用认证缓存）", "info", item)
                            checked = check_order_account(order_session, account, fetcher)
                            checked_public = public_account_check_result(checked)
                            self.log(f"账户 {account['name']}：{checked_public['message']}", "info" if checked_public["ok"] else "error", item)
                            with self.lock:
                                self.output.setdefault("accounts", {})[item["id"]] = checked_public
                            if auto and not checked_public["ok"]:
                                raise ValueError("账户未通过验证，已停止该商品自动下单")
                        contexts[item["id"]] = (product, routed, fetcher, stock_session, order_session, account, auto)
                    except (requests.RequestException, ValueError) as exc:
                        detail = str(exc) if isinstance(exc, ValueError) else "接口网络异常，请检查出口或接口连通性"
                        self.log(f"预检查未通过：{detail}；未提交订单。", "error", item)
                        finished[item["id"]] = {"status": "blocked", "url": item.get("target_url", "")}
                round_no = 0
                while not self.cancelled.is_set() and len(finished) < len(items):
                    round_no += 1
                    for item in items:
                        if self.cancelled.is_set():
                            break
                        if item["id"] in finished:
                            continue
                        product, routed, fetcher, stock_session, order_session, account, auto = contexts[item["id"]]
                        quantity = item.get("quantity", 1)
                        try:
                            if not product:
                                purchase_url = self._affiliate_purchase_url(item, item.get("target_url", ""))
                                self.log("商品已移出配置，保留购买链接；请从高级设置重新添加商品以恢复库存检查。", "ready", item)
                                finished[item["id"]] = {"status": "ready", "url": purchase_url, "quantity": quantity}
                                continue
                            result = inspect_product(stock_session, routed, fetcher)
                            write_inventory_cache(CONFIG_PATH.parent / "inventory-cache.json", routed, fetcher, result)
                            url = result.url or item.get("target_url", "")
                            available = self.enough_stock(result, quantity)
                            self.log(f"第 {round_no} 轮：{self._item_label(item, product)} · 需求 {quantity} 台 · {'可下单' if available else '缺货/库存不足/未知'}", "info", item)
                            if not available:
                                continue
                            if result.stock_mode == "availability":
                                self.log("商家仅提供有货状态，数量由下单接口最终核验；不会自动减少下单数量。", "info", item)
                            if auto and self.mode == "precise":
                                checked = check_order_account(order_session, account, fetcher)
                                checked = public_account_check_result(checked)
                                with self.lock:
                                    self.output.setdefault("accounts", {})[item["id"]] = checked
                                if not checked["ok"]:
                                    finished[item["id"]] = {"status": "blocked", "url": url, "quantity": quantity}
                                    self.log("提交前账户复核失败，已停止该商品，请到官网检查。", "error", item)
                                    continue
                                if self.cancelled.is_set():
                                    break
                                result = inspect_product(stock_session, routed, fetcher)
                                write_inventory_cache(CONFIG_PATH.parent / "inventory-cache.json", routed, fetcher, result)
                                if not self.enough_stock(result, quantity):
                                    self.log("提交前第二次库存检查不足，继续等待，不发送订单。", "info", item)
                                    continue
                                url = result.url or url
                            if auto:
                                self.log("库存满足下单条件，开始提交待付款订单（不会付款）", "info", item)
                                finished[item["id"]] = self._submit(order_session, item, account, url, product, fetcher, result)
                                self.log(f"下单流程结束：{finished[item['id']].get('status', 'unknown')}", "info" if finished[item["id"]].get("status") in {"submitted", "ready"} else "error", item)
                            else:
                                purchase_url = self._affiliate_purchase_url(item, url, product)
                                finished[item["id"]] = {"status": "ready", "url": purchase_url, "quantity": quantity}
                                self.log(f"已准备购买链接（数量 {quantity}）：{purchase_url}；请到官网手动下单和付款。", "ready", item)
                        except Exception as exc:
                            detail = _order_exception_detail(exc) if isinstance(exc, (requests.RequestException, ValueError)) else str(exc)
                            self.log(f"第 {round_no} 轮执行失败：{detail[:300]}；请检查商品、账户和出口配置。", "error", item)
                    with self.lock:
                        self.output["items"] = [{"item_id": key, **value} for key, value in finished.items()]
                        self.output["round"] = round_no
                    if self.mode == "manual" or len(finished) == len(items):
                        break
                    self.cancelled.wait(seckill.get("interval_seconds", 5))
        finally:
            with self.lock:
                self.output.update(items=[{"item_id": key, **value} for key, value in finished.items()], pending=False,
                    cancelled=self.cancelled.is_set(), checked_at=beijing_iso(),
                    duration_seconds=round(time.monotonic() - self.started, 1))
                self.done.set()
            completed_items = list(finished.values())
            if self.output.get("cancelled"):
                final_message = "秒杀任务已停止"
            elif any(item.get("status") == "submitted" for item in completed_items):
                final_message = "秒杀任务已完成；已确认的待付款订单请到官网手动支付"
            elif any(item.get("status") == "uncertain" for item in completed_items):
                final_message = "秒杀任务已完成；存在结果待核对商品，请先检查官网账单，勿重复启动"
            else:
                final_message = "秒杀任务已完成；没有确认创建待付款订单"
            self.log(final_message)
            failed = any(item.get("status") in {"blocked", "failed_before_submit", "uncertain"} for item in completed_items)
            self.publish_module_status("error" if failed else "success", final_message)

    def mode_label(self):
        return {"rapid": "极速秒杀（自动）", "precise": "精准秒杀（自动）", "manual": "手动秒杀"}.get(self.mode, self.mode)


def start_seckill(submitted: dict) -> dict:
    task_id = str(submitted.get("task_id", ""))
    if not re.fullmatch(r"[a-zA-Z0-9-]{16,64}", task_id):
        raise ValueError("秒杀任务编号无效")
    current = _seckill_action_config(submitted)
    requested_mode = str(submitted.get("mode", current["vps_seckill"]["mode"])).strip().casefold()
    if requested_mode not in {"rapid", "precise", "manual"}:
        raise ValueError("秒杀模式无效")
    if not any(item["enabled"] for item in current["vps_seckill"]["items"]):
        raise ValueError("请先从库存监控或库存通知选择至少一个秒杀商品")
    with SECKILL_LOCK:
        if task_id in STOPPED_SECKILL_IDS or task_id in SECKILL_TASKS:
            raise ValueError("秒杀任务编号已使用，请重新启动")
        if any(not task.done.is_set() for task in SECKILL_TASKS.values()):
            raise ValueError("已有 VPS 秒杀任务正在执行，请先停止")
        task = SeckillTask(task_id, current, requested_mode)
        SECKILL_TASKS[task_id] = task
    threading.Thread(target=task.run, daemon=True).start()
    return task.snapshot()


def stop_seckill(task_id: str) -> dict:
    if not re.fullmatch(r"[a-zA-Z0-9-]{16,64}", task_id):
        raise ValueError("秒杀任务编号无效")
    with SECKILL_LOCK:
        task = SECKILL_TASKS.get(task_id)
        if task is None:
            STOPPED_SECKILL_IDS[task_id] = True
            return {"task_id": task_id, "pending": False, "cancelled": True, "error": "秒杀任务已停止"}
    task.cancel()
    return task.snapshot()


def start_manual_refresh(role: str) -> dict:
    if role not in {"watch", "notify"}:
        raise ValueError("刷新模块无效")
    task_id = secrets.token_urlsafe(12)
    with REFRESH_LOCK:
        if any(task.get("pending") for task in REFRESH_TASKS.values()):
            raise ValueError("已有手动刷新正在执行，请稍后重试")
        REFRESH_TASKS[task_id] = {"task_id": task_id, "role": role, "pending": True, "started_at": beijing_iso()}

    def worker() -> None:
        result = {"task_id": task_id, "role": role, "pending": False}
        try:
            from monitor import check_once, effective_telegram, read_config
            current = read_config(CONFIG_PATH)
            token, chat_id = effective_telegram(current)
            check_once(requests.Session(), current, CONFIG_PATH.parent / "state.json", STATUS_PATH, token, chat_id, role, "manual")
            result.update(ok=True, message="刷新完成")
        except Exception as exc:  # noqa: BLE001 - expose a safe user-facing task result
            result.update(ok=False, error=str(exc)[:500])
            try:
                from monitor import module_schedule, update_module_status
                current = current_config()
                module = "monitor" if role == "watch" else "notify"
                update_module_status(STATUS_PATH, module, module_schedule(current, module), "error", result["error"], trigger="manual")
            except (OSError, ValueError, TypeError):
                pass
        with REFRESH_LOCK:
            REFRESH_TASKS[task_id] = result

    threading.Thread(target=worker, daemon=True).start()
    return dict(REFRESH_TASKS[task_id])


def default_config() -> dict:
    return {
        "enabled": False,
        "interval_seconds": 600,
        "monitor_schedule": {"mode": "auto", "interval_seconds": 600},
        "notify_schedule": {"mode": "auto", "interval_seconds": 600},
        "catalog_schedule": {"mode": "manual", "interval_seconds": 300},
        "seckill_schedule": {"mode": "manual", "interval_seconds": 5},
        "notify_current_stock_on_start": False,
        "isvoro_seckill": False,
        "affiliate_id": DEFAULT_AFFILIATES["vmiss"],
        "affiliates": dict(DEFAULT_AFFILIATES),
        "affiliate_notes": {provider: "" for provider in PROVIDERS},
        "affiliate_accounts": {provider: "" for provider in PROVIDERS},
        "affiliate_rates": {provider: "" for provider in PROVIDERS},
        "affiliate_coupons": {provider: "" for provider in PROVIDERS},
        "merchant_order": list(PROVIDERS),
        "watch_order": [],
        "notify_order": [],
        "fetcher": {
            "mode": "requests",
            "flaresolverr_url": "http://127.0.0.1:8191/v1",
            "timeout_seconds": 60,
            "wait_seconds": 0,
            "disable_media": True,
            "provider_flaresolverr": {},
            "cloudscraper": normalize_cloudscraper(None),
        },
        "telegram": {"bot_token": "", "chat_id": "", "disabled": False},
        "egress": {"nodes": [], "routes": {}},
        "vps_seckill": {"enabled": False, "mode": "manual", "interval_seconds": 5, "items": [], "accounts": []},
        "akile_market": {"enabled": False, "interval_seconds": 60, "account_id": "", "items": []},
        "isvoro_market": {"enabled": False, "interval_seconds": 600, "account_id": "", "items": []},
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


def normalize_cloudscraper(value: object) -> dict:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("cloudscraper 配置必须是对象")
    result = {}
    for key, label, default, low, high in (
        ("connect_timeout_seconds", "连接超时", 10, 1, 30),
        ("read_timeout_seconds", "读取超时", 25, 1, 120),
        ("challenge_delay_seconds", "验证等待", 5, 0, 15),
    ):
        number = value.get(key, default)
        if type(number) is not int or not low <= number <= high:
            raise ValueError(f"cloudscraper {label}必须是 {low} 到 {high} 秒的整数")
        result[key] = number
    return result


def cloudscraper_probe(target: str, fetcher: dict) -> dict:
    from importlib.metadata import version
    from bs4 import BeautifulSoup
    from monitor import fetch_with_cloudscraper
    name, url = CLOUDSCRAPER_TEST_TARGETS[target]
    diagnostics = {}
    body, final_url = fetch_with_cloudscraper(url, urlsplit(url).hostname, fetcher, diagnostics)
    title = BeautifulSoup(body, "html.parser").title
    result = {"ok": True, "target": target, "target_name": name, "url": final_url,
        "title": " ".join(title.get_text(" ", strip=True).split())[:160] if title else "无页面标题",
        "library_version": version("cloudscraper"), **diagnostics}
    if sys.platform == "linux":
        import resource
        result["peak_memory_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    return result


def normalize_flaresolverr_options(value: dict, defaults: dict | None = None) -> dict:
    defaults = defaults or {"timeout_seconds": 60, "wait_seconds": 0, "disable_media": True}
    timeout_value = value.get("timeout_seconds", defaults["timeout_seconds"])
    if isinstance(timeout_value, bool):
        raise ValueError("FlareSolverr 超时必须是整数")
    try:
        timeout_seconds = int(timeout_value)
    except (ValueError, TypeError):
        raise ValueError("FlareSolverr 超时必须是整数") from None
    if str(timeout_seconds) != str(timeout_value).strip():
        raise ValueError("FlareSolverr 超时必须是整数")
    if not 10 <= timeout_seconds <= 180:
        raise ValueError("FlareSolverr 超时范围是 10 到 180 秒")
    wait = value.get("wait_seconds", defaults["wait_seconds"])
    if isinstance(wait, bool) or not isinstance(wait, int) or not 0 <= wait <= 30:
        raise ValueError("网页渲染等待必须是 0 到 30 秒的整数")
    disable_media = value.get("disable_media", defaults["disable_media"])
    if type(disable_media) is not bool:
        raise ValueError("FlareSolverr 轻量加载必须是布尔值")
    return {"timeout_seconds": timeout_seconds, "wait_seconds": wait, "disable_media": disable_media}


def normalize_fetcher(value: object) -> dict:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("请求方式配置必须是对象")
    mode = str(value.get("mode", "requests")).strip().casefold()
    if mode not in {"requests", "flaresolverr", "cloudscraper"}:
        raise ValueError("请求方式必须是 requests、flaresolverr 或 cloudscraper")
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
    options = normalize_flaresolverr_options(value)
    overrides = value.get("provider_flaresolverr", {})
    if not isinstance(overrides, dict) or any(key not in (*PROVIDERS, "html") for key in overrides):
        raise ValueError("FlareSolverr 商家参数必须使用支持的商家标识")
    normalized_overrides = {}
    for provider, settings in overrides.items():
        if not isinstance(settings, dict) or any(key not in options for key in settings):
            raise ValueError(f"{provider} 的 FlareSolverr 独立设置只能包含超时、渲染等待和轻量加载")
        try:
            normalized_overrides[provider] = normalize_flaresolverr_options(settings, options)
        except ValueError as exc:
            raise ValueError(f"{provider}：{exc}") from None
    provider_modes = value.get("provider_modes", {})
    if not isinstance(provider_modes, dict) or any(key not in (*PROVIDERS, "html") or not isinstance(mode, str) or mode not in {"inherit", "requests", "flaresolverr", "cloudscraper"} for key, mode in provider_modes.items()):
        raise ValueError("商家请求方式必须是支持商家的 inherit、requests、flaresolverr 或 cloudscraper")
    return {
        "mode": mode,
        "flaresolverr_url": endpoint.rstrip("/"),
        **options,
        "provider_flaresolverr": normalized_overrides,
        "provider_modes": dict(provider_modes),
        "cloudscraper": normalize_cloudscraper(value.get("cloudscraper")),
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
    for account in normalized["vps_seckill"]["accounts"]:
        account["password"] = ""
        account["password_configured"] = bool(account.get("password_configured"))
    normalized["egress"] = egress.public(normalized.get("egress", {}))
    normalized["fetcher"]["egress"] = normalized["egress"]
    return normalized


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


def normalize_seckill(value: object, product_ids: set[str]) -> dict:
    """Validate the VPS seckill draft without making any order request."""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("VPS 秒杀配置必须是对象")
    enabled = value.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("VPS 秒杀启用状态必须是布尔值")
    mode = str(value.get("mode", "manual")).strip().casefold()
    if mode not in {"rapid", "precise", "manual"}:
        raise ValueError("VPS 秒杀模式必须是 rapid、precise 或 manual")
    interval = value.get("interval_seconds", 1 if mode == "rapid" else 5)
    if type(interval) is not int or not 1 <= interval <= 3600:
        raise ValueError("VPS 秒杀检查间隔必须是 1 到 3600 秒的整数")
    accounts = value.get("accounts", [])
    if not isinstance(accounts, list) or len(accounts) > 50:
        raise ValueError("VPS 秒杀账户最多 50 个")
    normalized_accounts, account_ids = [], set()
    for account in accounts:
        if not isinstance(account, dict):
            raise ValueError("VPS 秒杀账户配置必须是对象")
        account_id = str(account.get("id", "")).strip()
        name = str(account.get("name", account_id)).strip()
        provider = str(account.get("provider", "")).strip()
        username = str(account.get("username", "")).strip()
        password = str(account.get("password", ""))
        login_url = str(account.get("login_url", "")).strip()
        egress_node_id = str(account.get("egress_node_id", "")).strip()
        adapter_id = merchant_login_id(provider)
        if adapter_id in MERCHANT_LOGIN_ADAPTERS:
            provider = adapter_id
            login_url = MERCHANT_LOGIN_ADAPTERS[adapter_id]["url"]
        elif adapter_id:
            provider = adapter_id
            login_url = AKILE_LOGIN_URL if adapter_id == "akile" else ""
        for key in ("auto_submit",):
            if key in account and not isinstance(account[key], bool):
                raise ValueError(f"VPS 秒杀账户 {name or account_id} 的 {key} 必须是布尔值")
        if not account_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", account_id) or account_id in account_ids:
            raise ValueError("VPS 秒杀账户 ID 必须唯一且只包含字母、数字、下划线或短横线")
        if not name or len(name) > 120 or len(provider) > 80 or len(username) > 256 or len(password) > 512:
            raise ValueError("VPS 秒杀账户字段长度无效")
        if login_url:
            parsed = urlsplit(login_url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                raise ValueError(f"VPS 秒杀账户 {name} 的登录地址必须是 HTTPS URL")
        account_ids.add(account_id)
        normalized_accounts.append({"id": account_id, "name": name, "provider": provider, "username": username,
            "password": password, "login_url": login_url, "auto_submit": bool(account.get("auto_submit", False)),
            "egress_node_id": egress_node_id[:64],
            "password_configured": bool(password) or bool(account.get("password_configured", False))})
    items = value.get("items", [])
    if not isinstance(items, list) or len(items) > 100:
        raise ValueError("VPS 秒杀商品最多 100 项")
    normalized_items, item_ids = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("VPS 秒杀商品配置必须是对象")
        item_id = str(item.get("id", item.get("product_id", ""))).strip()
        product_id = str(item.get("product_id", "")).strip()
        if not item_id or item_id in item_ids or len(item_id) > 128:
            raise ValueError("VPS 秒杀商品 ID 必须唯一")
        if not product_id or product_id not in product_ids:
            # Keep the entry when a product is removed from the live catalog, but
            # require a saved target URL so a manual task can still open it.
            product_id = product_id[:128]
        coupon = str(item.get("coupon_code", "")).strip()
        operating_system = str(item.get("operating_system", "")).strip()
        billing_cycle = str(item.get("billing_cycle", "")).strip().casefold()
        payment_method = str(item.get("payment_method", "")).strip().casefold()
        target_url = str(item.get("target_url", "")).strip()
        submit_url = str(item.get("submit_url", "")).strip()
        if not product_id and not target_url:
            raise ValueError(f"VPS 秒杀商品 {item_id} 需要关联商品或填写目标购买链接")
        for label, url in (("目标", target_url), ("提交", submit_url)):
            if url:
                parsed = urlsplit(url)
                if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                    raise ValueError(f"VPS 秒杀{label}地址必须是 HTTPS URL")
        account_id = str(item.get("account_id", "")).strip()
        egress_node_id = str(item.get("egress_node_id", "")).strip()
        for key in ("enabled", "auto_submit"):
            if key in item and not isinstance(item[key], bool):
                raise ValueError(f"VPS 秒杀商品 {item_id} 的 {key} 必须是布尔值")
        for key in ("akile_protocol_agreed", "akile_broadcast_ip_ack"):
            if key in item and not isinstance(item[key], bool):
                raise ValueError(f"VPS 秒杀商品 {item_id} 的 {key} 必须是布尔值")
        if account_id and account_id not in account_ids:
            raise ValueError(f"VPS 秒杀商品 {item_id} 绑定的账户不存在")
        quantity = item.get("quantity", 1)
        if type(quantity) is not int or not 1 <= quantity <= 100:
            raise ValueError(f"VPS 秒杀商品 {item_id} 的下单数量必须是 1 到 100 的整数")
        if len(operating_system) > 80:
            raise ValueError(f"VPS 秒杀商品 {item_id} 的系统名称过长")
        if billing_cycle not in {"", *ORDER_BILLING_CYCLES}:
            raise ValueError(f"VPS 秒杀商品 {item_id} 的付款周期无效")
        if payment_method not in {"", *ORDER_PAYMENT_METHODS}:
            raise ValueError(f"VPS 秒杀商品 {item_id} 的付款方式无效")
        item_ids.add(item_id)
        normalized_items.append({"id": item_id, "product_id": product_id, "coupon_code": coupon[:256],
            "operating_system": operating_system, "billing_cycle": billing_cycle,
            "payment_method": payment_method, "target_url": target_url, "submit_url": submit_url, "account_id": account_id,
            "egress_node_id": egress_node_id[:64], "quantity": quantity,
            "enabled": bool(item.get("enabled", True)), "auto_submit": bool(item.get("auto_submit", False)),
            "akile_protocol_agreed": bool(item.get("akile_protocol_agreed", True)),
            "akile_broadcast_ip_ack": bool(item.get("akile_broadcast_ip_ack", True)),
            "label": str(item.get("label", ""))[:160]})
    return {"enabled": enabled, "mode": mode, "interval_seconds": interval,
        "items": normalized_items, "accounts": normalized_accounts}


def normalize_schedule(value: object, default_mode: str, default_interval: int, label: str,
                       minimum: int = 0, maximum: int = 86400) -> dict:
    """Normalize a module refresh schedule while accepting old scalar settings."""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError(f"{label}配置必须是对象")
    mode = str(value.get("mode", default_mode)).strip().casefold()
    if mode not in {"manual", "auto"}:
        raise ValueError(f"{label}模式必须是 manual 或 auto")
    interval = value.get("interval_seconds", default_interval)
    if isinstance(interval, bool):
        raise ValueError(f"{label}检查间隔必须是整数")
    try:
        interval = int(interval)
    except (TypeError, ValueError):
        raise ValueError(f"{label}检查间隔必须是整数") from None
    if str(interval) != str(value.get("interval_seconds", default_interval)).strip():
        raise ValueError(f"{label}检查间隔必须是整数")
    if interval < minimum or interval > maximum:
        raise ValueError(f"{label}检查间隔必须是 {minimum} 到 {maximum} 秒的整数")
    return {"mode": mode, "interval_seconds": interval}


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

    monitor_schedule = normalize_schedule(value.get("monitor_schedule"), "auto", interval, "库存监控")
    notify_schedule = normalize_schedule(value.get("notify_schedule"), "auto", interval, "库存通知")
    catalog_schedule = normalize_schedule(value.get("catalog_schedule"), "manual", 300, "库存大盘")
    seckill_schedule = normalize_schedule(value.get("seckill_schedule"), "manual", 5, "VPS 抢购", 1, 3600)

    affiliate_id = str(value.get("affiliate_id", "")).strip()
    if affiliate_id and not re.fullmatch(r"\d{1,20}", affiliate_id):
        raise ValueError("推广 ID 格式无效")

    affiliates = normalize_affiliates(value)
    affiliate_notes = normalize_affiliate_notes(value)
    affiliate_accounts = normalize_affiliate_accounts(value)
    affiliate_rates = normalize_affiliate_rates(value)
    affiliate_coupons = normalize_affiliate_coupons(value)
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
        provider = str(item.get("provider", {"vmiss": "VMISS", "vps_soy": "VPS.SOY", "akile": "Akile", "narwhal": "独角鲸云", "isvoro": "Isvoro", "dmit": "DMIT", "bwh": "搬瓦工"}.get(adapter, "自定义网站"))).strip()
        gid = str(item.get("gid", "")).strip()
        pid = str(item.get("pid", "")).strip()
        if "enabled" in item and not isinstance(item["enabled"], bool):
            raise ValueError(f"产品 {name or product_id} 的启用状态必须是布尔值")
        if not product_id or product_id in seen_ids:
            raise ValueError("每个产品都需要唯一 ID")
        if adapter not in {*PROVIDERS, "html"}:
            raise ValueError(f"产品 {name or product_id} 的适配器必须是 vmiss、vps_soy、akile、narwhal、isvoro、dmit、bwh 或 html")
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
            from dmit import PLAN_KEY_PATTERN, PRICING_URL, normalize_catalog_url
            category_url = normalize_catalog_url(item.get("category_url", PRICING_URL))
            plan_key = str(item.get("plan_key", "")).strip().upper()
            if (not pid and not plan_key) or (pid and not re.fullmatch(r"[1-9][0-9]{0,9}", pid)) or len(name) > 120 or len(category_url) > 2048:
                raise ValueError(f"产品 {name} 的 DMIT PID 必须是正整数（最多 10 位），名称最多 120 字符")
            if plan_key and not re.fullmatch(PLAN_KEY_PATTERN, plan_key):
                raise ValueError(f"产品 {name} 的 DMIT 套餐标识必须是 地区:平台:线路:套餐 的完整标识")
            normalized_item = {"id": product_id, "name": name, "provider": provider, "adapter": adapter, "category_url": category_url, "pid": pid, "enabled": item.get("enabled", False)}
            if plan_key:
                normalized_item["plan_key"] = plan_key
        elif adapter == "bwh":
            from bwh import PRODUCT_URL, normalize_catalog_url
            category_url = normalize_catalog_url(item.get("category_url", PRODUCT_URL))
            offer_key = str(item.get("bwh_offer_key", item.get("selector", ""))).strip()
            if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{1,40}:[A-Za-z0-9_-]{1,40}:[0-9]+", offer_key) or pid and not re.fullmatch(r"[1-9][0-9]{0,9}", pid):
                raise ValueError(f"产品 {name} 的搬瓦工套餐标识无效，请从库存大盘重新添加")
            normalized_item = {"id": product_id, "name": name, "provider": provider, "adapter": adapter, "category_url": category_url, "selector": offer_key, "bwh_offer_key": offer_key, "pid": pid, "enabled": item.get("enabled", False)}
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
        elif adapter == "narwhal":
            machine_id = str(item.get("machine_id", "")).strip()
            plan_id = str(item.get("plan_id", "")).strip()
            uuid_pattern = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}"
            if not re.fullmatch(uuid_pattern, machine_id) or not re.fullmatch(uuid_pattern, plan_id) or len(name) > 120:
                raise ValueError(f"产品 {name} 需要从独角鲸云目录选择有效的实例和套餐")
            normalized_item = {"id": product_id, "name": name, "provider": "独角鲸云", "adapter": adapter, "machine_id": machine_id.lower(), "plan_id": plan_id.lower(), "enabled": item.get("enabled", False)}
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
        if request_mode not in {"inherit", "requests", "flaresolverr", "cloudscraper"}:
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
        coupon = item.get("notification_coupon_code", "")
        if not isinstance(coupon, str) or len(coupon) > 128 or any(ch in coupon for ch in "\r\n"):
            raise ValueError(f"产品 {name} 的通知优惠码格式无效")
        normalized_item["notification_coupon_code"] = coupon.strip()
        snapshot = item.get("snapshot", {})
        if not isinstance(snapshot, dict):
            raise ValueError("商品快照必须是对象")
        normalized_item["snapshot"] = {key: str(snapshot.get(key, ""))[:2048 if key == "url" else 500] for key in ("name", "region", "specs", "traffic", "price", "url", "checked_at")}
        if normalized_item["snapshot"]["url"] and urlsplit(normalized_item["snapshot"]["url"]).scheme != "https":
            raise ValueError("商品购买快照必须是 HTTPS 地址")
        if type(snapshot.get("available")) is int and snapshot["available"] >= 0:
            normalized_item["snapshot"]["available"] = snapshot["available"]
        normalized_item["snapshot"]["stock_mode"] = "availability" if snapshot.get("stock_mode") == "availability" else "quantity"
        if adapter == "dmit" and type(snapshot.get("stock_version")) is int:
            normalized_item["snapshot"]["stock_version"] = max(0, snapshot["stock_version"])
        normalized_item["request_mode"] = request_mode
        seen_ids.add(product_id)
        normalized.append(normalized_item)
    enabled = value.get("enabled", False)
    if enabled and not any(item["enabled"] for item in normalized) and not value.get("isvoro_seckill", False):
        raise ValueError("启用监控前至少启用一个产品")
    fetcher = normalize_fetcher(value.get("fetcher", {}))
    telegram = normalize_telegram(value.get("telegram", {}))
    egress_config = egress.normalize(value.get("egress", {}))
    vps_seckill = normalize_seckill(value.get("vps_seckill", {}), seen_ids)
    akile_market = normalize_akile_market(value.get("akile_market", {}), vps_seckill.get("accounts", []))
    isvoro_market = normalize_isvoro_market(value.get("isvoro_market", {}), vps_seckill.get("accounts", []))
    product_orders = {}
    for key in ("watch_order", "notify_order"):
        order = value.get(key, [])
        if not isinstance(order, list) or len(order) > 100 or any(not isinstance(identifier, str) for identifier in order) or len(set(order)) != len(order):
            raise ValueError("商品顺序必须是最多 100 项、无重复的商品 ID 列表")
        product_orders[key] = [identifier for identifier in order if identifier in seen_ids]
    return {
        "enabled": enabled,
        "interval_seconds": interval,
        "monitor_schedule": monitor_schedule,
        "notify_schedule": notify_schedule,
        "catalog_schedule": catalog_schedule,
        "seckill_schedule": seckill_schedule,
        "notify_current_stock_on_start": value.get("notify_current_stock_on_start", False),
        "isvoro_seckill": value.get("isvoro_seckill", False),
        "affiliate_id": affiliates["vmiss"],
        "affiliates": affiliates,
        "affiliate_notes": affiliate_notes,
        "affiliate_accounts": affiliate_accounts,
        "affiliate_rates": affiliate_rates,
        "affiliate_coupons": affiliate_coupons,
        "merchant_order": merchant_order,
        **product_orders,
        "fetcher": {**fetcher, "egress": egress_config},
        "telegram": telegram,
        "egress": egress_config,
        "vps_seckill": vps_seckill,
        "akile_market": akile_market,
        "isvoro_market": isvoro_market,
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
    result = telegram_call(token, "getUpdates", allowed_updates=["message", "channel_post", "edited_message", "edited_channel_post"], limit=100)
    chats: dict[str, dict] = {}
    updates = result.get("result", [])
    if not isinstance(updates, list):
        raise TelegramAPIError("Telegram getUpdates 返回了无法识别的聊天列表")
    for update in updates:
        if not isinstance(update, dict):
            continue
        try:
            update_id = int(update.get("update_id", 0) or 0)
        except (TypeError, ValueError):
            update_id = 0
        for key in ("message", "channel_post", "edited_message", "edited_channel_post"):
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
                    "username": str(chat.get("username") or ""),
                    "last_update_id": update_id,
                }
                if update_id >= chats[chat_id].get("last_update_id", 0):
                    chats[chat_id]["last_update_id"] = update_id
    return sorted(chats.values(), key=lambda row: (-row.get("last_update_id", 0), row["name"].casefold()))


def current_config() -> dict:
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else default_config()
    return validate_config(raw)


def register_egress_node(payload: dict, authorization: str = "") -> dict:
    if not isinstance(payload, dict):
        raise ValueError("出口节点注册内容无效")
    current = current_config()
    node_id = str(payload.get("node_id", "")).strip()
    existing = next((item for item in current["egress"]["nodes"] if item["id"] == node_id), None)
    expected = existing.get("token", "") if existing else os.getenv("EGRESS_REGISTER_TOKEN", "").strip()
    supplied = authorization.removeprefix("Bearer ").strip()
    if not authorization.startswith("Bearer ") or not expected or not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise ValueError("出口节点注册令牌无效")
    node = {**(existing or {}), "id": node_id, "name": existing["name"] if existing else payload.get("name", node_id), "proxy_url": payload.get("proxy_url", ""),
        "health_url": payload.get("health_url", ""), "token": existing["token"] if existing else payload.get("token", ""), "public_ip": payload.get("public_ip", ""),
        "enabled": existing["enabled"] if existing else True, "pending_registration": False,
        "last_seen": beijing_iso(), "last_error": ""}
    nodes = [item for item in current.get("egress", {}).get("nodes", []) if item["id"] != node_id]
    nodes.append(node)
    current["egress"] = egress.normalize({"nodes": nodes, "routes": current.get("egress", {}).get("routes", {})})
    save_config(current)
    return {"ok": True, "node_id": node_id, "public_ip": node["public_ip"]}


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
                item=product, checked_at=result["checked_at"], test=True, duration=result["duration_seconds"], warning=result.get("warning", ""), purchase_url=link,
                coupon_code=effective_coupon(current_config(), product, product.get("adapter", "")))
        else:
            message = escape("❌ HostWatch · 商品单次测试\n"
                f"商家：{str(product.get('provider') or product.get('adapter', ''))[:80]}\n"
                f"商品：{str(product['name'])[:200]}\n测试失败：{str(result.get('error', '未知错误'))[:800]}\n"
                f"检查时间：{format_beijing(result['checked_at'])}\n耗时：{result['duration_seconds']} 秒")
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


def _akile_market_alert_state() -> dict:
    path = CONFIG_PATH.parent / "state-akile-market.json"
    if not path.exists():
        return {"records": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or not isinstance(data.get("records"), dict)
                or any(not isinstance(row, dict) or not isinstance(row.get("matched"), bool)
                       or not isinstance(row.get("sent"), bool) or not isinstance(row.get("reason"), str)
                       for row in data["records"].values())):
            raise ValueError()
        return data
    except (OSError, ValueError, TypeError):
        raise ValueError("交易市场去重状态损坏，已停止触发，请先检查状态文件") from None


def _save_akile_market_alert_state(value: dict) -> None:
    path = CONFIG_PATH.parent / "state-akile-market.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="market-state-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _akile_market_session(config: dict, account_id: str):
    from monitor import product_fetcher
    account = next((row for row in config["vps_seckill"]["accounts"]
                    if row["id"] == account_id and row["provider"] == "akile"), None)
    if not account:
        raise ValueError("请选择已保存的 Akile 网站账户")
    fetcher = product_fetcher(config["fetcher"], {"adapter": "akile", "egress_node_id": account.get("egress_node_id", "")})
    session = requests.Session()
    session.trust_env = False
    session.headers.update({"User-Agent": "HostWatch/1.0 (Akile marketplace)", "Accept": "application/json"})
    egress.apply_session(session, fetcher)
    try:
        checked = public_account_check_result(check_order_account(session, account, fetcher))
        if not checked.get("ok"):
            raise ValueError("Akile 账户认证失败：" + str(checked.get("message", "无法验证"))[:180])
        return session
    except Exception:
        session.close()
        raise


def _isvoro_market_session(config: dict, account_id: str):
    from monitor import product_fetcher
    account = next((row for row in config["vps_seckill"]["accounts"]
                    if row["id"] == account_id and row["provider"] == "isvoro"), None)
    if not account:
        raise ValueError("请选择已保存的 Isvoro 网站账户")
    fetcher = product_fetcher(config["fetcher"], {"adapter": "isvoro", "egress_node_id": account.get("egress_node_id", "")})
    session = requests.Session()
    session.trust_env = False
    session.headers.update({"User-Agent": "HostWatch/1.0 (Isvoro marketplace)", "Accept": "application/json"})
    egress.apply_session(session, fetcher)
    try:
        checked = public_account_check_result(check_order_account(session, account, fetcher))
        if not checked.get("ok"):
            raise ValueError("Isvoro 账户认证失败：" + str(checked.get("message", "无法验证"))[:180])
        return session
    except Exception:
        session.close()
        raise


def _akile_market_snapshot(account_id: str = "") -> dict:
    with AKILE_MARKET_LOCK:
        result = json.loads(json.dumps(AKILE_MARKET_CACHE))
    # A browser selecting another account must not see the previous account's snapshot.
    if account_id and account_id != result.get("account_id"):
        return {"items": [], "updated_at": "", "error": "", "results": [], "account_id": account_id}
    return result


def _isvoro_market_snapshot(account_id: str = "") -> dict:
    with ISVORO_MARKET_LOCK:
        result = json.loads(json.dumps(ISVORO_MARKET_CACHE))
    if account_id and account_id != result.get("account_id"):
        return {"items": [], "updated_at": "", "error": "", "results": [], "account_id": account_id}
    return result


def _isvoro_market_alert_state() -> dict:
    path = CONFIG_PATH.parent / "state-isvoro-market.json"
    if not path.exists():
        return {"records": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or not isinstance(data.get("records"), dict)
                or any(not isinstance(row, dict) or not isinstance(row.get("matched"), bool)
                       or not isinstance(row.get("sent"), bool) or not isinstance(row.get("reason"), str)
                       for row in data["records"].values())):
            raise ValueError()
        return data
    except (OSError, ValueError, TypeError):
        raise ValueError("Isvoro 交易市场去重状态损坏，已停止触发，请先检查状态文件") from None


def _save_isvoro_market_alert_state(value: dict) -> None:
    path = CONFIG_PATH.parent / "state-isvoro-market.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="isvoro-market-state-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _akile_market_alert(config: dict, rule: dict, item: dict, reason: str) -> bool:
    token, chat_id = effective_telegram(config)
    if not token or not chat_id:
        return False
    kind = "折价" if item["delta"] >= 0 else "溢价"
    message = "\n".join([
        "🔔 <b>HostWatch · Akile 交易市场</b>",
        f"商品：{escape(item['name'])} · {escape(item['region'])} · {escape(item['node'])}",
        f"挂单 ID：<code>{item['id']}</code>",
        f"售价：¥{item['price']:.2f} · 续费：¥{item['renewal']:.2f}/{item['cycle']}个月",
        f"剩余：{item['remaining_days']:.2f} 天 · 剩余价值：¥{item['fair_value']:.2f} · 月付：¥{item['monthly_price']:.2f}",
        f"{kind}：{abs(item['rate']):.2f}% · ¥{abs(item['delta']):.2f}",
        f"触发规则：{escape(rule.get('name') or rule['id'])}",
        escape(reason[:400]), f"检查时间：{format_beijing(item['updated_at'])}",
    ])
    keyboard = {"inline_keyboard": [[{"text": "查看 Akile 商品", "url": f"https://akile.ai/console/pushshop?highlight={item['id']}"}]]}
    telegram_call(token, "sendMessage", chat_id=chat_id, text=message, parse_mode="HTML",
        disable_web_page_preview=True, reply_markup=keyboard)
    return True


def _evaluate_akile_market(config: dict, items: list[dict], session) -> list[dict]:
    """Persist trigger decisions before delivery; failed delivery retries without re-running actions."""
    market = config["akile_market"]
    rules = [row for row in market["items"] if row["enabled"]]
    state = _akile_market_alert_state()
    records, results = state["records"], []
    # Pausing Telegram must not change the destination identity and discard
    # successfully delivered notifications when Telegram is resumed.
    telegram = config.get("telegram", {})
    token = telegram.get("bot_token") or os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = telegram.get("chat_id") or os.getenv("TELEGRAM_CHAT_ID", "")
    target = hashlib.sha256((token + ":" + chat_id).encode()).hexdigest()
    valid_prefixes = set()
    for rule in rules:
        signature = hashlib.sha256(json.dumps([rule, market["account_id"], target], sort_keys=True).encode()).hexdigest()
        valid_prefixes.add(signature)
        hits = [item for item in items if akile_market_rule_matches(rule, item)]
        if not hits:
            results.append({"rule_id": rule["id"], "status": "waiting", "message": "等待满足阈值的有效挂单"})
        for item in hits:
            key = signature + ":" + item["id"]
            record = records.get(key, {})
            if not record.get("matched"):
                record = {"matched": True, "sent": False, "at": beijing_iso()}
                if rule["action"] == "unpaid_order":
                    # The vendor requires a fresh WebAuthn user-presence assertion.
                    # Do not call makeOrder with a missing or fabricated verification token.
                    record.update(status="verification_required", reason="达到下单条件，但 Akile 要求用户在官网完成 Passkey 购买验证。未提交订单、未付款，请手动购买。")
                else:
                    record.update(status="matched", reason="已达到设定阈值，请到官网手动下单。")
                records[key] = record
                _save_akile_market_alert_state(state)
            record["last_seen"] = time.time()
            if not record.get("sent"):
                try:
                    record["sent"] = _akile_market_alert(config, rule, item, record["reason"])
                    record["notification"] = "已发送 Telegram" if record["sent"] else "Telegram 未配置或已停用，等待恢复后发送"
                except (ValueError, requests.RequestException):
                    record["notification"] = "Telegram 发送失败，下一轮重试通知"
                _save_akile_market_alert_state(state)
            results.append({"rule_id": rule["id"], "product_id": item["id"], "status": record["status"],
                            "message": record["reason"], "notification": record.get("notification", "")})
        hit_ids = {item["id"] for item in hits}
        readable = {item["id"] for item in items if not item.get("valuation_error")}
        for key, record in records.items():
            if key.startswith(signature + ":") and key.split(":", 1)[1] in readable - hit_ids:
                record["matched"] = False
    # Keep recent records across restarts without growing the file indefinitely.
    state["records"] = {key: val for key, val in records.items()
                        if key.split(":", 1)[0] in valid_prefixes
                        and val.get("last_seen", time.time()) >= time.time() - 30 * 86400}
    _save_akile_market_alert_state(state)
    return results


def _isvoro_market_rule_matches(rule: dict, item: dict) -> bool:
    if item.get("valuation_error") or item.get("remaining_days", 0) <= 0 or item.get("status") != 0:
        return False
    if rule["target"] == "listing":
        if rule["product_id"] != item["id"]:
            return False
    elif any(rule[key] != item.get(key) for key in ("plan_name", "region", "node")):
        return False
    checks = []
    for key, actual in (("rate_threshold", item["rate"]), ("amount_threshold", item["delta"])):
        if rule.get(key) is not None:
            checks.append(actual >= rule[key] if rule["direction"] == "discount" else -actual <= rule[key])
    return bool(checks) and (all(checks) if rule.get("match") == "all" else any(checks))


def _isvoro_market_alert(config: dict, rule: dict, item: dict) -> bool:
    token, chat_id = effective_telegram(config)
    if not token or not chat_id:
        return False
    kind = "折价" if item["delta"] >= 0 else "溢价"
    cycle = f"{item['cycle']}{'个月' if item['cycle_unit'] == 'month' else '年'}"
    message = "\\n".join([
        "🔔 <b>HostWatch · Isvoro 二手市场</b>",
        f"商品：{escape(item['name'])} · {escape(item['region'])} · {escape(item['node'])}",
        f"挂单 ID：<code>{item['id']}</code>",
        f"售价：{escape(item['currency_code'])} {item['price']:.2f} · 续费：{escape(item['currency_code'])} {item['renewal']:.2f}/{cycle}",
        f"剩余：{item['remaining_days']:.2f} 天 · 剩余价值：{escape(item['currency_code'])} {item['fair_value']:.2f} · 月付：{escape(item['currency_code'])} {item['monthly_price']:.2f}",
        f"{kind}：{abs(item['rate']):.2f}% · {escape(item['currency_code'])} {abs(item['delta']):.2f}",
        f"触发规则：{escape(rule.get('name') or rule['id'])}",
        f"检查时间：{format_beijing(item['updated_at'])}",
    ])
    telegram_call(token, "sendMessage", chat_id=chat_id, text=message, parse_mode="HTML",
        disable_web_page_preview=True, reply_markup={"inline_keyboard": [[{"text": "查看 Isvoro 商品", "url": "https://isvoro.com/marketplace"}]]})
    return True


def _evaluate_isvoro_market(config: dict, items: list[dict]) -> list[dict]:
    market = config["isvoro_market"]
    rules = [row for row in market["items"] if row["enabled"]]
    state = _isvoro_market_alert_state()
    records, results = state["records"], []
    telegram = config.get("telegram", {})
    token = telegram.get("bot_token") or os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = telegram.get("chat_id") or os.getenv("TELEGRAM_CHAT_ID", "")
    target = hashlib.sha256((token + ":" + chat_id).encode()).hexdigest()
    valid_prefixes = set()
    for rule in rules:
        signature = hashlib.sha256(json.dumps([rule, market["account_id"], target], sort_keys=True).encode()).hexdigest()
        valid_prefixes.add(signature)
        hits = [item for item in items if _isvoro_market_rule_matches(rule, item)]
        if not hits:
            results.append({"rule_id": rule["id"], "status": "waiting", "message": "等待满足阈值的有效挂单"})
        for item in hits:
            key = signature + ":" + item["id"]
            record = records.get(key, {})
            if not record.get("matched"):
                record = {"matched": True, "sent": False, "at": beijing_iso(), "reason": "已达到设定阈值，请到 Isvoro 官网手动购买。"}
                records[key] = record
                _save_isvoro_market_alert_state(state)
            record["last_seen"] = time.time()
            if not record.get("sent"):
                try:
                    record["sent"] = _isvoro_market_alert(config, rule, item)
                    record["notification"] = "已发送 Telegram" if record["sent"] else "Telegram 未配置或已停用，等待恢复后发送"
                except (ValueError, requests.RequestException):
                    record["notification"] = "Telegram 发送失败，下一轮重试通知"
                _save_isvoro_market_alert_state(state)
            results.append({"rule_id": rule["id"], "product_id": item["id"], "status": "matched",
                            "message": record["reason"], "notification": record.get("notification", "")})
        hit_ids = {item["id"] for item in hits}
        readable = {item["id"] for item in items if not item.get("valuation_error")}
        for key, record in records.items():
            if key.startswith(signature + ":") and key.split(":", 1)[1] in readable - hit_ids:
                record["matched"] = False
    state["records"] = {key: val for key, val in records.items()
                        if key.split(":", 1)[0] in valid_prefixes
                        and val.get("last_seen", time.time()) >= time.time() - 30 * 86400}
    _save_isvoro_market_alert_state(state)
    return results


def refresh_isvoro_market(*, account_id: str = "", evaluate: bool = False, automatic: bool = False) -> dict:
    if not ISVORO_MARKET_RUN_LOCK.acquire(blocking=False):
        raise ValueError("Isvoro 交易市场正在刷新，请稍后重试")
    try:
        config = current_config()
        market = config["isvoro_market"]
        account_id = account_id or market["account_id"]
        if not account_id:
            raise ValueError("请选择已保存的 Isvoro 网站账户")
        with _isvoro_market_session(config, account_id) as session:
            raw = isvoro_market_list(session)
            items = []
            for row in raw:
                try:
                    items.append(isvoro_market_valuation(row))
                except ValueError:
                    service = row.get("service") if isinstance(row.get("service"), dict) else {}
                    product = service.get("product") if isinstance(service.get("product"), dict) else {}
                    items.append({"id": str(row.get("id", "")), "name": str(product.get("name", ""))[:160],
                                  "valuation_error": "商品数据异常，无法估值"})
            results = _isvoro_market_snapshot(account_id).get("results", [])
            if evaluate:
                latest = current_config()
                if latest["isvoro_market"] != market or latest["vps_seckill"]["accounts"] != config["vps_seckill"]["accounts"]:
                    raise ValueError("配置在刷新期间发生变化，本轮未触发规则")
                if automatic and not latest["isvoro_market"]["enabled"]:
                    raise ValueError("Isvoro 交易市场已暂停")
                if account_id != market["account_id"]:
                    raise ValueError("规则检查必须使用已保存的市场账户")
                results = _evaluate_isvoro_market(latest, items)
        result = {"account_id": account_id, "updated_at": beijing_iso(), "items": items, "results": results,
                  "error": "", "warning": "", "running": current_config()["isvoro_market"]["enabled"]}
        with ISVORO_MARKET_LOCK:
            ISVORO_MARKET_CACHE.update(result)
        return result
    except (OSError, ValueError, requests.RequestException) as exc:
        with ISVORO_MARKET_LOCK:
            ISVORO_MARKET_CACHE["error"] = str(exc)[:300] if isinstance(exc, ValueError) else "Isvoro 市场网络请求失败，请检查商家出口与官网状态"
        raise ValueError(ISVORO_MARKET_CACHE["error"]) from None
    finally:
        ISVORO_MARKET_RUN_LOCK.release()


def _isvoro_market_loop() -> None:
    deadline = 0.0
    failures = 0
    while True:
        try:
            market = current_config()["isvoro_market"]
            with ISVORO_MARKET_LOCK:
                ISVORO_MARKET_CACHE["running"] = market["enabled"]
            if not market["enabled"]:
                failures = 0
                deadline = 0.0
            elif failures < ISVORO_MARKET_MAX_FAILURES and time.monotonic() >= deadline:
                try:
                    refresh_isvoro_market(evaluate=True, automatic=True)
                    failures = 0
                    deadline = time.monotonic() + market["interval_seconds"]
                except (OSError, ValueError, requests.RequestException) as exc:
                    failures += 1
                    with ISVORO_MARKET_LOCK:
                        ISVORO_MARKET_CACHE["error"] = (
                            f"Isvoro 市场连续请求失败 {failures}/{ISVORO_MARKET_MAX_FAILURES}：{str(exc)[:240]}"
                            if failures < ISVORO_MARKET_MAX_FAILURES else
                            f"Isvoro 市场连续请求失败 {ISVORO_MARKET_MAX_FAILURES} 次，已停止自动重试；请手动刷新或修改市场设置后再试"
                        )
                    deadline = time.monotonic() + max(300, market.get("interval_seconds", 600))
        except (OSError, ValueError, requests.RequestException) as exc:
            failures += 1
            with ISVORO_MARKET_LOCK:
                ISVORO_MARKET_CACHE["error"] = str(exc)[:300]
            deadline = time.monotonic() + max(300, market.get("interval_seconds", 600))
        woke = ISVORO_MARKET_WAKE.wait(60 if failures >= ISVORO_MARKET_MAX_FAILURES else 2)
        if woke:
            ISVORO_MARKET_WAKE.clear()
            failures = 0
            deadline = 0.0


ISVORO_MARKET_WAKE = threading.Event()
ISVORO_MARKET_THREAD = None
ISVORO_MARKET_MAX_FAILURES = 3


def start_isvoro_market_loop() -> None:
    global ISVORO_MARKET_THREAD
    if not ISVORO_MARKET_THREAD or not ISVORO_MARKET_THREAD.is_alive():
        ISVORO_MARKET_THREAD = threading.Thread(target=_isvoro_market_loop, name="isvoro-market-monitor", daemon=True)
        ISVORO_MARKET_THREAD.start()


def refresh_akile_market(*, account_id: str = "", evaluate: bool = False, automatic: bool = False) -> dict:
    if not AKILE_MARKET_RUN_LOCK.acquire(blocking=False):
        raise ValueError("交易市场正在刷新，请稍后重试")
    try:
        config = current_config()
        market = config["akile_market"]
        account_id = account_id or market["account_id"]
        with _akile_market_session(config, account_id) as session:
            raw = akile_market_list(session)
            items = []
            for row in raw:
                try:
                    items.append(akile_market_valuation(row))
                except ValueError:
                    # Invalid rows are visible but can never trigger rules.
                    items.append({"id": str(row.get("id", "")), "name": str(row.get("name", ""))[:160],
                                  "valuation_error": "商品数据异常，无法估值"})
            results = _akile_market_snapshot(account_id).get("results", [])
            warning = "官网同价商品分页排序存在重叠，当前列表可能缺少部分挂单；指定挂单将单独核验。" if session._hostwatch_market_incomplete is True else ""
            if evaluate:
                # Read configured listings directly, and query exact plans separately,
                # so overlapping general-market pages do not hide a watched listing.
                candidates = {row["id"]: row for row in items if row.get("id")}
                failed_details = []
                for rule in market["items"]:
                    if not rule["enabled"]:
                        continue
                    if rule["target"] == "plan":
                        for source in akile_market_list(session, name=rule["plan_name"], area=rule["region"]):
                            try:
                                row = akile_market_valuation(source)
                                candidates[row["id"]] = row
                            except ValueError:
                                pass
                    else:
                        candidates.setdefault(rule["product_id"], {"id": rule["product_id"]})
                verified = []
                for identifier, candidate in candidates.items():
                    if not any(rule["enabled"] and ((rule["target"] == "listing" and rule["product_id"] == identifier)
                           or (rule["target"] == "plan" and all(rule[key] == candidate.get(key) for key in ("plan_name", "region", "node"))))
                           for rule in market["items"]):
                        continue
                    try:
                        verified.append(akile_market_valuation(akile_market_detail(session, identifier)))
                    except (ValueError, requests.RequestException):
                        failed_details.extend({"rule_id": rule["id"], "product_id": identifier, "status": "unknown", "message": "商品详情无法核验，本轮不触发"}
                            for rule in market["items"] if rule["enabled"] and ((rule["target"] == "listing" and rule["product_id"] == identifier)
                                or (rule["target"] == "plan" and all(rule[key] == candidate.get(key) for key in ("plan_name", "region", "node")))))
                latest = current_config()
                if latest["akile_market"] != market or latest["vps_seckill"]["accounts"] != config["vps_seckill"]["accounts"]:
                    raise ValueError("配置在刷新期间发生变化，本轮未触发规则")
                if automatic and not latest["akile_market"]["enabled"]:
                    raise ValueError("交易市场已暂停")
                if account_id != market["account_id"]:
                    raise ValueError("规则检查必须使用已保存的市场账户")
                results = _evaluate_akile_market(latest, verified, session) + failed_details
                items = list({row["id"]: row for row in [*items, *verified]}.values())
        result = {"account_id": account_id, "updated_at": beijing_iso(), "items": items, "results": results,
                  "error": "", "warning": warning, "running": current_config()["akile_market"]["enabled"],
                  "order_capability": "Akile 交易市场要求官网 Passkey 验证；当前仅能提醒手动下单，未提交订单。"}
        with AKILE_MARKET_LOCK:
            AKILE_MARKET_CACHE.update(result)
        return result
    except (OSError, ValueError, requests.RequestException) as exc:
        with AKILE_MARKET_LOCK:
            AKILE_MARKET_CACHE["error"] = str(exc)[:300] if isinstance(exc, ValueError) else "交易市场网络请求失败，请检查商家出口与官网状态"
        raise ValueError(AKILE_MARKET_CACHE["error"]) from None
    finally:
        AKILE_MARKET_RUN_LOCK.release()


def _akile_market_loop() -> None:
    deadline = 0.0
    while True:
        try:
            market = current_config()["akile_market"]
            with AKILE_MARKET_LOCK:
                AKILE_MARKET_CACHE["running"] = market["enabled"]
            if market["enabled"] and time.monotonic() >= deadline:
                try:
                    refresh_akile_market(evaluate=True, automatic=True)
                finally:
                    deadline = time.monotonic() + market["interval_seconds"]
        except (OSError, ValueError, requests.RequestException) as exc:
            with AKILE_MARKET_LOCK:
                AKILE_MARKET_CACHE["error"] = str(exc)[:300]
            deadline = time.monotonic() + 30
        if AKILE_MARKET_WAKE.wait(2):
            AKILE_MARKET_WAKE.clear()
            deadline = 0


def start_akile_market_loop() -> None:
    global AKILE_MARKET_THREAD
    if not AKILE_MARKET_THREAD or not AKILE_MARKET_THREAD.is_alive():
        AKILE_MARKET_THREAD = threading.Thread(target=_akile_market_loop, name="akile-market-monitor", daemon=True)
        AKILE_MARKET_THREAD.start()


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
            timeout=120 if value.get("action") == "backup-import" else 30,
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


def _session_cookie_value(header: str) -> str:
    for part in str(header or "").split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and name == ADMIN_SESSION_COOKIE:
            token = value.strip()
            if re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
                return token
    return ""


def _create_admin_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    with ADMIN_SESSION_LOCK:
        ADMIN_SESSIONS[token] = {"username": username, "auth_fingerprint": _admin_auth_fingerprint(), "expires_at": time.time() + ADMIN_SESSION_TTL}
    return token


def _admin_auth_fingerprint() -> tuple[str, str]:
    path = users_path()
    if path is not None:
        try:
            return ("file", hashlib.sha256(path.read_bytes()).hexdigest())
        except OSError:
            return ("missing", "")
    password = os.getenv("ADMIN_PASSWORD", "")
    return ("legacy", hashlib.sha256(f"{os.getenv('ADMIN_USERNAME', 'admin')}:{password}".encode("utf-8")).hexdigest())


def _admin_session_username(header: str) -> str:
    token = _session_cookie_value(header)
    if not token:
        return ""
    with ADMIN_SESSION_LOCK:
        record = ADMIN_SESSIONS.get(token)
        if not record:
            return ""
        if record.get("expires_at", 0) <= time.time() or record.get("auth_fingerprint") != _admin_auth_fingerprint():
            ADMIN_SESSIONS.pop(token, None)
            return ""
        return str(record.get("username", ""))


def _revoke_admin_session(header: str) -> None:
    token = _session_cookie_value(header)
    if token:
        with ADMIN_SESSION_LOCK:
            ADMIN_SESSIONS.pop(token, None)


def _clear_admin_sessions() -> None:
    with ADMIN_SESSION_LOCK:
        ADMIN_SESSIONS.clear()


class Handler(BaseHTTPRequestHandler):
    server_version = "HostWatchAdmin/1.0"
    # Reuse the browser's authenticated HTTP connection for the lightweight
    # status requests instead of opening a new TCP connection every time.
    protocol_version = "HTTP/1.1"

    def send_error(self, code, message=None, explain=None):
        # Rejected requests may still have an unread body. Close them instead
        # of letting HTTP/1.1 parse that body as the next request.
        self.close_connection = True
        super().send_error(code, message, explain)

    def _secure(self) -> bool:
        try:
            if not ip_address(self.client_address[0]).is_loopback:
                self.send_error(403)
                return False
        except ValueError:
            self.send_error(403)
            return False
        return True

    def _authorized(self) -> bool:
        # Preserve legacy password-free SSH tunnels, while requiring credentials
        # when a reverse proxy forwards an arbitrary public hostname.
        if not authentication_required():
            try:
                host = (urlsplit("//" + self.headers.get("Host", "")).hostname or "").casefold().rstrip(".")
                local = host == "localhost" or ip_address(host).is_loopback
            except ValueError:
                local = False
            if not local:
                message = "管理页尚未配置管理员账号，请在服务器运行 hw init-admin，然后使用生成的账号登录。访问域名无需配置允许列表。"
                if self.path.startswith("/api/"):
                    self._send(503, "application/json; charset=utf-8", json.dumps({"error": message, "code": "admin_auth_not_configured"}, ensure_ascii=False).encode("utf-8"))
                else:
                    self._send(503, "text/html; charset=utf-8", ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>请初始化管理员 · HostWatch</title><h1>请初始化管理员账号</h1><p>' + message + '</p></html>').encode("utf-8"))
                return False
        if _admin_session_username(self.headers.get("Cookie", "")) or authorized(self.headers.get("Authorization", "")):
            return True
        if urlsplit(self.path).path in {"/", "/admin.html"}:
            self._send(200, "text/html; charset=utf-8", LOGIN_PAGE_PATH.read_bytes())
            return False
        self._send_unauthorized()
        return False

    def _send_unauthorized(self) -> None:
        self.close_connection = True
        self.send_response(401)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        body = json.dumps({"error": "需要登录管理员账号", "code": "admin_auth_required"}, ensure_ascii=False).encode("utf-8")
        self.send_header("WWW-Authenticate", 'Basic realm="HostWatch admin", charset="UTF-8"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _cookie_header(self, token: str = "", max_age: int | None = None) -> str:
        parts = [f"{ADMIN_SESSION_COOKIE}={token}", "Path=/", "HttpOnly", "SameSite=Lax"]
        if max_age is not None:
            parts.append(f"Max-Age={max_age}")
        return "; ".join(parts)

    def _auth_login(self) -> None:
        if not authentication_required():
            self._send(503, "application/json; charset=utf-8", json.dumps({"error": "尚未配置管理员账号，请先运行 hw init-admin", "code": "admin_auth_not_configured"}, ensure_ascii=False).encode("utf-8"))
            return
        try:
            value = request_body(self)
            username = str(value.get("username", ""))
            password = str(value.get("password", ""))
            if not username or not password or len(username) > 64 or len(password) > 256:
                raise ValueError("请输入用户名和密码")
            if not credentials_authorized(username, password):
                self._send(401, "application/json; charset=utf-8", json.dumps({"error": "用户名或密码错误"}, ensure_ascii=False).encode("utf-8"))
                return
            token = _create_admin_session(username)
            self._send(200, "application/json; charset=utf-8", json.dumps({"ok": True, "username": username}, ensure_ascii=False).encode("utf-8"), {"Set-Cookie": self._cookie_header(token, ADMIN_SESSION_TTL)})
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self._send(400, "application/json; charset=utf-8", json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"))

    def _auth_status(self) -> None:
        username = _admin_session_username(self.headers.get("Cookie", ""))
        authenticated = bool(username) or (not authentication_required() and self._is_local_host())
        self._send(200, "application/json; charset=utf-8", json.dumps({"authenticated": authenticated, "username": username}, ensure_ascii=False).encode("utf-8"))

    def _is_local_host(self) -> bool:
        try:
            host = (urlsplit("//" + self.headers.get("Host", "")).hostname or "").casefold().rstrip(".")
            return host == "localhost" or ip_address(host).is_loopback
        except ValueError:
            return False

    def _guard(self) -> bool:
        return self._secure() and self._authorized()

    def _send(self, status: int, content_type: str, body: bytes, headers: dict[str, str] | None = None) -> None:
        reusable = status < 400
        if not reusable:
            self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "keep-alive" if reusable else "close")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_download(self, filename: str, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/gzip")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "keep-alive")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if not self._secure():
            return
        path = urlsplit(self.path).path
        if path == "/login.html":
            self._send(200, "text/html; charset=utf-8", LOGIN_PAGE_PATH.read_bytes())
            return
        if path == "/login.js":
            self._send(200, "text/javascript; charset=utf-8", LOGIN_SCRIPT_PATH.read_bytes())
            return
        if path == "/api/auth/status":
            self._auth_status()
            return
        if not self._authorized():
            return
        if path in {"/", "/admin.html"}:
            self._send(200, "text/html; charset=utf-8", PAGE_PATH.read_bytes())
        elif path in {"/admin.js", "/catalog.js"}:
            self._send(200, "text/javascript; charset=utf-8", (ROOT / path[1:]).read_bytes())
        elif path == "/api/config":
            try:
                raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else default_config()
                data = json.dumps(public_config(raw), ensure_ascii=False).encode("utf-8")
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self._send(500, "application/json; charset=utf-8", json.dumps({"error": f"配置文件无法读取：{exc}"}, ensure_ascii=False).encode("utf-8"))
                return
            self._send(200, "application/json; charset=utf-8", data)
        elif path == "/api/market":
            provider = dict(parse_qsl(urlsplit(self.path).query)).get("provider", "akile")
            if provider == "isvoro":
                output = _isvoro_market_snapshot()
                output["running"] = current_config()["isvoro_market"]["enabled"]
            else:
                output = _akile_market_snapshot()
                output["running"] = current_config()["akile_market"]["enabled"]
            self._send(200, "application/json; charset=utf-8", json.dumps(output, ensure_ascii=False).encode("utf-8"))
        elif path == "/api/egress":
            try:
                data = json.dumps(public_config(json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else default_config()).get("egress", {"nodes": [], "routes": {}}), ensure_ascii=False).encode("utf-8")
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self._send(500, "application/json; charset=utf-8", json.dumps({"error": f"出口节点配置无法读取：{exc}"}, ensure_ascii=False).encode("utf-8")); return
            self._send(200, "application/json; charset=utf-8", data)
        elif path == "/api/rates":
            self._send(200, "application/json; charset=utf-8", json.dumps(exchange_rates(), ensure_ascii=False).encode("utf-8"))
        elif path == "/api/status":
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
            from monitor import MonitorError, fetch_isvoro_json, isvoro_categories, isvoro_category, product_fetcher
            try:
                slug = self.path.rsplit("/", 1)[-1]
                if self.path != "/api/isvoro/categories" and not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", slug):
                    raise MonitorError("Isvoro 分类标识无效")
                with requests.Session() as session:
                    path = "categories" if self.path == "/api/isvoro/categories" else f"categories/{slug}"
                    raw = fetch_isvoro_json(session, path, product_fetcher(current_config()["fetcher"], {"adapter": "isvoro"}))
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
        elif self.path.startswith("/api/seckill/test/status/"):
            task_id = self.path.rsplit("/", 1)[-1]
            with SECKILL_LOCK:
                task = SECKILL_TEST_TASKS.get(task_id)
                stopped_early = task_id in STOPPED_SECKILL_TEST_IDS
            if task is None and stopped_early:
                self._send(200, "application/json; charset=utf-8", json.dumps({"task_id": task_id, "pending": False, "ok": False, "cancelled": True, "error": "测试已停止"}, ensure_ascii=False).encode("utf-8"))
                return
            if task is None:
                self._send(404, "application/json; charset=utf-8", b'{"error":"Seckill test task not found"}')
                return
            self._send(200, "application/json; charset=utf-8", json.dumps(task.snapshot(), ensure_ascii=False).encode("utf-8"))
        elif self.path.startswith("/api/seckill/status/"):
            task_id = self.path.rsplit("/", 1)[-1]
            with SECKILL_LOCK:
                task = SECKILL_TASKS.get(task_id)
            if task is None:
                self._send(404, "application/json; charset=utf-8", b'{"error":"Seckill task not found"}')
                return
            self._send(200, "application/json; charset=utf-8", json.dumps(task.snapshot(), ensure_ascii=False).encode("utf-8"))
        elif self.path.startswith("/api/refresh/status/"):
            task_id = self.path.rsplit("/", 1)[-1]
            with REFRESH_LOCK:
                task = REFRESH_TASKS.get(task_id)
            if task is None:
                self._send(404, "application/json; charset=utf-8", b'{"error":"Refresh task not found"}')
                return
            self._send(200, "application/json; charset=utf-8", json.dumps(task, ensure_ascii=False).encode("utf-8"))
        elif urlsplit(self.path).path == "/api/telegram/chats":
            try:
                query = dict(parse_qsl(urlsplit(self.path).query, keep_blank_values=True))
                token, _ = telegram_credentials(query.get("token", ""))
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
        path = urlsplit(self.path).path
        if path == "/api/auth/login":
            if not self._secure():
                return
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                self._send(415, "application/json; charset=utf-8", b'{"error":"Content-Type must be application/json"}')
                return
            self._auth_login()
            return
        if path == "/api/auth/logout":
            if not self._secure():
                return
            _revoke_admin_session(self.headers.get("Cookie", ""))
            self._send(200, "application/json; charset=utf-8", b'{"ok":true}', {"Set-Cookie": self._cookie_header("", 0)})
            return
        if self.path == "/api/egress/register":
            if not self._secure():
                return
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                self._send(415, "application/json; charset=utf-8", b'{"error":"Content-Type must be application/json"}'); return
            if not MUTATION_LOCK.acquire(blocking=False):
                self._send(409, "application/json; charset=utf-8", b'{"error":"configuration busy"}'); return
            try:
                try:
                    result = register_egress_node(request_body(self), self.headers.get("Authorization", ""))
                    self._send(200, "application/json; charset=utf-8", json.dumps(result, ensure_ascii=False).encode("utf-8"))
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    self._send(400, "application/json; charset=utf-8", json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"))
            finally:
                MUTATION_LOCK.release()
            return
        if not self._guard():
            return
        content_type = "application/gzip" if self.path == "/api/manage/backup-import" else "application/json"
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != content_type:
            self._send(415, "application/json; charset=utf-8", json.dumps({"error": f"Content-Type must be {content_type}"}).encode())
            return
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.send_error(403)
            return
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc.casefold() != self.headers.get("Host", "").casefold():
            self.send_error(403)
            return
        # Stopping and polling tests must never wait behind config/service locks.
        if self.path in {"/api/products/test", "/api/products/test/stop", "/api/catalog/refresh", "/api/catalog/stop", "/api/seckill/start", "/api/seckill/stop", "/api/seckill/account-check", "/api/seckill/account-status", "/api/seckill/account-verification", "/api/seckill/test", "/api/seckill/test/stop", "/api/market/refresh", "/api/market/check", "/api/market/test-notification", "/api/egress/probe"}:
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
        if urlsplit(self.path).path not in {"/api/config", "/api/telegram/test", "/api/telegram/chats", "/api/manage", "/api/manage/backup-import", "/api/fetcher/test", "/api/products/test", "/api/products/test/stop", "/api/catalog/refresh", "/api/catalog/stop", "/api/refresh", "/api/seckill/start", "/api/seckill/stop", "/api/seckill/account-check", "/api/seckill/account-status", "/api/seckill/account-verification", "/api/seckill/account-cache-clear", "/api/seckill/test", "/api/seckill/test/stop", "/api/market/refresh", "/api/market/check", "/api/market/test-notification", "/api/egress/probe", "/api/egress/install-command", "/api/egress/import"}:
            self.send_error(404)
            return
        try:
            if self.path == "/api/manage/backup-import":
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_BACKUP_UPLOAD:
                    raise ValueError("请求体大小无效，导入文件须为不超过 20 MB 的 tar.gz 备份")
                # Hold task-start locks during restore so current workers cannot
                # overwrite restored records or continue using replaced settings.
                with TEST_LOCK, SECKILL_LOCK, CATALOG.lock:
                    if (any(task.snapshot().get("pending") for task in TEST_TASKS.values())
                            or any(task.snapshot().get("pending") for task in SECKILL_TASKS.values())
                            or (CATALOG.task and CATALOG.task.get("pending"))
                            or AKILE_MARKET_RUN_LOCK.locked() or ISVORO_MARKET_RUN_LOCK.locked()):
                        raise ValueError("请先停止或等待商品测试、秒杀任务和大盘刷新完成，再导入备份")
                    raw = self.rfile.read(length)
                    if len(raw) != length or not raw.startswith(b"\x1f\x8b"):
                        raise ValueError("请上传有效的 tar.gz 备份文件")
                    result = manager_action({"action": "backup-import", "content": base64.b64encode(raw).decode("ascii")})
                self._send(200, "application/json; charset=utf-8", json.dumps(result, ensure_ascii=False).encode("utf-8"))
                return
            submitted = request_body(self)
            if urlsplit(self.path).path == "/api/telegram/chats":
                token, _ = telegram_credentials(str(submitted.get("bot_token", "")).strip())
                if not token:
                    raise ValueError("请先在管理页填写或保存 Telegram bot token")
                result = {"ok": True, "chats": telegram_chats(token)}
                self._send(200, "application/json; charset=utf-8", json.dumps(result, ensure_ascii=False).encode("utf-8"))
                return
            if self.path == "/api/egress/install-command":
                current = current_config()
                if not isinstance(submitted.get("node"), dict):
                    raise ValueError("节点配置必须是 JSON 对象")
                node = dict(submitted["node"])
                existing = next((item for item in current["egress"]["nodes"] if item["id"] == node.get("id")), {})
                node["token"] = node.get("token") or existing.get("token") or secrets.token_urlsafe(32)
                # A stale browser draft must not undo a completed registration.
                if existing and not existing.get("pending_registration") and node.get("pending_registration"):
                    node.update({key: existing[key] for key in ("proxy_url", "health_url", "public_ip", "last_seen", "pending_registration")})
                node = egress.normalize({"nodes": [node]})["nodes"][0]
                output = egress.installation_command(node, str(submitted.get("register_url", "")))
                nodes = [item for item in current["egress"]["nodes"] if item["id"] != node["id"]] + [node]
                current["egress"] = egress.normalize({"nodes": nodes, "routes": current["egress"]["routes"]})
                save_config(current)
                output["node"] = egress.public({"nodes": [node]})["nodes"][0]
                self._send(200, "application/json; charset=utf-8", json.dumps(output, ensure_ascii=False).encode()); return
            if self.path == "/api/egress/import":
                result = register_egress_node(submitted, "Bearer " + str(submitted.get("token", "")))
                self._send(200, "application/json; charset=utf-8", json.dumps({**result, "egress": egress.public(current_config()["egress"])}, ensure_ascii=False).encode()); return
            if self.path in {"/api/catalog/refresh", "/api/catalog/stop"}:
                try:
                    if self.path.endswith("/stop"):
                        output = CATALOG.stop()
                    elif submitted.get("trigger", "manual") == "manual":
                        output = CATALOG.refresh(submitted.get("provider"), submitted.get("scope", ""))
                    else:
                        output = CATALOG.refresh(submitted.get("provider"), submitted.get("scope", ""), "auto")
                    if self.path.endswith("/refresh"):
                        from monitor import module_schedule, update_module_status
                        update_module_status(STATUS_PATH, "catalog", module_schedule(current_config(), "catalog"), "pending", "正在刷新库存大盘", trigger=output.get("trigger", "manual"))
                except CatalogBusy as exc:
                    self._send(409, "application/json; charset=utf-8", json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"))
                    return
                self._send(200, "application/json; charset=utf-8", json.dumps(output, ensure_ascii=False).encode("utf-8"))
                return
            if self.path in {"/api/market/refresh", "/api/market/check"}:
                provider = str(submitted.get("provider", "akile")).strip().casefold()
                if provider not in {"akile", "isvoro"}:
                    raise ValueError("交易市场商家无效")
                refresh = refresh_isvoro_market if provider == "isvoro" else refresh_akile_market
                output = refresh(account_id=str(submitted.get("account_id", "")), evaluate=self.path.endswith("/check"))
                self._send(200, "application/json; charset=utf-8", json.dumps(output, ensure_ascii=False).encode("utf-8"))
                return
            if self.path == "/api/market/test-notification":
                provider = str(submitted.get("provider", "akile")).strip().casefold()
                if provider not in {"akile", "isvoro"}:
                    raise ValueError("交易市场商家无效")
                label = "Isvoro 二手市场" if provider == "isvoro" else "Akile 交易市场"
                token, chat_id = effective_telegram(current_config())
                if not token or not chat_id:
                    raise ValueError("请先启用并保存 Telegram 机器人和接收 Chat ID")
                message = "\n".join([
                    f"🧪 <b>测试通知 · HostWatch {label}</b>",
                    "这是一条示例市场提醒，用于确认 Telegram 通知通道正常。",
                    "商品：测试套餐 · 示例地区 · 示例节点",
                    "配置：2 核 · 2048 MB · 40 GB · 带宽 500 Mbps · 流量 1 TB/月",
                    "售价：¥10.00 · 续费：¥20.00/1个月（折合 ¥20.00/月）",
                    "折价：50.00% · ¥10.00",
                    "触发规则：通知功能测试",
                    "手动测试消息，不代表真实挂单；不会创建订单或付款。",
                    f"测试时间：{format_beijing(beijing_iso())}",
                ])
                telegram_call(token, "sendMessage", chat_id=chat_id, text=message,
                              parse_mode="HTML", disable_web_page_preview=True)
                self._send(200, "application/json; charset=utf-8",
                    json.dumps({"ok": True, "message": f"{label}测试通知已发送至 Telegram。"}, ensure_ascii=False).encode("utf-8"))
                return
            if self.path == "/api/refresh":
                output = start_manual_refresh(str(submitted.get("role", "")))
                self._send(202, "application/json; charset=utf-8", json.dumps(output, ensure_ascii=False).encode("utf-8"))
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
            if self.path == "/api/seckill/account-check":
                output = seckill_account_check(submitted)
                self._send(200, "application/json; charset=utf-8", json.dumps(output, ensure_ascii=False).encode("utf-8"))
                return
            if self.path == "/api/seckill/account-status":
                output = merchant_account_status(str(submitted.get("account_id", "")).strip())
                self._send(200, "application/json; charset=utf-8", json.dumps(output, ensure_ascii=False).encode("utf-8"))
                return
            if self.path == "/api/seckill/account-verification":
                output = seckill_account_verification(submitted)
                self._send(200, "application/json; charset=utf-8", json.dumps(output, ensure_ascii=False).encode("utf-8"))
                return
            if self.path == "/api/seckill/account-cache-clear":
                account_id = str(submitted.get("account_id", "")).strip()
                account = next((row for row in current_config()["vps_seckill"].get("accounts", []) if row.get("id") == account_id), None)
                if not account:
                    raise ValueError("网站账户不存在")
                removed = _clear_merchant_sessions_for_account(account)
                self._send(200, "application/json; charset=utf-8", json.dumps({"ok": True, "removed": removed, "message": "登录缓存已清除，请下次检查时重新认证"}, ensure_ascii=False).encode("utf-8"))
                return
            if self.path == "/api/seckill/test":
                output = start_seckill_test(submitted)
                self._send(202, "application/json; charset=utf-8", json.dumps(output, ensure_ascii=False).encode("utf-8"))
                return
            if self.path == "/api/seckill/test/stop":
                output = stop_seckill_test(str(submitted.get("task_id", "")))
                self._send(200, "application/json; charset=utf-8", json.dumps(output, ensure_ascii=False).encode("utf-8"))
                return
            if self.path == "/api/seckill/start":
                output = start_seckill(submitted)
                self._send(200, "application/json; charset=utf-8", json.dumps(output, ensure_ascii=False).encode("utf-8"))
                return
            if self.path == "/api/seckill/stop":
                output = stop_seckill(str(submitted.get("task_id", "")))
                self._send(200, "application/json; charset=utf-8", json.dumps(output, ensure_ascii=False).encode("utf-8"))
                return
            if self.path == "/api/egress/probe":
                node_id = str(submitted.get("node_id", "")).strip()
                current = current_config()
                node = next((item for item in current["egress"]["nodes"] if item["id"] == node_id), None)
                if not node:
                    raise ValueError("出口节点不存在")
                try:
                    result = egress.probe(node)
                    node.update(public_ip=result["public_ip"], last_seen=beijing_iso(), last_error="")
                except (OSError, ValueError, requests.RequestException) as exc:
                    node["last_error"] = str(exc)[:240]
                    save_config(current)
                    raise ValueError(f"出口节点检查失败：{exc}") from None
                save_config(current)
                self._send(200, "application/json; charset=utf-8", json.dumps({"ok": True, **result, "last_seen": node["last_seen"], "last_error": ""}, ensure_ascii=False).encode("utf-8")); return
            if self.path == "/api/fetcher/test":
                fetcher = normalize_fetcher(submitted)
                data = json.dumps(flaresolverr_health(fetcher["flaresolverr_url"]), ensure_ascii=False).encode("utf-8")
                self._send(200, "application/json; charset=utf-8", data)
                return
            if self.path == "/api/manage":
                if submitted.get("action") == "backup-import":
                    raise ValueError("请通过“导入备份”上传 tar.gz 文件")
                if submitted.get("action") in {"admin-add", "admin-passwd", "admin-delete"} and users_path() not in {None, DEFAULT_USERS_PATH}:
                    raise ValueError("当前使用自定义账号文件，请使用终端管理该文件中的管理员")
                if submitted.get("action") == "monitor-start":
                    validate_config({**current_config(), "enabled": True})
                result = manager_action(submitted)
                if submitted.get("action") in {"admin-add", "admin-passwd", "admin-delete"}:
                    _clear_admin_sessions()
                if submitted.get("action") == "backup-download":
                    filename = result.get("name", "host-watch-backup.tar.gz")
                    if not isinstance(filename, str) or not re.fullmatch(r"host-watch-[0-9]{8}-[0-9]{6}-[a-z0-9_]{1,64}\.tar\.gz", filename):
                        raise ValueError("备份文件名无效")
                    encoded = result.get("content", "")
                    if not isinstance(encoded, str) or not encoded:
                        raise ValueError("备份内容为空")
                    try:
                        archive = base64.b64decode(encoded, validate=True)
                    except (ValueError, TypeError):
                        raise ValueError("备份内容无效") from None
                    self._send_download(filename, archive)
                    return
                data = json.dumps(result, ensure_ascii=False).encode("utf-8")
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
            submitted_egress = submitted.get("egress", current.get("egress", {"nodes": [], "routes": {}}))
            if not isinstance(submitted_egress, dict):
                raise ValueError("出口节点配置必须是对象")
            current_nodes = {item["id"]: item for item in current.get("egress", {}).get("nodes", [])}
            merged_nodes = []
            for node in submitted_egress.get("nodes", []):
                if not isinstance(node, dict):
                    continue
                node = dict(node)
                node_id = str(node.get("id", "")).strip()
                if not node.get("token") and node_id in current_nodes:
                    node["token"] = current_nodes[node_id].get("token", "")
                existing = current_nodes.get(node_id, {})
                if existing and not existing.get("pending_registration") and node.get("pending_registration"):
                    node.update({key: existing[key] for key in ("proxy_url", "health_url", "public_ip", "last_seen", "pending_registration")})
                merged_nodes.append(node)
            submitted["egress"] = {**submitted_egress, "nodes": merged_nodes}
            if "akile_market" not in submitted:
                submitted["akile_market"] = current["akile_market"]
            if "isvoro_market" not in submitted:
                submitted["isvoro_market"] = current["isvoro_market"]
            submitted_seckill = submitted.get("vps_seckill", current["vps_seckill"])
            if not isinstance(submitted_seckill, dict):
                raise ValueError("VPS 秒杀配置必须是对象")
            current_accounts = {item["id"]: item for item in current["vps_seckill"]["accounts"]}
            merged_accounts = []
            for account in submitted_seckill.get("accounts", []):
                if not isinstance(account, dict):
                    continue
                account = dict(account)
                account_id = str(account.get("id", "")).strip()
                if not account.get("password") and account_id in current_accounts:
                    account["password"] = current_accounts[account_id].get("password", "")
                merged_accounts.append(account)
            submitted_seckill["accounts"] = merged_accounts
            submitted["vps_seckill"] = submitted_seckill
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
            elif isinstance(submitted["fetcher"], dict):
                submitted["fetcher"].setdefault("provider_modes", current["fetcher"].get("provider_modes", {}))
                submitted["fetcher"].setdefault("cloudscraper", current["fetcher"]["cloudscraper"])
                submitted["fetcher"].setdefault("disable_media", current["fetcher"].get("disable_media", True))
                submitted["fetcher"].setdefault("provider_flaresolverr", current["fetcher"].get("provider_flaresolverr", {}))
            if "affiliates" not in submitted:
                submitted["affiliates"] = dict(current["affiliates"])
                if "affiliate_id" in submitted:
                    submitted["affiliates"]["vmiss"] = str(submitted["affiliate_id"]).strip()
            if "affiliate_notes" not in submitted:
                submitted["affiliate_notes"] = dict(current["affiliate_notes"])
            if "affiliate_accounts" not in submitted:
                submitted["affiliate_accounts"] = dict(current.get("affiliate_accounts", {}))
            if "affiliate_rates" not in submitted:
                submitted["affiliate_rates"] = dict(current.get("affiliate_rates", {}))
            if "affiliate_coupons" not in submitted:
                submitted["affiliate_coupons"] = dict(current.get("affiliate_coupons", {}))
            if "merchant_order" not in submitted:
                submitted["merchant_order"] = current["merchant_order"]
            for key in ("watch_order", "notify_order"):
                if key not in submitted:
                    submitted[key] = current[key]
            # Keep the legacy aggregate flag for compatibility with existing
            # configs and status records. Service availability is managed
            # separately below; manual schedules no longer stop the service.
            monitor_schedule = submitted.get("monitor_schedule", {})
            notify_schedule = submitted.get("notify_schedule", {})
            automatic_refresh = (
                isinstance(monitor_schedule, dict) and str(monitor_schedule.get("mode", "")).casefold() == "auto"
            ) or (
                isinstance(notify_schedule, dict) and str(notify_schedule.get("mode", "")).casefold() == "auto"
            )
            has_refreshable_products = any(isinstance(item, dict) and (item.get("watch_enabled") or item.get("notify_enabled") or item.get("enabled")) for item in submitted.get("products", [])) or bool(submitted.get("isvoro_seckill"))
            submitted["enabled"] = bool(automatic_refresh and has_refreshable_products)
            value = validate_config(submitted)
            save_config(value)
            old_accounts = {account["id"]: account for account in current["vps_seckill"]["accounts"]}
            new_accounts = {account["id"]: account for account in value["vps_seckill"]["accounts"]}
            for account_id, old_account in old_accounts.items():
                new_account = new_accounts.get(account_id)
                identity_fields = ("provider", "username", "password", "egress_node_id")
                if new_account is None or any(old_account.get(field) != new_account.get(field) for field in identity_fields):
                    _clear_merchant_sessions_for_account(old_account)
            if value["akile_market"] != current["akile_market"]:
                AKILE_MARKET_WAKE.set()
            if (value["isvoro_market"] != current["isvoro_market"]
                    or value["vps_seckill"]["accounts"] != current["vps_seckill"]["accounts"]):
                ISVORO_MARKET_WAKE.set()
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            status = 502 if isinstance(exc, TelegramAPIError) else (413 if "请求体大小" in str(exc) else 400)
            self._send(status, "application/json; charset=utf-8", json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"))
            return
        try:
            # Automatic saves only need to submit the service reload. Let the
            # monitor restart in the background so a small page edit does not
            # keep the management UI locked until systemd reports completion.
            service_action = {"action": "monitor-apply"}
            if self.headers.get("X-HostWatch-Auto-Save") == "1":
                service_action["background"] = True
            service = manager_action(service_action)
        except ValueError as exc:
            service = {"ok": False, "error": str(exc)}
        self._send(200, "application/json; charset=utf-8", json.dumps({"ok": True, "config": public_config(value), "service": service}, ensure_ascii=False).encode("utf-8"))

    def log_message(self, format: str, *args) -> None:
        print("%s - %s" % (self.address_string(), format % args))


def main() -> None:
    host = os.getenv("ADMIN_HOST", "127.0.0.1")
    if host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("管理页只允许监听 localhost")
    port = int(os.getenv("ADMIN_PORT", "8765"))
    print(f"HostWatch admin listening on http://{host}:{port}")
    restored_sessions = load_merchant_auth_cache()
    if restored_sessions:
        print(f"Restored {restored_sessions} encrypted merchant authentication sessions; each will be revalidated before reuse.")
    start_akile_market_loop()
    start_isvoro_market_loop()
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    if sys.argv[1:] == ["--stock-test-worker"]:
        stock_test_worker()
    elif sys.argv[1:] == ["--browser-session-worker"]:
        browser_session_worker()
    else:
        main()
