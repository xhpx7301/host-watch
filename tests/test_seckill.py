import copy
import io
import html
import json
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import Mock, patch

import admin
import monitor
import seckill
from test_monitor import html_product


def fixture():
    # Public frontend fields used by SeckillPage; the live API currently has no campaigns.
    return {"server_time": "2026-10-15T12:00:00+08:00", "activities": [{
        "id": 7, "title": "HK Lite 新品上线限时秒杀", "description": "限时优惠 <test>",
        "running": True, "starts_at": "2026-10-15T10:00:00+08:00", "ends_at": "2026-10-17T12:00:00+08:00",
        "items": [{"id": 71, "product_name": "HK Lite Mini", "plan_name": "年", "billing_period": 1, "billing_unit": "year",
            "seckill_price": "79.00", "original_price": "99.00", "currency_code": "CNY", "stock": 30, "sold": 27,
            "settings": {"cores": "1", "memory": "1024", "disk": "10", "bandwidth_limit": "1024", "ratelimit": "125"}}]}]}


class SeckillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.config = admin.validate_config({"isvoro_seckill": True, "enabled": True})

    def parse(self, data):
        with patch.object(monitor, "fetch_isvoro_json", return_value=data):
            return seckill.activities(Mock(), {})

    def check(self, data, config=None, token="mock", chat="123"):
        with patch.object(monitor, "fetch_isvoro_json", return_value=data):
            return seckill.check_once(Mock(), config or self.config, self.path, token, chat)

    def test_transient_seckill_config_keeps_saved_password_without_persisting_draft(self):
        saved = admin.validate_config({"vps_seckill": {"accounts": [{
            "id": "acct-1", "name": "VMISS", "provider": "vmiss", "username": "user@example.com",
            "password": "saved-secret", "login_url": "https://app.vmiss.com/login",
        }]}})
        draft = admin.public_config(saved)
        draft["vps_seckill"]["accounts"][0]["username"] = "draft@example.com"
        draft["vps_seckill"]["accounts"][0]["password"] = ""
        with patch.object(admin, "current_config", return_value=copy.deepcopy(saved)):
            action = admin._seckill_action_config({"config": draft})
        self.assertEqual(action["vps_seckill"]["accounts"][0]["username"], "draft@example.com")
        self.assertEqual(action["vps_seckill"]["accounts"][0]["password"], "saved-secret")
        self.assertEqual(saved["vps_seckill"]["accounts"][0]["username"], "user@example.com")

    def test_public_empty_response_and_running_upcoming_expired(self):
        self.assertEqual(self.parse({"activities": [], "server_time": fixture()["server_time"]})["activities"], [])
        data = fixture()
        upcoming = copy.deepcopy(data["activities"][0])
        upcoming.update(id=8, running=False, starts_at="2026-10-16T00:00:00+08:00")
        expired = copy.deepcopy(data["activities"][0])
        expired.update(id=9, starts_at="2026-10-13T00:00:00+08:00", ends_at="2026-10-15T11:00:00+08:00")
        data["activities"] += [upcoming, expired]
        result = self.parse(data)["activities"]
        self.assertEqual([(a["id"], a["running"]) for a in result], [("7", True), ("8", False)])
        self.assertEqual(result[0]["items"][0]["available"], 3)
        self.assertEqual(result[0]["items"][0]["specs"], "1C/1G/10GB")
        self.assertEqual(result[0]["items"][0]["traffic"], "1024GB@1000Mbps")

    def test_public_endpoint_works_with_http_and_browser_json_wrappers(self):
        for mode in ["requests", "flaresolverr", "cloudscraper"]:
            body = json.dumps({"code": 0, "data": fixture()})
            if mode == "flaresolverr": body = "<pre>" + html.escape(body) + "</pre>"
            with patch.object(monitor, "fetch_page", return_value=(body, "")) as fetch:
                result = seckill.activities(Mock(), {"mode": mode})
            self.assertEqual(result["activities"][0]["id"], "7")
            self.assertEqual(fetch.call_args.args[1], "https://isvoro.com/api/v1/seckill/activities")

    def test_once_per_id_survives_restart_disappearance_and_stock_changes(self):
        with patch.object(monitor, "telegram_request") as send:
            self.assertEqual(self.check(fixture())["sent_count"], 1)
            changed = fixture()
            changed["activities"][0]["items"][0].update(sold=30, seckill_price=49)
            changed["activities"][0]["title"] = "Updated title"
            self.assertEqual(self.check(changed)["sent_count"], 0)
            self.check({"activities": [], "server_time": fixture()["server_time"]})
            self.check(fixture())
            self.assertEqual(send.call_count, 1)
            second = fixture()
            second["activities"][0]["id"] = 8
            self.assertEqual(self.check(second)["sent_count"], 1)
        self.assertEqual(set(monitor.read_state(seckill.state_file(self.path))), {"7", "8"})
        self.assertFalse(self.path.exists())

    def test_upcoming_to_running_does_not_send_twice(self):
        upcoming = fixture()
        upcoming["activities"][0].update(running=False, starts_at="2026-10-15T13:00:00+08:00")
        with patch.object(monitor, "telegram_request") as send:
            self.check(upcoming)
            upcoming["server_time"] = "2026-10-15T14:00:00+08:00"
            upcoming["activities"][0]["running"] = True
            self.check(upcoming)
            send.assert_called_once()

    def test_disabled_or_missing_credentials_do_not_consume_notification(self):
        with patch.object(monitor, "telegram_request") as send:
            self.check(fixture(), token="")
            disabled = {**self.config, "telegram": {"disabled": True}}
            self.check(fixture(), config=disabled)
            self.assertFalse(seckill.state_file(self.path).exists())
            self.check(fixture())
            send.assert_called_once()

    def test_failed_delivery_retries_only_unsent_activity(self):
        data = fixture()
        other = copy.deepcopy(data["activities"][0]); other["id"] = 8
        data["activities"].append(other)
        with patch.object(monitor, "telegram_request", side_effect=[{}, monitor.MonitorError("delivery failed")]):
            with self.assertRaises(monitor.MonitorError):
                self.check(data)
        self.assertEqual(set(monitor.read_state(seckill.state_file(self.path))), {"7"})
        with patch.object(monitor, "telegram_request") as send:
            self.assertEqual(self.check(data)["sent_count"], 1)
            send.assert_called_once()

    def test_corrupt_state_or_invalid_response_cannot_send_or_reset_history(self):
        seckill.state_file(self.path).write_text("not json", encoding="utf-8")
        with patch.object(monitor, "telegram_request") as send, self.assertRaises(monitor.MonitorError):
            self.check(fixture())
        send.assert_not_called()
        for bad in [{}, {"activities": None, "server_time": fixture()["server_time"]}, {"activities": [], "server_time": "invalid"}]:
            with self.subTest(data=bad), self.assertRaises(monitor.MonitorError):
                self.parse(bad)
        for changes in [{"id": True}, {"running": "true"}, {"starts_at": "2026-10-18T12:00:00+08:00"}, {"items": None}]:
            data = fixture(); data["activities"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(monitor.MonitorError):
                self.parse(data)
        for changes in [{"stock": "30"}, {"sold": -1}, {"seckill_price": "NaN"}, {"settings": [None]}]:
            data = fixture(); data["activities"][0]["items"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(monitor.MonitorError):
                self.parse(data)

    def test_duplicate_campaign_and_item_ids_rejected(self):
        data = fixture(); data["activities"].append(copy.deepcopy(data["activities"][0]))
        with self.assertRaises(monitor.MonitorError): self.parse(data)
        data = fixture(); data["activities"][0]["items"] *= 2
        with self.assertRaises(monitor.MonitorError): self.parse(data)

    def test_campaign_message_escapes_content_currency_and_current_affiliate(self):
        activity = self.parse(fixture())["activities"][0]
        text, keyboard = seckill.campaign_message(activity, {"affiliates": {"isvoro": "new-code"}})
        for value in ["¥79", "1C/1G/10GB", "剩余 3 份", "IP：—", "服务器信息", "&lt;test&gt;", "2026-10-17", "aff=new-code"]:
            self.assertIn(value, text)
        self.assertIn("aff=new-code", keyboard["inline_keyboard"][0][0]["url"])
        activity["title"] = "😀" * 500
        activity["description"] = "😀" * 500
        item = activity["items"][0]
        activity["items"] = [{**item, **{k: "😀" * 500 for k in ["name", "plan", "price", "specs", "traffic"]}} for _ in range(20)]
        text, _ = seckill.campaign_message(activity, self.config, test=True)
        class Extract(HTMLParser):
            plain = ""
            def handle_data(self, value): self.plain += value
        parser = Extract(); parser.feed(text)
        self.assertLessEqual(len(parser.plain.encode("utf-16-le")) // 2, 4096)

    def test_seckill_runs_with_independent_notify_role_without_inventory_products(self):
        with patch.object(monitor, "fetch_isvoro_json", return_value=fixture()), patch.object(monitor, "telegram_request") as send:
            monitor.check_once(Mock(), self.config, self.path, self.path.parent / "status.json", "mock", "123", role="notify")
        send.assert_called_once()
        status = json.loads((self.path.parent / "status.json").read_text(encoding="utf-8"))
        self.assertTrue(status["seckill"]["ok"])
        self.assertEqual(status["products"], [])

    def test_seckill_only_round_and_merchant_request_policy(self):
        config = {**self.config, "fetcher": {"mode": "requests", "provider_modes": {"isvoro": "flaresolverr"}}}
        with patch.object(monitor, "fetch_isvoro_json", return_value=fixture()) as fetch, patch.object(monitor, "telegram_request"):
            monitor.check_once(Mock(), config, self.path, self.path.parent / "status.json", "mock", "123")
        self.assertEqual(fetch.call_args.args[1], "seckill/activities")
        self.assertEqual(fetch.call_args.args[2]["mode"], "flaresolverr")
        status = json.loads((self.path.parent / "status.json").read_text(encoding="utf-8"))
        self.assertTrue(status["seckill"]["ok"])
        self.assertEqual(status["products"], [])
        self.assertEqual(status["state"], "ok")

    def test_activity_error_does_not_hide_valid_product_stock(self):
        config = {**self.config, "products": [html_product()]}
        product = monitor.Product("one", "VPS", 3, "https://shop.example.com/item")
        with patch.object(monitor, "fetch_isvoro_json", side_effect=monitor.MonitorError("invalid activity")), patch.object(monitor, "fetch_html_stock", return_value=product):
            monitor.check_once(Mock(), config, self.path, self.path.parent / "status.json", "", "")
        status = json.loads((self.path.parent / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["state"], "partial")
        self.assertIn("invalid activity", status["message"])
        self.assertEqual(status["products"][0]["available"], 3)

    def test_flag_defaults_off_and_only_enabled_flag_can_start_without_products(self):
        self.assertFalse(admin.validate_config({})["isvoro_seckill"])
        self.assertTrue(self.config["isvoro_seckill"])
        with self.assertRaises(ValueError): admin.validate_config({"enabled": True})
        with self.assertRaises(ValueError): admin.validate_config({"isvoro_seckill": "true"})
        with patch.object(monitor, "fetch_isvoro_json") as fetch:
            with self.assertRaises(monitor.MonitorError):
                monitor.check_once(Mock(), {}, self.path, self.path.parent / "status.json", "", "")
            fetch.assert_not_called()

    def test_manual_worker_and_manual_notification_do_not_write_auto_state(self):
        product = {"id": seckill.TEST_PRODUCT_ID, "adapter": "isvoro", "provider": "Isvoro", "name": "限时秒杀", "_seckill": True}
        output = io.StringIO()
        with patch.object(admin.sys, "stdin", io.StringIO(json.dumps({"product": product, "fetcher": {}}))), patch.object(admin.sys, "stdout", output), patch.object(monitor, "fetch_isvoro_json", return_value=fixture()):
            admin.stock_test_worker()
        result = json.loads(output.getvalue()); result.update(checked_at="2026-10-15", duration_seconds=0.1)
        with patch.object(admin, "telegram_credentials", return_value=("mock", "123")), patch.object(admin, "current_config", return_value=self.config), patch.object(admin, "telegram_call") as send, patch.object(monitor, "write_state") as write:
            self.assertEqual(admin.send_stock_test_result(product, result)["status"], "sent")
            self.assertIn("秒杀单次测试", send.call_args.kwargs["text"])
            result["activities"] = []
            admin.send_stock_test_result(product, result)
            self.assertIn("暂无秒杀活动", send.call_args.kwargs["text"])
            write.assert_not_called()

    def test_manual_start_shares_registry_and_cancellation(self):
        with patch.object(admin, "TEST_TASKS", {}), patch.object(admin, "STOPPED_TEST_IDS", {}), patch.object(admin.threading, "Thread"):
            task_id = "seckill-test-1234567890"
            result = admin.start_stock_test({"task_id": task_id, "kind": "isvoro_seckill", "fetcher": {"mode": "requests"}})
            self.assertEqual(result["product_id"], seckill.TEST_PRODUCT_ID)
            task = admin.TEST_TASKS[task_id]
            self.assertTrue(task.product["_seckill"])
            admin.stop_stock_test(task_id)
            self.assertTrue(task.cancelled.is_set())
            with self.assertRaises(ValueError): admin.start_stock_test({"task_id": "other-test-1234567890", "kind": "isvoro_seckill"})


if __name__ == "__main__":
    unittest.main()
