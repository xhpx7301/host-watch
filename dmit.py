"""Read DMIT's public pricing/category cards without visiting cart mutation URLs."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

PRICING_URL = "https://www.dmit.io/pages/pricing"
HOST = "www.dmit.io"
REGIONS = {"LAX": "美国 · 洛杉矶", "HKG": "中国香港", "TYO": "日本 · 东京", "SJC": "美国 · 圣何塞"}
PLAN_KEY_PATTERN = r"[A-Z0-9]{2,10}:[A-Z0-9]{2,10}:[A-Z0-9]{2,20}:[A-Z0-9_. -]{1,100}"


def pricing_cards(soup, page_url: str) -> list[dict]:
    """DMIT's current pricing theme uses static onclick buttons and icon fields.

    Sold-out cards omit PID entirely. Keep a separate stable plan identity so
    those exact variants can be followed until their purchase PID reappears.
    """
    from monitor import MonitorError
    rows = []
    for group in soup.select(".plan-group[data-loc][data-hw][data-net]"):
        loc, hw, net = (str(group[key]).upper() for key in ("data-loc", "data-hw", "data-net"))
        for card in group.select(".plan-card"):
            short = text(card.select_one(".plan-card-name"))
            plan_key = ":".join((loc, hw, net, short.upper()))
            if not re.fullmatch(PLAN_KEY_PATTERN, plan_key):
                raise MonitorError("DMIT 价格卡片缺少可靠的地区、平台、线路或套餐标识")
            buttons = card.select(".plan-card-action .btn-order")
            pid, available, error = "", None, ""
            if len(buttons) == 1:
                button = buttons[0]
                label = text(button)
                disabled = button.has_attr("disabled") or button.get("aria-disabled") == "true" or "btn-order-disabled" in button.get("class", [])
                # Parse the known literal assignment without executing JavaScript.
                pattern = r"(?:if\s*\(!window\.__cfRLUnblockHandlers\)\s*return\s+false;\s*)?window\.location\.href\s*=\s*(['\"])(/cart\.php\?a=add&pid=[1-9][0-9]{0,9})\1\s*;?"
                onclick = re.fullmatch(pattern, str(button.get("onclick", "")).strip())
                if onclick:
                    pid = link_pid(onclick[2], page_url)
                if disabled and re.fullmatch(r"Out of Stock|Sold Out|缺货|售罄", label, re.I):
                    available = 0
                elif not disabled and pid and re.fullmatch(r"Order Now|Buy Now|立即购买|立即订购", label, re.I):
                    # This theme explicitly swaps the same button to Out of
                    # Stock. This is the pricing page's availability indicator,
                    # not a generic link somewhere else in the page.
                    available = 1
                else:
                    error = "DMIT 价格卡片的订购状态无法确认"
            else:
                error = "DMIT 价格卡片缺少唯一状态按钮"
            values = {}
            for node in card.select(".plan-spec"):
                for icon, key in (("fa-microchip", "cpu"), ("fa-memory", "ram"), ("fa-hdd", "disk"), ("fa-exchange-alt", "traffic"), ("fa-bolt", "bandwidth")):
                    if node.select_one("." + icon):
                        values[key] = text(node)
            cpu = re.fullmatch(r"([0-9]+)\s*vCores?", values.get("cpu", ""), re.I)
            specs = (["CPU：" + cpu[1] + " 核"] if cpu else [])
            specs += [label + "：" + values[key] for key, label in (("ram", "内存"), ("disk", "硬盘")) if values.get(key)]
            network = [label + "：" + values[key] for key, label in (("traffic", "流量"), ("bandwidth", "带宽")) if values.get(key)]
            line = {"PREMIUM": "Pro", "EYEBALL": "EB", "TIER1": "T1"}.get(net, net)
            name = f"{loc}.{hw}.{line}.{short}"
            rows.append({"pid": pid, "plan_key": plan_key, "name": name, "region": REGIONS.get(loc, loc), "line": f"{hw} / {line}", "specs": " · ".join(specs), "traffic": " · ".join(network), "price": text(card.select_one(".plan-card-price")), "available": available, "stock_mode": "availability", "error": error, "purchase_url": f"https://{HOST}/cart.php?a=add&pid={pid}" if pid else PRICING_URL})
    if len({r["plan_key"] for r in rows}) != len(rows) or len({r["pid"] for r in rows if r["pid"]}) != sum(bool(r["pid"]) for r in rows):
        raise MonitorError("DMIT 价格卡片出现重复 PID 或套餐标识，未更新库存")
    return rows


def normalize_catalog_url(raw: object = PRICING_URL) -> str:
    url = urlsplit(str(raw).strip())
    if url.scheme != "https" or url.hostname != HOST or url.username or url.password or url.port not in (None, 443) or url.fragment:
        raise ValueError("DMIT 分类页必须是 www.dmit.io 的 HTTPS 地址")
    query = parse_qs(url.query, keep_blank_values=True)
    if any(len(values) != 1 for values in query.values()) or any(key not in {"gid", "currency", "language"} for key in query):
        raise ValueError("DMIT 只能读取价格/分类页，不能使用加购物车或结账网址")
    path = url.path.rstrip("/")
    if path not in {"/pages/pricing", "/cart.php"} and not re.fullmatch(r"/store/[A-Za-z0-9_/-]+", path):
        raise ValueError("DMIT 需要价格页、cart.php?gid=分类ID 或 /store/ 分类页")
    for key in ("gid", "currency"):
        if key in query and not re.fullmatch(r"[1-9][0-9]{0,9}", query[key][0]):
            raise ValueError("DMIT 分类/货币 ID 必须是正整数")
    if path == "/cart.php" and "gid" not in query:
        raise ValueError("DMIT cart.php 分类页必须指定 gid")
    if "language" in query and not re.fullmatch(r"[a-zA-Z_-]{1,30}", query["language"][0]):
        raise ValueError("DMIT 语言参数无效")
    return urlunsplit(("https", HOST, path, urlencode(sorted((k, v[0]) for k, v in query.items())), ""))


def text(node) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def link_pid(href: str, base: str) -> str:
    try:
        url = urlsplit(urljoin(base, href))
        port = url.port
    except ValueError:
        return ""
    query = parse_qs(url.query)
    if url.scheme != "https" or url.hostname != HOST or url.username or url.password or port not in (None, 443):
        return ""
    values = query.get("pid", [])
    if url.path != "/cart.php" or query.get("a") != ["add"] or len(values) != 1:
        return ""
    return values[0] if re.fullmatch(r"[1-9][0-9]{0,9}", values[0]) else ""


def metadata(card, name: str) -> dict:
    raw = text(card)
    specs, network = [], []
    patterns = [
        (specs, "CPU", r"(\d+)\s*(?:v?CPU|cores?|核)\b"),
        (specs, "内存", r"(\d+(?:\.\d+)?\s*[MG]i?B)\s*(?:RAM|Memory|内存)"),
        (specs, "硬盘", r"(\d+(?:\.\d+)?\s*[GT]i?B)\s*(?:(?:NVMe|SSD|HDD)\s*)?(?:Storage|Disk|硬盘|SSD|NVMe)"),
        (network, "流量", r"(\d+(?:\.\d+)?\s*[TGM]i?B)\s*(?:Transfer|Traffic|Bandwidth|流量)"),
        (network, "带宽", r"(\d+(?:\.\d+)?\s*[GMK]?bps)\b"),
    ]
    for target, label, pattern in patterns:
        match = re.search(pattern, raw, re.I)
        if not match:
            aliases = {"CPU": r"CPU|vCPU", "内存": r"RAM|Memory|内存", "硬盘": r"Storage|Disk|硬盘", "流量": r"Transfer|Traffic|流量", "带宽": r"Port|Bandwidth|带宽"}[label]
            value = r"\d+" if label == "CPU" else r"\d+(?:\.\d+)?\s*[MG]i?B" if label == "内存" else r"\d+(?:\.\d+)?\s*[TGM]i?B" if label in {"硬盘", "流量"} else r"\d+(?:\.\d+)?\s*[GMK]?bps"
            match = re.search(r"(?:" + aliases + r")\s*[:：]?\s*(" + value + r")\b", raw, re.I)
        if match:
            target.append(label + "：" + match[1] + (" 核" if label == "CPU" else ""))
    if re.search(r"(?:unlimited|unmetered)\s*(?:traffic|transfer|bandwidth)|(?:traffic|transfer)\s*[:：]?\s*(?:unlimited|unmetered)", raw, re.I):
        network = [field for field in network if not field.startswith("流量：")] + ["流量：无限制"]
    price_nodes = card.select(".price-amount, .price-cycle") or card.select(".pricing-price, .product-price, .price")
    price = " / ".join(dict.fromkeys(text(n) for n in price_nodes))
    if not price:
        match = re.search(r"(?:USD\s*|US\$|\$|¥|€|£)\s*\d+(?:\.\d+)?(?:\s*(?:USD)?\s*(?:/|per)\s*(?:mo(?:nth)?|yr|year|月|年))?", raw, re.I)
        price = match[0] if match else ""
    code = name.split(".")[0].upper()
    line = re.search(r"(?:^|[.\s])(Pro|EB|Premium|Eyeball|Tier\s*1)(?=[.\s]|$)", name, re.I)
    return {"region": REGIONS.get(code, ""), "line": line[1] if line else "", "specs": " · ".join(specs), "traffic": " · ".join(network), "price": price[:500]}


def parse_cards(body: str, page_url: str) -> list[dict]:
    from monitor import MonitorError, SiteBlocked, _blocked_body
    if _blocked_body(body):
        raise SiteBlocked("DMIT 返回安全验证页，未更新库存")
    soup = BeautifulSoup(body, "html.parser")
    if soup.select_one(".plan-group .plan-card"):
        rows = pricing_cards(soup, page_url)
        if not rows:
            raise MonitorError("DMIT 价格卡片缺少分类信息，未更新库存")
        return rows
    candidates = {}
    for card in soup.select('[id^="product"], [data-pid], .package, .pricing-table, .pricing-item, .plan, .product'):
        pid = str(card.get("data-pid", ""))
        match = re.fullmatch(r"product([1-9][0-9]{0,9})", str(card.get("id", "")))
        if match:
            pid = match[1]
        ids = {link_pid(a["href"], page_url) for a in card.select("a[href]")} - {""}
        if len(ids) == 1 and not pid:
            pid = next(iter(ids))
        if re.fullmatch(r"[1-9][0-9]{0,9}", pid) and (not ids or ids == {pid}):
            candidates.setdefault(pid, []).append(card)
    # Custom pricing themes can wrap each purchase link in a plain column.
    for anchor in soup.select('a[href]'):
        pid = link_pid(anchor["href"], page_url)
        if not pid or pid in candidates:
            continue
        for parent in anchor.parents:
            ids = {link_pid(a["href"], page_url) for a in parent.select("a[href]")} - {""}
            if ids != {pid}:
                break
            if parent.select_one("h2, h3, h4, .package-title, .product-title"):
                candidates[pid] = [parent]
                break
    rows = []
    for pid, cards in candidates.items():
        # Nested wrappers describe one card, but separate cards sharing an ID
        # must not be silently merged (e.g. different billing variants).
        leaf = [c for c in cards if not any(c is not other and any(p is c for p in other.parents) for other in cards)]
        if len(leaf) != 1:
            raise MonitorError(f"DMIT PID={pid} 出现重复商品卡片，无法唯一匹配")
        card = leaf[0]
        title = card.select_one(".package-title, .product-title, .plan-title, .pricing-title, h3, h4, h2")
        name = text(title)
        if not name:
            raise MonitorError(f"DMIT PID={pid} 缺少套餐名称，未更新目录")
        info = metadata(card, name)
        stock_nodes = card.select(".package-qty, .qty, .stock, .stock-status, .availability, .out-of-stock, .sold-out")
        stock = " ".join(text(n) for n in stock_nodes)
        buttons = card.select("a, button, input[type=submit]")
        labels = " ".join(text(n) or str(n.get("value", "")) for n in buttons)
        sold = bool(re.search(r"out\s+of\s+stock|sold\s*out|unavailable|缺货|售罄|无货|售完|暂时缺货", stock + " " + labels, re.I))
        orders = [a for a in card.select("a[href]") if link_pid(a["href"], page_url) == pid and not a.has_attr("disabled") and a.get("aria-disabled") != "true" and "disabled" not in a.get("class", []) and re.search(r"order|buy|purchase|configure|购买|订购|立即开通|立即订阅", text(a), re.I)]
        quantity = re.fullmatch(r"(?:Stock\s*[:：]?\s*|库存\s*[:：]?\s*)?((?:\d{1,3}(?:,\d{3})+|\d+))(?:\s*(?:Available|in stock|台|件))?", stock, re.I) if stock else None
        available, mode, error = None, "availability", ""
        if quantity:
            count = int(quantity[1].replace(",", ""))
            if (count > 0 and sold) or (count == 0 and orders):
                error = "库存数量与购买状态冲突"
            else:
                available, mode = count, "quantity"
        elif sold and orders:
            error = "缺货标记与购买入口冲突"
        elif sold:
            available = 0
        elif orders:
            if re.fullmatch(r"in\s+stock|available|有货|现货|库存充足", stock, re.I):
                available = 1
            else:
                error = "缺少明确库存状态，购买链接不能证明有货"
        else:
            error = "缺少明确库存或可用订购入口，无法判断库存"
        rows.append({"pid": pid, "name": name[:120], "available": available, "stock_mode": mode, "error": error, "purchase_url": f"https://{HOST}/cart.php?a=add&pid={pid}", **info})
    return rows


def fetch_catalog(session, url: str, fetcher: dict) -> tuple[str, str]:
    from monitor import MonitorError, fetch_page
    expected = normalize_catalog_url(url)
    body, final = fetch_page(session, expected, fetcher, HOST)
    try:
        actual = normalize_catalog_url(final)
    except ValueError as exc:
        raise MonitorError("DMIT 返回了非目录页面，未更新库存") from exc
    if actual != expected:
        raise MonitorError("DMIT 返回分类与请求不一致，未更新库存")
    return body, final


def inspect(session, item: dict, fetcher: dict, cache: dict | None = None):
    from monitor import MonitorError, Product, ProductUnlisted
    url = normalize_catalog_url(item.get("category_url", PRICING_URL))
    key = (url, fetcher.get("mode", "requests"))
    if cache is None:
        cache = {}
    if key not in cache:
        body, final = fetch_catalog(session, url, fetcher)
        cache[key] = parse_cards(body, final)
        if not cache[key]:
            raise MonitorError("DMIT 页面没有可识别的商品卡片；请核对分类页或浏览器渲染设置")
    matches = [row for row in cache[key] if row.get("plan_key") == item["plan_key"]] if item.get("plan_key") else [row for row in cache[key] if row["pid"] == str(item["pid"])]
    if not matches:
        raise ProductUnlisted(f"DMIT 分类中未找到 {item.get('plan_key') or 'PID=' + str(item.get('pid', ''))}，可能已下架，请核对分类页")
    row = matches[0]
    if item.get("pid") and row["pid"] and str(item["pid"]) != row["pid"]:
        raise MonitorError("DMIT 套餐 PID 与已保存配置不一致，请核对后重新添加商品")
    if row["available"] is None:
        raise MonitorError("DMIT " + row["error"])
    return Product("dmit:" + (row.get("plan_key") or row["pid"]), row["name"], row["available"], row["purchase_url"], vendor="DMIT", stock_mode=row["stock_mode"], catalog_url=url, **{k: row[k] for k in ("region", "specs", "traffic", "price")})


def directory(session, scope: str, fetcher: dict) -> tuple[list, str, list]:
    from catalog import clean, make_row, scoped
    from monitor import MonitorError, Product
    body, final = fetch_catalog(session, PRICING_URL, fetcher)
    scopes = [{"id": "all", "name": "全部公开套餐（手动）", "url": PRICING_URL}]
    seen = {PRICING_URL}
    for a in BeautifulSoup(body, "html.parser").select("a[href]"):
        try:
            url = normalize_catalog_url(urljoin(final, a["href"]))
        except ValueError:
            continue
        if url in seen or urlsplit(url).path == "/pages/pricing":
            continue
        seen.add(url)
        parsed = urlsplit(url)
        gid = parse_qs(parsed.query).get("gid", [""])[0]
        identifier = "gid:" + gid if gid else "store:" + parsed.path.removeprefix("/store/").replace("/", ":")
        if any(s["id"] == identifier for s in scopes):
            continue
        scopes.append({"id": identifier, "name": f"{clean(text(a), 100)}（{gid}）" if gid else clean(text(a), 100), "url": url})
    raw_rows = parse_cards(body, final)
    if raw_rows:
        for code, label in REGIONS.items():
            if any(row["region"] == label for row in raw_rows):
                scopes.append({"id": code, "name": label, "url": PRICING_URL})
    elif len(scopes) > 1:
        scopes.pop(0)
    selected = scoped(scopes, scope)
    if selected["url"] != PRICING_URL:
        body, final = fetch_catalog(session, selected["url"], fetcher)
        raw_rows = parse_cards(body, final)
    if not raw_rows:
        raise MonitorError("DMIT 页面没有可识别的商品卡片，未更新目录；请核对网页请求方式或在高级设置添加分类页和 PID")
    rows = []
    for row in raw_rows:
        if selected["id"] in REGIONS and row["region"] != REGIONS[selected["id"]]:
            continue
        watch = {"adapter": "dmit", "provider": "DMIT", "name": row["name"], "category_url": selected["url"], "catalog_scope": selected["id"], "pid": row["pid"]}
        if row.get("plan_key"):
            watch["plan_key"] = row["plan_key"]
        product = Product("dmit:" + row["pid"], row["name"], row["available"], row["purchase_url"], vendor="DMIT", stock_mode=row["stock_mode"], **{k: row[k] for k in ("region", "specs", "traffic", "price")}) if row["available"] is not None else None
        result = make_row("dmit:" + (row.get("plan_key") or row["pid"]), watch, row["region"] or selected["name"], row["line"], row["specs"], row["price"], product, row["error"], row["traffic"])
        result.update(purchase_url=row["purchase_url"], stock_mode=row["stock_mode"])
        rows.append(result)
    return [{"id": s["id"], "name": s["name"]} for s in scopes], selected["id"], rows
