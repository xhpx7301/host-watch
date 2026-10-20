"""Shared display formatting for Telegram stock alerts and explicit test results."""
from __future__ import annotations

from html import escape
import math
import re
from urllib.parse import parse_qs, urlencode, urlsplit
from time_utils import format_beijing


CURRENCIES = {
    "CNY": "¥", "CNH": "¥", "RMB": "¥", "USD": "$", "CAD": "CA$", "AUD": "A$", "NZD": "NZ$",
    "HKD": "HK$", "SGD": "S$", "TWD": "NT$", "JPY": "JP¥", "EUR": "€", "GBP": "£", "KRW": "₩",
    "RUB": "₽", "INR": "₹", "TRY": "₺", "THB": "฿", "VND": "₫", "IDR": "Rp", "MYR": "RM",
    "BRL": "R$", "PHP": "₱", "CHF": "Fr.",
}
# Display-only reference rates; merchant checkout remains authoritative.
CNY_RATES = {
    "USD": 7.2, "CAD": 5.1, "AUD": 4.7, "NZD": 4.25, "HKD": 0.92, "SGD": 5.35, "TWD": 0.225,
    "JPY": 0.048, "EUR": 7.8, "GBP": 9.05, "KRW": 0.0052, "RUB": 0.08, "INR": 0.086, "TRY": 0.2,
    "THB": 0.2, "VND": 0.00029, "IDR": 0.00045, "MYR": 1.62, "BRL": 1.35, "PHP": 0.126, "CHF": 8.1,
}


def hardware_text(raw: str) -> str:
    value = re.sub(r"\s*·\s*", "/", str(raw or "—"))
    value = re.sub(r"(?:CPU\s*[:：]?\s*)?(\d+(?:\.\d+)?)\s*(?:核|v?CPU\b|cores?\b)", r"\1C", value, flags=re.I)
    def memory(match):
        amount = float(match[1]) / (1024 if match[2].lower().startswith("m") else 1)
        return f"{amount:g}G"
    value = re.sub(r"(?:内存|memory|RAM)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(Mi?B|Gi?B|M|G)\b", memory, value, flags=re.I)
    value = re.sub(r"(?:硬盘|磁盘|disk|storage)\s*[:：]?\s*(?=\d)", "", value, flags=re.I)
    value = re.sub(r"(\d+(?:\.\d+)?)\s*(Mi?B)\b", memory, value, flags=re.I)
    value = re.sub(r"(\d+(?:\.\d+)?C\s*/\s*)(\d+(?:\.\d+)?)\s*(Gi?B)\b", lambda m: m[1] + f"{float(m[2]):g}G", value, flags=re.I)
    value = re.sub(r"(\d)\s+([KMGT]i?B)\b", r"\1\2", value, flags=re.I)
    return re.sub(r"\s*/\s*", "/", value)


def network_text(raw: str) -> str:
    parts = re.split(r"\s*(?:·|@|\||\n)\s*|\s+/\s+", str(raw or "—"))
    cleaned = []
    for part in parts:
        part = re.sub(r"^(?:流量|带宽|traffic|bandwidth)\s*[:：]?\s*", "", part, flags=re.I)
        part = re.sub(r"(?:流量|带宽|\s+(?:traffic|bandwidth|port))$", "", part, flags=re.I).strip()
        part = re.sub(r"(\d)\s+([KMGT]i?B|[KMGT]?bps)\b", r"\1\2", part, flags=re.I)
        if part:
            cleaned.append(part)
    def speed(part):
        return bool(re.search(r"[KMGT]?bps|[KMGT]?B/s|带宽|bandwidth", part, re.I))
    return "@".join([p for p in cleaned if not speed(p)] + [p for p in cleaned if speed(p)]) or "—"


def price_text(raw: str) -> str:
    value = str(raw or "以商家页面为准")
    codes, amount = "|".join(CURRENCIES), r"([+-]?\d[\d,]*(?:\.\d+)?)"
    value = re.sub(rf"(?<![A-Za-z])({codes})\s*[$¥￥]?\s*{amount}", lambda m: CURRENCIES[m[1].upper()] + m[2], value, flags=re.I)
    value = re.sub(rf"[$¥￥]?\s*{amount}\s*({codes})(?![A-Za-z])", lambda m: CURRENCIES[m[2].upper()] + m[1], value, flags=re.I)
    value = re.sub(r"([¥$€£₩₽₹₺฿₫₱])\s+(?=\d)", r"\1", value.replace("￥", "¥"))
    for pattern, replacement in [
        (r"(?:per\s+)?semi[- ]?annual(?:ly)?|semiannually|half[- ]?year(?:ly)?", "半年"),
        (r"(?:per\s+)?quarter(?:ly)?", "季"),
        (r"(?:per\s+)?annual(?:ly)?|year(?:ly)?|year", "年"),
        (r"(?:per\s+)?monthly|month|mo", "月"),
        (r"(?:per\s+)?weekly|week", "周"),
        (r"(?:per\s+)?daily|day", "日"),
    ]:
        value = re.sub(pattern, replacement, value, flags=re.I)
    value = _collapse_repeated_price(value)
    converted = price_conversion(raw)
    return value + (f"\n{converted}" if converted else "")


def _collapse_repeated_price(value: str) -> str:
    """Collapse a duplicated amount/cycle sequence from repeated page nodes."""
    parts = [part.strip() for part in re.split(r"\s*/\s*", value)]
    if len(parts) < 4 or len(parts) % 2:
        return value
    half = len(parts) // 2
    cycles = {"月", "季", "半年", "年", "周", "日"}
    if (parts[:half] == parts[half:]
            and all(parts[index] in cycles for index in range(1, half, 2))):
        return " / ".join(parts[:half])
    return value


def price_conversion(raw: str) -> str:
    value = str(raw or "")
    amount = r"([+-]?\d[\d,]*(?:\.\d+)?)"
    codes = "|".join(CNY_RATES)
    match = re.search(rf"\b({codes})\s*[$¥￥]?\s*{amount}", value, re.I)
    currency, number = (match.group(1).upper(), match.group(2)) if match else ("", "")
    if not match:
        match = re.search(rf"{amount}\s*({codes})\b", value, re.I)
        if match:
            number, currency = match.group(1), match.group(2).upper()
    if not match:
        symbol_match = re.search(rf"(CA\$|A\$|NZ\$|HK\$|S\$|NT\$|JP¥|R\$|RM|Fr\.|[$€£₩₽₹₺฿₫₱])\s*{amount}", value, re.I)
        symbols = {"CA$": "CAD", "A$": "AUD", "NZ$": "NZD", "HK$": "HKD", "S$": "SGD", "NT$": "TWD", "JP¥": "JPY", "R$": "BRL", "RM": "MYR", "FR.": "CHF", "$": "USD", "€": "EUR", "£": "GBP", "₩": "KRW", "₽": "RUB", "₹": "INR", "₺": "TRY", "฿": "THB", "₫": "VND", "₱": "PHP"}
        if symbol_match:
            currency, number = symbols[symbol_match.group(1).upper()], symbol_match.group(2)
    if not currency or not number:
        return ""
    result = float(number.replace(",", "")) * CNY_RATES[currency]
    if not math.isfinite(result):
        return ""
    cycle = re.search(r"(?:/|per\s+)\s*(月|季|半年|年|周|日)", price_text_cycle(value), re.I)
    return f"折合 ¥{result:.1f}" + (f"/{cycle.group(1)}" if cycle else "")


def price_text_cycle(value: str) -> str:
    for pattern, replacement in [
        (r"(?:per\s+)?semi[- ]?annual(?:ly)?|semiannually|half[- ]?year(?:ly)?", "半年"),
        (r"(?:per\s+)?quarter(?:ly)?", "季"),
        (r"(?:per\s+)?annual(?:ly)?|year(?:ly)?|year", "年"),
        (r"(?:per\s+)?monthly|month|mo", "月"),
        (r"(?:per\s+)?weekly|week", "周"),
        (r"(?:per\s+)?daily|day", "日"),
    ]:
        value = re.sub(pattern, replacement, value, flags=re.I)
    return value


def purchase_button(url: str, label: str = "🛒 立即购买") -> dict:
    try:
        parsed = urlsplit(url)
        if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password:
            return {"inline_keyboard": [[{"text": label, "url": url}]]}
    except ValueError:
        pass
    return {}


REGION_FLAG_ALIASES = {
    "中国香港": ("🇭🇰", "中国香港"), "香港": ("🇭🇰", "中国香港"), "hong kong": ("🇭🇰", "中国香港"), "hk": ("🇭🇰", "中国香港"),
    "中国澳门": ("🇲🇴", "中国澳门"), "澳门": ("🇲🇴", "中国澳门"), "macao": ("🇲🇴", "中国澳门"), "macau": ("🇲🇴", "中国澳门"), "mo": ("🇲🇴", "中国澳门"),
    "中国台湾": ("🇹🇼", "中国台湾"), "台湾": ("🇹🇼", "中国台湾"), "taiwan": ("🇹🇼", "中国台湾"), "tw": ("🇹🇼", "中国台湾"),
    "日本": ("🇯🇵", "日本"), "japan": ("🇯🇵", "日本"), "jp": ("🇯🇵", "日本"),
    "韩国": ("🇰🇷", "韩国"), "south korea": ("🇰🇷", "韩国"), "korea": ("🇰🇷", "韩国"), "kr": ("🇰🇷", "韩国"),
    "新加坡": ("🇸🇬", "新加坡"), "singapore": ("🇸🇬", "新加坡"), "sg": ("🇸🇬", "新加坡"),
    "美国": ("🇺🇸", "美国"), "united states": ("🇺🇸", "美国"), "usa": ("🇺🇸", "美国"), "us": ("🇺🇸", "美国"),
    "加拿大": ("🇨🇦", "加拿大"), "canada": ("🇨🇦", "加拿大"), "ca": ("🇨🇦", "加拿大"),
    "英国": ("🇬🇧", "英国"), "united kingdom": ("🇬🇧", "英国"), "uk": ("🇬🇧", "英国"), "gb": ("🇬🇧", "英国"),
    "德国": ("🇩🇪", "德国"), "germany": ("🇩🇪", "德国"), "de": ("🇩🇪", "德国"),
    "法国": ("🇫🇷", "法国"), "france": ("🇫🇷", "法国"), "fr": ("🇫🇷", "法国"),
    "荷兰": ("🇳🇱", "荷兰"), "netherlands": ("🇳🇱", "荷兰"), "nl": ("🇳🇱", "荷兰"),
    "澳大利亚": ("🇦🇺", "澳大利亚"), "australia": ("🇦🇺", "澳大利亚"), "au": ("🇦🇺", "澳大利亚"),
    "中国": ("🇨🇳", "中国"), "china": ("🇨🇳", "中国"), "cn": ("🇨🇳", "中国"),
    "越南": ("🇻🇳", "越南"), "vietnam": ("🇻🇳", "越南"), "vn": ("🇻🇳", "越南"),
    "泰国": ("🇹🇭", "泰国"), "thailand": ("🇹🇭", "泰国"), "th": ("🇹🇭", "泰国"),
    "印度": ("🇮🇳", "印度"), "india": ("🇮🇳", "印度"), "in": ("🇮🇳", "印度"),
    "马来西亚": ("🇲🇾", "马来西亚"), "malaysia": ("🇲🇾", "马来西亚"), "my": ("🇲🇾", "马来西亚"),
    "印度尼西亚": ("🇮🇩", "印度尼西亚"), "indonesia": ("🇮🇩", "印度尼西亚"), "id": ("🇮🇩", "印度尼西亚"),
    "巴西": ("🇧🇷", "巴西"), "brazil": ("🇧🇷", "巴西"), "br": ("🇧🇷", "巴西"),
    "爱尔兰": ("🇮🇪", "爱尔兰"), "ireland": ("🇮🇪", "爱尔兰"), "ie": ("🇮🇪", "爱尔兰"),
    "瑞士": ("🇨🇭", "瑞士"), "switzerland": ("🇨🇭", "瑞士"), "ch": ("🇨🇭", "瑞士"),
    "西班牙": ("🇪🇸", "西班牙"), "spain": ("🇪🇸", "西班牙"), "es": ("🇪🇸", "西班牙"),
    "意大利": ("🇮🇹", "意大利"), "italy": ("🇮🇹", "意大利"), "it": ("🇮🇹", "意大利"),
    "波兰": ("🇵🇱", "波兰"), "poland": ("🇵🇱", "波兰"), "pl": ("🇵🇱", "波兰"),
    "瑞典": ("🇸🇪", "瑞典"), "sweden": ("🇸🇪", "瑞典"), "se": ("🇸🇪", "瑞典"),
    "芬兰": ("🇫🇮", "芬兰"), "finland": ("🇫🇮", "芬兰"), "fi": ("🇫🇮", "芬兰"),
    "挪威": ("🇳🇴", "挪威"), "norway": ("🇳🇴", "挪威"), "no": ("🇳🇴", "挪威"),
    "俄罗斯": ("🇷🇺", "俄罗斯"), "russia": ("🇷🇺", "俄罗斯"), "ru": ("🇷🇺", "俄罗斯"),
    "阿联酋": ("🇦🇪", "阿联酋"), "united arab emirates": ("🇦🇪", "阿联酋"), "uae": ("🇦🇪", "阿联酋"), "ae": ("🇦🇪", "阿联酋"),
}


def telegram_region(value: str) -> str:
    """Add a country flag to Telegram region labels without changing app data."""
    region = " ".join(str(value or "").split())
    if not region or any(ord(char) in range(0x1F1E6, 0x1F200) for char in region):
        return region
    lowered = region.casefold()
    for alias in sorted(REGION_FLAG_ALIASES, key=len, reverse=True):
        compact = re.match(r"[a-z0-9._]+", lowered[len(alias):]) if lowered.startswith(alias) else None
        if compact and len(alias) + compact.end() < len(lowered) and lowered[len(alias) + compact.end()] not in " ·-/,:：":
            compact = None
        if compact:
            remainder = region[len(alias) + compact.end():].strip(" ·-/,:：")
            flag, localized = REGION_FLAG_ALIASES[alias]
            return f"{flag} {localized}" + (f" · {remainder}" if remainder else "")
        prefix = lowered == alias or lowered.startswith(alias) and len(lowered) > len(alias) and lowered[len(alias)] in " ·-/,:："
        suffix = lowered.endswith(" " + alias) or lowered.endswith(" · " + alias) or lowered.endswith(", " + alias)
        if prefix or suffix:
            flag, localized = REGION_FLAG_ALIASES[alias]
            if suffix and not prefix:
                remainder = re.sub(r"[\s·,/\-]+$", "", region[:len(region) - len(alias)])
                return f"{flag} {localized}" + (f" · {remainder}" if remainder else "")
            remainder = region[len(alias):] if prefix and lowered != alias else ""
            return f"{flag} {localized}{remainder}"
    return region


def stock_message(*, vendor: str, name: str, available: int, stock_mode: str, region: str = "", specs: str = "",
                  traffic: str = "", price: str = "", line: str = "", item: dict | None = None,
                  old_count: int | None = None, checked_at: str = "", test: bool = False,
                  duration: object = None, warning: str = "", purchase_url: str = "",
                  live: bool = False, uncertain: bool = False, coupon_code: str = "", mother_name: str = "",
                  expires_at: str = "", configurations: list | None = None, machine_id: str = "") -> str:
    # Leave room for labels, links and manual-test diagnostics within Telegram's limit.
    remaining_units = 2400
    def safe(value, limit=200):
        nonlocal remaining_units
        text = " ".join(str(value if value is not None else "").split())
        clipped = text[:limit]
        encoded = clipped.encode("utf-16-le")
        clipped = encoded[:max(0, remaining_units - 1) * 2].decode("utf-16-le", errors="ignore")
        if len(clipped) < len(text) and remaining_units:
            clipped += "…"
        remaining_units -= len(clipped.encode("utf-16-le")) // 2
        return escape(clipped)
    item = item or {}
    checked_at = format_beijing(checked_at)
    status = "有货" if stock_mode == "availability" and available > 0 else f"{available} 台" if available > 0 else "缺货"
    change = ""
    if not test and not live:
        change = "首次发现有货" if old_count is None else "由缺货变为有货" if old_count == 0 else f"增加 {available - old_count} 台"
    title = "🧪 HostWatch · 商品单次测试" if test else "✅ HostWatch · 补货提醒"
    if live:
        status = ("有货" + (f" · 剩余 {available} 台" if stock_mode != "availability" else "")) if available > 0 else "售罄"
        if uncertain:
            status = "暂未确认（上次成功检查：" + status + "）"
        title = "⚠️ HostWatch · 库存暂未确认" if uncertain else "✅ HostWatch · 补货提醒" if available > 0 else "❌ HostWatch · 已售罄"
    is_narwhal = vendor in {"独角鲸云", "独角鲸鱼"}
    heading = "独角鲸鱼" if is_narwhal else f"{safe(vendor, 80)} · {safe(name, 200)}"
    lines = [f"<b>{title}</b>", f"<b>{heading}</b>", "",
             f"🌏 <b>地区：</b>{safe(telegram_region(region) or '—', 120)}"]
    if line:
        lines.append(f"🛣 <b>地区/线路：</b>{safe(line, 160)}")
    if mother_name:
        lines.append(f"🖥 <b>母鸡名称：</b>{safe(mother_name, 120)}")
    if expires_at:
        lines.append(f"📅 <b>母鸡到期时间：</b>{safe(expires_at, 40)}")
    if is_narwhal:
        lines.append(f"📦 <b>套餐名：</b>{safe(str(name).upper(), 200)}")
    if item.get("remarks"):
        lines.append(f"📝 <b>备注：</b>{safe(item['remarks'])}")
    if coupon_code:
        # Telegram renders <code> as a copyable monospace token in clients.
        lines.append(f"🎟 <b>优惠码：</b><code>{safe(coupon_code, 128)}</code>（点击复制）")
    carriers = [f"{label}：{safe(item.get('line_' + key), 100)}" for key, label in (("telecom", "电"), ("unicom", "联"), ("mobile", "移")) if item.get("line_" + key)]
    if carriers or item.get("line_label"):
        lines.append("📡 <b>线路：</b>" + (" | ".join(carriers) if carriers else safe(item["line_label"])))
    lines.extend([f"🖥 <b>硬件配置：</b>{safe(hardware_text(specs), 300)}",
                  f"🌐 <b>流量/带宽：</b>{safe(network_text(traffic), 200)}",
                  f"💰 <b>价格：</b>{safe(price_text(price), 350)}", "",
                  f"{'⚠️' if uncertain else '✅' if available > 0 else '❌'} <b>库存：</b>{status}" + (f"（{change}）" if change else "")])
    if configurations:
        lines += ["", "📦 <b>可选配置：</b>"]
        affiliate = parse_qs(urlsplit(purchase_url).query).get("aff", [""])[0]
        shown = 0
        for option in configurations[:5]:
            if not isinstance(option, dict):
                continue
            if shown:
                lines.append("")
            shown += 1
            stock = option.get("stock")
            label = "已售罄" if stock == 0 else f"剩 {stock} 台" if type(stock) is int else "库存未知"
            link = ""
            if stock != 0 and option.get("id") and machine_id:
                params = {"plan_id": str(option["id"]), "machine_id": machine_id}
                if affiliate:
                    params["aff"] = affiliate
                url = "https://dash.fuckip.me/deploy?" + urlencode(params)
                link = f' · <a href="{escape(url, quote=True)}">点击抢购</a>'
            lines.append(f"🔸 {safe(option.get('name', '配置'), 60)} · {safe(option.get('price', '价格未知'), 40)} [{label}]{link}")
            lines.append(f"⚙️ {safe(option.get('specs', ''), 100)} · 🌐 {safe(option.get('traffic', ''), 100)}")
        if len(configurations) > 5:
            lines.append(f"另有 {len(configurations) - 5} 个配置，请查看购买页面。")
        lines.append("")
    lines.append(f"🕒 <b>{'最后成功检查' if live else '检查时间'}：</b>{safe(checked_at, 80)}")
    if duration is not None:
        lines.append(f"⏱ <b>耗时：</b>{safe(duration, 40)} 秒")
    if warning:
        lines.append(f"提示：{safe(warning, 300)}")
    if test:
        lines.append("\n单次测试，不改变自动监控状态。")
    if purchase_button(purchase_url):
        lines.append(f'\n🛒 <b>购买链接：</b><a href="{escape(purchase_url, quote=True)}">点击直达购买</a>')
    return "\n".join(lines)
