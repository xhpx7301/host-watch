"""One Telegram message per restock cycle, persisted with the stock baseline."""
from __future__ import annotations

import hashlib
import json
import logging
import time
from affiliates import effective_coupon
from notifications import purchase_button, stock_message
from time_utils import now_beijing


REFRESH_SECONDS = 60


class StockAlerts:
    def __init__(self, backend, session, config, state_path, current, token, chat_id):
        self.backend = backend
        self.session, self.config, self.path, self.current = session, config, state_path, current
        self.token, self.chat_id = token, str(chat_id)
        self.enabled = bool(token and chat_id and not config.get("telegram", {}).get("disabled", False))
        # Bind message IDs to their bot and chat without storing credentials.
        self.target = hashlib.sha256((token + "\n" + self.chat_id).encode()).hexdigest()
        self.warnings = []

    def save(self):
        self.backend.write_state(self.path, self.current)

    def record(self, item):
        entry = self.current.get(item["id"], {})
        record = entry.get("telegram")
        if isinstance(record, dict) and record.get("target") == self.target:
            return record
        return None

    def payload(self, item, product):
        details = product.match_details or {}
        narwhal = product.vendor == "独角鲸云"
        display_name = (details.get("plan_name") or product.name) if narwhal else product.name
        return dict(vendor=product.vendor, name=display_name, available=product.available, stock_mode=product.stock_mode,
            region=product.region or product.area_name, specs=product.specs, traffic=product.traffic, price=product.price,
            line="" if narwhal else details.get("category_name", "") or product.node_name or item.get("category_name", ""),
            coupon_code=effective_coupon(self.config, item, item.get("adapter", "")),
            item={key: item[key] for key in ("remarks", "line_label", "line_telecom", "line_unicom", "line_mobile") if item.get(key)},
            checked_at=now_beijing().strftime("%Y-%m-%d %H:%M:%S 北京时间"), purchase_url=self.backend.purchase_link(product, item, self.config),
            mother_name=product.node_name if narwhal else "", expires_at=details.get("machine_expires_at", "") if narwhal else "",
            configurations=details.get("machine_plans") if narwhal else None, machine_id=str(details.get("machine_id", "")) if narwhal else "")

    def edit(self, record, payload, uncertain=False):
        if record.get("uneditable"):
            return
        content = {key: value for key, value in payload.items() if key != "checked_at"}
        signature = hashlib.sha256(json.dumps([content, uncertain], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if signature == record.get("signature") and (uncertain or time.time() - record.get("edited_at", 0) < REFRESH_SECONDS):
            return
        try:
            self.backend.telegram_request(self.session, self.token, "editMessageText", chat_id=self.chat_id, message_id=record["message_id"],
                text=stock_message(**payload, live=True, uncertain=uncertain,
                    warning="本次读取失败，请在管理页查看原因；上次结果不代表当前库存。" if uncertain else ""),
                parse_mode="HTML", disable_web_page_preview=True, reply_markup=purchase_button(payload["purchase_url"]))
        except self.backend.TelegramMessageUnavailable as exc:
            record["uneditable"] = True
            self.warnings.append(str(exc))
            logging.warning("%s", exc)
        record.update(signature=signature, edited_at=time.time())

    def uncertain(self, item):
        if not self.enabled or not self.backend.product_membership(item, "notify"):
            return
        record = self.record(item)
        if record and record.get("open") and record.get("message_id"):
            self.edit(record, record["snapshot"], uncertain=True)
            self.save()

    def sync(self, item, product, old_count, first, authorized=True):
        entry = self.current[item["id"]]
        if not self.backend.product_membership(item, "notify"):
            entry.pop("telegram", None)
            return
        if not self.enabled:
            return
        record = self.record(item)
        if record is None:
            entry.pop("telegram", None)
        # Finish a failed sold-out edit before starting the next cycle. Its
        # saved zero-stock snapshot must not be replaced by the new restock.
        if record and record.get("pending_close"):
            record["pending_restock"] = product.available > 0
            self.save()
            self.edit(record, record["snapshot"])
            record["pending_close"] = False
            self.save()
        payload = self.payload(item, product)
        if product.available <= 0:
            entry.pop("telegram_pending_auth", None)
            if record and record.get("open"):
                record.update(open=False, pending_close=True, snapshot=payload)
                self.save()
                self.edit(record, payload)
                record["pending_close"] = False
                self.save()
            elif record and record.get("pending_send"):
                entry.pop("telegram", None)
            return
        if not authorized:
            entry["telegram_pending_auth"] = True
            self.save()
            self.warnings.append(f"{item.get('name', item['id'])}：抢购账户未认证，暂缓发送库存通知")
            return
        pending_auth = entry.pop("telegram_pending_auth", False)
        if record and record.get("open"):
            self.edit(record, payload)
            record["snapshot"] = payload
            self.save()
            return
        if not (old_count == 0 or first or pending_auth or record and (record.get("pending_send") or record.get("pending_restock"))):
            return
        record = {"target": self.target, "pending_send": True, "snapshot": payload}
        entry["telegram"] = record
        self.save()
        result = self.backend.telegram_request(self.session, self.token, "sendMessage", chat_id=self.chat_id,
            text=stock_message(**payload, live=True), parse_mode="HTML", disable_web_page_preview=True,
            reply_markup=purchase_button(payload["purchase_url"]))
        message = result.get("result", {}) if isinstance(result, dict) else {}
        message_id = message.get("message_id") if isinstance(message, dict) else None
        if type(message_id) is not int or message_id <= 0:
            raise self.backend.MonitorError("Telegram 未返回有效消息 ID，无法跟踪补货通知")
        record.update(message_id=message_id, open=True, pending_send=False, edited_at=time.time())
        # Remember the content sent so an unchanged immediate recheck is quiet.
        content = {key: value for key, value in payload.items() if key != "checked_at"}
        record["signature"] = hashlib.sha256(json.dumps([content, False], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        self.save()
        logging.info("已发送本轮补货通知：%s，目前 %s 台", product.name, product.available)
