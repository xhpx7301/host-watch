from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from admin import effective_telegram, validate_config


ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state.json"
CONFIG_PATH = ROOT / "config.json"
STATUS_PATH = ROOT / "status.json"
MIN_INTERVAL_SECONDS = 300


class MonitorError(RuntimeError):
    """A page or notification response could not be trusted."""


class SiteBlocked(MonitorError):
    """The site denied automated access; stop instead of retrying."""


@dataclass(frozen=True)
class Product:
    key: str
    name: str
    available: int
    url: str
    card_id: str = ""
    vendor: str = "VMISS"
    stock_mode: str = "quantity"


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
        products.append(Product(key, name, int(match.group(1).replace(",", "")), product_url, product_id))

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
        "cf-chl-",
        "enable javascript and cookies to continue",
    )
    lowered = body.casefold()
    return any(marker in lowered for marker in blocked_markers)


def _validate_page_response(status_code: int, body: str, final_url: str, expected_host: str = "app.vmiss.com") -> tuple[str, str]:
    if status_code in (403, 429) or _blocked_body(body):
        raise SiteBlocked(f"{expected_host} 返回 HTTP {status_code} 或安全验证页面；监控已停止，请确认目标网站允许自动检查")
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
        raise SiteBlocked(f"访问 {expected_host} 商品页失败，监控已停止；请检查网络并确认目标网站允许自动检查") from None
    return _validate_page_response(response.status_code, response.text, response.url, expected_host)


def fetch_with_flaresolverr(session: requests.Session, url: str, fetcher: dict) -> tuple[str, str]:
    endpoint = str(fetcher["flaresolverr_url"])
    timeout_seconds = int(fetcher["timeout_seconds"])
    try:
        response = session.post(
            endpoint,
            json={"cmd": "request.get", "url": url, "maxTimeout": timeout_seconds * 1000},
            timeout=(10, timeout_seconds + 15),
        )
        payload = response.json()
    except (requests.RequestException, ValueError):
        raise SiteBlocked("FlareSolverr 请求失败，监控已停止；请检查 FlareSolverr 服务") from None
    if not response.ok or not isinstance(payload, dict) or payload.get("status") != "ok":
        raise SiteBlocked("FlareSolverr 返回异常，监控已停止；请检查 FlareSolverr 服务日志")
    solution = payload.get("solution")
    if not isinstance(solution, dict):
        raise SiteBlocked("FlareSolverr 响应缺少页面结果，监控已停止")
    body = solution.get("response")
    final_url = solution.get("url")
    status_code = solution.get("status")
    if not isinstance(body, str) or not isinstance(final_url, str) or not isinstance(status_code, int):
        raise SiteBlocked("FlareSolverr 响应格式异常，监控已停止")
    user_agent = solution.get("userAgent")
    if isinstance(user_agent, str) and user_agent.strip() and len(user_agent) <= 512:
        session.headers["User-Agent"] = user_agent.strip()
    cookies = solution.get("cookies", [])
    if isinstance(cookies, list):
        for cookie in cookies:
            if not isinstance(cookie, dict):
                continue
            name, value = cookie.get("name"), cookie.get("value")
            if not isinstance(name, str) or not isinstance(value, str) or not name:
                continue
            try:
                session.cookies.set(name, value, domain=cookie.get("domain"), path=cookie.get("path", "/"))
            except (TypeError, ValueError):
                continue
    return _validate_page_response(status_code, body, final_url)


def fetch_products(session: requests.Session, url: str, selectors: list[str], fetcher: dict | None = None) -> list[Product]:
    fetcher = fetcher or {"mode": "requests"}
    if fetcher.get("mode") == "flaresolverr":
        body, final_url_text = fetch_with_flaresolverr(session, url, fetcher)
    else:
        body, final_url_text = fetch_with_requests(session, url)
    final_url = urlsplit(final_url_text)
    if final_url.scheme != "https" or final_url.hostname != "app.vmiss.com":
        raise MonitorError("商品页重定向离开 app.vmiss.com；已停止，未处理页面内容")
    return parse_products(body, final_url_text, selectors)


def fetch_html_stock(session: requests.Session, item: dict) -> Product:
    """Read one configured public HTML stock indicator without bypass services."""
    url = str(item["url"])
    expected_host = (urlsplit(url).hostname or "").casefold()
    body, final_url = fetch_with_requests(session, url, expected_host)
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


def affiliate_url(product_url: str, affiliate_id: str, gid: str, pid: str) -> str:
    parsed = urlsplit(product_url)
    query = {"aff": affiliate_id, "pid" if pid else "gid": pid or gid}
    return urljoin(f"{parsed.scheme}://{parsed.netloc}/", "aff.php") + "?" + urlencode(query)


def read_config(path: Path) -> dict:
    if not path.exists():
        return {
            "enabled": False,
            "interval_seconds": 600,
            "notify_current_stock_on_start": False,
            "affiliate_id": "",
            "fetcher": {
                "mode": "requests",
                "flaresolverr_url": "http://127.0.0.1:8191/v1",
                "timeout_seconds": 60,
            },
            "telegram": {"bot_token": "", "chat_id": "", "disabled": False},
            "products": [],
        }
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
    products = [item for item in config.get("products", []) if item.get("enabled")]
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
        elif item.get("adapter") == "html":
            url = str(item.get("url", ""))
            if urlsplit(url).scheme != "https" or not str(item.get("stock_selector", "")).strip():
                raise MonitorError(f"商品 {name} 的通用网页配置不完整")
        else:
            raise MonitorError(f"商品 {name} 的监控适配器无效")
    return products


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
        response = session.post(endpoint, json=kwargs, timeout=(10, 25)) if method == "sendMessage" else session.get(endpoint, params=kwargs, timeout=(10, 25))
        response.raise_for_status()
        result = response.json()
    except (requests.RequestException, ValueError) as exc:
        # Never print an exception containing the Bot API URL (it includes the token).
        raise MonitorError(f"Telegram {method} 请求失败：{type(exc).__name__}") from None
    if not result.get("ok"):
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


def notification_text(product: Product, old_count: int | None, purchase_url: str) -> str:
    if product.stock_mode == "availability":
        increase = "（由缺货变为有货）" if old_count is not None else "（首次运行时已有库存）"
        stock = "有货"
    else:
        increase = f"（增加 {product.available - old_count} 台）" if old_count is not None else "（首次运行时已有库存）"
        stock = f"{product.available} 台"
    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    return (
        f"🟢 {product.vendor} 检测到库存变化\n"
        f"套餐：{product.name}\n"
        f"库存：{stock} {increase}\n"
        f"手动购买：{purchase_url}\n"
        f"检查时间：{now}"
    )


def check_once(
    session: requests.Session,
    config: dict,
    state_path: Path,
    status_path: Path,
    bot_token: str,
    chat_id: str,
) -> None:
    selected = selected_products(config)
    if not selected:
        raise MonitorError("没有启用的监控产品")

    by_category: dict[str, list[dict]] = {}
    for item in selected:
        if item["adapter"] != "vmiss":
            continue
        by_category.setdefault(item["category_url"].strip(), []).append(item)
    found: dict[str, Product] = {}
    for category_url, items in by_category.items():
        page_products = fetch_products(session, category_url, [], config.get("fetcher"))
        for item in items:
            selector = str(item["selector"]).strip().casefold()
            candidates = [p for p in page_products if selector in {
                p.key.casefold(),
                p.key.rstrip("/").rsplit("/", 1)[-1].casefold(),
                p.name.casefold(),
                p.card_id.casefold(),
            }]
            if len(candidates) != 1:
                raise MonitorError(f"商品 {item.get('name', selector)} 无法唯一匹配页面套餐；请检查 selector")
            matched = candidates[0]
            found[item["id"]] = Product(
                key=matched.key,
                name=matched.name,
                available=matched.available,
                url=matched.url,
                card_id=matched.card_id,
                vendor=item["provider"],
                stock_mode="quantity",
            )
    for item in selected:
        if item["adapter"] == "html":
            found[item["id"]] = fetch_html_stock(session, item)

    previous = read_state(state_path)
    current = dict(previous)
    notifications: list[tuple[dict, Product, int | None]] = []
    for item in selected:
        product = found[item["id"]]
        old = previous.get(item["id"])
        old_count = old.get("available") if old else None
        if product.available > 0 and (
            (old is None and config.get("notify_current_stock_on_start", False))
            or (old is not None and isinstance(old_count, int) and product.available > old_count)
        ):
            notifications.append((item, product, old_count))
        current[item["id"]] = {
            "name": product.name,
            "vendor": item["provider"],
            "available": product.available,
            "url": product.url,
        }

    if notifications and bot_token and chat_id:
        for item, product, old_count in notifications:
            purchase_url = (
                affiliate_url(product.url, str(config.get("affiliate_id", "")), str(item.get("gid", "")), str(item.get("pid", "")))
                if item["adapter"] == "vmiss"
                else (item.get("purchase_url") or product.url)
            )
            button_text = "🛒 打开购买页面" if item["adapter"] == "html" else "🛒 打开 VMISS 购买页"
            reply_markup = {"inline_keyboard": [[{"text": button_text, "url": purchase_url}]]}
            telegram_request(
                session,
                bot_token,
                "sendMessage",
                chat_id=chat_id,
                text=notification_text(product, old_count, purchase_url),
                disable_web_page_preview=True,
                reply_markup=reply_markup,
            )
            logging.info("已发送补货通知：%s，目前 %s 台", product.name, product.available)
    elif notifications and not config.get("telegram", {}).get("disabled", False):
        for _, product, _ in notifications:
            logging.warning("发现库存增加但未设置 Telegram：%s，目前 %s 台，%s", product.name, product.available, product.url)

    write_state(state_path, current)
    summary = "；".join(f"{item['provider']} / {found[item['id']].name}: {found[item['id']].available}" for item in selected)
    write_status(status_path, {
        "last_checked": datetime.now().astimezone().isoformat(timespec="seconds"),
        "state": "ok",
        "message": "检查完成",
        "products": [
            {
                "id": item["id"],
                "name": found[item["id"]].name,
                "vendor": item["provider"],
                "available": found[item["id"]].available,
                "stock_mode": item.get("stock_mode", "quantity"),
                "url": found[item["id"]].url,
                "purchase_url": (
                    affiliate_url(found[item["id"]].url, str(config.get("affiliate_id", "")), str(item.get("gid", "")), str(item.get("pid", "")))
                    if item["adapter"] == "vmiss"
                    else (item.get("purchase_url") or found[item["id"]].url)
                ),
            }
            for item in selected
        ],
    })
    logging.info("检查成功（%s 个套餐）：%s", len(selected), summary)


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="低频监控多个厂商的商品库存，并在库存增加时通过 Telegram 通知。")
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
            if not telegram_config.get("disabled", False) and (not bot_token or not chat_id):
                raise MonitorError("Telegram bot token 和 chat_id 必须同时填写，或在管理页停用 Telegram")
            interval = int(config.get("interval_seconds", 600))
            if interval < MIN_INTERVAL_SECONDS:
                raise MonitorError(f"检查间隔不能低于 {MIN_INTERVAL_SECONDS} 秒")
            check_once(session, config, state_path, status_path, bot_token, chat_id)
        except SiteBlocked as exc:
            logging.error("%s", exc)
            write_status(status_path, {"last_checked": datetime.now().astimezone().isoformat(timespec="seconds"), "state": "blocked", "message": str(exc), "products": []})
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
