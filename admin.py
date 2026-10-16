from __future__ import annotations

import base64
import hmac
import hashlib
import ipaddress
import json
import os
import re
import secrets
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
from affiliates import DEFAULT_AFFILIATES, PROVIDERS, affiliate_purchase_url, normalize_affiliate_notes, normalize_affiliates, normalize_merchant_order
from notifications import purchase_button, stock_message
import egress


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.getenv("VMISS_CONFIG", str(ROOT / "config.json")))
STATUS_PATH = Path(os.getenv("VMISS_STATUS", str(CONFIG_PATH.parent / "status.json")))
RATES_PATH = Path(os.getenv("VMISS_RATES", str(CONFIG_PATH.parent / "exchange-rates.json")))
PAGE_PATH = ROOT / "admin.html"
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
BROWSER_CLEANUP_SECONDS = 8
BROWSER_CLEANUP_WARNING = "本地测试进程已结束，但未能确认 FlareSolverr 浏览器释放；请检查服务日志，必要时重启 FlareSolverr，避免继续启动浏览器测试"
CATALOG = CatalogService(lambda: CONFIG_PATH.parent / "catalog.json", lambda: current_config()["fetcher"])
CLOUDSCRAPER_TEST_ID = "cloudscraper-probe"
CLOUDSCRAPER_TEST_TARGETS = {
    "vmiss": ("VMISS", "https://app.vmiss.com/store"),
    "vps_soy": ("VPS.SOY", "https://www.vps.soy/cart"),
    "akile": ("Akile", "https://akile.ai/shop/server?areaId=3"),
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
                self.output = {**result, "pending": True, "notifying": not self.cancelled.is_set() and not self.product.get("_request_probe"), "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"), "duration_seconds": round(time.monotonic() - self.started, 1)}
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


def check_order_account(session, account: dict) -> dict:
    result = {"status": "unknown", "ok": False, "account_id": account.get("id", ""),
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    if not account.get("login_url") or not account.get("username") or not account.get("password"):
        return {**result, "message": "缺少账户检查接口、用户名或密码，无法确认账户状态"}
    try:
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


def seckill_account_check(submitted: dict) -> dict:
    from monitor import product_fetcher
    current = validate_config(current_config())
    account = next((row for row in current["vps_seckill"]["accounts"] if row["id"] == submitted.get("account_id")), None)
    item = next((row for row in current["vps_seckill"]["items"] if row["id"] == submitted.get("item_id") and row["account_id"] == submitted.get("account_id")), None)
    if not account or not item:
        raise ValueError("请先保存商品与账户绑定，再检查该商品的账户")
    product = next((row for row in current["products"] if row["id"] == item["product_id"]), {})
    fetcher = product_fetcher(current.get("fetcher"), {**product, "egress_node_id": item["egress_node_id"]})
    with requests.Session() as session:
        session.trust_env = False
        egress.apply_session(session, fetcher)
        return {**check_order_account(session, account), "account_id": account["id"], "item_id": item["id"]}


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
        entry = {"at": datetime.now().astimezone().isoformat(timespec="seconds"), "level": level, "message": message}
        if item:
            entry["item_id"] = item.get("id", "")
        with self.lock:
            self.logs.append(entry)
            del self.logs[:-200]
            self.output["logs"] = list(self.logs)

    def snapshot(self) -> dict:
        with self.lock:
            return {**self.output, "logs": list(self.logs), "stopping": self.cancelled.is_set() and not self.done.is_set()}

    def cancel(self) -> None:
        self.cancelled.set()

    def _submit(self, session, item, account, url):
        quantity = item.get("quantity", 1)
        # This is an explicit unpaid-order adapter contract, never a generic checkout POST.
        # The adapter MUST enforce no charging, including saved cards/account credit.
        if self.cancelled.is_set():
            return {"status": "cancelled", "url": url, "quantity": quantity}
        key = hashlib.sha256(f"{self.id}:{item['id']}".encode()).hexdigest()
        payload = {"protocol": ORDER_PROTOCOL, "action": "create_unpaid_order",
            "product_id": item["product_id"], "purchase_url": url, "quantity": quantity,
            "coupon_code": item.get("coupon_code", ""), "payment_mode": "manual",
            "auto_pay": False, "idempotency_key": key}
        try:
            response = session.post(item["submit_url"], json=payload,
                headers={"Idempotency-Key": key}, timeout=(5, 20), allow_redirects=False)
            data = order_json(response)
            if (data.get("protocol") != ORDER_PROTOCOL or data.get("status") != "unpaid"
                    or not isinstance(data.get("order_id"), str) or not data["order_id"].strip()
                    or type(data.get("quantity")) is not int or data["quantity"] != quantity):
                raise ValueError("未确认待付款订单")
        except (requests.RequestException, ValueError):
            # A lost response may still have created an order. Never retry automatically.
            self.log("已发送一次下单请求，但无法确认待付款订单；已停止该商品，勿重复启动，请先到官网核对。未发起付款请求。", "error", item)
            return {"status": "uncertain", "url": url, "quantity": quantity}
        self.log(f"已创建待付款订单，数量 {quantity}；请手动前往官网核对订单并支付。", "success", item)
        # Do not expose or visit arbitrary response links, and never fetch payment_url.
        return {"status": "submitted", "order_id": data["order_id"][:128], "url": url, "quantity": quantity, "payment_status": "unpaid"}

    @staticmethod
    def enough_stock(result, quantity):
        return (type(result.available) is int and result.available > 0
            and (result.stock_mode == "availability" or result.available >= quantity))

    def run(self) -> None:
        from contextlib import ExitStack
        from monitor import inspect_product, product_fetcher
        products = {str(item.get("id")): item for item in self.config.get("products", [])}
        seckill = self.config.get("vps_seckill", {})
        accounts = {str(item.get("id")): item for item in seckill.get("accounts", [])}
        items = [item for item in seckill.get("items", []) if item.get("enabled", True)]
        finished, contexts = {}, {}
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
                        stock_session = stack.enter_context(requests.Session())
                        order_session = stack.enter_context(requests.Session())
                        for session in (stock_session, order_session):
                            session.trust_env = False
                            session.headers["User-Agent"] = "HostWatch/1.0 (VPS seckill; unpaid orders only)"
                            egress.apply_session(session, fetcher)
                        account = accounts.get(item.get("account_id"))
                        auto = self.mode != "manual" and item.get("auto_submit") and account and account.get("auto_submit") and item.get("submit_url")
                        if auto:
                            if not account.get("login_url") or urlsplit(account["login_url"]).netloc != urlsplit(item["submit_url"]).netloc:
                                raise ValueError("账户检查接口与待付款下单接口必须同源，以复用认证会话")
                            capability = order_json(order_session.get(item["submit_url"], timeout=(5, 20), allow_redirects=False))
                            if capability.get("protocol") != ORDER_PROTOCOL or capability.get("order_only") is not True or capability.get("automatic_payment") is not False:
                                raise ValueError("接口未明确支持仅创建待付款订单；普通结算网页不能自动提交")
                        if account:
                            checked = check_order_account(order_session, account)
                            self.log(f"账户 {account['name']}：{checked['message']}", "info" if checked["ok"] else "error", item)
                            with self.lock:
                                self.output.setdefault("accounts", {})[item["id"]] = checked
                            if auto and not checked["ok"]:
                                raise ValueError("账户未通过验证，已停止该商品自动提交")
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
                                self.log("商品已移出配置，保留购买链接；请从高级设置重新添加商品以恢复库存检查。", "ready", item)
                                finished[item["id"]] = {"status": "ready", "url": item.get("target_url", ""), "quantity": quantity}
                                continue
                            result = inspect_product(stock_session, routed, fetcher)
                            url = result.url or item.get("target_url", "")
                            available = self.enough_stock(result, quantity)
                            self.log(f"第 {round_no} 轮：{item.get('label') or item['product_id']} · 需求 {quantity} 台 · {'可下单' if available else '缺货/库存不足/未知'}", "info", item)
                            if not available:
                                continue
                            if result.stock_mode == "availability":
                                self.log("商家仅提供有货状态，数量由下单接口最终核验；不会自动减少下单数量。", "info", item)
                            if auto and self.mode == "precise":
                                checked = check_order_account(order_session, account)
                                with self.lock:
                                    self.output.setdefault("accounts", {})[item["id"]] = checked
                                if not checked["ok"]:
                                    finished[item["id"]] = {"status": "blocked", "url": url, "quantity": quantity}
                                    self.log("提交前账户复核失败，已停止该商品，请到官网检查。", "error", item)
                                    continue
                                if self.cancelled.is_set():
                                    break
                                result = inspect_product(stock_session, routed, fetcher)
                                if not self.enough_stock(result, quantity):
                                    self.log("提交前第二次库存检查不足，继续等待，不发送订单。", "info", item)
                                    continue
                                url = result.url or url
                            if auto:
                                finished[item["id"]] = self._submit(order_session, item, account, url)
                            else:
                                finished[item["id"]] = {"status": "ready", "url": url, "quantity": quantity}
                                self.log(f"已准备购买链接（数量 {quantity}）：{url}；请到官网手动下单和付款。", "ready", item)
                        except Exception:
                            self.log(f"第 {round_no} 轮库存检查失败，未发送下单请求；请检查商品和出口配置。", "error", item)
                    with self.lock:
                        self.output["items"] = [{"item_id": key, **value} for key, value in finished.items()]
                        self.output["round"] = round_no
                    if self.mode == "manual" or len(finished) == len(items):
                        break
                    self.cancelled.wait(seckill.get("interval_seconds", 5))
        finally:
            with self.lock:
                self.output.update(items=[{"item_id": key, **value} for key, value in finished.items()], pending=False,
                    cancelled=self.cancelled.is_set(), checked_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                    duration_seconds=round(time.monotonic() - self.started, 1))
                self.done.set()
            self.log("秒杀任务已停止" if self.output.get("cancelled") else "秒杀任务已完成；如有待付款订单，请到官网手动支付")

    def mode_label(self):
        return {"rapid": "极速秒杀（自动）", "precise": "精准秒杀（自动）", "manual": "手动秒杀"}.get(self.mode, self.mode)


def start_seckill(submitted: dict) -> dict:
    task_id = str(submitted.get("task_id", ""))
    if not re.fullmatch(r"[a-zA-Z0-9-]{16,64}", task_id):
        raise ValueError("秒杀任务编号无效")
    current = validate_config(current_config())
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


def default_config() -> dict:
    return {
        "enabled": False,
        "interval_seconds": 600,
        "notify_current_stock_on_start": False,
        "isvoro_seckill": False,
        "affiliate_id": DEFAULT_AFFILIATES["vmiss"],
        "affiliates": dict(DEFAULT_AFFILIATES),
        "affiliate_notes": {provider: "" for provider in PROVIDERS},
        "merchant_order": list(PROVIDERS),
        "watch_order": [],
        "notify_order": [],
        "fetcher": {
            "mode": "requests",
            "flaresolverr_url": "http://127.0.0.1:8191/v1",
            "timeout_seconds": 60,
            "wait_seconds": 0,
            "cloudscraper": normalize_cloudscraper(None),
        },
        "telegram": {"bot_token": "", "chat_id": "", "disabled": False},
        "egress": {"nodes": [], "routes": {}},
        "vps_seckill": {"enabled": False, "mode": "manual", "interval_seconds": 5, "items": [], "accounts": []},
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
    if not isinstance(provider_modes, dict) or any(key not in (*PROVIDERS, "html") or not isinstance(mode, str) or mode not in {"inherit", "requests", "flaresolverr", "cloudscraper"} for key, mode in provider_modes.items()):
        raise ValueError("商家请求方式必须是支持商家的 inherit、requests、flaresolverr 或 cloudscraper")
    return {
        "mode": mode,
        "flaresolverr_url": endpoint.rstrip("/"),
        "timeout_seconds": timeout_seconds,
        "wait_seconds": wait,
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
        if account_id and account_id not in account_ids:
            raise ValueError(f"VPS 秒杀商品 {item_id} 绑定的账户不存在")
        quantity = item.get("quantity", 1)
        if type(quantity) is not int or not 1 <= quantity <= 100:
            raise ValueError(f"VPS 秒杀商品 {item_id} 的下单数量必须是 1 到 100 的整数")
        item_ids.add(item_id)
        normalized_items.append({"id": item_id, "product_id": product_id, "coupon_code": coupon[:256],
            "target_url": target_url, "submit_url": submit_url, "account_id": account_id,
            "egress_node_id": egress_node_id[:64], "quantity": quantity,
            "enabled": bool(item.get("enabled", True)), "auto_submit": bool(item.get("auto_submit", False)),
            "label": str(item.get("label", ""))[:160]})
    return {"enabled": enabled, "mode": mode, "interval_seconds": interval,
        "items": normalized_items, "accounts": normalized_accounts}


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
    affiliate_notes = normalize_affiliate_notes(value)
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
        provider = str(item.get("provider", {"vmiss": "VMISS", "vps_soy": "VPS.SOY", "akile": "Akile", "isvoro": "Isvoro", "dmit": "DMIT", "bwh": "搬瓦工"}.get(adapter, "自定义网站"))).strip()
        gid = str(item.get("gid", "")).strip()
        pid = str(item.get("pid", "")).strip()
        if "enabled" in item and not isinstance(item["enabled"], bool):
            raise ValueError(f"产品 {name or product_id} 的启用状态必须是布尔值")
        if not product_id or product_id in seen_ids:
            raise ValueError("每个产品都需要唯一 ID")
        if adapter not in {*PROVIDERS, "html"}:
            raise ValueError(f"产品 {name or product_id} 的适配器必须是 vmiss、vps_soy、akile、isvoro、dmit、bwh 或 html")
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
    egress_config = egress.normalize(value.get("egress", {}))
    vps_seckill = normalize_seckill(value.get("vps_seckill", {}), seen_ids)
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
        "affiliate_notes": affiliate_notes,
        "merchant_order": merchant_order,
        **product_orders,
        "fetcher": {**fetcher, "egress": egress_config},
        "telegram": telegram,
        "egress": egress_config,
        "vps_seckill": vps_seckill,
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
        "last_seen": datetime.now().astimezone().isoformat(timespec="seconds"), "last_error": ""}
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
            message = "当前访问域名未加入管理页允许列表。反向代理已连接到 HostWatch，请在服务器终端运行 hw admin-domain 你的管理页域名，然后使用管理员账号登录。"
            if self.path.startswith("/api/"):
                self._send(403, "application/json; charset=utf-8", json.dumps({"error": message, "code": "admin_host_not_allowed"}, ensure_ascii=False).encode("utf-8"))
            else:
                body = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                        '<title>管理页域名尚未配置 · HostWatch</title><style>body{font:16px/1.7 system-ui,sans-serif;background:#f6f8fc;color:#24334b;margin:0;padding:8vh 20px}'
                        'main{max-width:640px;margin:auto;background:white;padding:28px;border:1px solid #dfe5ef;border-radius:16px}h1{font-size:24px}'
                        'pre{background:#f0f3f8;padding:14px;white-space:pre-wrap;overflow-wrap:anywhere;border-radius:8px}</style>'
                        '<main><p>HostWatch · HTTP 403</p><h1>管理页访问域名尚未配置</h1><p>' + message + '</p>'
                        '<pre>hw admin-domain 你的管理页域名</pre><p>此命令会放行指定域名并重启管理页。尚无管理员时会生成 admin 和随机密码；已有账号保持不变。</p>'
                        '<p>反向代理后端保持 http://127.0.0.1:8765。仅重启服务不会自动放行新域名。</p></main></html>')
                self._send(403, "text/html; charset=utf-8", body.encode("utf-8"))
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

    def _send_download(self, filename: str, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/gzip")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
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
        elif self.path == "/api/egress":
            try:
                data = json.dumps(public_config(json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else default_config()).get("egress", {"nodes": [], "routes": {}}), ensure_ascii=False).encode("utf-8")
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self._send(500, "application/json; charset=utf-8", json.dumps({"error": f"出口节点配置无法读取：{exc}"}, ensure_ascii=False).encode("utf-8")); return
            self._send(200, "application/json; charset=utf-8", data)
        elif self.path == "/api/rates":
            self._send(200, "application/json; charset=utf-8", json.dumps(exchange_rates(), ensure_ascii=False).encode("utf-8"))
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
        elif self.path.startswith("/api/seckill/status/"):
            task_id = self.path.rsplit("/", 1)[-1]
            with SECKILL_LOCK:
                task = SECKILL_TASKS.get(task_id)
            if task is None:
                self._send(404, "application/json; charset=utf-8", b'{"error":"Seckill task not found"}')
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
        if self.path in {"/api/products/test", "/api/products/test/stop", "/api/catalog/refresh", "/api/catalog/stop", "/api/seckill/start", "/api/seckill/stop", "/api/seckill/account-check", "/api/egress/probe"}:
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
        if self.path not in {"/api/config", "/api/telegram/test", "/api/manage", "/api/manage/backup-import", "/api/fetcher/test", "/api/products/test", "/api/products/test/stop", "/api/catalog/refresh", "/api/catalog/stop", "/api/seckill/start", "/api/seckill/stop", "/api/seckill/account-check", "/api/egress/probe", "/api/egress/install-command", "/api/egress/import"}:
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
                            or (CATALOG.task and CATALOG.task.get("pending"))):
                        raise ValueError("请先停止或等待商品测试、秒杀任务和大盘刷新完成，再导入备份")
                    raw = self.rfile.read(length)
                    if len(raw) != length or not raw.startswith(b"\x1f\x8b"):
                        raise ValueError("请上传有效的 tar.gz 备份文件")
                    result = manager_action({"action": "backup-import", "content": base64.b64encode(raw).decode("ascii")})
                self._send(200, "application/json; charset=utf-8", json.dumps(result, ensure_ascii=False).encode("utf-8"))
                return
            submitted = request_body(self)
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
            if self.path == "/api/seckill/account-check":
                output = seckill_account_check(submitted)
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
                    node.update(public_ip=result["public_ip"], last_seen=datetime.now().astimezone().isoformat(timespec="seconds"), last_error="")
                except (OSError, ValueError, requests.RequestException) as exc:
                    node["last_error"] = str(exc)[:240]
                    raise ValueError(f"出口节点检查失败：{exc}") from None
                save_config(current)
                self._send(200, "application/json; charset=utf-8", json.dumps({"ok": True, **result}, ensure_ascii=False).encode("utf-8")); return
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
            if "affiliates" not in submitted:
                submitted["affiliates"] = dict(current["affiliates"])
                if "affiliate_id" in submitted:
                    submitted["affiliates"]["vmiss"] = str(submitted["affiliate_id"]).strip()
            if "affiliate_notes" not in submitted:
                submitted["affiliate_notes"] = dict(current["affiliate_notes"])
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
