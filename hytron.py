"""Read HytronVPS's public node catalogue and expose node availability."""
from __future__ import annotations

import json
import math
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from urllib.parse import urlencode

HYTRON_HOST = "hyvps.hytron.io"
CATALOG_URL = f"https://{HYTRON_HOST}/api/v1/nodes/catalog"
RESOURCE_CAPS_URL = f"https://{HYTRON_HOST}/api/v1/orders/resource-caps"
ORDER_URL = "https://hyvps.hytron.io/order"
PROVIDER_NAME = "Hytron"


def clean(value: object, limit: int = 240) -> str:
    return " ".join(str(value or "").split())[:limit]


def _decimal(value: object, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"Hytron {label} 无效") from None
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"Hytron {label} 无效")
    return parsed


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"Hytron {label} 无效")
    return value


def fetch_catalog(session, fetcher: dict | None = None) -> list[dict]:
    from monitor import MonitorError, fetch_page

    body, final_url = fetch_page(session, CATALOG_URL, fetcher or {"mode": "requests"}, HYTRON_HOST)
    if final_url != CATALOG_URL:
        raise MonitorError("Hytron 目录接口重定向到非预期地址，未更新库存")
    try:
        payload = json.loads(body)
    except ValueError:
        raise MonitorError("Hytron 目录接口未返回有效 JSON，未更新库存") from None
    if not isinstance(payload, list) or any(not isinstance(node, dict) for node in payload):
        raise MonitorError("Hytron 目录结构异常，未更新库存")
    ids = [node.get("id") for node in payload]
    if any(not isinstance(identifier, str) or not identifier.strip() for identifier in ids) or len(set(ids)) != len(ids):
        raise MonitorError("Hytron 节点标识无效或重复，未更新库存")
    return payload


def fetch_resource_caps(session, fetcher: dict | None = None) -> dict:
    from monitor import MonitorError, fetch_page

    url = RESOURCE_CAPS_URL + "?" + urlencode({"cpu": 1, "ram_mb": 1024})
    body, final_url = fetch_page(session, url, fetcher or {"mode": "requests"}, HYTRON_HOST)
    if final_url != url:
        raise MonitorError("Hytron 资源限制接口重定向到非预期地址，未更新库存")
    try:
        payload = json.loads(body)
    except ValueError:
        raise MonitorError("Hytron 资源限制接口未返回有效 JSON，未更新库存") from None
    required = ("max_disk_gb", "max_ipv4", "ratio_disk_per_cpu_gb", "ratio_disk_per_ram_gb", "ratio_disk_min_gb", "ratio_ipv4_min", "hard_min_cpu", "hard_min_ram_mb", "hard_min_disk_gb")
    if not isinstance(payload, dict) or any(type(payload.get(key)) not in {int, float} or not math.isfinite(payload[key]) or payload[key] < 0 for key in required):
        raise MonitorError("Hytron 资源限制结构异常，未更新库存")
    return payload


def _starter(node: dict, caps: dict) -> dict | None:
    templates = [template for template in node.get("templates", []) if isinstance(template, dict) and template.get("is_orderable") is True and template.get("capacity_verified") is True and type(template.get("min_disk_gb")) in {int, float} and template["min_disk_gb"] > 0]
    if not templates:
        return None
    cpu = max(1, int(math.ceil(caps["hard_min_cpu"])))
    ram_mb = max(1024, int(math.ceil(caps["hard_min_ram_mb"])))
    disk_gb = max(3, int(math.ceil(caps["hard_min_disk_gb"])), int(math.ceil(min(template["min_disk_gb"] for template in templates))))
    capacity = node.get("capacity") or {}
    available = node.get("available") or {}
    def available_value(key: str, fallback: str = "") -> int:
        source = capacity.get(key) if isinstance(capacity.get(key), dict) else None
        value = source.get("available") if source else available.get(fallback or key, 0)
        return value if type(value) is int and value >= 0 else 0
    ipv4 = available_value("ipv4", "ipv4") or available_value("ips", "ips")
    ipv6 = available_value("ipv6", "ipv6")
    ip_count, ipv6_count = (1, 0) if ipv4 > 0 and caps["ratio_ipv4_min"] >= 1 else (0, 1) if ipv6 > 0 else (0, 0)
    if not ip_count and not ipv6_count:
        return None
    resources = {"cpu": cpu, "ram_mb": ram_mb, "disk_gb": disk_gb, "ipv4": ip_count, "ipv6": ipv6_count}
    return resources


def _quantity(node: dict, resources: dict) -> int:
    if node.get("has_capacity") is not True or node.get("needs_sync") is True:
        return 0
    capacity = node.get("capacity") or {}
    available = node.get("available") or {}
    def available_value(key: str, fallback: str = "") -> int:
        source = capacity.get(key) if isinstance(capacity.get(key), dict) else None
        value = source.get("available") if source else available.get(fallback or key, 0)
        return value if type(value) is int and value >= 0 else 0
    values = [available_value("cpu") // resources["cpu"], available_value("ram_mb") // resources["ram_mb"], available_value("disk_gb") // resources["disk_gb"], available_value("vm_slots", "vm_slots")]
    if resources["ipv4"]:
        values.append((available_value("ipv4", "ipv4") or available_value("ips", "ips")) // resources["ipv4"])
    if resources["ipv6"]:
        values.append(available_value("ipv6", "ipv6") // resources["ipv6"])
    return max(0, min(values))


def _product(node: dict, caps: dict, item: dict | None = None):
    from monitor import MonitorError, Product

    node_id = str(node.get("id", "")).strip()
    if not node_id:
        raise MonitorError("Hytron 节点缺少稳定 ID")
    resources = _starter(node, caps)
    if resources is None:
        raise MonitorError("Hytron 节点当前没有可验证的入门配置或公网地址")
    pricing = node.get("pricing")
    if not isinstance(pricing, dict):
        raise MonitorError("Hytron 节点缺少价格信息")
    components = [
        _decimal(pricing.get("price_per_cpu_hour"), "CPU 价格") * resources["cpu"],
        _decimal(pricing.get("price_per_gb_ram_hour"), "内存价格") * Decimal(resources["ram_mb"]) / Decimal(1024),
        _decimal(pricing.get("price_per_gb_disk_hour"), "磁盘价格") * resources["disk_gb"],
        _decimal(pricing.get("price_per_ip_hour"), "IPv4 价格") * resources["ipv4"],
        _decimal(pricing.get("price_per_ipv6_hour"), "IPv6 价格") * resources["ipv6"],
    ]
    hourly = sum(components, Decimal("0"))
    quantity = _quantity(node, resources)
    name = clean(node.get("name"), 120) or node_id
    location = clean(node.get("location"), 80) or clean(node.get("location_code"), 20) or "未知地区"
    purchase_url = ORDER_URL
    price = f"${hourly.quantize(Decimal('0.000001'), rounding=ROUND_CEILING):f}/小时 · 开通费 ${_decimal(pricing.get('setup_fee', 0), '开通费').quantize(Decimal('0.01')):f}"
    specs = f"{resources['cpu']} 核 · {resources['ram_mb'] // 1024} GB · {resources['disk_gb']} GB · 入门配置"
    traffic = f"IPv4 {resources['ipv4']} · IPv6 {resources['ipv6']} · 流量 ${_decimal(pricing.get('price_per_gb_traffic', 0), '流量价格'):f}/GB"
    return Product(node_id, name, quantity, purchase_url, vendor=PROVIDER_NAME, stock_mode="quantity", region=location, node_name=name, specs=specs, traffic=traffic, price=price, catalog_url=CATALOG_URL, match_details={"node_id": node_id, "location_code": clean(node.get("location_code"), 20), "starter": resources})


def inspect(session, item: dict, fetcher: dict | None = None, catalog: list[dict] | None = None, caps: dict | None = None):
    from monitor import MonitorError

    nodes = catalog if catalog is not None else fetch_catalog(session, fetcher)
    node_id = str(item.get("node_id", "")).strip()
    matches = [node for node in nodes if str(node.get("id", "")).strip() == node_id]
    if len(matches) != 1:
        raise MonitorError("Hytron 节点已下线或标识不唯一，保留配置等待恢复")
    return _product(matches[0], caps or fetch_resource_caps(session, fetcher), item)


def directory(session, scope: str = "", fetcher: dict | None = None):
    from monitor import MonitorError
    from catalog import make_row, scoped

    nodes = fetch_catalog(session, fetcher)
    caps = fetch_resource_caps(session, fetcher)
    scopes = [{"id": "all", "name": "全部节点"}]
    locations = {}
    for node in nodes:
        code = clean(node.get("location_code"), 20) or clean(node.get("location"), 80) or "unknown"
        locations.setdefault(code, clean(node.get("location"), 80) or code)
    scopes.extend({"id": code, "name": f"{name}（{code}）"} for code, name in sorted(locations.items()))
    selected = scoped(scopes, scope, "all")
    rows = []
    for node in nodes:
        code = clean(node.get("location_code"), 20) or clean(node.get("location"), 80) or "unknown"
        if selected["id"] != "all" and code != selected["id"]:
            continue
        node_id = str(node.get("id", "")).strip()
        watch = {"adapter": "hytron", "provider": PROVIDER_NAME, "name": clean(node.get("name"), 120) or node_id, "node_id": node_id, "category_url": CATALOG_URL, "selector": node_id}
        error, product = "", None
        try:
            product = _product(node, caps, watch)
        except MonitorError as exc:
            error = str(exc)
        resources = _starter(node, caps)
        specs = f"{resources['cpu']} 核 · {resources['ram_mb'] // 1024} GB · {resources['disk_gb']} GB · 入门配置" if resources else "入门配置暂不可用"
        rows.append(make_row(f"hytron:{node_id}", watch, node.get("location", code), clean(node.get("name"), 120), specs, product.price if product else "—", product, error, traffic=product.traffic if product else ""))
    return scopes, selected["id"], rows
