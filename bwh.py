"""Read BandwagonHost's public JSON catalogue without checkout mutations."""
from __future__ import annotations

import json
import re
from urllib.parse import quote, unquote, urlsplit, urlunsplit

CATALOG_HOST = "bwh81.net"
CATALOG_URL = f"https://{CATALOG_HOST}/order/catalog.json"
PRODUCT_URL = f"https://{CATALOG_HOST}/order/ecommerce/Los%20Angeles/USCA_9"
HOSTS = {CATALOG_HOST, f"www.{CATALOG_HOST}"}
TIER_NAMES = {
    "basic": "基础 VPS",
    "ecommerce": "电商 VPS",
    "ecommerce-sla-elevated": "电商 VPS · SLA 增强",
    "ultra": "Ultra VPS",
}
COUNTRY_NAMES = {
    "Canada": "加拿大", "Hong Kong": "中国香港", "Japan": "日本", "Netherlands": "荷兰",
    "Singapore": "新加坡", "United Arab Emirates": "阿联酋", "United States": "美国",
}
CITY_NAMES = {
    "Amsterdam": "阿姆斯特丹", "Dubai": "迪拜", "Fremont": "弗里蒙特", "Hong Kong": "中国香港",
    "Los Angeles": "洛杉矶", "New York": "纽约", "Osaka": "大阪", "San Jose": "圣何塞",
    "Singapore": "新加坡", "Tokyo": "东京", "Vancouver": "温哥华",
}
BILLING_NAMES = {
    "monthly": "月付", "quarterly": "季付", "semiannually": "半年付", "annually": "年付",
    "biennially": "两年付", "triennially": "三年付",
}


def clean(value: object, limit: int = 240) -> str:
    return " ".join(str(value or "").split())[:limit]


def location_name(location: dict) -> str:
    city = clean(location.get("city"), 80)
    country = clean(location.get("country"), 80)
    if country == "Hong Kong" or city == "Hong Kong":
        return "中国香港"
    return " · ".join(filter(None, (COUNTRY_NAMES.get(country, country), CITY_NAMES.get(city, city)))) or "未知地区"


def tier_name(tier_id: str) -> str:
    return TIER_NAMES.get(tier_id, tier_id.replace("-", " ").title())


def billing_name(period: str) -> str:
    return BILLING_NAMES.get(period, period or "周期未知")


def normalize_catalog_url(raw: object = PRODUCT_URL) -> str:
    parsed = urlsplit(str(raw).strip())
    if parsed.scheme != "https" or (parsed.hostname or "").casefold() not in HOSTS or parsed.username or parsed.password or parsed.port not in (None, 443) or parsed.query or parsed.fragment:
        raise ValueError("搬瓦工商品页必须是 bwh81.net 的 HTTPS 地区页面")
    parts = [unquote(part).strip() for part in parsed.path.split("/") if part]
    if len(parts) != 4 or parts[0] != "order" or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", parts[1]) or not re.fullmatch(r"[A-Za-z0-9 ._-]{1,120}", parts[2]) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", parts[3]):
        raise ValueError("搬瓦工商品页格式应为 /order/套餐类型/地区/机房 ID")
    return urlunsplit(("https", CATALOG_HOST, "/order/" + "/".join((parts[1], quote(parts[2], safe=""), parts[3])), "", ""))


def scope_from_url(url: str) -> str:
    parts = [unquote(part).strip() for part in urlsplit(normalize_catalog_url(url)).path.split("/") if part]
    return f"{parts[1]}:{parts[3]}"


def location_url(tier_id: str, location: dict, datacenter_id: str) -> str:
    """Build the public region page used by the catalogue selector."""
    city = clean(location.get("city"), 120)
    if not city or not datacenter_id:
        return PRODUCT_URL
    return urlunsplit(("https", CATALOG_HOST, "/order/" + "/".join((tier_id, quote(city, safe=""), datacenter_id)), "", ""))


def read_payload(session, fetcher: dict | None = None) -> dict:
    from monitor import MonitorError, fetch_page
    body, _ = fetch_page(session, CATALOG_URL, fetcher or {"mode": "requests"}, CATALOG_HOST)
    try:
        value = json.loads(body)
    except ValueError:
        raise MonitorError("搬瓦工目录接口未返回有效 JSON，未更新库存") from None
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or not isinstance(value.get("locations"), list) or not isinstance(value.get("offers"), list):
        raise MonitorError("搬瓦工目录结构异常，未更新库存")
    return value


def _scopes(payload: dict) -> list[dict]:
    result = [{"id": "all", "name": "全部公开套餐（手动）"}]
    for location in payload["locations"]:
        if not isinstance(location, dict):
            continue
        for data_center in location.get("datacenters", []):
            if not isinstance(data_center, dict):
                continue
            dc_id = str(data_center.get("id", "")).strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", dc_id):
                continue
            region = location_name(location)
            dc_name = clean(data_center.get("name"), 100)
            for tier in data_center.get("tiers", []):
                tier_id = str(tier).strip()
                if tier_id:
                    result.append({"id": f"{tier_id}:{dc_id}", "name": f"{region} · {dc_name} · {tier_name(tier_id)}（{dc_id}）"})
    return result


def _offer_key(offer: dict) -> str:
    location = offer.get("location") or {}
    price = offer.get("price") or {}
    return ":".join(str(value) for value in (offer.get("product_id", ""), location.get("datacenter_id", ""), price.get("billing_period", ""), price.get("amount_cents", "")))


def _product(offer: dict):
    from monitor import Product
    location = offer.get("location") or {}
    specs = offer.get("specifications") or {}
    price = offer.get("price") or {}
    product_id = offer.get("product_id")
    dc_id = str(location.get("datacenter_id", ""))
    period = str(price.get("billing_period", ""))
    if type(product_id) is not int or product_id <= 0 or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", dc_id) or not period:
        raise ValueError("搬瓦工商品缺少可靠的商品 ID、机房 ID 或计费周期")
    if type(offer.get("available")) is not bool:
        raise ValueError("搬瓦工商品库存字段不是布尔值")
    cart_url = str(offer.get("cart_url", "")).strip()
    parsed = urlsplit(cart_url)
    if parsed.scheme != "https" or (parsed.hostname or "").casefold() not in {"bandwagonhost.com", "www.bandwagonhost.com", *HOSTS}:
        raise ValueError("搬瓦工商品购买链接域名无效")
    name = clean(offer.get("name"), 140)
    if not name:
        raise ValueError("搬瓦工商品缺少名称")
    region = location_name(location)
    dc_name = clean(location.get("datacenter_name"), 100)
    full_name = f"{name} · {billing_name(period)}"
    cpu = specs.get("vcpu_count", "—")
    ram = specs.get("ram_mb", "—")
    disk = specs.get("storage_gb", "—")
    transfer = specs.get("transfer_gb_per_month", "—")
    speed = specs.get("port_speed_mbps", "—")
    amount = price.get("amount_cents")
    if type(amount) not in {int, float} or amount < 0:
        raise ValueError("搬瓦工商品价格无效")
    currency = clean(price.get("currency", "USD"), 10).upper()
    price_text = f"{currency} {amount / 100:g} / {billing_name(period)}"
    return Product(_offer_key(offer), full_name, 1 if offer["available"] else 0, cart_url, vendor="搬瓦工", stock_mode="availability", region=region,
        specs=f"{cpu} 核 · {ram} MB · {disk} GB", traffic=f"{transfer} GB流量 · {speed} Mbps", price=price_text,
        catalog_url=CATALOG_URL, match_details={"offer_key": _offer_key(offer), "product_id": str(product_id), "datacenter_id": dc_id, "datacenter_name": dc_name, "tier_id": str((offer.get("tier_ids") or [""])[0]), "billing_period": period})


def directory(session, scope: str = "", fetcher: dict | None = None) -> tuple[list, str, list]:
    payload = read_payload(session, fetcher)
    scopes = _scopes(payload)
    if len(scopes) <= 1:
        raise ValueError("搬瓦工暂未提供可浏览的套餐分类")
    requested = scope
    if not requested:
        requested = "ecommerce:USCA_9" if any(row["id"] == "ecommerce:USCA_9" for row in scopes) else scopes[0]["id"]
    if requested not in {row["id"] for row in scopes}:
        raise ValueError("搬瓦工地区或套餐类型已不存在，请重新刷新目录")
    tier_id, dc_id = requested.split(":", 1) if requested != "all" else ("", "")
    rows = []
    for offer in payload["offers"]:
        if not isinstance(offer, dict):
            continue
        location = offer.get("location") or {}
        if requested != "all" and (str(location.get("datacenter_id")) != dc_id or tier_id not in [str(value) for value in offer.get("tier_ids", [])]):
            continue
        try:
            product = _product(offer)
        except ValueError as exc:
            from monitor import MonitorError
            raise MonitorError(str(exc)) from None
        offer_tiers = [str(value) for value in offer.get("tier_ids", [])]
        offer_tier = tier_id or (offer_tiers[0] if offer_tiers else "")
        watch = {"adapter": "bwh", "provider": "搬瓦工", "name": product.name, "category_url": location_url(offer_tier, location, str(location.get("datacenter_id", ""))), "catalog_scope": f"{offer_tier}:{location.get('datacenter_id', '')}", "selector": product.key, "bwh_offer_key": product.key, "bwh_tier_id": offer_tier, "bwh_datacenter_id": str(location.get("datacenter_id", "")), "pid": str(offer["product_id"]), "purchase_url": product.url}
        rows.append({"key": f"bwh:{product.key}", "name": product.name, "provider": "搬瓦工", "region": product.region, "line": clean(location.get("datacenter_name"), 100), "specs": product.specs, "traffic": product.traffic, "price": product.price, "available": product.available, "stock_mode": product.stock_mode, "purchase_url": product.url, "error": "", "watch": watch})
    return scopes, requested, rows


def inspect(session, item: dict, fetcher: dict | None = None, cache: dict | None = None):
    cache = {} if cache is None else cache
    cache_key = ("catalog", (fetcher or {}).get("mode", "requests"))
    if cache_key not in cache:
        cache[cache_key] = read_payload(session, fetcher)
    payload = cache[cache_key]
    target = str(item.get("bwh_offer_key") or item.get("selector") or "")
    matches = []
    for offer in payload["offers"]:
        if not isinstance(offer, dict):
            continue
        try:
            product = _product(offer)
        except ValueError:
            continue
        if product.key == target:
            matches.append(product)
    if len(matches) != 1:
        from monitor import ProductUnlisted, MonitorError
        raise (ProductUnlisted if not matches else MonitorError)(f"搬瓦工套餐无法唯一匹配（找到 {len(matches)} 项）；请刷新搬瓦工库存大盘重新选择")
    return matches[0]
