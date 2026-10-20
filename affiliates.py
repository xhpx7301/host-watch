"""Affiliate settings and purchase links; catalog snapshots always stay raw."""
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import re

PROVIDERS = ("vmiss", "vps_soy", "akile", "narwhal", "isvoro", "dmit", "bwh")
AFFILIATE_PROVIDERS = tuple(provider for provider in PROVIDERS if provider != "vps_soy")
DEFAULT_AFFILIATES = {
    "vmiss": "5340",
    "akile": "eb2ff10d-f57f-485f-9621-ed873b36c2c0",
    "isvoro": "c57baee34737",
    "dmit": "22544", "bwh": "84938",
}
HOSTS = {"vmiss": "app.vmiss.com", "akile": "akile.ai", "narwhal": "dash.fuckip.me", "isvoro": "isvoro.com", "dmit": "www.dmit.io", "bwh": "bwh81.net"}


def _affiliate_settings(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("AFF 配置必须是支持商家的对象")
    # Older configurations included VPS.SOY even though it has no affiliate program.
    return {key: item for key, item in value.items() if key != "vps_soy"}


def normalize_affiliate_coupons(value: dict) -> dict:
    """Normalize one default Telegram coupon per merchant.

    An empty value means that notifications inherit no coupon. Product-level
    overrides are kept on the product and resolved by ``effective_coupon``.
    """
    supplied = value.get("affiliate_coupons", {})
    if supplied is None:
        supplied = {}
    if not isinstance(supplied, dict) or any(key not in (*AFFILIATE_PROVIDERS, "vps_soy") for key in supplied):
        raise ValueError("商家优惠码必须是支持商家的对象")
    result = {provider: "" for provider in AFFILIATE_PROVIDERS}
    for provider, raw in _affiliate_settings(supplied).items():
        if not isinstance(raw, str):
            raise ValueError("商家优惠码必须是文本")
        code = raw.strip()
        if len(code) > 128 or any(ch in code for ch in "\r\n"):
            raise ValueError(f"{provider} 优惠码长度或格式无效")
        result[provider] = code
    return result


def effective_coupon(config: dict, item: dict | None = None, provider: str = "") -> str:
    item = item or {}
    override = item.get("notification_coupon_code")
    if isinstance(override, str) and override.strip():
        return override.strip()
    return str(config.get("affiliate_coupons", {}).get(provider, "") or "").strip()


def normalize_affiliate_notes(value: dict) -> dict:
    supplied = value.get("affiliate_notes", {})
    if supplied is None:
        supplied = {}
    if not isinstance(supplied, dict) or any(key not in (*AFFILIATE_PROVIDERS, "vps_soy") for key in supplied):
        raise ValueError("AFF 备注必须是支持商家的对象")
    result = {provider: "" for provider in AFFILIATE_PROVIDERS}
    for provider, raw in _affiliate_settings(supplied).items():
        if not isinstance(raw, str):
            raise ValueError("AFF 备注必须是文本")
        note = raw.strip()
        if len(note) > 240:
            raise ValueError(f"{provider} AFF 备注不能超过 240 个字符")
        result[provider] = note
    return result


def _normalize_affiliate_text_map(value: dict, key: str, label: str, limit: int) -> dict:
    supplied = value.get(key, {})
    if supplied is None:
        supplied = {}
    if not isinstance(supplied, dict) or any(provider not in (*AFFILIATE_PROVIDERS, "vps_soy") for provider in supplied):
        raise ValueError(f"{label}必须是支持商家的对象")
    result = {provider: "" for provider in AFFILIATE_PROVIDERS}
    for provider, raw in _affiliate_settings(supplied).items():
        if not isinstance(raw, str):
            raise ValueError(f"{label}必须是文本")
        text = raw.strip()
        if len(text) > limit or any(ch in text for ch in "\r\n"):
            raise ValueError(f"{provider}{label}长度或格式无效")
        result[provider] = text
    return result


def normalize_affiliate_accounts(value: dict) -> dict:
    return _normalize_affiliate_text_map(value, "affiliate_accounts", "AFF 账号", 120)


def normalize_affiliate_rates(value: dict) -> dict:
    return _normalize_affiliate_text_map(value, "affiliate_rates", "AFF 佣金比例", 40)


def normalize_affiliates(value: dict) -> dict:
    supplied = value.get("affiliates", {})
    if not isinstance(supplied, dict) or any(key not in (*AFFILIATE_PROVIDERS, "vps_soy") for key in supplied):
        raise ValueError("AFF 配置必须是支持商家的对象")
    result = dict(DEFAULT_AFFILIATES)
    if value.get("affiliate_id"):
        result["vmiss"] = str(value["affiliate_id"]).strip()
    for provider, raw in _affiliate_settings(supplied).items():
        if not isinstance(raw, str):
            raise ValueError("AFF 推广码必须是文本")
        code = raw.strip()
        if "://" in code:
            url = urlsplit(code)
            accepted_hosts = {HOSTS[provider]}
            if provider == "bwh":
                accepted_hosts.update({"www.bwh81.net", "bandwagonhost.com", "www.bandwagonhost.com"})
            if url.scheme != "https" or url.hostname not in accepted_hosts or url.username or url.password or url.port not in (None, 443):
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
    # Keep the order written by pre-BandwagonHost configurations stable until
    # the admin page saves its expanded provider list. New/default configs
    # include ``bwh`` explicitly and therefore receive it here as well.
    providers = PROVIDERS if "bwh" in value else PROVIDERS[:-1]
    return value + [key for key in providers if key not in value]


def affiliate_purchase_url(raw: str, provider: str, config: dict, item: dict | None = None) -> str:
    if provider not in HOSTS or not raw:
        return raw
    try:
        url = urlsplit(raw)
        accepted_hosts = {HOSTS[provider]}
        if provider == "bwh":
            accepted_hosts.update({"www.bwh81.net", "bandwagonhost.com", "www.bandwagonhost.com"})
        if url.scheme != "https" or url.hostname not in accepted_hosts or url.username or url.password or url.port not in (None, 443):
            return raw
        code = config.get("affiliates", {}).get(provider, config.get("affiliate_id", "") if provider == "vmiss" else "")
        query = [(k, v) for k, v in parse_qsl(url.query, keep_blank_values=True) if k not in ("aff", "aff_code")]
        if provider == "bwh" and code:
            values = [(key, value) for key, value in query if key != "a"]
            if (item or {}).get("pid") or dict(values).get("pid"):
                return "https://bwh81.net/aff.php?" + urlencode({"aff": code, **dict(values)})
            return urlunsplit(("https", "bwh81.net", url.path, urlencode(values + [("aff", code)]), url.fragment))
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
