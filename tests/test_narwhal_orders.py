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
from tests.test_narwhal import MACHINE_ID, PLAN_ID, OTHER_PLAN_ID, item as product, plan


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
                       "os_image": "debian12", "status": "creating", "auto_renew": False,
                       "credentials": {"password": "private-vm-password"}}
        self.session = Mock(headers={"Authorization": "Bearer private-token"})
        self.session.get.side_effect = [response({"plans": [self.plan]}), response(self.detail)]
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
        self.assertFalse(result["auto_renew"])
        self.assertEqual(result["url"], f"https://dash.fuckip.me/instance/{VM_ID}")
        self.session.post.assert_called_once()
        post = self.session.post.call_args
        self.assertEqual(post.args[0], orders.VMS_URL)
        payload = post.kwargs["json"]
        self.assertEqual(payload["plan_id"], PLAN_ID)
        self.assertEqual(payload["os_image"], "debian12")
        self.assertFalse(payload["auto_renew"])
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

    def test_container_system_name_and_image_id_match_public_catalogue_shape(self):
        image = "docker.io/narwhalcloud/debian:podman"
        catalogue = {**self.plan, "supported_os": [{"id": image, "name": "Debian (Podman)"}]}
        for selected in ("", "Debian (Podman)", image):
            self.session.get.side_effect = [response({"plans": [catalogue]})]
            payload = orders.prepare_purchase(self.session, purchase_item(operating_system=selected), product())
            self.assertEqual(payload["os_image"], image)

    def test_invalid_preferences_and_wrong_account_block_before_purchase(self):
        for changes in ({"quantity": 2}, {"quantity": True}, {"billing_cycle": "annually"},
                        {"payment_method": "alipay"}, {"operating_system": "Debian 13"}):
            with self.subTest(changes=changes):
                self.session.get.side_effect = [response({"plans": [self.plan]})]
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
                self.session.get.side_effect = [response({"plans": [self.plan]})]
                self.session.post.side_effect = error
                task, result = self.submit()
                self.assertEqual(result["status"], "uncertain")
                self.assertEqual(result["payment_status"], "unknown")
                _, duplicate = self.submit(task=task, item=purchase_item(id="duplicate"))
                self.assertEqual(duplicate["status"], "blocked")
                self.session.post.assert_called_once()
                self.assertNotIn("private-vm-password", str(task.snapshot()))

    def test_rejected_malformed_or_redirected_purchase_stops_as_uncertain(self):
        malformed = Mock(status_code=200)
        malformed.json.side_effect = ValueError("private-vm-password")
        for reply in (response({}, status=402), response({}, status=302), response({}, code=1001),
                      response({"vm_id": "https://evil.example"}), malformed):
            with self.subTest(reply=reply):
                self.session.reset_mock()
                self.session.get.side_effect = [response({"plans": [self.plan]})]
                self.session.post.return_value = reply
                task, result = self.submit()
                self.assertEqual(result["status"], "uncertain")
                self.session.post.assert_called_once()
                self.assertEqual(self.session.get.call_count, 1)
                self.assertNotIn("private-vm-password", str(task.snapshot()))

    def test_vm_detail_mismatch_or_failure_retains_vm_link_without_repurchase(self):
        for changes in ({"id": OTHER_PLAN_ID}, {"machine_id": OTHER_PLAN_ID},
                        {"plan_id": OTHER_PLAN_ID}, {"os_image": "other"},
                        {"status": "error"}, {"auto_renew": True}):
            with self.subTest(changes=changes):
                self.session.reset_mock()
                self.session.get.side_effect = [response({"plans": [self.plan]}), response({**self.detail, **changes})]
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
        self.assertIn("已扣余额", message)
        self.assertIn("自动续费关闭", message)
        self.assertNotIn("待付款", message)
        self.assertNotIn("手动付款", message)
        button = send.call_args.kwargs["reply_markup"]["inline_keyboard"][0][0]
        self.assertIn("查看实例", button["text"])
        self.assertEqual(button["url"], result["url"])

    def test_automatic_task_uses_native_adapter_and_manual_mode_never_buys(self):
        for mode, expected in (("rapid", "purchased"), ("precise", "purchased"), ("manual", "ready")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                self.session.reset_mock()
                self.session.get.side_effect = [response({"plans": [self.plan]}), response(self.detail)]
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
