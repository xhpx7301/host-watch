"""Affiliate settings and purchase links; catalog snapshots always stay raw."""
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import re

PROVIDERS = ("vmiss", "vps_soy", "akile", "isvoro", "dmit")
DEFAULT_AFFILIATES = {
    "vmiss": "5340", "vps_soy": "",
    "akile": "eb2ff10d-f57f-485f-9621-ed873b36c2c0",
    "isvoro": "c57baee34737",
    "dmit": "22544",
}
HOSTS = {"vmiss": "app.vmiss.com", "vps_soy": "www.vps.soy", "akile": "akile.ai", "isvoro": "isvoro.com", "dmit": "www.dmit.io"}


def normalize_affiliates(value: dict) -> dict:
    supplied = value.get("affiliates", {})
    if not isinstance(supplied, dict) or any(key not in PROVIDERS for key in supplied):
        raise ValueError("AFF 配置必须是支持商家的对象")
    result = dict(DEFAULT_AFFILIATES)
    if value.get("affiliate_id"):
        result["vmiss"] = str(value["affiliate_id"]).strip()
    for provider, raw in supplied.items():
        if not isinstance(raw, str):
            raise ValueError("AFF 推广码必须是文本")
        code = raw.strip()
        if "://" in code:
            url = urlsplit(code)
            if url.scheme != "https" or url.hostname != HOSTS[provider] or url.username or url.password or url.port not in (None, 443):
                raise ValueError(f"{provider} 推广链接域名无效")
            parameter = "aff_code" if provider == "akile" else "aff"
            codes = [v for k, v in parse_qsl(url.query) if k == parameter]
            if len(codes) != 1:
                raise ValueError(f"{provider} 推广链接缺少唯一的 {parameter}")
            code = codes[0]
        pattern = r"\d{1,20}" if provider in {"vmiss", "dmit"} else r"[A-Za-z0-9_-]{1,128}"
        if code and not re.fullmatch(pattern, code):
            raise ValueError(f"{provider} AFF 推广码格式无效")
        result[provider] = code
    return result


def normalize_merchant_order(value: object) -> list:
    if not isinstance(value, list) or any(not isinstance(key, str) or key not in PROVIDERS for key in value) or len(set(value)) != len(value):
        raise ValueError("商家顺序必须是无重复的支持商家列表")
    return value + [key for key in PROVIDERS if key not in value]


def affiliate_purchase_url(raw: str, provider: str, config: dict, item: dict | None = None) -> str:
    if provider not in HOSTS or not raw:
        return raw
    try:
        url = urlsplit(raw)
        if url.scheme != "https" or url.hostname != HOSTS[provider] or url.username or url.password or url.port not in (None, 443):
            return raw
        code = config.get("affiliates", {}).get(provider, config.get("affiliate_id", "") if provider == "vmiss" else "")
        query = [(k, v) for k, v in parse_qsl(url.query, keep_blank_values=True) if k not in ("aff", "aff_code")]
        if provider in {"vmiss", "dmit"} and code:
            ids = item or {}
            if provider == "dmit" and not ids.get("pid"):
                values = dict(query)
                ids = {"pid": values.get("pid", "")}
            key = "pid" if ids.get("pid") else "gid" if ids.get("gid") else ""
            if key:
                return f"https://{HOSTS[provider]}/aff.php?" + urlencode({"aff": code, key: ids[key]})
        if code:
            query.append(("aff_code" if provider == "akile" else "aff", code))
        return urlunsplit((url.scheme, url.netloc, url.path, urlencode(query), url.fragment))
    except ValueError:
        return raw
