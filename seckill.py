"""Read-only Isvoro campaign discovery and persistent once-per-activity alerts."""
from datetime import datetime
from html import escape
from pathlib import Path

from affiliates import affiliate_purchase_url
from notifications import hardware_text, network_text, price_text, purchase_button


TEST_PRODUCT_ID = "isvoro-seckill"
URL = "https://isvoro.com/seckill"


def timestamp(value):
    from monitor import MonitorError
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.utcoffset() is None:
            raise ValueError()
        return result
    except (AttributeError, TypeError, ValueError):
        raise MonitorError("Isvoro 秒杀活动时间格式异常，未更新通知记录") from None


def activities(session, fetcher):
    from monitor import MonitorError, fetch_isvoro_json, isvoro_network, network_number
    data = fetch_isvoro_json(session, "seckill/activities", fetcher)
    if not isinstance(data, dict) or not isinstance(data.get("activities"), list) or len(data["activities"]) > 100:
        raise MonitorError("Isvoro 秒杀活动列表结构异常，未更新通知记录")
    now = timestamp(data.get("server_time"))
    result, seen = [], set()
    for row in data["activities"]:
        if not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] <= 0 or row["id"] in seen or not isinstance(row.get("title"), str) or not row["title"].strip() or type(row.get("running")) is not bool:
            raise MonitorError("Isvoro 秒杀活动标识或状态异常，未更新通知记录")
        seen.add(row["id"])
        start, end = timestamp(row.get("starts_at")), timestamp(row.get("ends_at"))
        if end <= start:
            raise MonitorError("Isvoro 秒杀活动起止时间异常，未更新通知记录")
        items = row.get("items")
        if not isinstance(items, list) or len(items) > 200:
            raise MonitorError("Isvoro 秒杀套餐列表异常，未更新通知记录")
        plans, item_ids = [], set()
        for item in items:
            if not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] <= 0 or item["id"] in item_ids or not isinstance(item.get("product_name"), str) or not item["product_name"].strip():
                raise MonitorError("Isvoro 秒杀套餐标识异常，未更新通知记录")
            item_ids.add(item["id"])
            stock, sold = item.get("stock"), item.get("sold")
            price = network_number(item.get("seckill_price"))
            if type(stock) is not int or type(sold) is not int or stock < 0 or sold < 0 or price is None:
                raise MonitorError("Isvoro 秒杀套餐价格或数量异常，未更新通知记录")
            settings = item.get("settings", {})
            if isinstance(settings, list):
                if any(not isinstance(s, dict) for s in settings):
                    raise MonitorError("Isvoro 秒杀配置异常")
                settings = {s.get("key"): s.get("value") for s in settings}
            if not isinstance(settings, dict):
                raise MonitorError("Isvoro 秒杀配置异常")
            plans.append({"id": item["id"], "name": item["product_name"][:120], "plan": str(item.get("plan_name") or "")[:60],
                "price": price_text(f"{item.get('currency_code', 'CNY')} {price:g}"), "available": max(0, stock - sold),
                "specs": hardware_text(f"{settings.get('cores', '—')} 核 · {settings.get('memory', '—')} MB · {settings.get('disk', '—')} GB"),
                "traffic": network_text(isvoro_network(settings)), "url": f"{URL}/buy/{item['id']}"})
        # The public list can include previews. Do not alert for expired rows.
        if end <= now:
            continue
        result.append({"id": str(row["id"]), "title": row["title"][:160], "description": str(row.get("description") or "")[:500],
            "starts_at": start.isoformat(timespec="seconds"), "ends_at": end.isoformat(timespec="seconds"),
            "running": start <= now, "items": plans})
    return {"activities": result, "server_time": now.isoformat(timespec="seconds")}


def campaign_message(activity, config, test=False):
    def safe(value, limit):
        return escape(" ".join(str(value).split())[:limit])
    title = "🧪 HostWatch · 秒杀单次测试" if test else "⚡ HostWatch · Isvoro 限时秒杀"
    lines = [f"<b>{title}</b>", f"<b>{safe(activity['title'], 80)}</b>",
        "状态：" + ("进行中" if activity["running"] else "即将开始"),
        "开始：" + safe(activity["starts_at"], 32), "结束：" + safe(activity["ends_at"], 32)]
    if activity.get("description"):
        lines.append(safe(activity["description"], 120))
    for item in activity["items"][:6]:
        lines += ["", f"<b>{safe(item['name'], 45)}</b> · {safe(item['plan'], 15)}",
            f"秒杀价：{safe(item['price'], 24)} · 剩余 {item['available']} 份",
            f"<code>{safe(item['specs'], 45)}</code> · {safe(item['traffic'], 45)}"]
    if len(activity["items"]) > 6:
        lines.append(f"另有 {len(activity['items']) - 6} 个套餐，详见活动页。")
    link = affiliate_purchase_url(URL, "isvoro", config)
    lines += ["", f'<a href="{escape(link, quote=True)}">查看秒杀活动 / 手动购买</a>']
    if test:
        lines.append("单次测试，不修改自动通知记录。")
    return "\n".join(lines), purchase_button(link)


def state_file(path):
    path = Path(path)
    return path.with_name(path.stem + "-seckill.json")


def check_once(session, config, path, token, chat_id):
    from monitor import product_fetcher, read_state, telegram_request, write_state
    data = activities(session, product_fetcher(config.get("fetcher"), {"adapter": "isvoro"}))
    sent = read_state(state_file(path))
    delivered = 0
    for activity in data["activities"]:
        if activity["id"] in sent or not token or not chat_id or config.get("telegram", {}).get("disabled", False):
            continue
        message, keyboard = campaign_message(activity, config)
        telegram_request(session, token, "sendMessage", chat_id=chat_id, text=message, parse_mode="HTML", disable_web_page_preview=True, reply_markup=keyboard)
        sent[activity["id"]] = {"title": activity["title"], "sent_at": data["server_time"]}
        # Persist after every successful delivery, including a partially sent batch.
        write_state(state_file(path), sent)
        delivered += 1
    return {**data, "sent_count": delivered}
