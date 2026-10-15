"""Read-only vendor catalogues and one cancellable, cached discovery task."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
from datetime import datetime
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup


PROVIDERS = {"vmiss": "VMISS", "vps_soy": "VPS.SOY", "akile": "Akile", "isvoro": "Isvoro"}
MAX_ROWS = 600


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def validate_source(provider: object, scope: object = "") -> tuple[str, str]:
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise ValueError("请选择支持的商家")
    if not isinstance(scope, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{0,120}", scope):
        raise ValueError("商品分类标识无效")
    return provider, scope


def clean(value: object, limit: int = 240) -> str:
    return " ".join(str(value or "").split())[:limit]


def settings(value: object) -> dict:
    if isinstance(value, list):
        return {str(row.get("key")): row.get("value") for row in value if isinstance(row, dict) and not row.get("encrypted")}
    return value if isinstance(value, dict) else {}


def make_row(key: str, watch: dict, region: str, line: str, specs: str, price: str, product=None, error: str = "") -> dict:
    return {"key": key, "name": watch["name"], "provider": watch["provider"], "region": clean(region), "line": clean(line),
            "specs": clean(product.specs if product and product.specs else specs, 500), "traffic": clean(product.traffic if product else "", 500), "price": clean(price), "available": product.available if product else None,
            "stock_mode": product.stock_mode if product else "quantity", "purchase_url": product.url if product else "",
            "error": clean(error), "watch": {**watch, "enabled": True, "request_mode": "inherit"}}


def scoped(scopes: list[dict], requested: str, preferred: str = "") -> dict:
    if not scopes:
        raise ValueError("商家暂未提供可浏览的分类")
    target = requested or (preferred if any(row["id"] == preferred for row in scopes) else scopes[0]["id"])
    matches = [row for row in scopes if row["id"] == target]
    if len(matches) != 1:
        raise ValueError("该分类已不存在或标识重复，请重新选择商家刷新目录")
    return matches[0]


def akile_catalog(session, scope: str) -> tuple[list, str, list]:
    from monitor import MonitorError, fetch_akile_catalog, parse_akile
    payload = fetch_akile_catalog(session, {"mode": "requests"})
    if not isinstance(payload, dict) or str(payload.get("status_code")) not in {"0", "200"} or not isinstance(payload.get("data"), dict) or not isinstance(payload["data"].get("areas"), list):
        raise MonitorError("Akile 目录结构异常")
    areas = payload["data"]["areas"]
    scopes = [{"id": "all", "name": "全部地区"}] + [{"id": str(a["id"]), "name": f"{clean(a.get('area_name'))}（{a['id']}）"} for a in areas]
    selected = scoped(scopes, scope, "all")
    rows = []
    for area in areas:
        if selected["id"] != "all" and str(area["id"]) != selected["id"]:
            continue
        for node in area.get("nodes") or []:
            for plan in node.get("plans") or []:
                watch = {"adapter": "akile", "provider": "Akile", "name": clean(plan.get("plan_name"), 120), "area_mode": "manual", "area_id": str(area["id"]), "node_name": clean(node.get("group_name"), 200), "selector": clean(plan.get("plan_name"), 200)}
                error, product = "", None
                try:
                    if plan.get("is_pre_sale") is True:
                        raise MonitorError("预售套餐，不作为现货库存")
                    product = parse_akile(payload, watch)
                except MonitorError as exc:
                    error = str(exc)
                prices = []
                for price in plan.get("price_datas") or []:
                    if type(price.get("price")) in {int, float} and type(price.get("cycle")) is int and price["cycle"] > 0:
                        prices.append(f"¥{price['price'] / 100:g} / {price['cycle']}个月")
                specs = f"{plan.get('cpu', '—')} 核 · {plan.get('memory', '—')} MB · {plan.get('disk', '—')} GB · {plan.get('flow', '—')} GB流量 · {plan.get('bandwidth', '—')} Mbps"
                rows.append(make_row(f"akile:{area['id']}:{node['id']}:{plan['id']}", watch, area.get("area_name", ""), node.get("group_name", ""), specs, "；".join(prices[:3]), product, error))
    return scopes, selected["id"], rows


def vmiss_catalog(session, scope: str) -> tuple[list, str, list]:
    from monitor import MonitorError, fetch_page, parse_products
    body, _ = fetch_page(session, "https://app.vmiss.com/store", {"mode": "requests"}, "app.vmiss.com")
    scopes_by_id = {}
    for link in BeautifulSoup(body, "html.parser").select('a[href]'):
        parsed = urlsplit(urljoin("https://app.vmiss.com", link["href"]))
        match = re.fullmatch(r"/store/([A-Za-z0-9_-]+)", parsed.path)
        if parsed.scheme == "https" and parsed.hostname == "app.vmiss.com" and not parsed.query and match:
            scopes_by_id.setdefault(match[1], {"id": match[1], "name": clean(link.get_text(" ", strip=True))})
    scopes = list(scopes_by_id.values())
    selected = scoped(scopes, scope, "cn-hong-kong-bgp")
    url = "https://app.vmiss.com/store/" + selected["id"]
    body, final_url = fetch_page(session, url, {"mode": "requests"}, "app.vmiss.com")
    if urlsplit(final_url).path.rstrip("/") != urlsplit(url).path:
        raise MonitorError("VMISS 返回分类与请求分类不一致")
    soup = BeautifulSoup(body, "html.parser")
    rows = []
    for card in soup.select(".section.products .package"):
        title = card.select_one(".package-title")
        identifier = re.fullmatch(r"product([1-9][0-9]*)", str(card.get("id", "")))
        if not title or not identifier:
            raise MonitorError("VMISS 商品缺少名称或独立产品 ID，未更新目录")
        watch = {"adapter": "vmiss", "provider": "VMISS", "name": clean(title.get_text(" ", strip=True), 120), "category_url": url, "selector": str(card["id"]), "pid": identifier[1], "gid": ""}
        error, product = "", None
        try:
            product = parse_products('<div class="section products">' + str(card) + '</div>', final_url, [watch["selector"]])[0]
        except MonitorError as exc:
            error = str(exc)
        price = card.select_one(".price-amount")
        cycle = card.select_one(".price-cycle")
        specs = card.select_one(".package-content")
        row = make_row("vmiss:" + identifier[1], watch, selected["name"], selected["name"], specs.get_text(" ", strip=True) if specs else "", " / ".join(n.get_text(" ", strip=True) for n in (price, cycle) if n), product, error)
        rows.append(row)
    if not rows:
        raise MonitorError("VMISS 分类没有可识别的商品卡片")
    return scopes, selected["id"], rows


def soy_catalog(session, scope: str) -> tuple[list, str, list]:
    from monitor import MonitorError, fetch_vps_soy_page, parse_vps_soy
    body, _ = fetch_vps_soy_page(session, "https://www.vps.soy/cart", {"mode": "requests"})
    scopes = []
    for node in BeautifulSoup(body, "html.parser").select(".secondgroup_item[onclick]"):
        match = re.fullmatch(r"window\.location\.href=['\"](/cart\?fid=([1-9][0-9]*)&gid=([1-9][0-9]*))['\"];?", node["onclick"].strip())
        if match:
            scopes.append({"id": f"{match[2]}:{match[3]}", "name": f"{clean(node.get_text(' ', strip=True))}（GID={match[3]}）", "url": "https://www.vps.soy" + match[1]})
    selected = scoped(scopes, scope, "1:8")
    body, final_url = fetch_vps_soy_page(session, selected["url"], {"mode": "requests"})
    soup = BeautifulSoup(body, "html.parser")
    rows = []
    for card in soup.select(".cartitem"):
        title = card.select_one(".card-body h4")
        if not title:
            raise MonitorError("VPS.SOY 商品缺少名称")
        name = clean(title.get_text(" ", strip=True), 120)
        watch = {"adapter": "vps_soy", "provider": "VPS.SOY", "name": name, "url": selected["url"], "selector": name}
        error, product = "", None
        try:
            product = parse_vps_soy(body, final_url, name)
        except MonitorError as exc:
            error = str(exc)
        price_node = card.select_one(".cart-num")
        specs = card.select_one(".card-body .card-text")
        rows.append(make_row(f"vps_soy:{selected['id']}:{name}", watch, selected["name"], selected["name"], specs.get_text(" ", strip=True) if specs else "", price_node.parent.get_text(" ", strip=True) if price_node else "", product, error))
    if not rows and not soup.select(".secondgroup_item"):
        raise MonitorError("VPS.SOY 返回页面缺少商品目录")
    return [{"id": row["id"], "name": row["name"]} for row in scopes], selected["id"], rows


def isvoro_catalog(session, scope: str) -> tuple[list, str, list]:
    from monitor import MonitorError, SiteBlocked, fetch_isvoro_json, inspect_isvoro, isvoro_categories, isvoro_category
    fetcher = {"mode": "requests"}
    raw_types = fetch_isvoro_json(session, "categories", fetcher)
    scopes = []
    for kind in isvoro_categories(raw_types):
        for line in kind["children"] or [kind]:
            scopes.append({"id": f"{kind['id']}:{line['id']}", "name": f"{kind['name']} / {line['name']}（{line['id']}）", "type": kind, "line": line})
    selected = scoped(scopes, scope, "24:27")
    kind, line = selected["type"], selected["line"]
    category_path = "categories/" + line["slug"]
    raw_category = fetch_isvoro_json(session, category_path, fetcher)
    category = isvoro_category(raw_category)
    if category["id"] != line["id"]:
        raise MonitorError("Isvoro 分类 ID 不一致")
    cache = {("categories", "requests"): raw_types, (category_path, "requests"): raw_category}
    rows = []
    for entry in category["products"]:
        watch = {"adapter": "isvoro", "provider": "Isvoro", "name": clean(entry["name"], 120), "type_id": str(kind["id"]), "type_name": kind["name"], "category_id": str(line["id"]), "category_name": line["name"], "product_id": str(entry["id"]), "product_name": entry["name"]}
        product, error, detail = None, "", {}
        try:
            detail = fetch_isvoro_json(session, "products/" + entry["slug"], fetcher)
            cache[("products/" + entry["slug"], "requests")] = detail
            product = inspect_isvoro(session, watch, fetcher, cache)
        except SiteBlocked:
            raise
        except MonitorError as exc:
            error = str(exc)
        props = settings(detail.get("settings")) if isinstance(detail, dict) else {}
        specs = f"{props.get('cores', '—')} 核 · {props.get('memory', '—')} MB · {props.get('disk', '—')} GB · {props.get('bandwidth_limit', '—')} GB流量"
        prices = []
        for plan in detail.get("plans", []) if isinstance(detail, dict) else []:
            if plan.get("hidden") is True:
                continue
            for price in plan.get("prices", []):
                if type(price.get("price")) in {int, float}:
                    prices.append(f"{price.get('currency_code', '')} {price['price']:g} / {clean(plan.get('name'))}")
        rows.append(make_row(f"isvoro:{kind['id']}:{line['id']}:{entry['id']}", watch, kind["name"], line["name"], specs, "；".join(prices[:3]), product, error))
    return [{"id": row["id"], "name": row["name"]} for row in scopes], selected["id"], rows


def load_catalog(provider: str, scope: str = "") -> dict:
    provider, scope = validate_source(provider, scope)
    with requests.Session() as session:
        session.headers["User-Agent"] = "HostWatch/1.0 (read-only VPS catalogue)"
        scopes, actual, rows = {"vmiss": vmiss_catalog, "vps_soy": soy_catalog, "akile": akile_catalog, "isvoro": isvoro_catalog}[provider](session, scope)
    if len(rows) > MAX_ROWS or len({row["key"] for row in rows}) != len(rows):
        raise ValueError("商品目录超过容量或存在重复标识，未更新缓存")
    return {"provider": provider, "scope": actual, "scopes": scopes, "rows": rows, "fetched_at": now(), "unknown": sum(row["available"] is None for row in rows)}


class CatalogBusy(ValueError):
    pass


class CatalogFailure(ValueError):
    pass


class CatalogService:
    def __init__(self, cache_path):
        self.cache_path = cache_path
        self.lock = threading.Lock()
        self.task = None
        self.process = None

    def read_cache(self) -> dict:
        path = self.cache_path()
        try:
            if path.stat().st_size > 8 * 1024 * 1024:
                return {}
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def snapshot(self, provider: str, scope: str = "") -> dict:
        provider, scope = validate_source(provider, scope)
        cache = self.read_cache()
        candidates = [v for v in cache.values() if v.get("provider") == provider and (not scope or v.get("scope") == scope)]
        data = max(candidates, key=lambda v: v["fetched_at"]) if candidates else None
        with self.lock:
            task = dict(self.task) if self.task else None
        return {"data": data, "task": task}

    def refresh(self, provider: str, scope: str = "") -> dict:
        provider, scope = validate_source(provider, scope)
        with self.lock:
            if self.task and self.task["pending"]:
                if (self.task["provider"], self.task["scope"]) != (provider, scope):
                    raise CatalogBusy("已有目录刷新正在执行，请先停止或等待完成")
                return dict(self.task)
            self.task = {"provider": provider, "scope": scope, "pending": True, "cancelled": False, "started_at": now(), "error": ""}
            task = self.task
        threading.Thread(target=self.run, args=(task,), daemon=True).start()
        return dict(task)

    def stop(self) -> dict:
        with self.lock:
            task, process = self.task, self.process
            if task and task["pending"]:
                task["cancelled"] = True
            output = dict(task) if task else {"pending": False, "cancelled": True}
        if process is not None:
            try:
                process.kill()
            except OSError:
                pass
        return output

    def run(self, task: dict) -> None:
        process = None
        try:
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
            with self.lock:
                self.process = process
                cancelled = task["cancelled"]
            if cancelled:
                process.kill()
            stdout, _ = process.communicate(json.dumps({"provider": task["provider"], "scope": task["scope"]}), timeout=240)
            with self.lock:
                if task["cancelled"]:
                    return
                result = json.loads(stdout)
                if not result.get("ok"):
                    raise CatalogFailure(clean(result.get("error") or "目录刷新失败", 500))
                data = result["data"]
                cache = self.read_cache()
                cache[f"{data['provider']}:{data['scope']}"] = data
                cache = dict(sorted(cache.items(), key=lambda pair: pair[1]["fetched_at"])[-64:])
                while len(cache) > 1 and len(json.dumps(cache, ensure_ascii=False).encode("utf-8")) > 8 * 1024 * 1024:
                    del cache[next(iter(cache))]
                path = self.cache_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                fd, name = tempfile.mkstemp(prefix="catalog-", suffix=".tmp", dir=path.parent)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as stream:
                        json.dump(cache, stream, ensure_ascii=False)
                    os.replace(name, path)
                finally:
                    if os.path.exists(name):
                        os.unlink(name)
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
            with self.lock:
                if not task["cancelled"]:
                    task["error"] = (str(exc) + "；保留上次快照。") if isinstance(exc, CatalogFailure) else "目录刷新失败或超时，保留上次缓存；请重试或查看商家访问情况。"
            # Do not include subprocess/network exceptions that may contain URLs.
        finally:
            if process is not None and process.poll() is None:
                try:
                    process.kill()
                    process.communicate(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            with self.lock:
                self.process = None
                task.update(pending=False, finished_at=now())


if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    sys.stdout.reconfigure(encoding="utf-8")
    from monitor import MonitorError
    try:
        value = json.loads(sys.stdin.read(1025))
        output = {"ok": True, "data": load_catalog(value["provider"], value.get("scope", ""))}
    except (ValueError, KeyError, TypeError, MonitorError, requests.RequestException) as exc:
        output = {"ok": False, "error": str(exc) if isinstance(exc, (MonitorError, ValueError)) else "商家目录响应无效"}
    print(json.dumps(output, ensure_ascii=False))
