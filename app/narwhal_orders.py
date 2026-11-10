"""Narwhal's balance-funded, single-VM purchase API (no live calls at import)."""
from __future__ import annotations

import re
import secrets
import uuid
from decimal import Decimal, InvalidOperation

import requests


API_BASE = "https://api.fuckip.me/api/v1"
VMS_URL = f"{API_BASE}/vms"
DASHBOARD_URL = "https://dash.fuckip.me/"


class PurchaseError(ValueError):
    def __init__(self, message: str, *, vm_id: str = "", rejected: bool = False):
        super().__init__(message)
        self.vm_id = vm_id
        self.rejected = rejected


class APIResponseError(ValueError):
    def __init__(self, status: int, detail: str, code: int | None = None):
        self.status, self.code = status, code
        error_code = f"，商家错误码 {code}" if code is not None else ""
        super().__init__(f"{detail}（HTTP {status}{error_code}）")


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
    # Expose only numeric status codes and our own messages. VM responses,
    # error messages, and JSON parsing exceptions can contain credentials.
    status = response.status_code
    try:
        payload = response.json()
    except ValueError:
        raise APIResponseError(status, "独角鲸接口未返回有效 JSON") from None
    code = payload.get("code") if isinstance(payload, dict) else None
    error_code = code if type(code) is int and 0 < code <= 999_999_999 else None
    if not 200 <= status < 300 or error_code is not None:
        raise APIResponseError(status, "独角鲸接口返回错误", error_code)
    if type(code) is not int or code != 0 or not isinstance(payload.get("data"), dict):
        raise APIResponseError(status, "独角鲸接口返回结构无法确认")
    return payload["data"]


def load_plan(session, product: dict) -> dict:
    """Read exact plan metadata, including systems, even while sold out."""
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
    return matches[0]


def supported_systems(plan: dict) -> list[dict]:
    systems = plan.get("supported_os")
    if (not isinstance(systems, list) or not systems
            or any(not isinstance(os, dict) or not isinstance(os.get("id"), str)
                   or not 1 <= len(os["id"].strip()) <= 256 or not isinstance(os.get("name"), str)
                   or not os["name"].strip() for os in systems)
            or len({os["id"] for os in systems}) != len(systems)):
        raise ValueError("独角鲸未返回有效的可用系统列表")
    return [{"id": os["id"], "name": os["name"]} for os in systems]


def default_system(systems: list[dict]) -> str:
    return next((os["id"] for os in systems if re.search(r"\balpine\b", os["name"], re.I)), systems[0]["id"])


def prepare_purchase(session, item: dict, product: dict) -> dict:
    validate_settings(item)
    plan = load_plan(session, product)
    plan_id = plan["id"]
    if (plan.get("status") != "active" or plan.get("machine_status") != "online"
            or plan.get("sold_out") is not False or plan.get("ram_insufficient") is True
            or type(plan.get("remaining")) is not int or plan["remaining"] < 0):
        raise ValueError("独角鲸套餐缺货、节点离线或库存无法确认")
    systems = supported_systems(plan)
    requested = str(item.get("operating_system", "")).strip()
    def normalized(text):
        return re.sub(r"[\W_]", "", text.casefold())

    selected = [os for os in systems if os["id"] == requested or normalized(os["name"]) == normalized(requested)] if requested else [os for os in systems if os["id"] == default_system(systems)]
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
    stage = "提交购买"
    try:
        response = session.post(VMS_URL, json=payload, headers={"Idempotency-Key": key},
                                timeout=(5, 60), allow_redirects=False)
        data = api_data(response)
        vm_id = identifier(data.get("vm_id"))
        if not vm_id:
            raise APIResponseError(response.status_code, "购买响应缺少有效实例编号")
        stage = "核对实例"
        detail = api_data(session.get(f"{VMS_URL}/{vm_id}", timeout=(5, 20), allow_redirects=False))
        checks = (
            ("实例编号", detail.get("id") == vm_id),
            ("节点", detail.get("machine_id") == product["machine_id"]),
            ("套餐", detail.get("plan_id", payload["plan_id"]) == payload["plan_id"]),
            ("系统", detail.get("os_image", payload["os_image"]) == payload["os_image"]),
            ("创建状态", detail.get("status") in ("creating", "starting", "running", "stopped")),
            ("自动续费", detail.get("auto_renew") is payload["auto_renew"]),
        )
        mismatches = [label for label, ok in checks if not ok]
        if mismatches:
            raise PurchaseError(f"核对实例：无法确认{'、'.join(mismatches)}", vm_id=vm_id)
        return {"vm_id": vm_id, "url": f"{DASHBOARD_URL}instance/{vm_id}",
                "instance_status": detail["status"], "payment_status": "paid", "auto_renew": payload["auto_renew"]}
    except APIResponseError as exc:
        # Only classify specific, known pre-purchase refusals as rejected.
        # An error while reading details cannot undo an accepted purchase.
        refusal = None
        if stage == "提交购买":
            if (exc.status, exc.code) == (401, 10002):
                refusal = "独角鲸拒绝购买认证，请重新检查网站账户"
            elif 400 <= exc.status < 500:
                refusal = {13200: "节点商家已限制此账户购买", 13201: "此账户已屏蔽该节点商家"}.get(exc.code)
        message = f"{stage}：{exc}" + (f"；{refusal}" if refusal else "")
        raise PurchaseError(message, vm_id=vm_id, rejected=refusal is not None) from None
    except PurchaseError:
        raise
    except Exception as exc:
        reason = "请求超时" if isinstance(exc, requests.Timeout) else (
            "网络请求异常" if isinstance(exc, (requests.RequestException, OSError)) else "未能确认接口结果")
        raise PurchaseError(f"{stage}：{reason}", vm_id=vm_id) from None
