"""Shared display formatting for Telegram stock alerts and explicit test results."""
from __future__ import annotations

from html import escape
import re
from urllib.parse import urlsplit


CURRENCIES = {
    "CNY": "¥", "CNH": "¥", "RMB": "¥", "USD": "$", "CAD": "CA$", "AUD": "A$", "NZD": "NZ$",
    "HKD": "HK$", "SGD": "S$", "TWD": "NT$", "JPY": "JP¥", "EUR": "€", "GBP": "£", "KRW": "₩",
    "RUB": "₽", "INR": "₹", "TRY": "₺", "THB": "฿", "VND": "₫", "IDR": "Rp", "MYR": "RM",
    "BRL": "R$", "PHP": "₱", "CHF": "Fr.",
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
    return re.sub(r"([¥$€£₩₽₹₺฿₫₱])\s+(?=\d)", r"\1", value.replace("￥", "¥"))


def purchase_button(url: str) -> dict:
    try:
        parsed = urlsplit(url)
        if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password:
            return {"inline_keyboard": [[{"text": "🛒 立即购买", "url": url}]]}
    except ValueError:
        pass
    return {}


def stock_message(*, vendor: str, name: str, available: int, stock_mode: str, region: str = "", specs: str = "",
                  traffic: str = "", price: str = "", line: str = "", item: dict | None = None,
                  old_count: int | None = None, checked_at: str = "", test: bool = False,
                  duration: object = None, warning: str = "", purchase_url: str = "",
                  live: bool = False, uncertain: bool = False) -> str:
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
    lines = [f"<b>{title}</b>", f"<b>{safe(vendor, 80)} · {safe(name, 200)}</b>", "",
             f"🌏 <b>地区：</b>{safe(region or '—', 120)}"]
    if line:
        lines.append(f"🛣 <b>地区/线路：</b>{safe(line, 160)}")
    if item.get("remarks"):
        lines.append(f"📝 <b>备注：</b>{safe(item['remarks'])}")
    carriers = [f"{label}：{safe(item.get('line_' + key), 100)}" for key, label in (("telecom", "电"), ("unicom", "联"), ("mobile", "移")) if item.get("line_" + key)]
    if carriers or item.get("line_label"):
        lines.append("📡 <b>线路：</b>" + (" | ".join(carriers) if carriers else safe(item["line_label"])))
    lines.extend([f"🖥 <b>硬件配置：</b><code>{safe(hardware_text(specs), 300)}</code>",
                  f"🌐 <b>流量/带宽：</b>{safe(network_text(traffic), 200)}",
                  f"💰 <b>价格：</b>{safe(price_text(price), 350)}", "",
                  f"{'⚠️' if uncertain else '✅' if available > 0 else '❌'} <b>库存：</b>{status}" + (f"（{change}）" if change else ""),
                  f"🕒 <b>{'最后成功检查' if live else '检查时间'}：</b>{safe(checked_at, 80)}"])
    if duration is not None:
        lines.append(f"⏱ <b>耗时：</b>{safe(duration, 40)} 秒")
    if warning:
        lines.append(f"提示：{safe(warning, 300)}")
    if test:
        lines.append("\n单次测试，不改变自动监控状态。")
    if purchase_button(purchase_url):
        lines.append(f'\n🛒 <a href="{escape(purchase_url, quote=True)}">点击直达购买</a>')
    return "\n".join(lines)
