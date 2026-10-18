from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from admin import default_config, effective_telegram, normalize_cloudscraper, normalize_vps_soy_url, validate_config
from affiliates import affiliate_purchase_url
from notifications import purchase_button, stock_message
import egress
from time_utils import beijing_iso, beijing_logging_converter, now_beijing


ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state.json"
CONFIG_PATH = ROOT / "config.json"
STATUS_PATH = ROOT / "status.json"
_BROWSER_THREAD_LOCK = threading.Lock()
_INVENTORY_CACHE_THREAD_LOCK = threading.Lock()
INVENTORY_CACHE_MAX_AGE_SECONDS = 28


class MonitorError(RuntimeError):
    """A page or notification response could not be trusted."""


class ProductUnlisted(MonitorError):
    """A valid directory no longer includes the selected product; keep waiting."""


class SiteBlocked(MonitorError):
    """The site denied automated access; stop instead of retrying."""


class RequestServiceError(MonitorError):
    """Browser service unavailable; stop until its configuration is fixed."""


class TelegramMessageUnavailable(MonitorError):
    """A previous bot message was deleted or can no longer be edited."""


@dataclass(frozen=True)
class Product:
    key: str
    name: str
    available: int
    url: str
    card_id: str = ""
    vendor: str = "VMISS"
    stock_mode: str = "quantity"
    area_id: str = ""
    area_name: str = ""
    node_name: str = ""
    catalog_url: str = ""
    match_details: dict | None = None
    region: str = ""
    specs: str = ""
    traffic: str = ""
    price: str = ""


def html_metadata(card, region="") -> dict:
    """Keep hardware and network fields separate without guessing missing values."""
    nodes = card.select(".package-content li, .card-body .card-text")
    if not nodes:
        nodes = card.select(".package-content")
    hardware, network = [], []
    for node in nodes:
        value = " ".join(node.get_text(" ", strip=True).split())
        if re.match(r"^库存\s*[:：]", value):
            continue
        fields = re.split(r"\s*(?=(?:CPU|内存|硬盘|带宽|流量|端口|架构|网络|Memory|RAM|Storage|Disk|Traffic|Bandwidth)\s*[:：])", value, flags=re.I)
        for field in filter(None, fields):
            (network if re.search(r"流量|带宽|traffic|bandwidth|[GM]bps", field, re.I) else hardware).append(field)
    prices = card.select(".price-amount, .price-cycle")
    soy_price = card.select_one(".cart-num")
    price_parts = []
    seen_prices = set()
    for node in prices:
        value = " ".join(node.get_text(" ", strip=True).split())
        if value and value not in seen_prices:
            price_parts.append(value)
            seen_prices.add(value)
    price = " / ".join(price_parts)
    if not price and soy_price:
        price = soy_price.parent.get_text(" ", strip=True)
    return {"region": region[:500], "specs": " · ".join(hardware)[:500], "traffic": " · ".join(network)[:500], "price": price[:500]}


VMISS_COUNTRY_NAMES = {
    "cn": "中国", "china": "中国", "hk": "中国香港", "mo": "中国澳门",
    "tw": "中国台湾", "jp": "日本", "kr": "韩国", "sg": "新加坡",
    "us": "美国", "ca": "加拿大", "gb": "英国", "uk": "英国",
    "de": "德国", "fr": "法国", "nl": "荷兰", "au": "澳大利亚",
}
VMISS_CITY_NAMES = {
    "hongkong": "香港", "hong kong": "香港", "macau": "澳门", "macao": "澳门",
    "taipei": "台北", "tokyo": "东京", "osaka": "大阪", "seoul": "首尔",
    "singapore": "新加坡", "losangeles": "洛杉矶", "los angeles": "洛杉矶",
    "newyork": "纽约", "new york": "纽约", "vancouver": "温哥华",
    "toronto": "多伦多", "london": "伦敦", "frankfurt": "法兰克福",
    "amsterdam": "阿姆斯特丹", "sydney": "悉尼", "melbourne": "墨尔本",
}


def normalize_vmiss_region(value: object) -> str:
    """Turn VMISS's country-city-line title into a compact Chinese label."""
    raw = " ".join(str(value or "").split())
    if not raw:
        return ""
    parts = [part.strip() for part in re.split(r"\s*-\s*", raw) if part.strip()]
    if len(parts) < 2:
        key = re.sub(r"[\s_-]+", " ", raw).casefold()
        return VMISS_CITY_NAMES.get(key, raw)
    country_key = re.sub(r"[\s_-]+", " ", parts[0]).casefold()
    city_key = re.sub(r"[\s_-]+", " ", parts[1]).casefold()
    country = VMISS_COUNTRY_NAMES.get(country_key, parts[0])
    city = VMISS_CITY_NAMES.get(city_key, parts[1])
    if country in {"中国香港", "中国澳门", "中国台湾"} and city in {"香港", "澳门", "台北"}:
        label = country
    elif city == country:
        label = country
    else:
        label = country + city
    return " · ".join([label, *parts[2:]])


def vmiss_metadata(card, region="") -> dict:
    """Separate VMISS's unlabelled inline resources before classifying them."""
    metadata = html_metadata(card, normalize_vmiss_region(region))
    raw = " · ".join(node.get_text(" ", strip=True) for node in card.select(".package-content"))
    fields = re.split(r"\s*·\s*|\s+(?=\d+(?:\.\d+)?\s*(?:v?CPU|cores?|[KMGT]i?B|[KMGT]?bps|IPv[46])\b)", raw, flags=re.I)
    hardware, network = [], []
    for field in fields:
        value = " ".join(field.split()).strip(" ·")
        if value:
            target = network if re.search(r"traffic|bandwidth|[KMGT]?bps|流量|带宽", value, re.I) else hardware
            if value not in target:
                target.append(value)
    return {**metadata, "specs": " · ".join(hardware)[:500], "traffic": " · ".join(network)[:500]}


def vmiss_order_element(card):
    """Accept the Lagom link and the button/id variants used by VMISS pages."""
    return card.select_one(
        "a.btn-order-now[href], "
        "a[data-href], a[data-url], button.btn-order-now, button[id$='-order-button']"
    )


def vmiss_order_href(card, page_url: str, order_el) -> str:
    if order_el is None:
        return ""
    href = str(order_el.get("href") or order_el.get("data-href") or order_el.get("data-url") or "").strip()
    if not href:
        onclick = str(order_el.get("onclick", ""))
        match = re.search(r"(?:location(?:\.href)?|window\.open)\s*\(?\s*['\"]([^'\"]+)", onclick, re.I)
        href = match.group(1).strip() if match else ""
    return href


def soy_metadata(card, region="") -> dict:
    """Read labelled configuration values; omit port counts and store notices."""
    metadata = html_metadata(card, region)
    raw = " ".join(node.get_text(" ", strip=True) for node in card.select(".card-body .card-text"))
    hardware, network = [], []
    fields = [
        (hardware, "CPU", r"CPU|vCPU|处理器", r"(\d+(?:\.\d+)?)\s*(?:核|cores?|vCPU)?", "核"),
        (hardware, "内存", r"内存|Memory|RAM", r"(\d+(?:\.\d+)?)\s*(Mi?B|Gi?B|M|G)\b", ""),
        (hardware, "硬盘", r"硬盘|磁盘|Storage|Disk", r"(\d+(?:\.\d+)?)\s*([KMGT]i?B)\b", ""),
        (network, "流量", r"流量|Traffic", r"(\d+(?:\.\d+)?)\s*([KMGT]i?B)\b|无限制|不限量|不限流量|unlimited", ""),
        (network, "带宽", r"带宽|Bandwidth", r"(\d+(?:\.\d+)?)\s*([KMGT]?bps|[KMGT]?B/s)\b", ""),
    ]
    for target, label, aliases, value, unit in fields:
        match = re.search(r"(?:^|[\s·/])(?:" + aliases + r")\s*[:：]\s*(" + value + r")", raw, re.I)
        if match:
            result = " ".join(match[1].split())
            if label == "CPU":
                result = match[2] + " " + unit
            elif label == "内存":
                result = match[2] + " " + {"m": "MB", "g": "GB"}.get(match[3].lower(), match[3])
            target.append(label + "：" + result)
    return {**metadata, "specs": " · ".join(hardware), "traffic": " · ".join(network)}



def parse_products(html: str, page_url: str, selectors: list[str]) -> list[Product]:
    """Read WHMCS Lagom product cards; reject incomplete/ambiguous pages."""
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(".section.products .package")
    if not cards:
        raise MonitorError("页面中没有找到商品卡片，页面结构可能已变化")

    products: list[Product] = []
    matched_selectors: set[str] = set()
    page_host = urlsplit(page_url).hostname
    for card in cards:
        title_el = card.select_one(".package-title, [class*='package-title']")
        qty_el = card.select_one(".package-qty")
        order_el = vmiss_order_element(card)
        if not title_el or not order_el:
            raise MonitorError("有商品卡片缺少名称或订购链接；本轮不更新状态")

        name = " ".join(title_el.get_text(" ", strip=True).split())
        if not name:
            raise MonitorError("有商品卡片名称为空；本轮不更新状态")
        disabled = ("disabled" in order_el.get("class", []) or order_el.has_attr("disabled")
                    or str(order_el.get("aria-disabled", "")).casefold() == "true")
        # VMISS omits package-qty for plans without a published stock limit.
        # Only use the button state when the quantity element is absent;
        # malformed explicit inventory must remain unknown.
        stock_mode = "quantity"
        if qty_el is not None:
            qty_text = " ".join(qty_el.get_text(" ", strip=True).split())
            match = re.fullmatch(r"(\d{1,3}(?:,\d{3})+|\d+)\s+Available", qty_text, flags=re.IGNORECASE)
            if match:
                available = int(match.group(1).replace(",", ""))
                if available > 0 and disabled:
                    raise MonitorError(f"套餐 {name} 的库存数量与订购按钮状态不一致；本轮不更新状态")
            elif re.fullmatch(r"(?:in\s+stock|available|有货|现货)", qty_text, re.I):
                available, stock_mode = 1, "availability"
            elif re.fullmatch(r"(?:out\s+of\s+stock|unavailable|sold\s+out|缺货|售罄|无货)", qty_text, re.I):
                available = 0
            else:
                raise MonitorError(f"无法识别套餐 {name} 的库存文本：{qty_text!r}")
        else:
            available, stock_mode = (0 if disabled else 1), "availability"

        href = vmiss_order_href(card, page_url, order_el)
        if not href or href.startswith(("#", "?")):
            raise MonitorError(f"套餐 {name} 缺少有效订购链接；本轮不更新状态")
        product_url = urljoin(page_url, href)
        parsed_product_url = urlsplit(product_url)
        if (parsed_product_url.scheme != "https" or parsed_product_url.hostname != page_host
                or parsed_product_url.username or parsed_product_url.password):
            raise MonitorError(f"套餐 {name} 的购买链接不在本站；拒绝将 aff 参数附加到外站链接")
        if parsed_product_url.path in {"", "/", urlsplit(page_url).path.rstrip("/")} or parsed_product_url.fragment:
            raise MonitorError(f"套餐 {name} 缺少有效订购链接；本轮不更新状态")
        # The title can repeat for variants, so use its order URL as the stable key.
        key = parsed_product_url.path
        product_id = (card.get("id") or "").strip()
        slug = parsed_product_url.path.rstrip("/").rsplit("/", 1)[-1]
        identifiers = {key.casefold(), slug.casefold(), product_id.casefold(), name.casefold()}
        matches = identifiers.intersection(selector.casefold() for selector in selectors)
        matched_selectors.update(matches)
        if selectors and not matches:
            continue
        products.append(Product(key, name, available, product_url, product_id, stock_mode=stock_mode, **vmiss_metadata(card, soup.select_one("h1").get_text(" ", strip=True) if soup.select_one("h1") else "")))

    if not products:
        raise MonitorError("页面已读取，但没有商品符合 WATCH_PRODUCTS")
    missing = {selector for selector in selectors if selector.casefold() not in matched_selectors}
    if missing:
        raise MonitorError("WATCH_PRODUCTS 中的标识未在页面找到：" + ", ".join(sorted(missing)))
    if len({p.key for p in products}) != len(products):
        raise MonitorError("商品链接重复，无法可靠地区分套餐")
    return products


def _blocked_body(body: str) -> bool:
    blocked_markers = (
        "attention required! | cloudflare",
        "sorry, you have been blocked",
        "enable javascript and cookies to continue",
        "performing security verification",
        "verify you are human",
        "turnstile",
        '<title>just a moment',
    )
    lowered = body.casefold()
    return any(marker in lowered for marker in blocked_markers)


def _validate_page_response(status_code: int, body: str, final_url: str, expected_host: str = "app.vmiss.com", headers: dict | None = None) -> tuple[str, str]:
    headers = {str(key).casefold(): str(value) for key, value in (headers or {}).items()}
    ray = headers.get("cf-ray", "")
    detail = f"；Cloudflare Ray ID: {ray}" if re.fullmatch(r"[a-zA-Z0-9-]{1,100}", ray) else ""
    if status_code == 429:
        raise SiteBlocked(f"{expected_host} 返回 HTTP 429（请求限流）；请降低访问频率并检查站点限流规则{detail}")
    if headers.get("cf-mitigated", "").casefold() == "challenge" or _blocked_body(body):
        raise SiteBlocked(f"{expected_host} 返回安全验证页（HTTP {status_code}）；请检查 Cloudflare 安全事件和监控访问规则{detail}")
    if status_code == 403:
        raise SiteBlocked(f"{expected_host} 返回 HTTP 403（拒绝访问）；请核对站点或源站访问规则{detail}")
    if status_code < 200 or status_code >= 300:
        raise MonitorError(f"{expected_host} 商品页返回 HTTP {status_code}，本轮监控已停止")
    parsed_url = urlsplit(final_url)
    if parsed_url.scheme != "https" or (parsed_url.hostname or "").casefold() != expected_host.casefold():
        raise MonitorError(f"商品页重定向离开 {expected_host}；已停止，未处理页面内容")
    return body, final_url


def fetch_with_requests(session: requests.Session, url: str, expected_host: str = "app.vmiss.com") -> tuple[str, str]:
    try:
        response = session.get(url, timeout=(10, 25), allow_redirects=True)
    except requests.exceptions.ProxyError as exc:
        raise MonitorError(f"通过出口节点访问 {expected_host} 失败：{egress.proxy_error(exc)}") from None
    except requests.RequestException:
        raise MonitorError(f"访问 {expected_host} 商品页失败；请检查 DNS、TLS 和服务器网络") from None
    return _validate_page_response(response.status_code, response.text, response.url, expected_host, getattr(response, "headers", {}))


def fetch_with_cloudscraper(url: str, expected_host: str, fetcher: dict | None = None, diagnostics: dict | None = None) -> tuple[str, str]:
    """Keep challenge cookies and browser headers out of the shared TG session."""
    try:
        options = normalize_cloudscraper((fetcher or {}).get("cloudscraper"))
        import cloudscraper
        from cloudscraper.exceptions import CloudflareException
    except ImportError:
        raise RequestServiceError("cloudscraper 依赖未安装或不完整，请通过“更新 HostWatch”安装依赖后重试") from None
    def request(scraper):
        egress.apply_session(scraper, fetcher)
        response = scraper.get(url, timeout=(options["connect_timeout_seconds"], options["read_timeout_seconds"]), allow_redirects=True)
        result = _validate_page_response(response.status_code, response.text, response.url, expected_host, getattr(response, "headers", {}))
        if diagnostics is not None:
            diagnostics.update(status_code=response.status_code, response_bytes=len(response.content))
        return result

    try:
        # The published release's native interpreter needs no browser or Node.
        # Bound challenge submissions; never opt into external CAPTCHA services.
        # A catalogue worker may provide one short-lived scraper so a clearance
        # cookie survives the discovery page -> selected category request.
        shared = (fetcher or {}).get("_cloudscraper_session")
        if shared is not None:
            return request(shared)
        with cloudscraper.create_scraper(interpreter="native", delay=options["challenge_delay_seconds"], solveDepth=1, doubleDown=False, debug=False) as scraper:
            return request(scraper)
    except CloudflareException:
        raise SiteBlocked(f"{expected_host} 的 Cloudflare 验证未通过或不受 cloudscraper 支持；请检查站点访问规则或调整请求方式") from None
    except requests.exceptions.ProxyError as exc:
        raise MonitorError(f"cloudscraper 通过出口节点访问 {expected_host} 失败：{egress.proxy_error(exc)}") from None
    except requests.Timeout:
        raise MonitorError(f"cloudscraper 访问 {expected_host} 超时，请检查服务器网络或目标站点响应") from None
    except requests.RequestException:
        raise MonitorError(f"cloudscraper 访问 {expected_host} 失败，请检查 DNS、TLS 和服务器网络") from None
    except MonitorError:
        raise
    except (ImportError, RuntimeError):
        raise RequestServiceError("cloudscraper 初始化或解释器运行失败，请更新依赖并检查服务日志") from None


@contextmanager
def cloudscraper_request_session(fetcher: dict | None = None):
    """Reuse one bounded cloudscraper session for a multi-page catalogue read."""
    value = dict(fetcher or {})
    if value.get("mode") != "cloudscraper":
        yield value
        return
    try:
        options = normalize_cloudscraper(value.get("cloudscraper"))
        import cloudscraper
    except ImportError:
        raise RequestServiceError("cloudscraper 依赖未安装或不完整，请通过“更新 HostWatch”安装依赖后重试") from None
    try:
        scraper = cloudscraper.create_scraper(interpreter="native", delay=options["challenge_delay_seconds"], solveDepth=1, doubleDown=False, debug=False)
    except (ImportError, RuntimeError):
        raise RequestServiceError("cloudscraper 初始化失败，请更新依赖并检查服务日志") from None
    value["_cloudscraper_session"] = scraper
    try:
        yield value
    finally:
        scraper.close()


@contextmanager
def browser_request_slot(wait: bool = False):
    """Serialize FlareSolverr requests across monitor/admin processes.

    Automatic stock checks run in the monitor service while VPS seckill runs in
    the admin service.  FlareSolverr commonly uses one Chromium profile, so
    requests must share one cross-process queue.  Keep the same lock on Linux
    and Windows; the previous Windows no-op allowed both services to launch
    Chromium at the same time and produced browser-occupied errors.
    """
    lock_path = Path(os.getenv("VMISS_CONFIG", str(CONFIG_PATH))).parent / ".browser-request.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    thread_acquired = _BROWSER_THREAD_LOCK.acquire(blocking=wait)
    if not thread_acquired:
        raise RequestServiceError("另一个浏览器请求正在执行，请等待完成或停止测试后重试") from None
    lock = None
    try:
        lock = lock_path.open("a+b")
        if sys.platform == "win32":
            import msvcrt
            lock.seek(0)
            if lock.tell() == 0:
                lock.write(b"0")
                lock.flush()
            while True:
                try:
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if not wait:
                        raise RequestServiceError("另一个浏览器请求正在执行，请等待完成或停止测试后重试") from None
                    time.sleep(0.2)
        else:
            import fcntl
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
            except BlockingIOError:
                raise RequestServiceError("另一个浏览器请求正在执行，请等待完成或停止测试后重试") from None
        try:
            yield
        finally:
            if sys.platform == "win32":
                import msvcrt
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_UN)
    finally:
        if lock is not None:
            lock.close()
        _BROWSER_THREAD_LOCK.release()


def check_browser_memory(fetcher: dict) -> None:
    if urlsplit(fetcher["flaresolverr_url"]).hostname not in {"localhost", "127.0.0.1", "::1"}:
        return
    try:
        info = Path("/proc/meminfo").read_text()
    except OSError:
        return
    match = re.search(r"^MemAvailable:\s+(\d+)\s+kB", info, re.MULTILINE)
    if match and int(match.group(1)) < 256 * 1024:
        raise RequestServiceError("服务器可用内存不足 256 MiB，已取消启动浏览器；请释放内存或使用普通 HTTPS 请求")


def fetch_with_flaresolverr(session: requests.Session, url: str, fetcher: dict, expected_host: str = "app.vmiss.com") -> tuple[str, str]:
    if fetcher.get("_browser_slot_held"):
        return _fetch_with_flaresolverr(session, url, fetcher, expected_host)
    with browser_request_slot(wait=True):
        check_browser_memory(fetcher)
        return _fetch_with_flaresolverr(session, url, fetcher, expected_host)


def _copy_browser_state(session: requests.Session, solution: dict, expected_host: str) -> None:
    """Copy only same-site browser state into the dedicated follow-up session."""
    user_agent = str(solution.get("userAgent", "")).strip()
    if user_agent and len(user_agent) <= 512 and "\r" not in user_agent and "\n" not in user_agent:
        session.headers["User-Agent"] = user_agent
    cookies = solution.get("cookies", [])
    if isinstance(cookies, dict):
        cookies = [{"name": name, "value": value} for name, value in cookies.items()]
    if not isinstance(cookies, list):
        return
    host = str(expected_host or "").casefold().lstrip(".")
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name", ""))
        value = str(cookie.get("value", ""))
        domain = str(cookie.get("domain", "")).strip().casefold().lstrip(".") or host
        path = str(cookie.get("path", "/"))
        if (not name or len(name) > 256 or len(value) > 4096 or "\r" in name or "\n" in name
                or "\r" in value or "\n" in value or not path.startswith("/")
                or not host or not (host == domain or host.endswith("." + domain))):
            continue
        session.cookies.set(name, value, domain=domain, path=path)


def _fetch_with_flaresolverr(session: requests.Session, url: str, fetcher: dict, expected_host: str) -> tuple[str, str]:
    endpoint = str(fetcher["flaresolverr_url"])
    timeout_seconds = int(fetcher["timeout_seconds"])
    wait = int(fetcher.get("wait_seconds", 0))
    request = {"cmd": "request.get", "url": url, "maxTimeout": timeout_seconds * 1000}
    if fetcher.get("_test_browser_session"):
        request["session"] = fetcher["_test_browser_session"]
    if wait:
        request["waitInSeconds"] = wait
    if "disable_media" in fetcher:
        request["disableMedia"] = fetcher["disable_media"]
    if fetcher.get("egress_proxy_url"):
        request["proxy"] = egress.browser_proxy(fetcher)
    request_kwargs = {"json": request, "timeout": (10, timeout_seconds + wait + 15)}
    if fetcher.get("egress_proxy_url"):
        # The API is a control connection, not a request to the merchant.
        # Empty mappings still inherit Session.proxies in requests.
        request_kwargs["proxies"] = {"http": "", "https": ""}
    browser_busy = False
    for busy_attempt in range(3):
        try:
            response = session.post(endpoint, **request_kwargs)
            payload = response.json()
        except requests.Timeout:
            raise RequestServiceError("FlareSolverr 请求超时；请检查服务器资源、目标站点验证和超时设置") from None
        except requests.RequestException:
            raise RequestServiceError("无法连接 FlareSolverr；请在网页测试服务中检查连接，或查看容器日志") from None
        except ValueError:
            raise RequestServiceError("FlareSolverr 返回非 JSON 响应；请核对 API 地址是否以 /v1 结尾") from None
        if isinstance(payload, dict) and payload.get("status") != "ok":
            busy_message = str(payload.get("message", "")).casefold()
            busy_markers = ("browser is already", "already running", "browser busy", "occupied", "in use", "正在使用", "已占用")
            browser_busy = any(marker in busy_message for marker in busy_markers)
            if browser_busy and busy_attempt < 2:
                time.sleep(0.5 * (busy_attempt + 1))
                continue
        break
    if not isinstance(payload, dict):
        raise RequestServiceError(f"FlareSolverr API 返回异常（HTTP {response.status_code}）；请检查服务日志")
    if payload.get("status") != "ok":
        if browser_busy:
            raise RequestServiceError("FlareSolverr 浏览器仍在处理上一请求，已自动排队并重试；请检查服务资源，或适当增大抢购检查间隔") from None
        # Do not expose raw browser errors, which can contain URLs or cookies.
        message = str(payload.get("message", "")).casefold()
        if "captcha" in message or "challenge" in message:
            raise SiteBlocked(f"{expected_host} 的浏览器验证未完成；请检查站点安全规则，FlareSolverr 无法保证通过验证")
        if "timeout" in message or "timed out" in message:
            raise RequestServiceError("FlareSolverr 获取页面超时；请检查目标站点和浏览器服务日志")
        raise RequestServiceError("FlareSolverr 无法获取页面；请检查浏览器服务日志")
    if not response.ok:
        raise RequestServiceError(f"FlareSolverr API 返回异常（HTTP {response.status_code}）；请检查服务日志")
    solution = payload.get("solution")
    if not isinstance(solution, dict):
        raise RequestServiceError("FlareSolverr 响应缺少页面结果，监控已停止")
    body = solution.get("response")
    final_url = solution.get("url")
    status_code = solution.get("status")
    if not isinstance(body, str) or not isinstance(final_url, str) or not isinstance(status_code, int):
        raise RequestServiceError("FlareSolverr 响应格式异常，监控已停止")
    if fetcher.get("_copy_browser_state"):
        _copy_browser_state(session, solution, expected_host)
    # Parse the browser's HTML directly; cookies and User-Agent need not be copied
    # into the shared HTTP session used for other vendors and Telegram unless a
    # caller explicitly requests a same-site login bootstrap above.
    page_headers = solution.get("headers")
    return _validate_page_response(status_code, body, final_url, expected_host, page_headers if isinstance(page_headers, dict) else {})


def product_fetcher(fetcher: dict | None, item: dict) -> dict:
    result = dict(fetcher or {"mode": "requests"})
    # A resolved fetcher may be reused for another merchant or resolved again
    # inside a worker. Always start from its original shared browser defaults.
    defaults = result.pop("_flaresolverr_defaults", None)
    if defaults is not None:
        result.update(defaults)
    base_mode = result.get("mode", "requests")
    # An explicit merchant policy also overrides old per-product choices.
    # Missing policies preserve legacy configurations until the user unifies them.
    provider = item.get("adapter", "html")
    mode = result.get("provider_modes", {}).get(provider, item.get("request_mode", "inherit"))
    if mode != "inherit":
        result["mode"] = mode
    override = result.get("provider_flaresolverr", {}).get(provider)
    if result.get("mode") == "flaresolverr" and override is not None:
        result["_flaresolverr_defaults"] = {"mode": base_mode, **{key: result.get(key, default) for key, default in (("timeout_seconds", 60), ("wait_seconds", 0), ("disable_media", True))}}
        result.update(override)
    if "egress" in result:
        result.pop("egress_proxy_url", None)
        result.pop("egress_node_id", None)
        try:
            node = egress.resolve(result["egress"], item.get("adapter", "html"), str(item.get("egress_node_id", "")))
            if node:
                result["egress_proxy_url"] = egress.connect_url(node)
                result["egress_node_id"] = node["id"]
        except RuntimeError as exc:
            raise MonitorError(str(exc)) from None
    return result


def login_fetcher(fetcher: dict | None, provider: str) -> dict:
    """Resolve the request policy used for merchant account authentication.

    Login and product-page requests intentionally have separate policies.  An
    explicit login policy wins; ``inherit`` follows the merchant page policy,
    preserving behavior for older configurations.
    """
    result = dict(fetcher or {"mode": "requests"})
    defaults = result.pop("_flaresolverr_defaults", None)
    if defaults is not None:
        result.update(defaults)
    base_mode = result.get("mode", "requests")
    provider = str(provider or "html")
    mode = result.get("provider_login_modes", {}).get(provider, "inherit")
    if mode == "inherit":
        mode = result.get("provider_modes", {}).get(provider, "inherit")
    if mode != "inherit":
        result["mode"] = mode
    override = result.get("provider_flaresolverr", {}).get(provider)
    if result.get("mode") == "flaresolverr" and override is not None:
        result["_flaresolverr_defaults"] = {"mode": base_mode, **{key: result.get(key, default) for key, default in (("timeout_seconds", 60), ("wait_seconds", 0), ("disable_media", True))}}
        result.update(override)
    return result


def fetcher_route(fetcher: dict | None, item: dict) -> dict:
    """Describe a request policy without publishing proxy URLs or credentials."""
    settings = fetcher or {}
    provider = item.get("adapter", "html")
    mode = settings.get("provider_modes", {}).get(provider, item.get("request_mode", "inherit"))
    node_id = item.get("egress_node_id") or settings.get("egress", {}).get("routes", {}).get(provider, "")
    node = next((row for row in settings.get("egress", {}).get("nodes", []) if row["id"] == node_id), {})
    route = {"mode": settings.get("mode", "requests") if mode == "inherit" else mode,
             "node_id": node_id, "node_name": node.get("name", node_id), "public_ip": node.get("public_ip", ""), "error": ""}
    try:
        resolved = product_fetcher(settings, item)
        if resolved.get("mode") == "flaresolverr":
            route.update({key: resolved.get(key, default) for key, default in (("timeout_seconds", 60), ("wait_seconds", 0), ("disable_media", True))})
    except MonitorError as exc:
        route["error"] = str(exc)
    return route


def fetch_page(session: requests.Session, url: str, fetcher: dict | None, expected_host: str) -> tuple[str, str]:
    egress.apply_session(session, fetcher)
    if (fetcher or {}).get("mode") == "cloudscraper":
        return fetch_with_cloudscraper(url, expected_host, fetcher)
    if (fetcher or {}).get("mode") == "flaresolverr":
        return fetch_with_flaresolverr(session, url, fetcher, expected_host)
    return fetch_with_requests(session, url, expected_host)


def fetch_products(session: requests.Session, url: str, selectors: list[str], fetcher: dict | None = None) -> list[Product]:
    body, final_url_text = fetch_page(session, url, fetcher, "app.vmiss.com")
    final_url = urlsplit(final_url_text)
    if final_url.scheme != "https" or final_url.hostname != "app.vmiss.com":
        raise MonitorError("商品页重定向离开 app.vmiss.com；已停止，未处理页面内容")
    return parse_products(body, final_url_text, selectors)


def fetch_html_stock(session: requests.Session, item: dict, fetcher: dict | None = None) -> Product:
    """Read one configured stock indicator from HTTP or browser-rendered HTML."""
    url = str(item["url"])
    expected_host = (urlsplit(url).hostname or "").casefold()
    body, final_url = fetch_page(session, url, product_fetcher(fetcher, item), expected_host)
    soup = BeautifulSoup(body, "html.parser")
    selector = str(item["stock_selector"])
    try:
        matches = soup.select(selector)
    except Exception as exc:
        raise MonitorError(f"商品 {item['name']} 的 CSS 选择器无效：{exc}") from None
    if len(matches) != 1:
        raise MonitorError(f"商品 {item['name']} 的 CSS 选择器应精确匹配一个库存元素，实际匹配 {len(matches)} 个")
    stock_text = " ".join(matches[0].get_text(" ", strip=True).split())
    if not stock_text:
        raise MonitorError(f"商品 {item['name']} 的库存元素为空，不能安全判断库存")

    stock_mode = item["stock_mode"]
    if stock_mode == "quantity":
        try:
            match = re.search(item["stock_pattern"], stock_text, re.IGNORECASE)
        except re.error as exc:
            raise MonitorError(f"商品 {item['name']} 的数量正则无效：{exc}") from None
        if not match:
            raise MonitorError(f"商品 {item['name']} 的库存文本无法匹配数量规则：{stock_text[:160]}")
        quantity_text = match.group(1)
        digits = re.sub(r"[^0-9]", "", quantity_text)
        if not digits:
            raise MonitorError(f"商品 {item['name']} 的库存数量不是有效整数：{quantity_text!r}")
        available = int(digits)
    else:
        if re.search(item["unavailable_pattern"], stock_text, re.IGNORECASE):
            available = 0
        elif re.search(item["available_pattern"], stock_text, re.IGNORECASE):
            available = 1
        else:
            raise MonitorError(f"商品 {item['name']} 的库存文本未匹配有货或缺货规则：{stock_text[:160]}")
    return Product(
        key=url,
        name=item["name"],
        available=available,
        url=final_url,
        vendor=item["provider"],
        stock_mode=stock_mode,
    )


def parse_vps_soy(html: str, page_url: str, selector: str) -> Product:
    """Match Yunyoo cart cards by full title; negative inventory means sold out."""
    soup = BeautifulSoup(html, "html.parser")
    target = " ".join(selector.split()).casefold()
    matches = []
    for card in soup.select(".cartitem"):
        titles = card.select(".card-body h4")
        if len(titles) == 1 and " ".join(titles[0].get_text(" ", strip=True).split()).casefold() == target:
            matches.append((card, titles[0]))
    if len(matches) != 1:
        query = parse_qs(urlsplit(page_url).query)
        names = [title.get_text(" ", strip=True)[:80] for title in soup.select(".cartitem .card-body h4")][:6]
        hint = f"当前分类 FID={query.get('fid', ['默认'])[0]}、GID={query.get('gid', ['默认'])[0]}；请核对分组及套餐完整名称"
        if not matches and names:
            hint += f"，本页套餐：{'、'.join(names)}"
        raise (ProductUnlisted if not matches else MonitorError)(f"VPS.SOY 套餐 {selector} 无法唯一匹配（找到 {len(matches)} 项）；{hint}")
    card, title = matches[0]
    stocks = [node.get_text(" ", strip=True) for node in card.select("p.card-text") if re.match(r"^库存\s*[:：]", node.get_text(" ", strip=True))]
    if len(stocks) != 1:
        raise MonitorError("VPS.SOY 套餐缺少唯一库存字段，页面结构可能已变化")
    quantity = re.fullmatch(r"库存\s*[:：]\s*(-?(?:\d{1,3}(?:,\d{3})+|\d+))", stocks[0])
    if not quantity:
        raise MonitorError("VPS.SOY 套餐库存不是可识别的整数，未更新库存")
    count = int(quantity.group(1).replace(",", ""))
    sold_out = any(urlsplit(img.get("src", "")).path.endswith("/saleout.svg") for img in card.select("img[src]"))
    if count > 0 and sold_out:
        raise MonitorError("VPS.SOY 库存数量与售罄标记冲突，未更新库存")
    links = card.select(".card-footer a[href]")
    if len(links) != 1:
        raise MonitorError("VPS.SOY 套餐缺少唯一购买入口，页面结构可能已变化")
    href = links[0]["href"].strip()
    if count > 0 and (links[0].has_attr("disabled") or "disabled" in links[0].get("class", []) or re.search(r"cursor\s*:\s*not-allowed", links[0].get("style", ""), re.IGNORECASE)):
        raise MonitorError("VPS.SOY 库存与禁用购买按钮冲突，未更新库存")
    purchase_url = page_url
    if href and not href.casefold().startswith("javascript:") and href != "#":
        purchase_url = urljoin(page_url, href)
        parsed = urlsplit(purchase_url)
        if parsed.scheme != "https" or parsed.hostname != "www.vps.soy" or parsed.username or parsed.password or parsed.port not in {None, 443}:
            raise MonitorError("VPS.SOY 购买入口离开本站，已停止处理")
    elif count > 0:
        raise MonitorError("VPS.SOY 显示有库存但购买按钮不可用，未更新库存")
    gid = parse_qs(urlsplit(page_url).query).get("gid", ["8"])[0]
    region = ""
    for group in soup.select(".secondgroup_item[onclick]"):
        match = re.search(r"[?&]gid=([0-9]+)", group["onclick"])
        if match and match[1] == gid:
            region = group.get_text(" ", strip=True)
            break
    return Product(selector, title.get_text(" ", strip=True), max(0, count), purchase_url, vendor="VPS.SOY", catalog_url=page_url, **soy_metadata(card, region))


def fetch_vps_soy_page(session: requests.Session, catalog_url: str, fetcher: dict) -> tuple[str, str]:
    try:
        catalog_url = normalize_vps_soy_url(catalog_url)
    except ValueError as exc:
        raise MonitorError(str(exc)) from exc
    body, final_url = fetch_page(session, catalog_url, fetcher, "www.vps.soy")
    requested, actual = (parse_qs(urlsplit(url).query) for url in (catalog_url, final_url))
    if any(key in requested and requested[key] != actual.get(key) for key in ("fid", "gid")):
        raise MonitorError("VPS.SOY 返回页面的 FID/GID 与请求分类不一致，未更新库存；请核对商品分类页")
    return body, final_url


AKILE_STORE_URL = "https://api.akile.ai/api/v1/store/GetVpsStoreV3"


def fetch_akile_catalog(session: requests.Session, fetcher: dict) -> object:
    """Use the same unauthenticated, read-only catalog as Akile's shop."""
    body, _ = fetch_page(session, AKILE_STORE_URL, fetcher, "api.akile.ai")
    try:
        return json.loads(body)
    except ValueError:
        # Chromium renders a JSON response inside a pre element.
        if fetcher.get("mode") == "flaresolverr":
            nodes = BeautifulSoup(body, "html.parser").select("pre")
            if len(nodes) == 1:
                try:
                    return json.loads(nodes[0].get_text())
                except ValueError:
                    pass
        raise MonitorError("Akile 商品接口未返回有效 JSON，未更新库存") from None


def network_number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def traffic_amount(value: object) -> str:
    if isinstance(value, str) and value.strip().casefold() in {"无限制", "不限量", "不限流量", "unlimited"}:
        return "无限制"
    number = network_number(value)
    if number is None:
        return "—"
    return "无限制" if number == 0 else f"{number:g} GB流量"


def akile_network(plan: dict) -> str:
    flow = traffic_amount(plan.get("flow"))
    speed = network_number(plan.get("bandwidth"))
    return flow + (f" · {speed:g} Mbps" if speed is not None else "")


def isvoro_network(settings: dict) -> str:
    def first(keys):
        return next((number for key in keys if (number := network_number(settings.get(key))) is not None), None)
    flow = first(("bandwidth_limit", "traffic_limit", "traffic", "data_transfer", "transfer", "monthly_traffic"))
    speed = first(("bandwidth_speed",))
    if speed is None:
        rate = first(("ratelimit",))
        # Isvoro's storefront converts ratelimit (MB/s) to Mbps by multiplying by 8.
        speed = rate * 8 if rate is not None else first(("bandwidth", "speed", "port_speed"))
    return traffic_amount(flow) + (f" · {speed:g} Mbps" if speed is not None else "")


def parse_akile(payload: object, item: dict) -> Product:
    """Resolve one exact node/plan pair; never infer stock from a shop URL."""
    if not isinstance(payload, dict) or type(payload.get("status_code")) not in {int, str} or str(payload["status_code"]) not in {"0", "200"}:
        raise MonitorError("Akile 商品接口返回失败状态，未更新库存")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise MonitorError("Akile 商品接口缺少目录数据，未更新库存")

    def entries(value: object, label: str) -> list[dict]:
        if not isinstance(value, list) or any(not isinstance(entry, dict) for entry in value):
            raise MonitorError(f"Akile {label}列表结构异常，未更新库存")
        return value

    def matching(value: object, field: str, target: str, label: str) -> list[dict]:
        return [entry for entry in entries(value, label) if str(entry.get(field, "")).strip().casefold() == target.strip().casefold()]

    def unique(value: object, field: str, target: str, label: str) -> dict:
        matches = matching(value, field, target, label)
        if len(matches) != 1:
            raise (ProductUnlisted if not matches else MonitorError)(f"Akile {label} {target} 无法唯一匹配（找到 {len(matches)} 项），请核对配置")
        return matches[0]

    all_areas = entries(data.get("areas"), "地区")
    automatic = item.get("area_mode", "auto") == "auto"
    areas = all_areas if automatic else [unique(all_areas, "id", item["area_id"], "地区")]
    nodes = [(area, node) for area in areas for node in matching(area.get("nodes"), "group_name", item["node_name"], "线路")]
    if not nodes:
        hint = "请核对线路完整名称"
        if not automatic:
            elsewhere = [area for area in all_areas if matching(area.get("nodes"), "group_name", item["node_name"], "线路")]
            locations = "、".join(f"{str(area.get('area_name') or '地区')[:40]}（ID={area.get('id')}）" for area in elsewhere[:6])
            hint = f"当前限定地区 ID={item['area_id']}；" + (f"该线路实际位于 {locations}，请选择自动识别地区或修改地区 ID" if locations else hint)
        raise ProductUnlisted(f"Akile 线路 {item['node_name']} 无法唯一匹配（找到 0 项）；{hint}")
    candidates = [(area, node, plan) for area, node in nodes for plan in matching(node.get("plans"), "plan_name", item["selector"], "套餐")]
    if len(candidates) != 1:
        if not candidates:
            names = list(dict.fromkeys(str(plan.get("plan_name", ""))[:80] for _, node in nodes for plan in entries(node.get("plans"), "套餐")))
            hint = "请填写该线路的套餐完整名称" + (f"，可选：{'、'.join(names[:6])}" if names else "，目录中可能已下架")
        else:
            locations = "、".join(f"{str(area.get('area_name') or '地区')[:40]}（ID={area.get('id')}）" for area, _, _ in candidates[:6])
            hint = f"匹配地区：{locations}；请使用指定地区 ID 缩小范围，不能自动选择重名商品"
        raise (ProductUnlisted if not candidates else MonitorError)(f"Akile 套餐 {item['selector']} 无法唯一匹配（找到 {len(candidates)} 项）；{hint}")
    area, node, plan = candidates[0]
    if plan.get("is_pre_sale") is True:
        raise MonitorError("Akile 当前套餐为预售，不能将预售名额当作现货库存")
    stock = plan.get("stock")
    if type(stock) is not int or stock < 0:
        raise MonitorError("Akile 套餐库存不是非负整数，未更新库存")
    if any(type(entry.get("id")) is not int or entry["id"] <= 0 for entry in (area, node, plan)) or not isinstance(plan.get("tag"), str) or plan["tag"] not in {"traffic", "bandwidth", "performance"}:
        raise MonitorError("Akile 套餐缺少有效购买标识，未更新库存")
    purchase_url = "https://akile.ai/shop/server?" + urlencode({"type": plan["tag"], "areaId": area["id"], "nodeId": node["id"], "planId": plan["id"]})
    return Product(str(plan["id"]), plan["plan_name"], stock, purchase_url, vendor="Akile", area_id=str(area["id"]), area_name=str(area.get("area_name") or f"地区 {area['id']}")[:80], node_name=str(node["group_name"]), region=str(area.get("area_name") or ""),
        specs=f"{plan.get('cpu', '—')} 核 · {plan.get('memory', '—')} MB · {plan.get('disk', '—')} GB",
        traffic=akile_network(plan),
        price="；".join(f"¥{p['price'] / 100:g} / {p['cycle']}个月" for p in (plan.get('price_datas') or []) if type(p.get('price')) in {int, float} and type(p.get('cycle')) is int and p['cycle'] > 0)[:500],
        match_details={"area_id": area["id"], "area_name": str(area.get("area_name") or ""),
                       "node_id": node["id"], "node_name": str(node.get("group_name") or ""),
                       "plan_id": plan["id"], "plan_name": str(plan.get("plan_name") or ""),
                       "plan": plan})


def fetch_isvoro_json(session: requests.Session, path: str, fetcher: dict) -> object:
    parts = str(path).split("/")
    valid_catalog_path = len(parts) in {1, 2} and parts[0] in {"categories", "products"} and (len(parts) == 1 or isvoro_slug(parts[1]))
    if path != "seckill/activities" and not valid_catalog_path:
        raise MonitorError("Isvoro 目录路径无效")
    body, _ = fetch_page(session, "https://isvoro.com/api/v1/" + path, fetcher, "isvoro.com")
    if fetcher.get("mode") == "flaresolverr":
        nodes = BeautifulSoup(body, "html.parser").select("pre")
        if len(nodes) == 1:
            body = nodes[0].get_text()
    try:
        payload = json.loads(body)
    except ValueError:
        raise MonitorError("Isvoro 接口未返回有效 JSON，未更新库存") from None
    if not isinstance(payload, dict) or type(payload.get("code")) is not int or payload["code"] != 0 or "data" not in payload:
        raise MonitorError("Isvoro 接口返回失败状态，未更新库存")
    return payload["data"]


def isvoro_slug(value: object) -> bool:
    """Accept vendor slugs with spaces and Unicode punctuation while blocking path escapes."""
    return isinstance(value, str) and value not in {".", ".."} and bool(re.fullmatch(r"[^\x00-\x1f/\\?#]{1,100}", value))


def isvoro_entry(value: object) -> dict:
    if not isinstance(value, dict) or type(value.get("id")) is not int or value["id"] <= 0 or not isinstance(value.get("name"), str) or not value["name"].strip() or not isvoro_slug(value.get("slug")):
        raise MonitorError("Isvoro 目录条目标识无效，未更新库存")
    return {key: value[key] for key in ("id", "name", "slug")}


def isvoro_list(value: object) -> list[dict]:
    if not isinstance(value, list) or any(not isinstance(entry, dict) for entry in value):
        raise MonitorError("Isvoro 目录列表结构异常，未更新库存")
    return value


def isvoro_categories(value: object) -> list[dict]:
    result = []
    for entry in isvoro_list(value):
        if entry.get("hidden") is True:
            continue
        row = isvoro_entry(entry)
        row["children"] = [isvoro_entry(child) for child in isvoro_list(entry.get("children", [])) if child.get("hidden") is not True]
        result.append(row)
    return result


def isvoro_category(value: object) -> dict:
    row = isvoro_entry(value)
    if value.get("hidden") is True:
        raise MonitorError("Isvoro 地区/线路已隐藏，未更新库存")
    row["products"] = []
    for product in isvoro_list(value.get("products")):
        if product.get("hidden") is True:
            continue
        if product.get("category_id") != row["id"]:
            raise MonitorError("Isvoro 套餐归属的地区/线路不一致，未更新库存")
        row["products"].append(isvoro_entry(product))
    return row


def inspect_isvoro(session: requests.Session, item: dict, fetcher: dict, cache: dict | None = None) -> Product:
    cache = {} if cache is None else cache
    def get(path):
        key = (path, fetcher.get("mode", "requests"))
        if key not in cache:
            cache[key] = fetch_isvoro_json(session, path, fetcher)
        return cache[key]
    def unique(rows, identifier, label):
        matches = [row for row in rows if str(row["id"]) == str(identifier)]
        if len(matches) != 1:
            raise (ProductUnlisted if not matches else MonitorError)(f"Isvoro {label} ID={identifier} 无法唯一匹配（找到 {len(matches)} 项）；请刷新目录重新选择")
        return matches[0]
    kind = unique(isvoro_categories(get("categories")), item["type_id"], "类型")
    # Types without subcategories (e.g. dedicated servers) are their own line.
    line = unique(kind["children"] or [kind], item["category_id"], "地区/线路")
    raw_category = get("categories/" + line["slug"])
    category = isvoro_category(raw_category)
    if category["id"] != line["id"]:
        raise MonitorError("Isvoro 地区/线路目录 ID 不一致，未更新库存")
    selected = unique(category["products"], item["product_id"], "套餐")
    raw_product = get("products/" + selected["slug"])
    detail = isvoro_entry(raw_product)
    if detail["id"] != selected["id"] or raw_product.get("hidden") is True:
        raise MonitorError("Isvoro 套餐详情 ID 不一致或商品已隐藏，未更新库存")
    if not any(plan.get("hidden") is not True for plan in isvoro_list(raw_product.get("plans"))):
        raise MonitorError("Isvoro 套餐没有可用购买周期，未更新库存")
    if "stock" not in raw_product or (raw_product["stock"] is not None and (type(raw_product["stock"]) is not int or raw_product["stock"] < 0)):
        raise MonitorError("Isvoro 套餐库存不是非负整数或明确的不限量状态，未更新库存")
    if "ip_available" in raw_product and type(raw_product["ip_available"]) is not bool:
        raise MonitorError("Isvoro IP 可用状态无效，未更新库存")
    settings = raw_product.get("settings", {})
    if isinstance(settings, list):
        settings = {str(setting.get("key")): setting.get("value") for setting in isvoro_list(settings)}
    if not isinstance(settings, dict):
        raise MonitorError("Isvoro 套餐设置结构异常，未更新库存")
    if str(settings.get("presale_enabled", "")).casefold() in {"1", "true"}:
        raise MonitorError("Isvoro 当前套餐为预售，不能将预售名额当作现货库存")
    count = raw_product["stock"]
    mode = "availability" if count is None else "quantity"
    available = 1 if count is None else count
    if raw_product.get("ip_available") is False:
        available = 0
    purchase_url = "https://isvoro.com/buy/" + line["slug"] + "?" + urlencode({"product": selected["id"]})
    return Product(str(selected["id"]), detail["name"], available, purchase_url, vendor="Isvoro", stock_mode=mode,
        region=kind["name"], specs=f"{settings.get('cores', '—')} 核 · {settings.get('memory', '—')} MB · {settings.get('disk', '—')} GB",
        traffic=isvoro_network(settings),
        price="；".join(f"{p.get('currency_code', '')} {p['price']:g} / {plan.get('name', '')}" for plan in isvoro_list(raw_product.get('plans')) if plan.get('hidden') is not True for p in (plan.get('prices') or []) if isinstance(p, dict) if type(p.get('price')) in {int, float})[:500], match_details={"type_id": str(kind["id"]), "type_name": kind["name"], "category_id": str(line["id"]), "category_name": line["name"], "product_id": str(selected["id"]), "product_slug": str(selected.get("slug") or ""), "plan_name": detail["name"], "plans": [plan for plan in isvoro_list(raw_product.get("plans")) if plan.get("hidden") is not True], "checkout_fields": raw_product.get("checkout_fields") or []})


def inspect_product(session: requests.Session, item: dict, fetcher: dict) -> Product:
    """Read one product without saving history, changing services or notifying."""
    if item["adapter"] == "html":
        return fetch_html_stock(session, item, fetcher)
    if item["adapter"] == "dmit":
        from dmit import inspect
        return inspect(session, item, product_fetcher(fetcher, item))
    if item["adapter"] == "vps_soy":
        body, final_url = fetch_vps_soy_page(session, item["url"], product_fetcher(fetcher, item))
        return parse_vps_soy(body, final_url, item["selector"])
    if item["adapter"] == "akile":
        return parse_akile(fetch_akile_catalog(session, product_fetcher(fetcher, item)), item)
    if item["adapter"] == "isvoro":
        return inspect_isvoro(session, item, product_fetcher(fetcher, item))
    if item["adapter"] == "bwh":
        from bwh import inspect
        return inspect(session, item, product_fetcher(fetcher, item))
    if item["adapter"] == "vmiss":
        products = fetch_products(session, item["category_url"], [item["selector"]], product_fetcher(fetcher, item))
        if len(products) != 1:
            raise MonitorError("商品无法唯一匹配页面套餐；请检查商品选择标识")
        return products[0]
    raise MonitorError(f"不支持的监控适配器：{item.get('adapter', '')}")


def affiliate_url(product_url: str, affiliate_id: str, gid: str, pid: str) -> str:
    parsed = urlsplit(product_url)
    query = {"aff": affiliate_id, "pid" if pid else "gid": pid or gid}
    return urljoin(f"{parsed.scheme}://{parsed.netloc}/", "aff.php") + "?" + urlencode(query)


def purchase_link(product: Product, item: dict, config: dict) -> str:
    return affiliate_purchase_url(item.get("purchase_url") or product.url, item["adapter"], config, item)


def read_config(path: Path) -> dict:
    if not path.exists():
        return default_config()
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        return validate_config(config)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise MonitorError(f"配置文件无法读取：{exc}") from exc


def write_status(path: Path, status: dict) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep module status and the other role's product rows when one schedule
    # finishes independently of the other.
    try:
        previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if isinstance(previous, dict):
            if "products" not in status and isinstance(previous.get("products"), list):
                status["products"] = previous["products"]
            if isinstance(status.get("products"), list) and isinstance(previous.get("products"), list):
                rows = {str(row.get("id")): row for row in previous["products"] if isinstance(row, dict) and row.get("id")}
                rows.update({str(row.get("id")): row for row in status["products"] if isinstance(row, dict) and row.get("id")})
                status["products"] = list(rows.values())
            if isinstance(previous.get("modules"), dict) and isinstance(status.get("modules"), dict):
                merged = dict(previous["modules"])
                merged.update(status["modules"])
                status["modules"] = merged
            elif isinstance(previous.get("modules"), dict) and "modules" not in status:
                status["modules"] = previous["modules"]
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def module_schedule(config: dict, module: str) -> dict:
    defaults = {
        "monitor": ("monitor_schedule", "auto", int(config.get("interval_seconds", 600)), 0),
        "notify": ("notify_schedule", "auto", int(config.get("interval_seconds", 600)), 0),
        "catalog": ("catalog_schedule", "manual", 300, 0),
        "seckill": ("seckill_schedule", "manual", int(config.get("vps_seckill", {}).get("interval_seconds", 5)), 1),
    }
    key, mode, interval, minimum = defaults[module]
    value = config.get(key, {})
    if not isinstance(value, dict):
        value = {}
    return {"mode": str(value.get("mode", mode)).casefold(),
            "interval_seconds": max(minimum, int(value.get("interval_seconds", interval)))}


def update_module_status(path: Path, module: str, schedule: dict, result: str,
                        message: str = "", checked_at: str | None = None, trigger: str | None = None) -> None:
    if path is None:
        return
    try:
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        current = {}
    current = current if isinstance(current, dict) else {}
    modules = current.get("modules") if isinstance(current.get("modules"), dict) else {}
    previous = modules.get(module) if isinstance(modules.get(module), dict) else {}
    at = checked_at or beijing_iso()
    log = {"at": at, "result": result, "trigger": trigger or schedule.get("mode", "manual"), "message": message or ("完成" if result == "success" else "等待执行")}
    logs = [item for item in previous.get("logs", []) if isinstance(item, dict)][-19:]
    if result != "pending" or not logs:
        logs.append(log)
    modules[module] = {
        "mode": schedule.get("mode", "manual"),
        "interval_seconds": schedule.get("interval_seconds", 0),
        "running": result == "pending",
        "last_result": result,
        "last_checked": at,
        "last_manual_refresh": previous.get("last_manual_refresh", ""),
        "last_auto_refresh": previous.get("last_auto_refresh", ""),
        "message": message or previous.get("message", ""),
        "logs": logs[-20:],
    }
    if result != "pending":
        if (trigger or schedule.get("mode")) == "manual":
            modules[module]["last_manual_refresh"] = at
        else:
            modules[module]["last_auto_refresh"] = at
    current["modules"] = modules
    write_status(path, current)


def selected_products(config: dict, role: str | None = None) -> list[dict]:
    products = [item for item in config.get("products", []) if role is None and (product_membership(item, "watch") or product_membership(item, "notify")) or role and product_membership(item, role)]
    for item in products:
        name = item.get("name", "(未命名)")
        if item.get("adapter") == "vmiss":
            url = str(item.get("category_url", ""))
            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.hostname != "app.vmiss.com":
                raise MonitorError(f"商品 {name} 的分类页必须是 app.vmiss.com 的 HTTPS 地址")
            if bool(item.get("gid")) == bool(item.get("pid")):
                raise MonitorError(f"商品 {name} 必须且只能填写 gid 或 pid 其中一个")
            if not str(item.get("selector", "")).strip():
                raise MonitorError(f"商品 {name} 缺少商品选择标识")
        elif item.get("adapter") == "dmit":
            from dmit import PLAN_KEY_PATTERN, PRICING_URL, normalize_catalog_url
            try:
                normalize_catalog_url(item.get("category_url", PRICING_URL))
                pid, plan_key = str(item.get("pid", "")), str(item.get("plan_key", ""))
                if (not pid and not plan_key) or pid and not re.fullmatch(r"[1-9][0-9]{0,9}", pid) or plan_key and not re.fullmatch(PLAN_KEY_PATTERN, plan_key):
                    raise ValueError("缺少有效 PID 或套餐标识")
            except ValueError as exc:
                raise MonitorError(f"商品 {name} 的 DMIT 配置无效：{exc}") from exc
        elif item.get("adapter") == "bwh":
            from bwh import PRODUCT_URL, normalize_catalog_url
            try:
                normalize_catalog_url(item.get("category_url", PRODUCT_URL))
            except ValueError as exc:
                raise MonitorError(f"商品 {name} 的搬瓦工配置无效：{exc}") from exc
            if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{1,40}:[A-Za-z0-9_-]{1,40}:[0-9]+", str(item.get("bwh_offer_key", item.get("selector", "")))):
                raise MonitorError(f"商品 {name} 缺少有效的搬瓦工套餐标识")
        elif item.get("adapter") == "vps_soy":
            if urlsplit(str(item.get("url", ""))).hostname != "www.vps.soy" or not item.get("selector"):
                raise MonitorError(f"商品 {name} 的 VPS.SOY 配置不完整")
        elif item.get("adapter") == "isvoro":
            if any(not re.fullmatch(r"[1-9][0-9]{0,9}", str(item.get(key, ""))) for key in ("type_id", "category_id", "product_id")):
                raise MonitorError(f"商品 {name} 的 Isvoro 类型、地区/线路和套餐配置不完整")
        elif item.get("adapter") == "akile":
            area_mode = item.get("area_mode", "auto")
            area_id = str(item.get("area_id", ""))
            if area_mode not in {"auto", "manual"} or ((area_mode == "manual" or area_id) and not re.fullmatch(r"[1-9][0-9]{0,9}", area_id)) or not item.get("node_name") or not item.get("selector"):
                raise MonitorError(f"商品 {name} 的 Akile 配置不完整")
        elif item.get("adapter") == "html":
            url = str(item.get("url", ""))
            if urlsplit(url).scheme != "https" or not str(item.get("stock_selector", "")).strip():
                raise MonitorError(f"商品 {name} 的通用网页配置不完整")
        else:
            raise MonitorError(f"商品 {name} 的监控适配器无效")
    return products


def product_membership(item: dict, role: str) -> bool:
    if "watch_enabled" in item or "notify_enabled" in item:
        return bool(item.get(role + "_enabled", False))
    return bool(item.get("enabled", False))


def read_state(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        products = loaded.get("products", {})
        if not isinstance(products, dict):
            raise ValueError("products is not an object")
        return products
    except (OSError, json.JSONDecodeError, ValueError, AttributeError) as exc:
        raise MonitorError(f"状态文件 {path} 无法读取；为避免误报已停止：{exc}") from exc


def write_state(path: Path, state: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    payload = {"updated_at": beijing_iso(), "products": state}
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def inventory_cache_identity(item: dict, fetcher: dict) -> dict:
    """Return only request-affecting product fields for shared stock caching."""
    fields = ("url", "category_url", "selector", "stock_selector", "stock_mode", "stock_pattern",
              "available_pattern", "unavailable_pattern", "request_mode", "gid", "pid", "plan_key",
              "bwh_offer_key", "type_id", "category_id", "product_id", "node_name", "area_id")
    return {
        "id": str(item.get("id", "")),
        "adapter": str(item.get("adapter", "")),
        "mode": str(fetcher.get("mode", "requests")),
        "egress_node_id": str(fetcher.get("egress_node_id", "")),
        "wait_seconds": str(fetcher.get("wait_seconds", "")),
        "disable_media": str(fetcher.get("disable_media", "")),
        "timeout_seconds": str(fetcher.get("timeout_seconds", "")),
        "cloudscraper": fetcher.get("cloudscraper", {}),
        **{key: str(item.get(key, "")) for key in fields if key in item},
    }


def inventory_cache_key(item: dict, fetcher: dict) -> str:
    identity = json.dumps(inventory_cache_identity(item, fetcher), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def read_inventory_cache(path: Path | None, item: dict, fetcher: dict) -> Product | None:
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        entry = payload.get("entries", {}).get(inventory_cache_key(item, fetcher), {})
        checked_at = float(entry.get("checked_at_epoch", 0))
        product = entry.get("product")
        if time.time() - checked_at > INVENTORY_CACHE_MAX_AGE_SECONDS or not isinstance(product, dict):
            return None
        return Product(**product)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def write_inventory_cache(path: Path | None, item: dict, fetcher: dict, product: Product) -> None:
    """Publish one fresh inventory result for other modules to consume."""
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _INVENTORY_CACHE_THREAD_LOCK:
            payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            entries = payload.get("entries", {}) if isinstance(payload, dict) else {}
            if not isinstance(entries, dict):
                entries = {}
            entries[inventory_cache_key(item, fetcher)] = {
                "identity": inventory_cache_identity(item, fetcher),
                "checked_at": beijing_iso(),
                "checked_at_epoch": time.time(),
                "product": asdict(product),
            }
            cutoff = time.time() - 5 * 60
            entries = {key: value for key, value in entries.items()
                       if isinstance(value, dict) and float(value.get("checked_at_epoch", 0)) >= cutoff}
            fd, temporary_name = tempfile.mkstemp(prefix="inventory-cache-", suffix=".tmp", dir=path.parent)
            os.close(fd)
            temporary = Path(temporary_name)
            try:
                temporary.write_text(json.dumps({"updated_at": beijing_iso(), "entries": entries}, ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(path)
            finally:
                if temporary.exists():
                    temporary.unlink()
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        # Cache failures must never turn a valid stock result into an error.
        return


def telegram_request(session: requests.Session, token: str, method: str, **kwargs) -> dict:
    # Merchant routing must not leak into notification API calls on this session.
    egress.apply_session(session, None)
    endpoint = f"https://api.telegram.org/bot{token}/{method}"
    try:
        response = session.post(endpoint, json=kwargs, timeout=(10, 25)) if method in {"sendMessage", "editMessageText"} else session.get(endpoint, params=kwargs, timeout=(10, 25))
        result = response.json()
        if method == "editMessageText" and isinstance(result, dict) and result.get("error_code") == 400:
            description = str(result.get("description", "")).casefold()
            if "message is not modified" in description:
                return {"ok": True, "result": True}
            if any(reason in description for reason in ("message to edit not found", "message can't be edited", "message_id_invalid")):
                raise TelegramMessageUnavailable("Telegram 原通知已删除或无法编辑；本轮不另发消息，将在下次补货时发送新通知")
        response.raise_for_status()
    except (requests.RequestException, ValueError) as exc:
        # Never print an exception containing the Bot API URL (it includes the token).
        raise MonitorError(f"Telegram {method} 请求失败：{type(exc).__name__}") from None
    if not isinstance(result, dict) or not result.get("ok"):
        raise MonitorError(f"Telegram {method} 返回失败；请检查 bot 和目标 chat 配置")
    return result


def show_chat_ids(session: requests.Session, token: str) -> None:
    result = telegram_request(session, token, "getUpdates")
    chats: dict[str, tuple[str, str]] = {}
    for update in result.get("result", []):
        for key in ("message", "channel_post", "edited_message", "edited_channel_post"):
            item = update.get(key, {})
            chat = item.get("chat", {})
            if chat and "id" in chat:
                label = chat.get("title") or chat.get("username") or chat.get("first_name") or "(未命名)"
                chats[str(chat["id"])] = (chat.get("type", "unknown"), label)
    if not chats:
        print("还没有找到对话。先在 Telegram 中打开你的 bot 并发送 /start，然后重新运行此命令。")
        return
    for chat_id, (chat_type, label) in chats.items():
        print(f"chat_id={chat_id}  type={chat_type}  name={label}")


def notification_text(product: Product, old_count: int | None, purchase_url: str, item: dict | None = None) -> str:
    return stock_message(vendor=product.vendor, name=product.name, available=product.available, stock_mode=product.stock_mode,
        region=product.region or product.area_name, specs=product.specs, traffic=product.traffic, price=product.price,
        line=(product.match_details or {}).get("category_name", "") or product.node_name or (item or {}).get("category_name", ""), item=item, old_count=old_count, purchase_url=purchase_url,
        checked_at=now_beijing().strftime("%Y-%m-%d %H:%M:%S 北京时间"))


def collect_inventory(session, config, selected, found, unavailable, cache_path: Path | None = None):
    by_category: dict[tuple[str, str, str], list[dict]] = {}
    for item in selected:
        if item["adapter"] != "vmiss":
            continue
        fetcher = product_fetcher(config.get("fetcher"), item)
        by_category.setdefault((item["category_url"].strip(), fetcher.get("mode", "requests"), fetcher.get("egress_node_id", "")), []).append(item)
    for (category_url, mode, node_id), items in by_category.items():
        page_products = fetch_products(session, category_url, [], product_fetcher(config.get("fetcher"), items[0]))
        for item in items:
            selector = str(item["selector"]).strip().casefold()
            candidates = [p for p in page_products if selector in {
                p.key.casefold(),
                p.key.rstrip("/").rsplit("/", 1)[-1].casefold(),
                p.name.casefold(),
                p.card_id.casefold(),
            }]
            if not candidates:
                unavailable[item["id"]] = "目录中未找到商品，可能已下架；请核对商品标识，保留配置等待重新上架"
                continue
            if len(candidates) != 1:
                raise MonitorError(f"商品 {item.get('name', selector)} 无法唯一匹配页面套餐；请检查 selector")
            matched = candidates[0]
            product = replace(matched, vendor=item["provider"])
            found[item["id"]] = product
            write_inventory_cache(cache_path, item, product_fetcher(config.get("fetcher"), item), product)
    soy_pages = {}
    akile_catalogs = {}
    isvoro_catalogs = {}
    dmit_catalogs = {}
    bwh_catalogs = {}
    for item in selected:
        try:
            if item["adapter"] == "html":
                fetcher = product_fetcher(config.get("fetcher"), item)
                found[item["id"]] = fetch_html_stock(session, item, fetcher)
                write_inventory_cache(cache_path, item, fetcher, found[item["id"]])
            elif item["adapter"] == "dmit":
                from dmit import inspect
                fetcher = product_fetcher(config.get("fetcher"), item)
                found[item["id"]] = inspect(session, item, fetcher, dmit_catalogs)
                write_inventory_cache(cache_path, item, fetcher, found[item["id"]])
            elif item["adapter"] == "bwh":
                from bwh import inspect
                fetcher = product_fetcher(config.get("fetcher"), item)
                found[item["id"]] = inspect(session, item, fetcher, bwh_catalogs)
                write_inventory_cache(cache_path, item, fetcher, found[item["id"]])
            elif item["adapter"] == "vps_soy":
                fetcher = product_fetcher(config.get("fetcher"), item)
                key = (normalize_vps_soy_url(item["url"]), fetcher.get("mode", "requests"))
                if key not in soy_pages:
                    soy_pages[key] = fetch_vps_soy_page(session, key[0], fetcher)
                body, final_url = soy_pages[key]
                found[item["id"]] = parse_vps_soy(body, final_url, item["selector"])
                write_inventory_cache(cache_path, item, fetcher, found[item["id"]])
            elif item["adapter"] == "akile":
                fetcher = product_fetcher(config.get("fetcher"), item)
                mode = fetcher.get("mode", "requests")
                if mode not in akile_catalogs:
                    akile_catalogs[mode] = fetch_akile_catalog(session, fetcher)
                found[item["id"]] = parse_akile(akile_catalogs[mode], item)
                write_inventory_cache(cache_path, item, fetcher, found[item["id"]])
            elif item["adapter"] == "isvoro":
                fetcher = product_fetcher(config.get("fetcher"), item)
                found[item["id"]] = inspect_isvoro(session, item, fetcher, isvoro_catalogs)
                write_inventory_cache(cache_path, item, fetcher, found[item["id"]])
        except ProductUnlisted as exc:
            unavailable[item["id"]] = str(exc) + "；可能已下架或配置不匹配，保留配置等待重新上架"


def check_once(
    session: requests.Session,
    config: dict,
    state_path: Path,
    status_path: Path,
    bot_token: str,
    chat_id: str,
    role: str = "both",
    trigger: str | None = None,
) -> None:
    if role not in {"both", "watch", "notify"}:
        raise MonitorError("检查模块无效")
    selected = selected_products(config, None if role == "both" else role)
    schedule = module_schedule(config, "monitor" if role == "watch" else "notify" if role == "notify" else "monitor")
    module = "monitor" if role == "watch" else "notify" if role == "notify" else "monitor"
    if not selected and not (role in {"both", "notify"} and config.get("isvoro_seckill", False)):
        update_module_status(status_path, module, schedule, "skipped", "没有启用的商品")
        raise MonitorError("没有启用的监控产品")

    update_module_status(status_path, module, schedule, "pending", "正在检查库存", trigger=trigger)

    seckill_status = None
    if role in {"both", "notify"} and config.get("isvoro_seckill", False):
        from seckill import check_once as check_seckill
        try:
            seckill_status = {"ok": True, **check_seckill(session, config, state_path, bot_token, chat_id)}
        except (SiteBlocked, RequestServiceError):
            raise
        except (MonitorError, requests.RequestException) as exc:
            seckill_status = {"ok": False, "error": str(exc)}
        seckill_status["checked_at"] = beijing_iso()

    from stock_alerts import StockAlerts
    previous = read_state(state_path)
    current = {key: dict(value) for key, value in previous.items()}
    alerts = StockAlerts(sys.modules[__name__], session, config, state_path, current, bot_token, chat_id)
    found, unavailable = {}, {}
    cache_path = status_path.parent / "inventory-cache.json" if status_path is not None else None
    uncached = []
    for item in selected:
        try:
            fetcher = product_fetcher(config.get("fetcher"), item)
            # The notification role is the cache consumer. The monitor role
            # always performs the authoritative read and refreshes the cache;
            # this keeps the 10-second抢购/监控 path from ever using stale data.
            cached = read_inventory_cache(cache_path, item, fetcher) if role == "notify" else None
        except MonitorError:
            cached = None
        if cached is None:
            uncached.append(item)
        else:
            found[item["id"]] = cached
            logging.info("复用共享库存结果：%s", item.get("name", item["id"]))
    try:
        collect_inventory(session, config, uncached, found, unavailable, cache_path)
    except (MonitorError, requests.RequestException):
        # Only failed or not-yet-checked products are uncertain. Do not turn
        # request failures into a zero-stock baseline or overwrite the cause.
        for item in selected:
            if item["id"] not in found:
                try:
                    alerts.uncertain(item)
                except MonitorError as error:
                    logging.error("更新库存未确认状态失败：%s", error)
        raise

    notification_errors = []
    for item in selected:
        if item["id"] in unavailable:
            try:
                alerts.uncertain(item)
            except MonitorError as error:
                notification_errors.append(str(error))
            continue
        product = found[item["id"]]
        old = previous.get(item["id"])
        old_count = old.get("available") if old else None
        subscribed = product_membership(item, "notify")
        if old and old.get("notify_enabled") is False and subscribed:
            old_count = None
            old = None
            current[item["id"]].pop("telegram", None)
        current[item["id"]] = {
            **current.get(item["id"], {}),
            "name": product.name,
            "vendor": item["provider"],
            "available": product.available,
            "url": product.url,
            "notify_enabled": subscribed,
        }
        if role in {"both", "notify"} and subscribed:
            try:
                alerts.sync(item, product, old_count, old is None and config.get("notify_current_stock_on_start", False))
            except MonitorError as error:
                # Delivery state is saved per product. Retrying one failed message
                # must not duplicate messages already sent for other products.
                notification_errors.append(str(error))
                logging.error("更新商品 Telegram 通知失败：%s / %s", item["name"], error)

    # Stop updating messages for products that have left the notification list.
    subscribed_ids = {item["id"] for item in config.get("products", []) if product_membership(item, "notify")}
    for key, entry in current.items():
        if key not in subscribed_ids:
            entry.pop("telegram", None)
    write_state(state_path, current)
    checked_at = beijing_iso()
    rows = []
    for item in selected:
        if item["id"] in unavailable:
            # Never convert an absent listing to sold out or advance its baseline.
            snapshot = item.get("snapshot", {})
            rows.append({**snapshot, "id": item["id"], "name": item["name"], "vendor": item["provider"],
                         "available": None, "error": unavailable[item["id"]], "checked_at": checked_at,
                         "url": snapshot.get("url") or previous.get(item["id"], {}).get("url", "")})
            logging.warning("商品待核对：%s / %s", item["name"], unavailable[item["id"]])
            continue
        product = found[item["id"]]
        rows.append({"id": item["id"], "name": product.name, "vendor": item["provider"],
                     "available": product.available, "stock_mode": product.stock_mode, "url": product.url,
                     "purchase_url": purchase_link(product, item, config), "checked_at": checked_at,
                     **({"stock_version": product.match_details["stock_version"]} if item["adapter"] == "dmit" else {}),
                     **{field: getattr(product, field) for field in ("region", "specs", "traffic", "price")}})
    write_status(status_path, {
        "last_checked": checked_at, "state": "partial" if unavailable or notification_errors or alerts.warnings or seckill_status and not seckill_status["ok"] else "ok",
        "message": (f"{len(unavailable)} 个商品未匹配，保留配置并继续检查其他商品：" + "；".join(f"{item['name']}：{unavailable[item['id']]}" for item in selected if item['id'] in unavailable) if unavailable else "检查完成") + (f"；秒杀检查失败：{seckill_status['error']}" if seckill_status and not seckill_status["ok"] else "") + ("；Telegram 通知待重试：" + "；".join(notification_errors) if notification_errors else "") + ("；" + "；".join(alerts.warnings) if alerts.warnings else ""),
        "products": rows,
        **({"seckill": seckill_status} if seckill_status is not None else {}),
    })
    update_module_status(status_path, module, schedule, "error" if unavailable or notification_errors else "success",
                         "；".join(notification_errors) if notification_errors else (f"{len(unavailable)} 个商品待核对" if unavailable else "检查完成"), checked_at, trigger)
    logging.info("本轮检查完成：%s 个成功，%s 个未匹配", len(found), len(unavailable))


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="低频监控多个厂商的商品库存，每轮补货发送一次 Telegram 通知并更新原消息。")
    parser.add_argument("--once", action="store_true", help="只检查一次后退出")
    parser.add_argument("--role", choices=("both", "watch", "notify"), default="both", help="检查库存监控、库存通知，或兼容模式下两者")
    parser.add_argument("--show-chat-ids", action="store_true", help="读取 bot 收到的对话并显示 chat_id")
    parser.add_argument("--config", default=os.getenv("VMISS_CONFIG", str(CONFIG_PATH)))
    args = parser.parse_args()

    config_path = Path(args.config).expanduser()
    if not config_path.is_absolute():
        config_path = ROOT / config_path
    status_path = Path(os.getenv("VMISS_STATUS", str(config_path.parent / "status.json")))
    state_path = Path(os.getenv("STATE_FILE", str(STATE_PATH))).expanduser()
    if not state_path.is_absolute():
        state_path = ROOT / state_path
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    logging.Formatter.converter = beijing_logging_converter
    logging.basicConfig(level=logging.INFO, format="%(asctime)s 北京时间 %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    session = requests.Session()
    session.headers.update({
        "User-Agent": "HostWatch/1.0 (polite periodic stock check)",
        "Accept": "text/html,application/xhtml+xml",
    })

    if args.show_chat_ids:
        try:
            config = read_config(config_path)
            bot_token, _ = effective_telegram(config)
            if not bot_token:
                parser.error("先在管理页或环境变量中设置 TELEGRAM_BOT_TOKEN")
            show_chat_ids(session, bot_token)
            return 0
        except MonitorError as exc:
            logging.error("%s", exc)
            return 1

    last_run = {"watch": 0.0, "notify": 0.0}
    active_due = []
    while True:
        try:
            config = read_config(config_path)
            # The systemd monitor process is the long-lived HostWatch service.
            # Manual module schedules mean "do not run an automatic check";
            # they do not stop this process or the management service.
            if not config.get("enabled", False) and not any(key in config for key in ("monitor_schedule", "notify_schedule")):
                # Legacy configurations without independent schedules still
                # use the old aggregate switch as an explicit stop marker.
                logging.info("旧版监控总开关关闭，不访问商品网站")
                return 0
            bot_token, chat_id = effective_telegram(config)
            telegram_config = config.get("telegram", {})
            if not any(product_membership(p, "notify") for p in config.get("products", [])) and not config.get("isvoro_seckill", False):
                bot_token, chat_id = "", ""
                telegram_config = {"disabled": True}
            if not telegram_config.get("disabled", False) and (not bot_token or not chat_id):
                raise MonitorError("Telegram bot token 和 chat_id 必须同时填写，或在管理页停用 Telegram")
            legacy_schedule = not any(key in config for key in ("monitor_schedule", "notify_schedule"))
            if args.once:
                if args.role == "both":
                    check_once(session, config, state_path, status_path, bot_token, chat_id)
                else:
                    check_once(session, config, state_path, status_path, bot_token, chat_id, args.role)
                return 0
            if legacy_schedule and args.role == "both":
                check_once(session, config, state_path, status_path, bot_token, chat_id)
                time.sleep(int(config.get("interval_seconds", 600)))
                continue
            now = time.monotonic()
            due = []
            for role in ("watch", "notify"):
                schedule = module_schedule(config, "monitor" if role == "watch" else "notify")
                has_products = bool(selected_products(config, role)) or (role == "notify" and config.get("isvoro_seckill", False))
                if schedule["mode"] == "auto" and has_products and (not last_run[role] or now - last_run[role] >= schedule["interval_seconds"]):
                    due.append(role)
            active_due = due
            if not due:
                waits = []
                for role in ("watch", "notify"):
                    schedule = module_schedule(config, "monitor" if role == "watch" else "notify")
                    if schedule["mode"] == "auto" and (selected_products(config, role) or (role == "notify" and config.get("isvoro_seckill", False))):
                        waits.append(max(0.25, schedule["interval_seconds"] - (now - last_run[role])) if last_run[role] else 0.25)
                time.sleep(min(waits) if waits else 1.0)
                continue
            for role in due:
                check_once(session, config, state_path, status_path, bot_token, chat_id, role)
                last_run[role] = time.monotonic()
        except (SiteBlocked, RequestServiceError) as exc:
            logging.error("%s", exc)
            state = "blocked" if isinstance(exc, SiteBlocked) else "error"
            write_status(status_path, {"last_checked": beijing_iso(), "state": state, "message": str(exc), "products": []})
            for role in active_due:
                update_module_status(status_path, "monitor" if role == "watch" else "notify", module_schedule(config, "monitor" if role == "watch" else "notify"), "error", str(exc))
            if bot_token and chat_id:
                try:
                    telegram_request(session, bot_token, "sendMessage", chat_id=chat_id, text=f"🔴 库存监控已停止\n原因：{exc}\n请确认目标网站允许当前的自动检查方式后再恢复。")
                except MonitorError as notify_error:
                    logging.error("发送停止告警失败：%s", notify_error)
            return 0
        except (MonitorError, requests.RequestException) as exc:
            logging.error("监控失败：%s", exc)
            write_status(status_path, {"last_checked": beijing_iso(), "state": "error", "message": str(exc), "products": []})
            for role in active_due:
                update_module_status(status_path, "monitor" if role == "watch" else "notify", module_schedule(config, "monitor" if role == "watch" else "notify"), "error", str(exc))
            return 1

        if args.once:
            return 0


if __name__ == "__main__":
    sys.exit(main())
