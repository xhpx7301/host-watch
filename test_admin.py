import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import admin


class AdminAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "config.json"
        for setting in (patch.object(admin, "CONFIG_PATH", self.config),
                        patch.object(admin, "authorized", return_value=True),
                        patch.dict(os.environ, {"ADMIN_ALLOWED_HOSTS": "127.0.0.1"})):
            setting.start()
            self.addCleanup(setting.stop)
        self.server = admin.ThreadingHTTPServer(("127.0.0.1", 0), admin.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, path, payload, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        self.addCleanup(connection.close)
        connection.request("POST", path, json.dumps(payload), {"Content-Type": "application/json", **(headers or {})})
        response = connection.getresponse()
        return response.status, response.read()

    def test_save_applies_service_and_returns_redacted_config(self):
        value = admin.default_config()
        value["telegram"] = {"bot_token": "private-token", "chat_id": "123"}
        with patch.object(admin, "manager_action", return_value={"ok": True}) as manager:
            status, body = self.request("/api/config", value)
        self.assertEqual(status, 200)
        self.assertNotIn(b"private-token", body)
        self.assertTrue(json.loads(body)["service"]["ok"])
        manager.assert_called_once_with({"action": "monitor-apply"})
        self.assertEqual(json.loads(self.config.read_text())["telegram"]["bot_token"], "private-token")

    def test_save_failure_does_not_apply_service(self):
        with patch.object(admin, "save_config", side_effect=OSError("disk full")), patch.object(admin, "manager_action") as manager:
            status, _ = self.request("/api/config", admin.default_config())
        self.assertEqual(status, 400)
        manager.assert_not_called()

    def test_saved_config_is_preserved_when_service_fails(self):
        with patch.object(admin, "manager_action", side_effect=ValueError("service error")):
            status, body = self.request("/api/config", admin.default_config())
        self.assertEqual(status, 200)
        self.assertTrue(self.config.exists())
        self.assertFalse(json.loads(body)["service"]["ok"])
        self.assertIn("service error", json.loads(body)["service"]["error"])

    def test_cross_site_and_non_json_requests_cannot_control_services(self):
        for headers in ({"Content-Type": "text/plain"}, {"Sec-Fetch-Site": "cross-site"}, {"Origin": "https://evil.example"}):
            with self.subTest(headers=headers), patch.object(admin, "manager_action") as manager:
                status, _ = self.request("/api/manage", {"action": "monitor-stop"}, headers)
                self.assertIn(status, (403, 415))
                manager.assert_not_called()

    def test_start_rejects_invalid_monitor_config(self):
        with patch.object(admin, "manager_action") as manager:
            status, _ = self.request("/api/manage", {"action": "monitor-start"})
        self.assertEqual(status, 400)
        manager.assert_not_called()

    def test_authentication_is_required_for_management(self):
        with patch.object(admin, "authorized", return_value=False), patch.object(admin, "manager_action") as manager:
            status, _ = self.request("/api/manage", {"action": "update"})
        self.assertEqual(status, 401)
        manager.assert_not_called()

    def test_browser_health_uses_unsaved_endpoint_without_config_mutation(self):
        response = unittest.mock.Mock(ok=True)
        response.json.return_value = {"msg": "FlareSolverr is ready!", "version": "3.test"}
        with patch.object(admin.requests, "get", return_value=response) as get, patch.object(admin, "manager_action") as manager:
            status, body = self.request("/api/fetcher/test", {"flaresolverr_url": "http://127.0.0.1:8191/v1"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["version"], "3.test")
        get.assert_called_once_with("http://127.0.0.1:8191/", timeout=(3, 5), allow_redirects=False)
        manager.assert_not_called()
        self.assertFalse(self.config.exists())

    def test_health_rejects_unrelated_service(self):
        response = unittest.mock.Mock(ok=True)
        response.json.return_value = {"status": "ok"}
        with patch.object(admin.requests, "get", return_value=response):
            status, body = self.request("/api/fetcher/test", {})
        self.assertEqual(status, 400)
        self.assertIn("未返回 FlareSolverr", json.loads(body)["error"])

    def test_browser_settings_validate_and_legacy_defaults_are_preserved(self):
        self.assertEqual(admin.normalize_fetcher({})["wait_seconds"], 0)
        for wait in [-1, 31, True, 1.5]:
            with self.subTest(wait=wait), self.assertRaises(ValueError):
                admin.normalize_fetcher({"wait_seconds": wait})

    def test_product_request_mode_defaults_and_rejects_unknown_mode(self):
        item = {"id": "vps", "name": "test", "adapter": "html", "url": "https://shop.example.com/item", "stock_selector": ".stock"}
        self.assertEqual(admin.validate_config({"products": [item]})["products"][0]["request_mode"], "inherit")
        with self.assertRaisesRegex(ValueError, "请求方式无效"):
            admin.validate_config({"products": [{**item, "request_mode": "unknown"}]})

    def test_single_product_test_uses_unsaved_disabled_product_without_side_effects(self):
        from monitor import Product
        item = {"id": "test", "name": "test", "adapter": "html", "url": "https://shop.example.com/item", "stock_selector": ".stock", "enabled": False}
        with patch("monitor.inspect_product", return_value=Product("test", "test", 7, item["url"], stock_mode="quantity")) as inspect, patch.object(admin, "manager_action") as manager, patch("monitor.write_state") as history, patch("monitor.write_status") as status, patch("monitor.telegram_request") as telegram:
            code, body = self.request("/api/products/test", {"product": item, "fetcher": {"mode": "flaresolverr", "wait_seconds": 3}})
        result = json.loads(body)
        self.assertEqual(code, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(result["available"], 7)
        self.assertIn("checked_at", result)
        self.assertFalse(inspect.call_args.args[1]["enabled"])
        self.assertEqual(inspect.call_args.args[2]["wait_seconds"], 3)
        for action in [manager, history, status, telegram]:
            action.assert_not_called()
        self.assertFalse(self.config.exists())

    def test_single_product_test_returns_inline_error(self):
        from monitor import SiteBlocked
        item = {"id": "test", "name": "test", "adapter": "html", "url": "https://shop.example.com/item", "stock_selector": ".stock"}
        with patch("monitor.inspect_product", side_effect=SiteBlocked("HTTP 403")):
            code, body = self.request("/api/products/test", {"product": item})
        self.assertEqual(code, 200)
        self.assertFalse(json.loads(body)["ok"])
        self.assertEqual(json.loads(body)["error"], "HTTP 403")

    def test_single_product_validation_prevents_network_request(self):
        with patch("monitor.inspect_product") as inspect:
            code, _ = self.request("/api/products/test", {"product": {"id": "bad"}})
        self.assertEqual(code, 400)
        inspect.assert_not_called()

    def test_save_cannot_change_monitoring_switch(self):
        value = admin.default_config()
        value["enabled"] = True
        with patch.object(admin, "manager_action", return_value={"ok": True}):
            code, body = self.request("/api/config", value)
        self.assertEqual(code, 200)
        self.assertFalse(json.loads(body)["config"]["enabled"])

    def test_saving_while_monitoring_preserves_enabled_flag(self):
        item = {"id": "test", "name": "test", "adapter": "html", "url": "https://shop.example.com/item", "stock_selector": ".stock", "enabled": True}
        value = {**admin.default_config(), "enabled": True, "products": [item]}
        self.config.write_text(json.dumps(value))
        with patch.object(admin, "manager_action", return_value={"ok": True}):
            code, body = self.request("/api/config", {**value, "enabled": False})
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["config"]["enabled"])


if __name__ == "__main__":
    unittest.main()
