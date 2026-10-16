"""Validated egress-node configuration and proxy selection helpers."""
from __future__ import annotations

import ipaddress
import base64
import json
import shlex
import time
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import requests


PROVIDERS = {"vmiss", "vps_soy", "akile", "isvoro", "dmit", "bwh", "html"}
IP_CHECK_URLS = ("https://api.ipify.org", "https://ifconfig.me/ip")


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
        pending = node.get("pending_registration", False)
        if not isinstance(pending, bool):
            raise ValueError("节点待安装状态必须是布尔值")
        proxy_url = "" if pending and not node.get("proxy_url") else _https_url(node.get("proxy_url", ""), f"出口节点 {name or node_id} 的代理地址", allow_http=True)
        health_url = "" if pending and not node.get("health_url") else _https_url(node.get("health_url", ""), f"出口节点 {name or node_id} 的健康检查地址", allow_http=True)
        port = node.get("listen_port", urlsplit(proxy_url).port or 8787)
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("节点监听端口必须是 1–65535 的整数")
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
            "pending_registration": pending, "listen_port": port,
            "last_seen": str(node.get("last_seen", ""))[:80], "last_error": str(node.get("last_error", ""))[:240]})
    routes = value.get("routes", {})
    if not isinstance(routes, dict) or any(str(provider) not in PROVIDERS or (node_id and node_id not in seen) for provider, node_id in routes.items()):
        raise ValueError("出口路由必须为支持商家到节点 ID 的映射")
    routes = {str(provider): str(node_id) for provider, node_id in routes.items() if node_id}
    return {"nodes": result, "routes": routes}


def installation_command(node: dict, register_url: str = "") -> dict:
    """Build a POSIX shell command; credentials are data, never shell syntax."""
    if register_url:
        register_url = _https_url(register_url, "主服务回报地址", allow_http=True)
        host = urlsplit(register_url).hostname
        local = host in {"localhost", "localhost.localdomain"}
        try:
            local = local or ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
        if local:
            register_url = ""
    if not node.get("token"):
        raise ValueError("请先设置节点代理令牌")
    settings = {"EGRESS_NODE_ID": node["id"], "EGRESS_NODE_NAME": node["name"],
                "EGRESS_TOKEN": node["token"], "EGRESS_LISTEN_HOST": "0.0.0.0",
                "EGRESS_LISTEN_PORT": node.get("listen_port", 8787), "EGRESS_REGISTER_URL": register_url}
    encoded = base64.b64encode(json.dumps(settings, ensure_ascii=False).encode()).decode()
    script_url = "https://raw.githubusercontent.com/xhpx7301/host-watch/main/install-egress.sh"
    bootstrap = (
        "set -eu; "
        "if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then "
        "if [ \"$(id -u)\" -ne 0 ]; then echo '请使用 root 执行安装命令。' >&2; exit 1; fi; "
        "if command -v apt-get >/dev/null 2>&1; then apt-get update; apt-get install -y ca-certificates curl; "
        "elif command -v apk >/dev/null 2>&1; then apk add --no-cache ca-certificates curl; "
        "else echo '仅支持 Debian 和 Alpine。' >&2; exit 1; fi; fi; "
        'installer=$(mktemp); trap \'rm -f "$installer"\' EXIT HUP INT TERM; '
        f'if command -v curl >/dev/null 2>&1; then curl -fsSL --connect-timeout 20 --max-time 120 {script_url} -o "$installer"; '
        f'else wget -T 120 -O "$installer" {script_url}; fi; '
        'sh "$installer" "$1"'
    )
    return {"command": "sh -c " + shlex.quote(bootstrap) + " sh " + shlex.quote(encoded),
            "auto_register": bool(register_url), "register_url": register_url}


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
    # requests discards both auth fields when password is None. The explicit
    # empty password is required to generate Proxy-Authorization: Basic token:.
    return urlunsplit((parsed.scheme, f"{quote(node['token'], safe='')}:@{parsed.netloc}", parsed.path, parsed.query, ""))


def apply_session(session: requests.Session, fetcher: dict | None) -> None:
    """Set or clear per-request proxy state; never silently fall back."""
    proxy = (fetcher or {}).get("egress_proxy_url", "")
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    elif hasattr(session, "proxies"):
        session.proxies = {}


def browser_proxy(fetcher: dict) -> dict:
    """FlareSolverr requires an object, including auth at session creation."""
    parsed = urlsplit(fetcher["egress_proxy_url"])
    proxy = {"url": urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, parsed.query, ""))}
    if parsed.username is not None:
        proxy.update(username=unquote(parsed.username), password=unquote(parsed.password or ""))
    return proxy


def proxy_error(exc: requests.RequestException) -> str:
    """Describe proxy failures without publishing credential-bearing URLs."""
    message = str(exc)
    if "407" in message:
        return "代理认证失败（HTTP 407）；请重新复制此节点的安装命令，在子服务器执行以同步令牌"
    if "502" in message:
        return "节点无法连接目标站点（HTTP 502）；请检查子服务器的 DNS 和到目标站点的网络"
    return "无法建立代理连接；请检查节点服务、代理令牌和主服务器到节点端口的连通性"


def probe(node: dict, timeout: float = 8) -> dict:
    started = time.monotonic()
    headers = {"Authorization": f"Bearer {node['token']}"} if node.get("token") else {}
    with requests.Session() as session:
        session.trust_env = False
        try:
            response = session.get(node["health_url"], headers=headers, timeout=timeout, allow_redirects=False)
            response.raise_for_status()
        except requests.HTTPError:
            if response.status_code == 401:
                raise ValueError("节点健康检查认证失败（HTTP 401）；请在子服务器重新执行此节点安装命令以同步令牌") from None
            raise ValueError("节点健康检查返回错误状态，请检查节点服务和健康检查地址") from None
        except requests.RequestException:
            raise ValueError("无法访问节点健康检查；请检查节点服务、监听端口、防火墙和健康检查地址") from None
        try:
            payload = response.json()
        except ValueError:
            raise ValueError("出口节点健康检查未返回有效 JSON") from None
        if not isinstance(payload, dict) or payload.get("node_id") != node["id"]:
            raise ValueError("出口节点健康响应无效，节点 ID 不匹配")
        try:
            proxy = connect_url(node)
        except RuntimeError as exc:
            raise ValueError(str(exc)) from None
        session.proxies = {"http": proxy, "https": proxy}
        failure = ""
        for endpoint in IP_CHECK_URLS:
            try:
                # This must use CONNECT + proxy auth + verified TLS, not /health.
                response = session.get(endpoint, timeout=timeout, allow_redirects=False)
                response.raise_for_status()
                actual_ip = str(ipaddress.ip_address(response.text.strip()))
                return {"ok": True, "public_ip": actual_ip, "reported_public_ip": str(payload.get("public_ip", "")),
                        "proxy_verified": True, "latency_ms": round((time.monotonic() - started) * 1000), "checked_at": time.time()}
            except requests.exceptions.ProxyError as exc:
                raise ValueError("节点在线，但 HTTPS 代理验证失败：" + proxy_error(exc)) from None
            except requests.exceptions.SSLError:
                failure = "HTTPS 证书验证失败，请检查主服务器的 CA 证书和系统时间"
            except requests.Timeout:
                failure = "HTTPS 代理请求超时，请检查子服务器出站网络或 IP 检测站点可达性"
            except (requests.RequestException, ValueError):
                failure = "经代理读取公网 IP 失败，请检查子服务器出站网络或 IP 检测站点可达性"
        raise ValueError("节点在线，但尚未验证 HTTPS 代理可用：" + failure)
