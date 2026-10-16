"""Validated egress-node configuration and proxy selection helpers."""
from __future__ import annotations

import ipaddress
import time
from urllib.parse import quote, urlsplit, urlunsplit

import requests


PROVIDERS = {"vmiss", "vps_soy", "akile", "isvoro", "dmit", "bwh", "html"}


def _https_url(value: object, label: str, allow_http: bool = False) -> str:
    url = str(value or "").strip()
    parsed = urlsplit(url)
    schemes = {"https", "http"} if allow_http else {"https"}
    if not url or parsed.scheme not in schemes or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or len(url) > 2048:
        raise ValueError(f"{label}必须是有效的 {'HTTP(S)' if allow_http else 'HTTPS'} URL")
    return url.rstrip("/")


def normalize(value: object) -> dict:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("出口节点配置必须是对象")
    nodes = value.get("nodes", [])
    if not isinstance(nodes, list) or len(nodes) > 50:
        raise ValueError("出口节点最多 50 个")
    result, seen = [], set()
    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError("出口节点必须是对象")
        node_id = str(node.get("id", "")).strip()
        name = str(node.get("name", node_id)).strip()
        proxy_url = _https_url(node.get("proxy_url", ""), f"出口节点 {name or node_id} 的代理地址", allow_http=True)
        health_url = _https_url(node.get("health_url", ""), f"出口节点 {name or node_id} 的健康检查地址", allow_http=True)
        token = str(node.get("token", ""))
        public_ip = str(node.get("public_ip", "")).strip()
        if not node_id or len(node_id) > 64 or not all(char.isalnum() or char in "_-" for char in node_id) or node_id in seen:
            raise ValueError("出口节点 ID 必须唯一且只包含字母、数字、下划线或短横线")
        if not name or len(name) > 120 or len(token) > 512:
            raise ValueError("出口节点名称或令牌长度无效")
        if public_ip:
            try:
                ipaddress.ip_address(public_ip)
            except ValueError:
                raise ValueError(f"出口节点 {name} 的公网 IP 无效") from None
        enabled = node.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"出口节点 {name} 的启用状态必须是布尔值")
        seen.add(node_id)
        result.append({"id": node_id, "name": name, "proxy_url": proxy_url, "health_url": health_url,
            "token": token, "public_ip": public_ip, "enabled": enabled,
            "last_seen": str(node.get("last_seen", ""))[:80], "last_error": str(node.get("last_error", ""))[:240]})
    routes = value.get("routes", {})
    if not isinstance(routes, dict) or any(str(provider) not in PROVIDERS or (node_id and node_id not in seen) for provider, node_id in routes.items()):
        raise ValueError("出口路由必须为支持商家到节点 ID 的映射")
    routes = {str(provider): str(node_id) for provider, node_id in routes.items() if node_id}
    return {"nodes": result, "routes": routes}


def public(value: object) -> dict:
    normalized = normalize(value)
    for node in normalized["nodes"]:
        configured = bool(node.get("token"))
        node["token"] = ""
        node["token_configured"] = configured
    return normalized


def resolve(value: object, provider: str, explicit: str = "") -> dict | None:
    normalized = normalize(value)
    node_id = explicit or normalized["routes"].get(provider, "")
    if not node_id:
        return None
    node = next((item for item in normalized["nodes"] if item["id"] == node_id), None)
    if not node:
        raise RuntimeError(f"商家 {provider} 绑定的出口节点不存在")
    if not node["enabled"]:
        raise RuntimeError(f"出口节点 {node['name']} 已停用，已阻止主服务器直连")
    if not node["proxy_url"]:
        raise RuntimeError(f"出口节点 {node['name']} 没有代理地址，已阻止主服务器直连")
    return node


def connect_url(node: dict) -> str:
    """Return a proxy URL with the node token in standard Basic proxy auth."""
    parsed = urlsplit(node["proxy_url"])
    if not node.get("token"):
        raise RuntimeError(f"出口节点 {node['name']} 未配置代理令牌，已阻止主服务器直连")
    return urlunsplit((parsed.scheme, f"{quote(node['token'], safe='')}@{parsed.netloc}", parsed.path, parsed.query, ""))


def apply_session(session: requests.Session, fetcher: dict | None) -> None:
    """Set or clear per-request proxy state; never silently fall back."""
    proxy = (fetcher or {}).get("egress_proxy_url", "")
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    elif hasattr(session, "proxies"):
        session.proxies = {}


def probe(node: dict, timeout: float = 8) -> dict:
    started = time.monotonic()
    headers = {"Authorization": f"Bearer {node['token']}"} if node.get("token") else {}
    response = requests.get(node["health_url"], headers=headers, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("node_id") != node["id"] or not payload.get("public_ip"):
        raise ValueError("出口节点健康响应无效")
    return {"ok": True, "public_ip": str(payload["public_ip"]), "latency_ms": round((time.monotonic() - started) * 1000), "checked_at": time.time()}
