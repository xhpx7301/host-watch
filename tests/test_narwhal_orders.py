"""Balance purchases are simulated; these tests never contact a merchant."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import admin
import narwhal_orders as orders
from monitor import parse_narwhal
from tests.test_narwhal import MACHINE_ID, PLAN_ID, OTHER_PLAN_ID, OTHER_MACHINE_ID, item as product, plan


PREVIEW = {"is_valid": True, "final_amount": 0.8}
VM_ID = "4d34a87e-62b6-4825-b3bc-36722b7c8c1a"


def response(data, status=200, code=0):
    result = Mock(status_code=status)
    result.json.return_value = {"code": code, "data": data}
    return result


def purchase_item(**changes):
    return {"id": "buy-one", "product_id": "watch-1", "account_id": "narwhal-account",
            "quantity": 1, "operating_system": "Debian 12", "billing_cycle": "monthly",
            "payment_method": "balance", "coupon_code": "PROMO", **changes}


def configuration():
    config = admin.default_config()
    config["products"] = [product()]
    config["vps_seckill"].update(items=[purchase_item()], accounts=[{
        "id": "narwhal-account", "name": "Narwhal", "provider": "narwhal",
        "username": "user@example.com", "password": "account-private-password"}])
    return admin.validate_config(config)


class NarwhalPurchaseTests(unittest.TestCase):
    def setUp(self):
        self.plan = plan(supported_os=[{"id": "debian12", "name": "Debian 12"},
                                       {"id": "ubuntu24", "name": "Ubuntu 24"}])
        self.detail = {"id": VM_ID, "machine_id": MACHINE_ID, "plan_id": PLAN_ID,
                       "os_image": "debian12", "status": "creating", "auto_renew": True,
                       "credentials": {"password": "private-vm-password"}}
        self.session = Mock(headers={"Authorization": "Bearer private-token"})
        self.session.get.side_effect = [response({"plans": [self.plan]}), response(PREVIEW), response(self.detail)]
        self.session.post.return_value = response({"vm_id": VM_ID})
        self.config = configuration()

    def task(self):
        return admin.SeckillTask("narwhal-purchase-test", self.config, "rapid")

    def submit(self, task=None, item=None, account=None):
        task = task or self.task()
        return task, task._submit(self.session, item or self.config["vps_seckill"]["items"][0],
            account or self.config["vps_seckill"]["accounts"][0], "https://dash.fuckip.me/deploy",
            self.config["products"][0])

    def test_purchase_posts_once_and_verifies_vm_without_leaking_credentials(self):
        task, result = self.submit()
        self.assertEqual(result["status"], "purchased")
        self.assertEqual(result["payment_status"], "paid")
        self.assertTrue(result["auto_renew"])
        self.assertEqual(result["url"], f"https://dash.fuckip.me/instance/{VM_ID}")
        self.session.post.assert_called_once()
        post = self.session.post.call_args
        self.assertEqual(post.args[0], orders.VMS_URL)
        payload = post.kwargs["json"]
        self.assertEqual(payload["plan_id"], PLAN_ID)
        self.assertEqual(payload["os_image"], "debian12")
        self.assertTrue(payload["auto_renew"])
        self.assertEqual(payload["promo_code"], "PROMO")
        self.assertGreaterEqual(len(payload["password"]), 24)
        self.assertNotEqual(payload["password"], "account-private-password")
        self.assertEqual(len(post.kwargs["headers"]["Idempotency-Key"]), 64)
        for call in self.session.get.call_args_list + self.session.post.call_args_list:
            self.assertFalse(call.kwargs["allow_redirects"])
            self.assertTrue(call.args[0].startswith(orders.API_BASE))
        public = str(result) + str(task.snapshot())
        for secret in (payload["password"], "private-vm-password", "account-private-password", "private-token"):
            self.assertNotIn(secret, public)
        self.session.delete.assert_not_called()

    def test_default_system_and_no_coupon(self):
        payload = orders.prepare_purchase(self.session, purchase_item(operating_system="", coupon_code=""), product())
        self.assertEqual(payload["os_image"], "debian12")
        self.assertNotIn("promo_code", payload)
        self.assertEqual(self.session.get.call_count, 1)

    def test_blank_system_prefers_alpine_and_explicit_selection_is_preserved(self):
        systems = [{"id": "debian", "name": "Debian (Podman)"},
                   {"id": "docker.io/narwhalcloud/alpine:podman", "name": "Alpine (Podman)"}]
        for requested, expected in (("", systems[1]["id"]), ("Debian (Podman)", "debian"), ("debian", "debian")):
            with self.subTest(requested=requested):
                self.session.get.side_effect = [response({"plans": [{**self.plan, "supported_os": systems}]})]
                payload = orders.prepare_purchase(self.session, purchase_item(operating_system=requested, coupon_code=""), product())
                self.assertEqual(payload["os_image"], expected)

    def test_system_lookup_is_read_only_works_sold_out_and_uses_item_egress(self):
        systems = [{"id": "debian", "name": "Debian (Podman)"}, {"id": "alpine", "name": "Alpine (Podman)"}]
        self.session.get.side_effect = [response({"plans": [{**self.plan, "sold_out": True, "supported_os": systems}]})]
        self.session.__enter__ = Mock(return_value=self.session)
        self.session.__exit__ = Mock(return_value=False)
        with patch.object(admin, "_seckill_action_config", return_value=self.config), \
             patch.object(admin.requests, "Session", return_value=self.session), \
             patch.object(admin.egress, "apply_session") as route, \
             patch.object(admin, "check_order_account") as authenticate:
            result = admin.seckill_systems({"item_id": "buy-one"})
        self.assertEqual(result["systems"], systems)
        self.assertEqual(result["default_system"], "alpine")
        self.assertEqual(result["plan_id"], PLAN_ID)
        self.assertFalse(self.session.trust_env)
        route.assert_called_once()
        authenticate.assert_not_called()
        self.session.post.assert_not_called()
        self.session.get.assert_called_once_with(f"{orders.API_BASE}/plans", params={"machine_id": MACHINE_ID},
                                                 timeout=(5, 60), allow_redirects=False)

    def test_system_lookup_rejects_unknown_items_or_invalid_lists(self):
        with patch.object(admin, "_seckill_action_config", return_value=self.config), \
             patch.object(admin.requests, "Session") as session:
            with self.assertRaises(ValueError):
                admin.seckill_systems({"item_id": "missing"})
            session.assert_not_called()
        for systems in ([], [None], [{"id": "", "name": "Alpine"}], [{"id": "os", "name": ""}],
                        [{"id": "same", "name": "Debian"}, {"id": "same", "name": "Alpine"}]):
            with self.subTest(systems=systems), self.assertRaises(ValueError):
                orders.supported_systems({"supported_os": systems})

    def test_auto_renew_can_be_disabled_and_verified_and_reported(self):
        self.detail["auto_renew"] = False
        with patch.object(admin, "effective_telegram", return_value=("bot-token", "100")), \
             patch.object(admin, "telegram_call") as send:
            _, result = self.submit(item=purchase_item(narwhal_auto_renew=False))
        self.assertEqual(result["status"], "purchased")
        self.assertFalse(result["auto_renew"])
        self.assertFalse(self.session.post.call_args.kwargs["json"]["auto_renew"])
        self.assertIn("自动续费关闭", send.call_args.kwargs["text"])

    def test_node_coupon_overrides_legacy_row_and_is_validated_for_exact_plan(self):
        self.config["vps_seckill"]["narwhal_coupons"] = {MACHINE_ID: "NODE-PROMO"}
        _, result = self.submit()
        self.assertEqual(result["status"], "purchased")
        self.assertEqual(self.session.post.call_args.kwargs["json"]["promo_code"], "NODE-PROMO")
        preview = self.session.get.call_args_list[1]
        self.assertEqual(preview.args[0], f"{orders.API_BASE}/promo-codes/preview")
        self.assertEqual(preview.kwargs["params"], {"code": "NODE-PROMO", "plan_id": PLAN_ID, "amount": 1})
        self.assertEqual(orders.coupon_for(self.config, purchase_item(), product(plan_id=OTHER_PLAN_ID)), "NODE-PROMO")
        self.assertEqual(orders.coupon_for(self.config, purchase_item(coupon_code="OTHER"), product(machine_id=OTHER_MACHINE_ID)), "OTHER")

    def test_explicit_empty_node_coupon_does_not_fall_back_to_legacy_or_merchant_code(self):
        self.config["affiliate_coupons"]["narwhal"] = "MERCHANT"
        self.config["vps_seckill"]["narwhal_coupons"] = {MACHINE_ID: ""}
        self.session.get.side_effect = [response({"plans": [self.plan]}), response(self.detail)]
        _, result = self.submit()
        self.assertEqual(result["status"], "purchased")
        self.assertNotIn("promo_code", self.session.post.call_args.kwargs["json"])
        self.assertEqual(self.session.get.call_count, 2)

    def test_invalid_or_unverifiable_promo_stops_without_purchasing_at_full_price(self):
        for reply in (response({"is_valid": False, "final_amount": 1}), response({"is_valid": True}),
                      response({"is_valid": True, "final_amount": 2}),
                      response({"is_valid": True, "final_amount": -1}),
                      response({"is_valid": True, "final_amount": "NaN"}), response({}, status=403),
                      requests.Timeout("private-token")):
            with self.subTest(reply=reply):
                self.session.get.side_effect = [response({"plans": [self.plan]}), reply]
                _, result = self.submit()
                self.assertEqual(result["status"], "blocked")
                self.session.post.assert_not_called()

    def test_node_codes_and_auto_renew_survive_normalization_and_reject_invalid_settings(self):
        config = configuration()
        self.assertTrue(config["vps_seckill"]["items"][0]["narwhal_auto_renew"])
        config["vps_seckill"]["items"][0]["narwhal_auto_renew"] = False
        config["vps_seckill"]["narwhal_coupons"] = {MACHINE_ID: " CODE ", OTHER_MACHINE_ID: ""}
        normalized = admin.validate_config(config)
        self.assertFalse(normalized["vps_seckill"]["items"][0]["narwhal_auto_renew"])
        self.assertEqual(normalized["vps_seckill"]["narwhal_coupons"], {MACHINE_ID: "CODE", OTHER_MACHINE_ID: ""})
        for coupons in ([], {"invalid": "CODE"}, {MACHINE_ID: 12}, {MACHINE_ID: "CODE\n"}, {MACHINE_ID: "A" * 257}):
            config["vps_seckill"]["narwhal_coupons"] = coupons
            with self.assertRaises(ValueError):
                admin.validate_config(config)
        config["vps_seckill"]["narwhal_coupons"] = {}
        config["vps_seckill"]["items"][0]["narwhal_auto_renew"] = "true"
        with self.assertRaises(ValueError):
            admin.validate_config(config)

    def test_container_system_name_and_image_id_match_public_catalogue_shape(self):
        image = "docker.io/narwhalcloud/debian:podman"
        catalogue = {**self.plan, "supported_os": [{"id": image, "name": "Debian (Podman)"}]}
        for selected in ("", "Debian (Podman)", image):
            self.session.get.side_effect = [response({"plans": [catalogue]})]
            payload = orders.prepare_purchase(self.session, purchase_item(operating_system=selected, coupon_code=""), product())
            self.assertEqual(payload["os_image"], image)

    def test_invalid_preferences_and_wrong_account_block_before_purchase(self):
        for changes in ({"quantity": 2}, {"quantity": True}, {"billing_cycle": "annually"},
                        {"payment_method": "alipay"}, {"operating_system": "Debian 13"}):
            with self.subTest(changes=changes):
                self.session.get.side_effect = [response({"plans": [self.plan]}), response(PREVIEW)]
                _, result = self.submit(item=purchase_item(**changes))
                self.assertEqual(result["status"], "blocked")
                self.session.post.assert_not_called()
        _, result = self.submit(account={"provider": "hytron", "username": "other@example.com"})
        self.assertEqual(result["status"], "blocked")
        self.session.post.assert_not_called()

    def test_unavailable_or_invalid_catalogue_never_buys(self):
        for changes in ({"sold_out": True}, {"ram_insufficient": True}, {"status": "disabled"},
                        {"machine_status": "offline"}, {"remaining": -1},
                        {"machine_id": OTHER_PLAN_ID}, {"supported_os": []}, {"supported_os": [None]}):
            with self.subTest(changes=changes):
                self.session.get.side_effect = [response({"plans": [{**self.plan, **changes}]})]
                _, result = self.submit()
                self.assertEqual(result["status"], "blocked")
                self.session.post.assert_not_called()

    def test_timeout_and_unexpected_exception_never_retry_same_account_plan(self):
        for error in (requests.Timeout("password=secret-token"), RuntimeError("private-vm-password")):
            with self.subTest(error=type(error).__name__):
                self.session.reset_mock()
                self.session.get.side_effect = [response({"plans": [self.plan]}), response(PREVIEW)]
                self.session.post.side_effect = error
                task, result = self.submit()
                self.assertEqual(result["status"], "uncertain")
                self.assertEqual(result["payment_status"], "unknown")
                _, duplicate = self.submit(task=task, item=purchase_item(id="duplicate"))
                self.assertEqual(duplicate["status"], "blocked")
                self.session.post.assert_called_once()
                self.assertIn("提交购买：请求超时" if isinstance(error, requests.Timeout) else "提交购买：未能确认接口结果",
                              str(task.snapshot()))
                self.assertNotIn("private-vm-password", str(task.snapshot()))
                self.assertNotIn("secret-token", str(task.snapshot()))

    def test_unconfirmed_purchase_logs_status_and_code_without_exposing_vendor_response(self):
        malformed = Mock(status_code=200)
        malformed.json.side_effect = ValueError("private-vm-password")
        unrecognized = response({}, code=1001)
        unrecognized.json.return_value["msg"] = "private-vm-password"
        for reply, expected in ((response({}, status=402), "HTTP 402"), (response({}, status=302), "HTTP 302"),
                                (unrecognized, "商家错误码 1001"), (malformed, "未返回有效 JSON"),
                                (response({"vm_id": "https://evil.example"}), "缺少有效实例编号"),
                                (response({}, status=500, code=13200), "HTTP 500")):
            with self.subTest(expected=expected):
                self.session.reset_mock()
                self.session.get.side_effect = [response({"plans": [self.plan]}), response(PREVIEW)]
                self.session.post.return_value = reply
                task, result = self.submit()
                self.assertEqual(result["status"], "uncertain")
                self.session.post.assert_called_once()
                self.assertEqual(self.session.get.call_count, 2)
                public = str(task.snapshot())
                self.assertIn("提交购买：", public)
                self.assertIn(expected, public)
                self.assertNotIn("private-vm-password", public)
                self.assertNotIn("https://evil.example", public)

    def test_confirmed_merchant_refusals_are_distinct_and_never_retry(self):
        for status, code, message in ((401, 10002, "重新检查网站账户"),
                                      (403, 13200, "节点商家已限制此账户购买"),
                                      (403, 13201, "此账户已屏蔽该节点商家")):
            with self.subTest(code=code):
                self.session.reset_mock()
                self.session.get.side_effect = [response({"plans": [self.plan]}), response(PREVIEW)]
                self.session.post.return_value = response(None, status=status, code=code)
                task, result = self.submit()
                self.assertEqual(result["status"], "rejected")
                self.assertIn(message, str(task.snapshot()))
                self.assertIn(f"HTTP {status}，商家错误码 {code}", str(task.snapshot()))
                _, duplicate = self.submit(task=task, item=purchase_item(id="duplicate"))
                self.assertEqual(duplicate["status"], "blocked")
                self.session.post.assert_called_once()
                self.assertEqual(self.session.get.call_count, 2)

    def test_details_auth_error_is_uncertain_after_accepted_purchase(self):
        self.session.get.side_effect = [response({"plans": [self.plan]}), response(PREVIEW),
                                        response(None, status=401, code=10002)]
        task, result = self.submit()
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(result["vm_id"], VM_ID)
        self.assertEqual(result["payment_status"], "unknown")
        self.assertIn("核对实例：独角鲸接口返回错误（HTTP 401，商家错误码 10002）", str(task.snapshot()))
        self.session.post.assert_called_once()

    def test_new_instance_visibility_delay_retries_details_but_never_posts_again(self):
        self.session.get.side_effect = [response({"plans": [self.plan]}), response(PREVIEW),
                                        response(None, status=404), response(self.detail)]
        with patch.object(orders.time, "sleep") as wait:
            _, result = self.submit()
        self.assertEqual(result["status"], "purchased")
        self.assertEqual(self.session.get.call_count, 4)
        self.assertEqual(self.session.get.call_args_list[-2], self.session.get.call_args_list[-1])
        self.session.post.assert_called_once()
        wait.assert_called_once_with(0.5)

    def test_vm_detail_mismatch_or_failure_retains_vm_link_without_repurchase(self):
        for changes in ({"id": OTHER_PLAN_ID}, {"machine_id": OTHER_PLAN_ID},
                        {"plan_id": OTHER_PLAN_ID}, {"os_image": "other"},
                        {"status": "error"}, {"auto_renew": False}):
            with self.subTest(changes=changes):
                self.session.reset_mock()
                self.session.get.side_effect = [response({"plans": [self.plan]}), response(PREVIEW), response({**self.detail, **changes})]
                _, result = self.submit()
                self.assertEqual(result["status"], "uncertain")
                self.assertEqual(result["vm_id"], VM_ID)
                self.assertIn(VM_ID, result["url"])
                self.session.post.assert_called_once()

    def test_notification_failure_cannot_turn_purchase_into_retry(self):
        task = self.task()
        with patch.object(task, "_notify_unpaid_order", side_effect=RuntimeError("Telegram down")):
            _, result = self.submit(task=task)
        self.assertEqual(result["status"], "purchased")
        self.assertEqual(result["telegram_status"], "failed")
        _, duplicate = self.submit(task=task)
        self.assertEqual(duplicate["status"], "blocked")
        self.session.post.assert_called_once()

    def test_paid_telegram_uses_instance_link_and_never_asks_for_payment(self):
        with patch.object(admin, "effective_telegram", return_value=("bot-token", "100")), \
             patch.object(admin, "telegram_call") as send:
            _, result = self.submit()
        self.assertEqual(result["telegram_status"], "sent")
        message = send.call_args.kwargs["text"]
        self.assertIn("已从余额扣款", message)
        self.assertIn("自动续费开启", message)
        self.assertIn("东京实例", message)
        self.assertIn("系统：Debian 12", message)
        self.assertIn("下单前报价：$0.80/月（含折扣预览）", message)
        self.assertNotIn("PROMO", message)
        self.assertNotIn("private-vm-password", message)
        self.assertNotIn("待付款", message)
        self.assertNotIn("手动付款", message)
        button = send.call_args.kwargs["reply_markup"]["inline_keyboard"][0][0]
        self.assertIn("查看实例", button["text"])
        self.assertEqual(button["url"], result["url"])

    def test_automatic_task_uses_native_adapter_and_manual_mode_never_buys(self):
        for mode, expected in (("rapid", "purchased"), ("precise", "purchased"), ("manual", "ready")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                self.session.reset_mock()
                self.session.get.side_effect = [response({"plans": [self.plan]}), response(PREVIEW), response(self.detail)]
                self.session.__enter__ = Mock(return_value=self.session)
                self.session.__exit__ = Mock(return_value=False)
                task = admin.SeckillTask(f"narwhal-{mode}", self.config, mode)
                with patch.object(admin.requests, "Session", return_value=self.session), \
                     patch.object(admin, "check_order_account", return_value={"ok": True, "status": "valid", "message": "OK"}), \
                     patch.object(admin, "save_merchant_account_check"), \
                     patch("monitor.inspect_product", return_value=parse_narwhal([self.plan], product())), \
                     patch("monitor.write_inventory_cache"), \
                     patch.object(admin, "STATUS_PATH", Path(directory) / "status.json"):
                    task.run()
                result = task.snapshot()
                self.assertEqual(result["items"][0]["status"], expected)
                if mode == "manual":
                    self.session.post.assert_not_called()
                else:
                    self.session.post.assert_called_once()
                    self.assertIn("已扣余额", result["logs"][-1]["message"])

    def test_configuration_test_is_read_only_and_reports_balance_purchase(self):
        for quantity, ok in ((1, True), (2, False)):
            config = copy.deepcopy(self.config)
            config["vps_seckill"]["items"][0]["quantity"] = quantity
            self.session.__enter__ = Mock(return_value=self.session)
            self.session.__exit__ = Mock(return_value=False)
            with patch.object(admin.requests, "Session", return_value=self.session), \
                 patch.object(admin, "check_order_account", return_value={"ok": True, "status": "valid", "message": "OK"}), \
                 patch("monitor.inspect_product", return_value=parse_narwhal([self.plan], product())):
                result = admin.seckill_test({"config": config, "mode": "rapid"})
            self.assertEqual(result["items"][0]["order_submission"]["ok"], ok)
            self.session.post.assert_not_called()

    def test_configuration_test_reports_invalid_node_coupon_without_buying(self):
        self.config["vps_seckill"]["narwhal_coupons"] = {MACHINE_ID: "INVALID"}
        self.session.get.side_effect = [response({"plans": [self.plan]}), response({"is_valid": False})]
        self.session.__enter__ = Mock(return_value=self.session)
        self.session.__exit__ = Mock(return_value=False)
        with patch.object(admin.requests, "Session", return_value=self.session), \
             patch.object(admin, "check_order_account", return_value={"ok": True, "status": "valid", "message": "OK"}), \
             patch("monitor.inspect_product", return_value=parse_narwhal([self.plan], product())):
            result = admin.seckill_test({"config": self.config, "mode": "rapid"})
        self.assertFalse(result["ok"])
        self.assertIn("折扣码", result["items"][0]["order_submission"]["message"])
        self.session.post.assert_not_called()

    def test_balance_method_is_only_accepted_for_narwhal(self):
        config = configuration()
        self.assertEqual(config["vps_seckill"]["items"][0]["payment_method"], "balance")
        config["products"][0] = {"id": "watch-1", "name": "Other", "adapter": "html",
            "url": "https://example.com", "stock_selector": "body", "stock_pattern": "([0-9]+)"}
        with self.assertRaisesRegex(ValueError, "付款方式"):
            admin.validate_config(config)

    def test_legacy_preferences_and_removed_product_manual_link_survive_upgrade(self):
        config = configuration()
        config["vps_seckill"]["items"][0]["payment_method"] = "alipay"
        restored = admin.validate_config(config)
        with self.assertRaises(orders.PurchaseError):
            orders.validate_settings(restored["vps_seckill"]["items"][0])
        config = configuration()
        config["products"] = []
        config["vps_seckill"]["items"][0]["target_url"] = "https://dash.fuckip.me/deploy"
        restored = admin.validate_config(config)
        self.assertEqual(restored["vps_seckill"]["items"][0]["payment_method"], "balance")
