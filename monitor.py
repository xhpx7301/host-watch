from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from admin import default_config, effective_telegram, normalize_cloudscraper, normalize_vps_soy_url, validate_config
from affiliates import affiliate_purchase_url
from notifications import purchase_button, stock_message
import egress


ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state.json"
CONFIG_PATH = ROOT / "config.json"
STATUS_PATH = ROOT / "status.json"


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
    price = " / ".join(n.get_text(" ", strip=True) for n in prices)
    if not price and soy_price:
        price = soy_price.parent.get_text(" ", strip=True)
    return {"region": region[:500], "specs": " · ".join(hardware)[:500], "traffic": " · ".join(network)[:500], "price": price[:500]}


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
        title_el = card.select_one(".package-title")
        qty_el = card.select_one(".package-qty")
        order_el = card.select_one("a.btn-order-now[href]")
        if not title_el or not qty_el or not order_el:
            raise MonitorError("有商品卡片缺少名称、库存或订购链接；本轮不更新状态")

        name = " ".join(title_el.get_text(" ", strip=True).split())
        qty_text = " ".join(qty_el.get_text(" ", strip=True).split())
        match = re.fullmatch(r"([\d,]+)\s+Available", qty_text, flags=re.IGNORECASE)
        if not name or not match:
            raise MonitorError(f"无法识别套餐 {name or '(无名称)'} 的库存文本：{qty_text!r}")

        product_url = urljoin(page_url, order_el["href"])
        parsed_product_url = urlsplit(product_url)
        if parsed_product_url.scheme != "https" or parsed_product_url.hostname != page_host:
            raise MonitorError(f"套餐 {name} 的购买链接不在本站；拒绝将 aff 参数附加到外站链接")
        # The title can repeat for variants, so use its order URL as the stable key.
        key = parsed_product_url.path
        product_id = (card.get("id") or "").strip()
        slug = parsed_product_url.path.rstrip("/").rsplit("/", 1)[-1]
        identifiers = {key.casefold(), slug.casefold(), product_id.casefold(), name.casefold()}
        matches = identifiers.intersection(selector.casefold() for selector in selectors)
        matched_selectors.update(matches)
        if selectors and not matches:
            continue
        products.append(Product(key, name, int(match.group(1).replace(",", "")), product_url, product_id, **html_metadata(card, soup.select_one("h1").get_text(" ", strip=True) if soup.select_one("h1") else "")))

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
        proxy = (fetcher or {}).get("egress_proxy_url", "")
        if proxy:
            scraper.proxies = {"http": proxy, "https": proxy}
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
    """Serialize Chromium across the admin and monitor services on Debian."""
    if sys.platform != "linux":
        yield
        return
    import fcntl
    lock_path = Path(os.getenv("VMISS_CONFIG", str(CONFIG_PATH))).parent / ".browser-request.lock"
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            raise RequestServiceError("另一个浏览器请求正在执行，请等待完成或停止测试后重试") from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


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


def _fetch_with_flaresolverr(session: requests.Session, url: str, fetcher: dict, expected_host: str) -> tuple[str, str]:
    endpoint = str(fetcher["flaresolverr_url"])
    timeout_seconds = int(fetcher["timeout_seconds"])
    wait = int(fetcher.get("wait_seconds", 0))
    request = {"cmd": "request.get", "url": url, "maxTimeout": timeout_seconds * 1000}
    if fetcher.get("_test_browser_session"):
        request["session"] = fetcher["_test_browser_session"]
    if wait:
        request["waitInSeconds"] = wait
    if fetcher.get("egress_proxy_url"):
        request["proxy"] = fetcher["egress_proxy_url"]
    try:
        request_kwargs = {"json": request, "timeout": (10, timeout_seconds + wait + 15)}
        if fetcher.get("egress_proxy_url"):
            request_kwargs["proxies"] = {}
        response = session.post(endpoint, **request_kwargs)
        payload = response.json()
    except requests.Timeout:
        raise RequestServiceError("FlareSolverr 请求超时；请检查服务器资源、目标站点验证和超时设置") from None
    except requests.RequestException:
        raise RequestServiceError("无法连接 FlareSolverr；请在网页测试服务连接，或检查容器日志") from None
    except ValueError:
        raise RequestServiceError("FlareSolverr 返回非 JSON 响应；请核对 API 地址是否以 /v1 结尾") from None
    if not isinstance(payload, dict):
        raise RequestServiceError(f"FlareSolverr API 返回异常（HTTP {response.status_code}）；请检查服务日志")
    if payload.get("status") != "ok":
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
    # Parse the browser's HTML directly; cookies and User-Agent need not be copied
    # into the shared HTTP session used for other vendors and Telegram.
    page_headers = solution.get("headers")
    return _validate_page_response(status_code, body, final_url, expected_host, page_headers if isinstance(page_headers, dict) else {})


def product_fetcher(fetcher: dict | None, item: dict) -> dict:
    result = dict(fetcher or {"mode": "requests"})
    # An explicit merchant policy also overrides old per-product choices.
    # Missing policies preserve legacy configurations until the user unifies them.
    mode = result.get("provider_modes", {}).get(item.get("adapter", "html"), item.get("request_mode", "inherit"))
    if mode != "inherit":
        result["mode"] = mode
    node = egress.resolve(result.get("egress", {}), item.get("adapter", "html"), str(item.get("egress_node_id", ""))) if result.get("egress") else None
    if node:
        result["egress_proxy_url"] = egress.connect_url(node)
        result["egress_node_id"] = node["id"]
    return result


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
        price="；".join(f"¥{p['price'] / 100:g} / {p['cycle']}个月" for p in (plan.get('price_datas') or []) if type(p.get('price')) in {int, float} and type(p.get('cycle')) is int and p['cycle'] > 0)[:500])


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
        price="；".join(f"{p.get('currency_code', '')} {p['price']:g} / {plan.get('name', '')}" for plan in isvoro_list(raw_product.get('plans')) if plan.get('hidden') is not True for p in (plan.get('prices') or []) if isinstance(p, dict) if type(p.get('price')) in {int, float})[:500], match_details={"type_id": str(kind["id"]), "type_name": kind["name"], "category_id": str(line["id"]), "category_name": line["name"], "product_id": str(selected["id"]), "plan_name": detail["name"]})


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
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def selected_products(config: dict) -> list[dict]:
    products = [item for item in config.get("products", []) if product_membership(item, "watch") or product_membership(item, "notify")]
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
    payload = {"updated_at": datetime.now().astimezone().isoformat(timespec="seconds"), "products": state}
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def telegram_request(session: requests.Session, token: str, method: str, **kwargs) -> dict:
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
        checked_at=datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"))


def collect_inventory(session, config, selected, found, unavailable):
    by_category: dict[tuple[str, str], list[dict]] = {}
    for item in selected:
        if item["adapter"] != "vmiss":
            continue
        mode = product_fetcher(config.get("fetcher"), item).get("mode", "requests")
        by_category.setdefault((item["category_url"].strip(), mode), []).append(item)
    for (category_url, mode), items in by_category.items():
        page_products = fetch_products(session, category_url, [], {**config.get("fetcher", {}), "mode": mode})
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
            found[item["id"]] = replace(matched, vendor=item["provider"])
    soy_pages = {}
    akile_catalogs = {}
    isvoro_catalogs = {}
    dmit_catalogs = {}
    bwh_catalogs = {}
    for item in selected:
        try:
            if item["adapter"] == "html":
                found[item["id"]] = fetch_html_stock(session, item, config.get("fetcher"))
            elif item["adapter"] == "dmit":
                from dmit import inspect
                found[item["id"]] = inspect(session, item, product_fetcher(config.get("fetcher"), item), dmit_catalogs)
            elif item["adapter"] == "bwh":
                from bwh import inspect
                found[item["id"]] = inspect(session, item, product_fetcher(config.get("fetcher"), item), bwh_catalogs)
            elif item["adapter"] == "vps_soy":
                fetcher = product_fetcher(config.get("fetcher"), item)
                key = (normalize_vps_soy_url(item["url"]), fetcher.get("mode", "requests"))
                if key not in soy_pages:
                    soy_pages[key] = fetch_vps_soy_page(session, key[0], fetcher)
                body, final_url = soy_pages[key]
                found[item["id"]] = parse_vps_soy(body, final_url, item["selector"])
            elif item["adapter"] == "akile":
                fetcher = product_fetcher(config.get("fetcher"), item)
                mode = fetcher.get("mode", "requests")
                if mode not in akile_catalogs:
                    akile_catalogs[mode] = fetch_akile_catalog(session, fetcher)
                found[item["id"]] = parse_akile(akile_catalogs[mode], item)
            elif item["adapter"] == "isvoro":
                found[item["id"]] = inspect_isvoro(session, item, product_fetcher(config.get("fetcher"), item), isvoro_catalogs)
        except ProductUnlisted as exc:
            unavailable[item["id"]] = str(exc) + "；可能已下架或配置不匹配，保留配置等待重新上架"


def check_once(
    session: requests.Session,
    config: dict,
    state_path: Path,
    status_path: Path,
    bot_token: str,
    chat_id: str,
) -> None:
    selected = selected_products(config)
    if not selected and not config.get("isvoro_seckill", False):
        raise MonitorError("没有启用的监控产品")

    seckill_status = None
    if config.get("isvoro_seckill", False):
        from seckill import check_once as check_seckill
        try:
            seckill_status = {"ok": True, **check_seckill(session, config, state_path, bot_token, chat_id)}
        except (SiteBlocked, RequestServiceError):
            raise
        except (MonitorError, requests.RequestException) as exc:
            seckill_status = {"ok": False, "error": str(exc)}
        seckill_status["checked_at"] = datetime.now().astimezone().isoformat(timespec="seconds")

    from stock_alerts import StockAlerts
    previous = read_state(state_path)
    current = {key: dict(value) for key, value in previous.items()}
    alerts = StockAlerts(sys.modules[__name__], session, config, state_path, current, bot_token, chat_id)
    found, unavailable = {}, {}
    try:
        collect_inventory(session, config, selected, found, unavailable)
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
    checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
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
                     **{field: getattr(product, field) for field in ("region", "specs", "traffic", "price")}})
    write_status(status_path, {
        "last_checked": checked_at, "state": "partial" if unavailable or notification_errors or alerts.warnings or seckill_status and not seckill_status["ok"] else "ok",
        "message": (f"{len(unavailable)} 个商品未匹配，保留配置并继续检查其他商品：" + "；".join(f"{item['name']}：{unavailable[item['id']]}" for item in selected if item['id'] in unavailable) if unavailable else "检查完成") + (f"；秒杀检查失败：{seckill_status['error']}" if seckill_status and not seckill_status["ok"] else "") + ("；Telegram 通知待重试：" + "；".join(notification_errors) if notification_errors else "") + ("；" + "；".join(alerts.warnings) if alerts.warnings else ""),
        "products": rows,
        **({"seckill": seckill_status} if seckill_status is not None else {}),
    })
    logging.info("本轮检查完成：%s 个成功，%s 个未匹配", len(found), len(unavailable))


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="低频监控多个厂商的商品库存，每轮补货发送一次 Telegram 通知并更新原消息。")
    parser.add_argument("--once", action="store_true", help="只检查一次后退出")
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

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
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

    while True:
        try:
            config = read_config(config_path)
            if not config.get("enabled", False):
                logging.info("监控总开关关闭，不访问商品网站")
                return 0
            bot_token, chat_id = effective_telegram(config)
            telegram_config = config.get("telegram", {})
            if not any(product_membership(p, "notify") for p in config.get("products", [])) and not config.get("isvoro_seckill", False):
                bot_token, chat_id = "", ""
                telegram_config = {"disabled": True}
            if not telegram_config.get("disabled", False) and (not bot_token or not chat_id):
                raise MonitorError("Telegram bot token 和 chat_id 必须同时填写，或在管理页停用 Telegram")
            interval = int(config.get("interval_seconds", 600))
            if interval < 0:
                raise MonitorError("检查间隔不能为负数")
            check_once(session, config, state_path, status_path, bot_token, chat_id)
        except (SiteBlocked, RequestServiceError) as exc:
            logging.error("%s", exc)
            state = "blocked" if isinstance(exc, SiteBlocked) else "error"
            write_status(status_path, {"last_checked": datetime.now().astimezone().isoformat(timespec="seconds"), "state": state, "message": str(exc), "products": []})
            if bot_token and chat_id:
                try:
                    telegram_request(session, bot_token, "sendMessage", chat_id=chat_id, text=f"🔴 库存监控已停止\n原因：{exc}\n请确认目标网站允许当前的自动检查方式后再恢复。")
                except MonitorError as notify_error:
                    logging.error("发送停止告警失败：%s", notify_error)
            return 0
        except (MonitorError, requests.RequestException) as exc:
            logging.error("监控失败：%s", exc)
            write_status(status_path, {"last_checked": datetime.now().astimezone().isoformat(timespec="seconds"), "state": "error", "message": str(exc), "products": []})
            return 1

        if args.once:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
