"""Narwhal's balance-funded, single-VM purchase API (no live calls at import)."""
from __future__ import annotations

import re
import secrets
import uuid
from decimal import Decimal, InvalidOperation


API_BASE = "https://api.fuckip.me/api/v1"
VMS_URL = f"{API_BASE}/vms"
DASHBOARD_URL = "https://dash.fuckip.me/"


class PurchaseError(ValueError):
    def __init__(self, message: str, *, request_sent: bool = False, vm_id: str = ""):
        super().__init__(message)
        self.request_sent = request_sent
        self.vm_id = vm_id


def identifier(value: object) -> str:
    try:
        return str(uuid.UUID(value)) if isinstance(value, str) else ""
    except ValueError:
        return ""


def validate_settings(item: dict) -> None:
    if type(item.get("quantity", 1)) is not int or item.get("quantity", 1) != 1:
        raise PurchaseError("独角鲸余额购买每项仅支持 1 台，不会拆单或减少数量")
    if item.get("billing_cycle", "") not in {"", "monthly"}:
        raise PurchaseError("独角鲸余额购买仅支持月付")
    if item.get("payment_method", "") not in {"", "balance"}:
        raise PurchaseError("独角鲸购买会直接扣账户余额，请选择余额付款")
    if type(item.get("narwhal_auto_renew", True)) is not bool:
        raise PurchaseError("独角鲸自动续费设置必须是布尔值")


def coupon_for(config: dict, item: dict, product: dict) -> str:
    """Node-specific settings, including explicit blanks, override legacy rows."""
    coupons = config.get("vps_seckill", {}).get("narwhal_coupons", {})
    return str(coupons.get(product.get("machine_id"), item.get("coupon_code", ""))).strip()


def api_data(response) -> dict:
    # Never include vendor responses: VM details can contain credentials.
    if not 200 <= response.status_code < 300:
        raise ValueError(f"独角鲸接口返回 HTTP {response.status_code}，请检查官网余额、库存和账户状态")
    payload = response.json()
    if (not isinstance(payload, dict) or type(payload.get("code")) is not int
            or payload["code"] != 0 or not isinstance(payload.get("data"), dict)):
        raise ValueError("独角鲸接口未确认成功，请到官网检查余额、库存和实例")
    return payload["data"]


def prepare_purchase(session, item: dict, product: dict) -> dict:
    validate_settings(item)
    plan_id, machine_id = identifier(product.get("plan_id")), identifier(product.get("machine_id"))
    if not plan_id or not machine_id:
        raise PurchaseError("独角鲸套餐或节点标识无效，未提交购买")
    data = api_data(session.get(f"{API_BASE}/plans", params={"machine_id": machine_id},
                                timeout=(5, 60), allow_redirects=False))
    plans = data.get("plans")
    if not isinstance(plans, list) or any(not isinstance(plan, dict) for plan in plans):
        raise ValueError("独角鲸套餐目录结构无法确认")
    matches = [p for p in plans if p.get("id") == plan_id and p.get("machine_id") == machine_id]
    if len(matches) != 1:
        raise ValueError("独角鲸套餐与所选节点不匹配或已下架")
    plan = matches[0]
    if (plan.get("status") != "active" or plan.get("machine_status") != "online"
            or plan.get("sold_out") is not False or plan.get("ram_insufficient") is True
            or type(plan.get("remaining")) is not int or plan["remaining"] < 0):
        raise ValueError("独角鲸套餐缺货、节点离线或库存无法确认")
    systems = plan.get("supported_os")
    if (not isinstance(systems, list) or not systems
            or any(not isinstance(os, dict) or not isinstance(os.get("id"), str)
                   or not os["id"].strip() or not isinstance(os.get("name"), str) for os in systems)):
        raise ValueError("独角鲸未返回有效的可用系统列表")
    requested = str(item.get("operating_system", "")).strip()
    def normalized(text):
        return re.sub(r"[\W_]", "", text.casefold())

    selected = [os for os in systems if os["id"] == requested or normalized(os["name"]) == normalized(requested)] if requested else systems[:1]
    if len(selected) != 1:
        raise ValueError("独角鲸套餐不支持所选系统，请选择该节点可用系统或跟随默认系统")
    coupon = str(item.get("coupon_code", "")).strip()
    if coupon:
        # Match the official deployment page's read-only promotion preview.
        preview = api_data(session.get(f"{API_BASE}/promo-codes/preview",
            params={"code": coupon, "plan_id": plan_id, "amount": plan.get("price_monthly")},
            timeout=(5, 20), allow_redirects=False))
        try:
            original = Decimal(str(plan.get("price_monthly")))
            final = Decimal(str(preview.get("final_amount")))
            valid_amount = original.is_finite() and final.is_finite() and 0 <= final <= original
        except (InvalidOperation, ValueError):
            valid_amount = False
        if preview.get("is_valid") is not True or not valid_amount:
            raise PurchaseError("独角鲸折扣码对所选套餐无效或优惠金额无法确认，已停止购买，不会按原价下单")
    payload = {"plan_id": plan_id, "os_image": selected[0]["id"],
               "password": secrets.token_urlsafe(24), "auto_renew": item.get("narwhal_auto_renew", True)}
    if coupon:
        payload["promo_code"] = coupon
    return payload


def purchase(session, payload: dict, product: dict, key: str) -> dict:
    """Send one purchase POST, then read that VM; never retry, pay again or refund."""
    vm_id = ""
    try:
        data = api_data(session.post(VMS_URL, json=payload, headers={"Idempotency-Key": key},
                                     timeout=(5, 60), allow_redirects=False))
        vm_id = identifier(data.get("vm_id"))
        if not vm_id:
            raise ValueError("独角鲸购买响应缺少有效实例编号")
        detail = api_data(session.get(f"{VMS_URL}/{vm_id}", timeout=(5, 20), allow_redirects=False))
        if (detail.get("id") != vm_id or detail.get("machine_id") != product["machine_id"]
                or "plan_id" in detail and detail["plan_id"] != payload["plan_id"]
                or "os_image" in detail and detail["os_image"] != payload["os_image"]
                or detail.get("status") not in {"creating", "starting", "running", "stopped"}
                or detail.get("auto_renew") is not payload["auto_renew"]):
            raise ValueError("独角鲸实例详情未能确认节点、套餐、系统、创建状态或自动续费设置")
        return {"vm_id": vm_id, "url": f"{DASHBOARD_URL}instance/{vm_id}",
                "instance_status": detail["status"], "payment_status": "paid", "auto_renew": payload["auto_renew"]}
    except Exception:
        # Even malformed JSON or an unexpected response may follow a debit.
        # Do not leak exception details (they may contain VM credentials).
        raise PurchaseError("独角鲸购买请求已发送，可能已扣余额；请到官网核对实例和交易记录，勿重复启动",
                            request_sent=True, vm_id=vm_id) from None
