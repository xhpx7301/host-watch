"""Independent stock following and Telegram subscriptions."""
import json
import http.client
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import admin
import monitor
import test_admin
from test_monitor import html_product
from test_akile import catalog as akile_fixture
from test_akile import item as akile_item
from test_isvoro import directory, item as isvoro_item


class MembershipTests(unittest.TestCase):
    def test_unlisted_product_keeps_history_and_resumes_without_false_stock(self):
        with tempfile.TemporaryDirectory() as tmp:
            state, status = Path(tmp) / "state.json", Path(tmp) / "status.json"
            previous = {"missing": {"available": 0, "notify_enabled": True}, "other": {"available": 0, "notify_enabled": True}}
            monitor.write_state(state, previous)
            config = {"products": [akile_item(id="missing", selector="Removed"), akile_item(id="other", selector="HKL-TW-Starter")]}
            payload = akile_fixture()
            with patch.object(monitor, "fetch_akile_catalog", return_value=payload), patch.object(monitor, "telegram_request") as send:
                monitor.check_once(Mock(), config, state, status, "token", "chat")
                self.assertEqual(send.call_count, 1)  # Healthy product still notifies.
                self.assertEqual(monitor.read_state(state)["missing"], previous["missing"])
                output = json.loads(status.read_text(encoding="utf-8"))
                self.assertEqual(output["state"], "partial")
                self.assertIsNone(output["products"][0]["available"])
                self.assertIn("未匹配", output["message"])
                config["products"][0]["selector"] = "HKL-TW-Mini"
                payload["data"]["areas"][0]["nodes"][0]["plans"][0]["stock"] = 2
                monitor.check_once(Mock(), config, state, status, "token", "chat")
                self.assertEqual(send.call_count, 2)
                self.assertEqual(json.loads(status.read_text(encoding="utf-8"))["state"], "ok")

    def test_migration_and_membership_matrix_round_trip(self):
        for old in (False, True):
            result = admin.validate_config({"products": [html_product(enabled=old)]})
            product = result["products"][0]
            self.assertEqual((product["watch_enabled"], product["notify_enabled"]), (old, old))
        for watch, notify in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(watch=watch, notify=notify):
                value = admin.validate_config({"products": [html_product(
                    enabled=True, watch_enabled=watch, notify_enabled=notify,
                    remarks="自用", line_label="CN2 GIA",
                    snapshot={"price": "USD 5 / 月", "specs": "1 核", "traffic": "1 TB", "available": 3,
                              "url": "https://shop.example.com/item"})]})
                product = admin.validate_config(json.loads(json.dumps(value)))["products"][0]
                self.assertEqual(product["enabled"], watch or notify)
                self.assertEqual(product["remarks"], "自用")
                self.assertEqual(product["line_label"], "CN2 GIA")
                self.assertEqual(product["snapshot"]["traffic"], "1 TB")
                self.assertEqual(len(monitor.selected_products(value)), int(watch or notify))
        product = admin.validate_config({"products": [html_product(watch_enabled=True)]})["products"][0]
        self.assertFalse(product["notify_enabled"])

    def test_watch_only_never_notifies_but_notification_only_does(self):
        with tempfile.TemporaryDirectory() as tmp:
            state, status = Path(tmp) / "state.json", Path(tmp) / "status.json"
            watch = html_product(id="watch", watch_enabled=True, notify_enabled=False)
            notify = html_product(id="notify", watch_enabled=False, notify_enabled=True)
            config = admin.validate_config({"products": [watch, notify], "notify_current_stock_on_start": True})
            result = monitor.Product("key", "Auto name", 2, "https://shop.example.com/order", region="香港",
                                     specs="1 核 · 1 GB", traffic="1 TB · 1 Gbps", price="USD 5 / 月")
            with patch.object(monitor, "fetch_html_stock", return_value=result), patch.object(monitor, "telegram_request") as send:
                monitor.check_once(Mock(), config, state, status, "token", "123")
                self.assertEqual(send.call_count, 1)
                self.assertIn("1 TB", send.call_args.kwargs["text"])
                monitor.check_once(Mock(), config, state, status, "token", "123")
                self.assertEqual(send.call_count, 1)  # No repeated in-stock message.
                saved = json.loads(status.read_text(encoding="utf-8"))["products"]
                self.assertEqual(len(saved), 2)
                self.assertEqual(saved[0]["price"], "USD 5 / 月")
                self.assertEqual(config["products"][0]["name"], watch["name"])

    def test_enabling_notifications_on_followed_product_obeys_first_stock_setting(self):
        for first in (False, True):
            with self.subTest(first=first), tempfile.TemporaryDirectory() as tmp:
                state, status = Path(tmp) / "state.json", Path(tmp) / "status.json"
                product = html_product(watch_enabled=True, notify_enabled=False)
                config = {"products": [product], "notify_current_stock_on_start": first}
                with patch.object(monitor, "fetch_html_stock", return_value=monitor.Product("p", "VPS", 3, "https://shop.example.com/item")), patch.object(monitor, "telegram_request") as send:
                    monitor.check_once(Mock(), config, state, status, "token", "123")
                    product["notify_enabled"] = True
                    monitor.check_once(Mock(), config, state, status, "token", "123")
                    monitor.check_once(Mock(), config, state, status, "token", "123")
                    self.assertEqual(send.call_count, int(first))

    def test_watch_only_runs_without_telegram_and_never_sends_stop_alerts(self):
        config = admin.validate_config({"enabled": True, "products": [html_product(watch_enabled=True, notify_enabled=False)]})
        for credentials in (("", ""), ("token", "123")):
            with self.subTest(credentials=bool(credentials[0])), patch("sys.argv", ["monitor.py", "--once"]), patch.object(monitor, "read_config", return_value=config), patch.object(monitor, "effective_telegram", return_value=credentials), patch.object(monitor, "check_once") as check:
                self.assertEqual(monitor.main(), 0)
                self.assertEqual(check.call_args.args[-2:], ("", ""))
            with patch("sys.argv", ["monitor.py", "--once"]), patch.object(monitor, "read_config", return_value=config), patch.object(monitor, "effective_telegram", return_value=credentials), patch.object(monitor, "write_status"), patch.object(monitor, "check_once", side_effect=monitor.SiteBlocked("blocked")), patch.object(monitor, "telegram_request") as send:
                self.assertEqual(monitor.main(), 0)
                send.assert_not_called()

    def test_vendor_metadata_uses_same_response_as_stock(self):
        payload = akile_fixture()
        plan = payload["data"]["areas"][0]["nodes"][0]["plans"][0]
        plan.update(cpu=2, memory=2048, disk=30, flow=500, bandwidth=1000, price_datas=[{"price": 2499, "cycle": 1}])
        product = monitor.parse_akile(payload, {"area_id": "6", "area_mode": "manual", "node_name": "HKL-TW", "selector": plan["plan_name"]})
        self.assertIn("2048 MB", product.specs)
        self.assertIn("500 GB", product.traffic)
        self.assertEqual(product.price, "¥24.99 / 1个月")
        data = directory()
        data["products/hkpordmini"].update(settings={"cores": 1, "memory": 1024, "disk": 10, "bandwidth_limit": 100}, plans=[{"name": "月", "prices": [{"price": 10, "currency_code": "USD"}]}])
        with patch.object(monitor, "fetch_isvoro_json", side_effect=lambda session, path, fetcher: data[path]):
            product = monitor.inspect_isvoro(Mock(), isvoro_item(), {})
        self.assertIn("1024 MB", product.specs)
        self.assertIn("100 GB", product.traffic)
        self.assertEqual(product.price, "USD 10 / 月")

    def test_html_hardware_network_and_price_are_read_separately(self):
        card = monitor.BeautifulSoup('<div class="cartitem"><div class="card-body"><div class="card-text">CPU：1 核 内存：512 MB 硬盘：5 GB 带宽：1 Gbps 流量：200 GB</div></div><p class="card-text">库存：4</p><div>¥ <span class="cart-num">4.50</span> 元 / 月</div></div>', "html.parser")
        result = monitor.html_metadata(card)
        self.assertIn("内存：512 MB", result["specs"])
        self.assertNotIn("流量", result["specs"])
        self.assertIn("流量：200 GB", result["traffic"])
        self.assertNotIn("库存", result["specs"])
        self.assertIn("4.50", result["price"])


class MembershipAPITests(unittest.TestCase):
    setUp = test_admin.AdminAPITests.setUp
    request = test_admin.AdminAPITests.request

    def test_save_reload_preserves_independent_selections_and_manual_fields(self):
        value = admin.default_config()
        value["products"] = [html_product(watch_enabled=True, notify_enabled=False, remarks="备注", line_label="手动线路")]
        with patch.object(admin, "manager_action", return_value={"ok": True}):
            code, body = self.request("/api/config", value)
        self.assertEqual(code, 200)
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        self.addCleanup(connection.close)
        connection.request("GET", "/api/config")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        body = response.read()
        saved = json.loads(body)["products"][0]
        self.assertTrue(saved["enabled"])
        self.assertTrue(saved["watch_enabled"])
        self.assertFalse(saved["notify_enabled"])
        self.assertEqual(saved["remarks"], "备注")
        self.assertEqual(saved["line_label"], "手动线路")


if __name__ == "__main__":
    unittest.main()
