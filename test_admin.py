import http.client
import base64
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock

import admin


class AdminAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "config.json"
        for setting in (patch.object(admin, "CONFIG_PATH", self.config),
                        patch.object(admin, "authorized", return_value=True),
                        patch.dict(admin.STOPPED_TEST_IDS, {}, clear=True),
                        patch.object(admin, "telegram_credentials", return_value=("", "")),
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

    def get_request(self, path):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        self.addCleanup(connection.close)
        connection.request("GET", path)
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

    def test_management_backup_download_returns_archive_attachment(self):
        archive = b"backup bytes"
        payload = {
            "ok": True,
            "name": "host-watch-20261016-120000-abcd.tar.gz",
            "content": base64.b64encode(archive).decode("ascii"),
        }
        with patch.object(admin, "manager_action", return_value=payload) as manager:
            status, body = self.request("/api/manage", {"action": "backup-download"})
        self.assertEqual(status, 200)
        self.assertEqual(body, archive)
        manager.assert_called_once_with({"action": "backup-download"})

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

    def test_exchange_rates_refresh_daily_and_reuse_cache(self):
        response = Mock(ok=True)
        response.json.return_value = {"base": "CNY", "date": "2026-10-16", "rates": {"USD": 0.14, "HKD": 1.17}}
        rates_path = Path(self.temp.name) / "exchange-rates.json"
        with patch.object(admin, "RATES_PATH", rates_path), patch.object(admin.requests, "get", return_value=response) as get:
            first = admin.exchange_rates()
            second = admin.exchange_rates()
        self.assertEqual(first["rates"]["USD"], 0.14)
        self.assertFalse(first["stale"])
        self.assertEqual(second["rates"]["HKD"], 1.17)
        get.assert_called_once_with(admin.EXCHANGE_RATES_URL, timeout=(3, 8))

    def test_exchange_rate_failure_keeps_last_successful_rates(self):
        rates_path = Path(self.temp.name) / "exchange-rates.json"
        rates_path.write_text(json.dumps({"base": "CNY", "rates": {"USD": 0.14}, "date": "2026-10-15", "fetched_at": 1}), encoding="utf-8")
        with patch.object(admin, "RATES_PATH", rates_path), patch.object(admin.requests, "get", side_effect=admin.requests.Timeout):
            result = admin.exchange_rates()
        self.assertTrue(result["stale"])
        self.assertEqual(result["rates"]["USD"], 0.14)

    def test_exchange_rate_endpoint_returns_live_rate_payload(self):
        payload = {"base": "CNY", "rates": {"USD": 0.14}, "date": "2026-10-16", "fetched_at": 1, "stale": False}
        with patch.object(admin, "exchange_rates", return_value=payload):
            status, body = self.get_request("/api/rates")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), payload)

    def test_product_request_mode_defaults_and_rejects_unknown_mode(self):
        item = {"id": "vps", "name": "test", "adapter": "html", "url": "https://shop.example.com/item", "stock_selector": ".stock"}
        self.assertEqual(admin.validate_config({"products": [item]})["products"][0]["request_mode"], "inherit")
        with self.assertRaisesRegex(ValueError, "请求方式无效"):
            admin.validate_config({"products": [{**item, "request_mode": "unknown"}]})

    def test_single_product_test_uses_unsaved_disabled_product_without_side_effects(self):
        item = {"id": "test", "name": "test", "adapter": "html", "url": "https://shop.example.com/item", "stock_selector": ".stock", "enabled": False, "request_mode": "requests"}
        task_id = "test-1234567890123456"
        process = Mock()
        process.communicate.return_value = (json.dumps({"ok": True, "available": 7, "stock_mode": "quantity"}), None)
        process.poll.return_value = 0
        with patch.dict(admin.TEST_TASKS, {}, clear=True), patch.object(admin.subprocess, "Popen", return_value=process), patch.object(admin, "manager_action") as manager, patch("monitor.write_state") as history, patch("monitor.write_status") as status, patch("monitor.telegram_request") as telegram:
            code, _ = self.request("/api/products/test", {"task_id": task_id, "product": item, "fetcher": {"mode": "flaresolverr", "wait_seconds": 3}})
            self.assertTrue(admin.TEST_TASKS[task_id].done.wait(3))
            result = admin.TEST_TASKS[task_id].snapshot()
        self.assertEqual(code, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(result["available"], 7)
        self.assertIn("checked_at", result)
        submitted = json.loads(process.communicate.call_args.args[0])
        self.assertFalse(submitted["product"]["enabled"])
        self.assertEqual(submitted["fetcher"]["wait_seconds"], 3)
        for action in [manager, history, status, telegram]:
            action.assert_not_called()
        self.assertFalse(self.config.exists())

    def test_single_product_test_returns_inline_error(self):
        item = {"id": "test", "name": "test", "adapter": "html", "url": "https://shop.example.com/item", "stock_selector": ".stock"}
        task_id = "test-1234567890123456"
        process = Mock()
        process.communicate.return_value = (json.dumps({"ok": False, "error": "HTTP 403"}), None)
        process.poll.return_value = 0
        with patch.dict(admin.TEST_TASKS, {}, clear=True), patch.object(admin.subprocess, "Popen", return_value=process):
            code, _ = self.request("/api/products/test", {"task_id": task_id, "product": item})
            self.assertTrue(admin.TEST_TASKS[task_id].done.wait(3))
            result = admin.TEST_TASKS[task_id].snapshot()
        self.assertEqual(code, 200)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "HTTP 403")

    def test_single_product_validation_prevents_network_request(self):
        with patch("monitor.inspect_product") as inspect:
            code, _ = self.request("/api/products/test", {"task_id": "test-1234567890123456", "product": {"id": "bad"}})
        self.assertEqual(code, 400)
        inspect.assert_not_called()

    def test_async_start_and_stop_work_while_management_lock_is_held(self):
        item = {"id": "test", "name": "test", "adapter": "html", "url": "https://shop.example.com/item", "stock_selector": ".stock"}
        task_id = "test-1234567890123456"
        with patch.dict(admin.TEST_TASKS, {}, clear=True), patch.object(admin.StockTestTask, "run"), admin.MUTATION_LOCK:
            code, body = self.request("/api/products/test", {"task_id": task_id, "product": item})
            self.assertEqual(code, 200)
            self.assertTrue(json.loads(body)["pending"])
            code, body = self.request("/api/products/test/stop", {"task_id": task_id})
            self.assertEqual(code, 200)
            self.assertTrue(admin.TEST_TASKS[task_id].cancelled.is_set())
        self.assertFalse(self.config.exists())

    def test_cross_site_request_cannot_cancel_test(self):
        code, _ = self.request("/api/products/test/stop", {"task_id": "test-1234567890123456"}, {"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(code, 403)

    def test_slow_process_spawn_cannot_block_start_status_or_stop_requests(self):
        item = {"id": "test", "name": "test", "adapter": "html", "url": "https://shop.example.com/item", "stock_selector": ".stock"}
        task_id = "slow-spawn-1234567890123456"
        spawning, release = threading.Event(), threading.Event()
        import subprocess
        import sys
        real_popen = subprocess.Popen

        def spawn(*args, **kwargs):
            spawning.set()
            release.wait(5)
            return real_popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)

        with patch.dict(admin.TEST_TASKS, {}, clear=True), patch.object(admin.subprocess, "Popen", side_effect=spawn):
            try:
                code, _ = self.request("/api/products/test", {"task_id": task_id, "product": item})
                self.assertEqual(code, 200)
                self.assertTrue(spawning.wait(3))
                connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=1)
                self.addCleanup(connection.close)
                connection.request("GET", f"/api/products/test/status/{task_id}")
                response = connection.getresponse()
                self.assertTrue(json.loads(response.read())["pending"])
                connection.request("POST", "/api/products/test/stop", json.dumps({"task_id": task_id}), {"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertTrue(json.loads(response.read())["stopping"])
            finally:
                release.set()
                self.assertTrue(admin.TEST_TASKS[task_id].done.wait(3))
            self.assertTrue(admin.TEST_TASKS[task_id].snapshot()["cancelled"])

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
