"""Read-only vendor catalogues and one cancellable, cached discovery task."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import uuid
from contextlib import nullcontext
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from time_utils import beijing_iso


PROVIDERS = {"vmiss": "VMISS", "vps_soy": "VPS.SOY", "akile": "Akile", "narwhal": "独角鲸云", "isvoro": "Isvoro", "dmit": "DMIT", "bwh": "搬瓦工"}
MAX_ROWS = 600
NARWHAL_MAX_ROWS = 5000
NARWHAL_DIRECTORY_VERSION = 2
METADATA_VERSION = 1


def now() -> str:
    return beijing_iso()


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


def make_row(key: str, watch: dict, region: str, line: str, specs: str, price: str, product=None, error: str = "", traffic: str = "") -> dict:
    return {"key": key, "name": watch["name"], "provider": watch["provider"], "region": clean(region), "line": clean(line),
            "specs": clean(product.specs if product and product.specs else specs, 500), "traffic": clean(product.traffic if product else traffic, 500), "price": clean(price), "available": product.available if product else None,
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


def akile_catalog(session, scope: str, fetcher: dict | None = None) -> tuple[list, str, list]:
    from monitor import MonitorError, akile_network, fetch_akile_catalog, parse_akile
    payload = fetch_akile_catalog(session, fetcher or {"mode": "requests"})
    if not isinstance(payload, dict) or str(payload.get("status_code")) not in {"0", "200"} or not isinstance(payload.get("data"), dict) or not isinstance(payload["data"].get("areas"), list):
        raise MonitorError("Akile 目录结构异常")
    areas = payload["data"]["areas"]
    scopes = [{"id": "all", "name": "全部公开套餐（手动）"}] + [{"id": str(a["id"]), "name": f"{clean(a.get('area_name'))}（{a['id']}）"} for a in areas]
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
                specs = f"{plan.get('cpu', '—')} 核 · {plan.get('memory', '—')} MB · {plan.get('disk', '—')} GB"
                rows.append(make_row(f"akile:{area['id']}:{node['id']}:{plan['id']}", watch, area.get("area_name", ""), node.get("group_name", ""), specs, "；".join(prices[:3]), product, error, traffic=akile_network(plan)))
    return scopes, selected["id"], rows


def narwhal_catalog(session, scope: str, fetcher: dict | None = None) -> tuple[list, str, list]:
    from monitor import MonitorError, fetch_narwhal_plans, parse_narwhal
    all_plans = fetch_narwhal_plans(session, fetcher=fetcher)
    machines = {}
    for plan in all_plans:
        machine_id, machine_name = plan.get("machine_id"), plan.get("machine_name")
        if isinstance(machine_id, str) and machine_id and isinstance(machine_name, str) and machine_name.strip():
            machine = machines.setdefault(machine_id, {"id": machine_id, "name": clean(machine_name, 200), "region": clean(plan.get("machine_region"), 80), "plans": []})
            machine["plans"].append(plan)
    if not machines:
        raise MonitorError("独角鲸云没有可识别的实例")
    scopes = [{"id": "all", "name": "全部套餐"}] + [
        {"id": machine["id"], "name": machine["name"], "region": machine["region"], "plan_count": len(machine["plans"])}
        for machine in sorted(machines.values(), key=lambda machine: machine["name"].casefold())]
    selected = scoped(scopes, scope, "all")
    plans = all_plans if selected["id"] == "all" else machines[selected["id"]]["plans"]
    rows = []
    for plan in plans:
        plan_id = plan.get("id")
        if not isinstance(plan_id, str) or not plan_id:
            raise MonitorError("独角鲸云套餐缺少稳定 ID，未更新目录")
        machine_id = plan.get("machine_id")
        if machine_id not in machines:
            raise MonitorError("独角鲸云套餐缺少实例信息，未更新目录")
        watch = {"adapter": "narwhal", "provider": "独角鲸云", "name": clean(plan.get("name"), 120), "machine_id": machine_id, "plan_id": plan_id}
        error, product = "", None
        try:
            product = parse_narwhal(machines[machine_id]["plans"], watch)
        except MonitorError as exc:
            error = str(exc)
        specs = f"{plan.get('cpu', '—')} 核 · {plan.get('ram_mb', '—')} MB · {plan.get('disk_gb', '—')} GB · {plan.get('bandwidth_mbps', '—')} Mbps"
        rows.append(make_row(f"narwhal:{machine_id}:{plan_id}", watch, plan.get("machine_region", ""), plan.get("machine_name", ""), specs, f"¥{plan.get('price_monthly', '—')}/月", product, error, traffic=f"{plan.get('monthly_traffic_gb', '—')} GB/月"))
    return scopes, selected["id"], rows


def vmiss_catalog(session, scope: str, fetcher: dict | None = None) -> tuple[list, str, list]:
    from monitor import MonitorError, fetch_page, normalize_vmiss_region, parse_products, vmiss_metadata
    fetcher = fetcher or {"mode": "requests"}
    body, _ = fetch_page(session, "https://app.vmiss.com/store", fetcher, "app.vmiss.com")
    scopes_by_id = {}
    for link in BeautifulSoup(body, "html.parser").select('a[href]'):
        parsed = urlsplit(urljoin("https://app.vmiss.com", link["href"]))
        match = re.fullmatch(r"/store/([A-Za-z0-9_-]+)", parsed.path)
        if parsed.scheme == "https" and parsed.hostname == "app.vmiss.com" and not parsed.query and match:
            scopes_by_id.setdefault(match[1], {"id": match[1], "name": normalize_vmiss_region(link.get_text(" ", strip=True))})
    scopes = [{"id": "all", "name": "全部公开套餐（手动）"}] + list(scopes_by_id.values())
    selected = scoped(scopes, scope, "cn-hong-kong-bgp")
    targets = list(scopes_by_id.values()) if selected["id"] == "all" else [selected]
    rows = []
    for target in targets:
        url = "https://app.vmiss.com/store/" + target["id"]
        body, final_url = fetch_page(session, url, fetcher, "app.vmiss.com")
        if urlsplit(final_url).path.rstrip("/") != urlsplit(url).path:
            raise MonitorError("VMISS 返回分类与请求分类不一致")
        soup = BeautifulSoup(body, "html.parser")
        for card in soup.select(".section.products .package"):
            title = card.select_one(".package-title")
            identifier = re.fullmatch(r"product([1-9][0-9]*)", str(card.get("id", "")))
            if not title or not identifier:
                raise MonitorError("VMISS 商品缺少名称或独立产品 ID，未更新目录")
            watch = {"adapter": "vmiss", "provider": "VMISS", "name": clean(title.get_text(" ", strip=True), 120), "category_url": url, "catalog_scope": target["id"], "selector": str(card["id"]), "pid": identifier[1], "gid": ""}
            error, product = "", None
            try:
                product = parse_products('<div class="section products">' + str(card) + '</div>', final_url, [watch["selector"]])[0]
            except MonitorError as exc:
                error = str(exc)
            price = card.select_one(".price-amount")
            cycle = card.select_one(".price-cycle")
            metadata = vmiss_metadata(card)
            row = make_row(f"vmiss:{target['id']}:{identifier[1]}", watch, target["name"], target["name"], metadata["specs"], " / ".join(n.get_text(" ", strip=True) for n in (price, cycle) if n), product, error, traffic=metadata["traffic"])
            rows.append(row)
    if not rows:
        raise MonitorError("VMISS 分类没有可识别的商品卡片")
    return scopes, selected["id"], rows


def soy_catalog(session, scope: str, fetcher: dict | None = None) -> tuple[list, str, list]:
    from monitor import MonitorError, fetch_vps_soy_page, parse_vps_soy, soy_metadata
    fetcher = fetcher or {"mode": "requests"}
    body, _ = fetch_vps_soy_page(session, "https://www.vps.soy/cart", fetcher)
    scopes = []
    for node in BeautifulSoup(body, "html.parser").select(".secondgroup_item[onclick]"):
        match = re.fullmatch(r"window\.location\.href=['\"](/cart\?fid=([1-9][0-9]*)&gid=([1-9][0-9]*))['\"];?", node["onclick"].strip())
        if match:
            scopes.append({"id": f"{match[2]}:{match[3]}", "name": f"{clean(node.get_text(' ', strip=True))}（GID={match[3]}）", "url": "https://www.vps.soy" + match[1]})
    if not scopes:
        raise MonitorError("VPS.SOY 返回页面缺少商品分类")
    scopes = [{"id": "all", "name": "全部公开套餐（手动）"}] + scopes
    selected = scoped(scopes, scope, "1:8")
    targets = [row for row in scopes if row["id"] != "all"] if selected["id"] == "all" else [selected]
    rows = []
    for target in targets:
        body, final_url = fetch_vps_soy_page(session, target["url"], fetcher)
        soup = BeautifulSoup(body, "html.parser")
        for card in soup.select(".cartitem"):
            title = card.select_one(".card-body h4")
            if not title:
                raise MonitorError("VPS.SOY 商品缺少名称")
            name = clean(title.get_text(" ", strip=True), 120)
            watch = {"adapter": "vps_soy", "provider": "VPS.SOY", "name": name, "url": target["url"], "catalog_scope": target["id"], "selector": name}
            error, product = "", None
            try:
                product = parse_vps_soy(body, final_url, name)
            except MonitorError as exc:
                error = str(exc)
            price_node = card.select_one(".cart-num")
            metadata = soy_metadata(card)
            rows.append(make_row(f"vps_soy:{target['id']}:{name}", watch, target["name"], target["name"], metadata["specs"], price_node.parent.get_text(" ", strip=True) if price_node else "", product, error, traffic=metadata["traffic"]))
    if not rows and not soup.select(".secondgroup_item"):
        raise MonitorError("VPS.SOY 返回页面缺少商品目录")
    return [{"id": row["id"], "name": row["name"]} for row in scopes], selected["id"], rows


def isvoro_catalog(session, scope: str, fetcher: dict | None = None) -> tuple[list, str, list]:
    from monitor import MonitorError, SiteBlocked, fetch_isvoro_json, inspect_isvoro, isvoro_categories, isvoro_category, isvoro_network
    fetcher = fetcher or {"mode": "requests"}
    mode = fetcher.get("mode", "requests")
    raw_types = fetch_isvoro_json(session, "categories", fetcher)
    scopes = []
    for kind in isvoro_categories(raw_types):
        for line in kind["children"] or [kind]:
            scopes.append({"id": f"{kind['id']}:{line['id']}", "name": f"{kind['name']} / {line['name']}（{line['id']}）", "type": kind, "line": line})
    if not scopes:
        raise MonitorError("Isvoro 返回页面缺少商品分类")
    scopes = [{"id": "all", "name": "全部公开套餐（手动）"}] + scopes
    selected = scoped(scopes, scope, "24:27")
    targets = [row for row in scopes if row["id"] != "all"] if selected["id"] == "all" else [selected]
    cache = {("categories", mode): raw_types}
    rows = []
    for target in targets:
        kind, line = target["type"], target["line"]
        category_path = "categories/" + line["slug"]
        raw_category = fetch_isvoro_json(session, category_path, fetcher)
        category = isvoro_category(raw_category)
        if category["id"] != line["id"]:
            raise MonitorError("Isvoro 分类 ID 不一致")
        cache[(category_path, mode)] = raw_category
        for entry in category["products"]:
            watch = {"adapter": "isvoro", "provider": "Isvoro", "name": clean(entry["name"], 120), "type_id": str(kind["id"]), "type_name": kind["name"], "category_id": str(line["id"]), "category_name": line["name"], "catalog_scope": target["id"], "product_id": str(entry["id"]), "product_name": entry["name"]}
            product, error, detail = None, "", {}
            try:
                detail = fetch_isvoro_json(session, "products/" + entry["slug"], fetcher)
                cache[("products/" + entry["slug"], mode)] = detail
                product = inspect_isvoro(session, watch, fetcher, cache)
            except SiteBlocked:
                raise
            except MonitorError as exc:
                error = str(exc)
            props = settings(detail.get("settings")) if isinstance(detail, dict) else {}
            specs = f"{props.get('cores', '—')} 核 · {props.get('memory', '—')} MB · {props.get('disk', '—')} GB"
            prices = []
            for plan in detail.get("plans", []) if isinstance(detail, dict) else []:
                if plan.get("hidden") is True:
                    continue
                for price in plan.get("prices", []):
                    if type(price.get("price")) in {int, float}:
                        prices.append(f"{price.get('currency_code', '')} {price['price']:g} / {clean(plan.get('name'))}")
            rows.append(make_row(f"isvoro:{kind['id']}:{line['id']}:{entry['id']}", watch, kind["name"], line["name"], specs, "；".join(prices[:3]), product, error, traffic=isvoro_network(props)))
    return [{"id": row["id"], "name": row["name"]} for row in scopes], selected["id"], rows


def load_catalog(provider: str, scope: str = "", fetcher: dict | None = None) -> dict:
    provider, scope = validate_source(provider, scope)
    from monitor import cloudscraper_request_session, product_fetcher, fetcher_route
    route = fetcher_route(fetcher, {"adapter": provider})
    fetcher = product_fetcher(fetcher, {"adapter": provider})
    with cloudscraper_request_session(fetcher) as fetcher, requests.Session() as session:
        session.headers["User-Agent"] = "HostWatch/1.0 (read-only VPS catalogue)"
        if provider == "dmit":
            from dmit import directory
            scopes, actual, rows = directory(session, scope, fetcher)
        elif provider == "bwh":
            from bwh import directory
            scopes, actual, rows = directory(session, scope, fetcher)
        else:
            scopes, actual, rows = {"vmiss": vmiss_catalog, "vps_soy": soy_catalog, "akile": akile_catalog, "narwhal": narwhal_catalog, "isvoro": isvoro_catalog}[provider](session, scope, fetcher)
    if len(rows) > (NARWHAL_MAX_ROWS if provider == "narwhal" else MAX_ROWS) or len({row["key"] for row in rows}) != len(rows):
        raise ValueError("商品目录超过容量或存在重复标识，未更新缓存")
    data = {"provider": provider, "scope": actual, "scopes": scopes, "rows": rows, "fetched_at": now(), "route": route, "metadata_version": METADATA_VERSION, "unknown": sum(row["available"] is None for row in rows)}
    if provider == "dmit":
        from dmit import STOCK_VERSION
        data["stock_version"] = STOCK_VERSION
    if provider == "narwhal":
        data["narwhal_directory_version"] = NARWHAL_DIRECTORY_VERSION
    return data


class CatalogBusy(ValueError):
    pass


class CatalogFailure(ValueError):
    pass


class CatalogService:
    def __init__(self, cache_path, fetcher_settings=None):
        self.cache_path = cache_path
        self.fetcher_settings = fetcher_settings or (lambda: {"mode": "requests"})
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
        # Akile and Narwhal return their full directory in one response. Keep
        # one snapshot and derive area or instance views locally so changing
        # the selector never starts another request to the vendor.
        if provider in {"akile", "narwhal"}:
            full = [v for v in cache.values() if v.get("provider") == provider and v.get("scope") == "all"]
            full = max(full, key=lambda v: v["fetched_at"]) if full else None
            if full is not None and (not scope or scope != "all"):
                known = {str(row.get("id")) for row in full.get("scopes", [])}
                if not scope or scope in known:
                    if provider == "narwhal":
                        data = self._narwhal_instance_snapshot(full, scope)
                    else:
                        data = self._akile_area_snapshot(full, scope)
            elif full is not None:
                data = full
        elif scope and scope != "all":
            full = [v for v in cache.values() if v.get("provider") == provider and v.get("scope") == "all"]
            full = max(full, key=lambda v: v["fetched_at"]) if full else None
            if full is not None:
                known = {str(row.get("id")) for row in full.get("scopes", [])}
                if scope in known:
                    scoped_data = self._catalog_scope_snapshot(full, scope)
                    if scoped_data is not None:
                        data = scoped_data
        with self.lock:
            task = dict(self.task) if self.task else None
        # Old Isvoro snapshots contain traffic only, even when the API supplies
        # ratelimit. Keep them visible while the browser refreshes this scope.
        needs_refresh = bool(data and provider == "isvoro" and data.get("metadata_version") != METADATA_VERSION)
        if data and provider == "narwhal" and data.get("narwhal_directory_version") != NARWHAL_DIRECTORY_VERSION:
            needs_refresh = True
        if data and provider == "dmit":
            from dmit import STOCK_VERSION
            if data.get("stock_version") != STOCK_VERSION:
                needs_refresh = True
                # Retain metadata, but never advertise unverified legacy stock.
                rows = [{**row, "available": None, "error": "旧版库存尚未校验购买页，请刷新目录"} if (row.get("available") or 0) > 0 else row for row in data.get("rows", [])]
                data = {**data, "rows": rows, "unknown": sum(row.get("available") is None for row in rows)}
        from monitor import fetcher_route
        return {"data": data, "task": task, "needs_refresh": needs_refresh,
                "route": fetcher_route(self.fetcher_settings(), {"adapter": provider})}

    @staticmethod
    def _narwhal_instance_snapshot(data: dict, scope: str) -> dict:
        if not scope or scope == "all":
            return data
        rows = [row for row in data.get("rows", []) if row.get("watch", {}).get("machine_id") == scope]
        return {**data, "scope": scope, "rows": rows, "unknown": sum(row.get("available") is None for row in rows)}

    @staticmethod
    def _akile_area_snapshot(data: dict, scope: str) -> dict:
        if not scope or scope == "all":
            return data
        rows = [row for row in data.get("rows", []) if str(row.get("watch", {}).get("area_id", "")) == scope]
        return {**data, "scope": scope, "rows": rows, "unknown": sum(row.get("available") is None for row in rows)}

    @staticmethod
    def _catalog_scope_snapshot(data: dict, scope: str) -> dict:
        rows = data.get("rows", [])
        mapped_scopes = {str((row.get("watch") or {}).get("catalog_scope", "")) for row in rows}
        mapped = [row for row in rows if str((row.get("watch") or {}).get("catalog_scope", "")) == scope]
        # A provider may expose a full scope without assigning category metadata
        # to every row. In that case let the caller fetch the requested category.
        if rows and (not any("catalog_scope" in (row.get("watch") or {}) for row in rows) or scope not in mapped_scopes):
            return None
        return {**data, "scope": scope, "rows": mapped, "unknown": sum(row.get("available") is None for row in mapped)}

    def refresh(self, provider: str, scope: str = "", trigger: str = "manual") -> dict:
        provider, scope = validate_source(provider, scope)
        from monitor import fetcher_route
        settings = copy.deepcopy(self.fetcher_settings())
        fetch_scope = "all" if provider in {"akile", "narwhal"} else scope
        with self.lock:
            if self.task and self.task["pending"]:
                if (self.task["provider"], self.task.get("fetch_scope", self.task["scope"])) != (provider, fetch_scope):
                    raise CatalogBusy("已有目录刷新正在执行，请先停止或等待完成")
                return dict(self.task)
            self.task = {"provider": provider, "scope": scope, "fetch_scope": fetch_scope, "pending": True, "cancelled": False, "started_at": now(), "error": "", "trigger": "auto" if trigger == "auto" else "manual", "route": fetcher_route(settings, {"adapter": provider})}
            task = self.task
        threading.Thread(target=self.run, args=(task, settings), daemon=True).start()
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

    def run(self, task: dict, settings: dict | None = None) -> None:
        module_result = "success"
        module_message = "目录刷新完成"
        try:
            stdout = self.fetch_result(task, settings)
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
            module_result = "error"
            module_message = str(exc)[:500] or "目录刷新失败"
            with self.lock:
                if not task["cancelled"]:
                    task["error"] = (str(exc) + "；保留上次快照。") if isinstance(exc, CatalogFailure) else "目录刷新失败或超时，保留上次缓存；请重试或查看商家访问情况。"
            # Do not include subprocess/network exceptions that may contain URLs.
        finally:
            with self.lock:
                self.process = None
                task.update(pending=False, finished_at=now())
            try:
                from admin import STATUS_PATH, current_config
                from monitor import module_schedule, update_module_status
                update_module_status(STATUS_PATH, "catalog", module_schedule(current_config(), "catalog"), module_result, module_message, trigger=task.get("trigger"))
            except (OSError, ValueError, TypeError):
                pass

    def worker(self, path: Path, mode: str, payload: dict, timeout: int, task: dict, cancellable=True) -> str:
        process = subprocess.Popen([sys.executable, str(path), mode], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
        if cancellable:
            with self.lock:
                self.process = process
                cancelled = task["cancelled"]
            if cancelled:
                process.kill()
        try:
            return process.communicate(json.dumps(payload), timeout=timeout)[0]
        finally:
            if process.poll() is None:
                try:
                    process.kill()
                    process.communicate(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if cancellable:
                with self.lock:
                    if self.process is process:
                        self.process = None

    def fetch_result(self, task: dict, settings: dict | None = None) -> str:
        from admin import BROWSER_CLEANUP_SECONDS, BROWSER_CLEANUP_WARNING
        from monitor import MonitorError, browser_request_slot, check_browser_memory, product_fetcher
        try:
            fetcher = product_fetcher(settings if settings is not None else self.fetcher_settings(), {"adapter": task["provider"]})
        except MonitorError as exc:
            raise CatalogFailure(str(exc)) from None
        browser = fetcher["mode"] == "flaresolverr"
        session_id = "hostwatch-catalog-" + uuid.uuid4().hex
        attempted = created = False
        root = Path(__file__).resolve().parent
        try:
            with browser_request_slot() if browser else nullcontext():
                if browser:
                    check_browser_memory(fetcher)
                try:
                    if browser and not task["cancelled"]:
                        attempted = True
                        import egress
                        payload = {"command": "create", "endpoint": fetcher["flaresolverr_url"], "session": session_id}
                        if fetcher.get("egress_proxy_url"):
                            payload["proxy"] = egress.browser_proxy(fetcher)
                        raw = self.worker(root / "admin.py", "--browser-session-worker", payload, 35, task)
                        if task["cancelled"]:
                            return "{}"
                        if not json.loads(raw).get("ok"):
                            raise CatalogFailure("目录浏览器启动失败，请检查 FlareSolverr 服务")
                        created = True
                        fetcher.update(_test_browser_session=session_id, _browser_slot_held=True)
                    if task["cancelled"]:
                        return "{}"
                    return self.worker(root / "catalog.py", "--worker", {"provider": task["provider"], "scope": task.get("fetch_scope", task["scope"]), "fetcher": fetcher}, 240, task)
                finally:
                    if attempted:
                        with self.lock:
                            task["phase"] = "cleaning"
                        try:
                            raw = self.worker(root / "admin.py", "--browser-session-worker", {"command": "destroy", "endpoint": fetcher["flaresolverr_url"], "session": session_id, "creation_uncertain": not created}, BROWSER_CLEANUP_SECONDS, task, cancellable=False)
                            cleanup = json.loads(raw)
                            warning = cleanup.get("warning", "") if cleanup.get("ok") else cleanup.get("warning") or BROWSER_CLEANUP_WARNING
                        except (OSError, ValueError, subprocess.TimeoutExpired):
                            warning = BROWSER_CLEANUP_WARNING
                        if warning:
                            # Stopping must also show uncertain browser cleanup.
                            with self.lock:
                                task["error"] = warning
        except MonitorError as exc:
            raise CatalogFailure(str(exc)) from exc


if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    sys.stdout.reconfigure(encoding="utf-8")
    from monitor import MonitorError
    try:
        value = json.loads(sys.stdin.read(16385))
        output = {"ok": True, "data": load_catalog(value["provider"], value.get("scope", ""), value.get("fetcher"))}
    except (ValueError, KeyError, TypeError, MonitorError, requests.RequestException) as exc:
        output = {"ok": False, "error": str(exc) if isinstance(exc, (MonitorError, ValueError)) else "商家目录响应无效"}
    print(json.dumps(output, ensure_ascii=False))
