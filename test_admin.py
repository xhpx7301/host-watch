import http.client
import base64
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
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

    def test_backup_download_manager_action_allows_archive_creation_time(self):
        with patch.object(admin.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout='{"ok":true}\n', stderr="")) as run:
            result = admin.manager_action({"action": "backup-download"})
        self.assertTrue(result["ok"])
        self.assertEqual(run.call_args.kwargs["timeout"], 180)

    def test_market_endpoints_require_auth_and_refresh_does_not_evaluate(self):
        with patch.object(admin, "authorized", return_value=False), patch.object(admin, "refresh_akile_market") as refresh:
            code, _ = self.request("/api/market/refresh", {"account_id": "ak"})
            self.assertEqual(code, 401)
            refresh.assert_not_called()
        with patch.object(admin, "refresh_akile_market", return_value={"items": []}) as refresh:
            code, _ = self.request("/api/market/refresh", {"account_id": "ak"})
            self.assertEqual(code, 200)
            refresh.assert_called_once_with(account_id="ak", evaluate=False)
        with patch.object(admin, "refresh_akile_market", return_value={"items": []}) as refresh:
            code, _ = self.request("/api/market/check", {})
            self.assertEqual(code, 200)
            refresh.assert_called_once_with(account_id="", evaluate=True)
        with patch.object(admin, "refresh_akile_market", return_value={"items": [], "results": []}) as refresh:
            code, _ = self.request("/api/market/check", {"account_id": "ak", "rule_id": "rule-1"})
            self.assertEqual(code, 200)
            refresh.assert_called_once_with(account_id="ak", evaluate=True, rule_id="rule-1")

    def test_market_test_notification_sends_explicit_sample_to_saved_telegram(self):
        with patch.object(admin, "authorized", return_value=False), patch.object(admin, "telegram_call") as send:
            code, _ = self.request("/api/market/test-notification", {})
            self.assertEqual(code, 401)
            send.assert_not_called()
        with patch.object(admin, "effective_telegram", return_value=("test-token", "123")), \
                patch.object(admin, "telegram_call") as send:
            code, body = self.request("/api/market/test-notification", {})
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertIn("已发送至 Telegram", json.loads(body)["message"])
        send.assert_called_once()
        args, kwargs = send.call_args
        self.assertEqual(args, ("test-token", "sendMessage"))
        self.assertEqual(kwargs["chat_id"], "123")
        self.assertIn("测试通知", kwargs["text"])
        self.assertIn("不会创建订单或付款", kwargs["text"])

    def test_market_test_notification_requires_active_saved_telegram(self):
        with patch.object(admin, "effective_telegram", return_value=("", "")), patch.object(admin, "telegram_call") as send:
            code, body = self.request("/api/market/test-notification", {})
        self.assertEqual(code, 400)
        self.assertIn("保存 Telegram", json.loads(body)["error"])
        send.assert_not_called()

    def test_market_test_notification_reports_telegram_delivery_failure(self):
        with patch.object(admin, "effective_telegram", return_value=("test-token", "123")), \
                patch.object(admin, "telegram_call", side_effect=admin.TelegramAPIError("Telegram sendMessage 失败")):
            code, body = self.request("/api/market/test-notification", {})
        self.assertEqual(code, 502)
        self.assertIn("sendMessage", json.loads(body)["error"])

    def test_seckill_success_notification_includes_merchant_and_total_duration(self):
        config = admin.default_config()
        config["products"] = [{"id": "product-1", "provider": "VMISS", "name": "US Basic"}]
        config["vps_seckill"]["accounts"] = [{"id": "account-1", "name": "VMISS 备用账号", "provider": "vmiss"}]
        task = admin.SeckillTask("seckill-test-1234567890", config, "rapid")
        item = {"id": "item-1", "product_id": "product-1", "account_id": "account-1", "quantity": 1}
        with patch.object(admin, "effective_telegram", return_value=("test-token", "123")), \
                patch.object(admin, "telegram_call") as send:
            result = task._notify_unpaid_order(item, "INV-123", 1, "https://example.com/invoice/INV-123",
                                               duration_seconds=21.4)
        self.assertEqual(result, "sent")
        message = send.call_args.kwargs["text"]
        self.assertIn("商家：VMISS", message)
        self.assertIn("账户：VMISS 备用账号", message)
        self.assertIn("总耗时：21.4 秒", message)

    def test_older_config_save_preserves_market_settings(self):
        from test_akile_market import config as market_config
        current = market_config()
        self.config.write_text(json.dumps(current), encoding="utf-8")
        draft = dict(current)
        draft.pop("akile_market")
        with patch.object(admin, "manager_action", return_value={"ok": True}):
            code, body = self.request("/api/config", draft)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["config"]["akile_market"], current["akile_market"])
        self.assertNotIn(b"secret-token", body)

    def test_versioned_static_assets_are_served(self):
        for asset in ("admin.js", "catalog.js"):
            with self.subTest(asset=asset):
                status, body = self.get_request(f"/{asset}?v=card-save-test")
                self.assertEqual(status, 200)
                self.assertEqual(body, (Path(admin.ROOT) / asset).read_bytes())

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

    def test_provider_schedules_keep_module_settings_independent(self):
        from monitor import module_schedule
        value = admin.default_config()
        value["monitor_schedule"] = {"mode": "auto", "interval_seconds": 600}
        value["provider_schedules"] = {
            "monitor": {"vmiss": {"mode": "manual", "interval_seconds": 600}, "dmit": {"mode": "auto", "interval_seconds": 45}},
            "notify": {"vmiss": {"mode": "auto", "interval_seconds": 180}, "dmit": {"mode": "manual", "interval_seconds": 600}},
            "catalog": {"vmiss": {"mode": "manual", "interval_seconds": 300}},
            "seckill": {"vmiss": {"mode": "rapid", "interval_seconds": 2}, "dmit": {"mode": "precise", "interval_seconds": 20}},
        }
        normalized = admin.validate_config(value)
        self.assertEqual(module_schedule(normalized, "monitor", "vmiss")["mode"], "manual")
        self.assertEqual(module_schedule(normalized, "monitor", "dmit")["interval_seconds"], 45)
        self.assertEqual(module_schedule(normalized, "notify", "vmiss")["interval_seconds"], 180)
        self.assertEqual(normalized["provider_schedules"]["seckill"]["dmit"]["mode"], "precise")

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

    def test_backup_download_accepts_real_tempfile_suffixes(self):
        # The manager uses mkstemp, whose alphabet includes g-z and underscore.
        for suffix in ("gz6n3_qw", "abcd", "1234abcd"):
            with self.subTest(suffix=suffix):
                payload = {"ok": True, "name": f"host-watch-20261016-063315-{suffix}.tar.gz",
                    "content": base64.b64encode(b"archive bytes").decode("ascii")}
                with patch.object(admin, "manager_action", return_value=payload):
                    status, body = self.request("/api/manage", {"action": "backup-download"})
                self.assertEqual(status, 200)
                self.assertEqual(body, b"archive bytes")

    def test_backup_download_still_rejects_unsafe_filenames(self):
        for filename in ("../backup.tar.gz", "host-watch-20261016-063315-a\r\nBad: header.tar.gz",
                         "host-watch-20261016-063315-../bad.tar.gz", "other.tar.gz"):
            with self.subTest(filename=filename), patch.object(admin, "manager_action", return_value={"name": filename, "content": "Yg=="}):
                status, body = self.request("/api/manage", {"action": "backup-download"})
                self.assertEqual(status, 400)
                self.assertIn("备份文件名无效", json.loads(body)["error"])

    def test_backup_schedule_is_forwarded_without_toggling_monitor(self):
        payload = {"action": "backup-schedule", "enabled": True, "time": "04:25"}
        with patch.object(admin, "manager_action", return_value={"ok": True, "backup_schedule": payload}) as manager:
            status, body = self.request("/api/manage", payload)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        manager.assert_called_once_with(payload)

    def upload_backup(self, content=b"\x1f\x8bfixture", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        self.addCleanup(connection.close)
        connection.request("POST", "/api/manage/backup-import", content,
                           {"Content-Type": "application/gzip", **(headers or {})})
        response = connection.getresponse()
        return response.status, response.read()

    def test_backup_import_forwards_archive_to_manager(self):
        # Archives can exceed the ordinary 64 KiB configuration body limit.
        content = b"\x1f\x8b" + b"fixture" * 12_000
        with patch.object(admin, "manager_action", return_value={"ok": True, "rollback_path": "/var/backups/before.tar.gz"}) as manager:
            code, body = self.upload_backup(content)
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["ok"])
        manager.assert_called_once_with({"action": "backup-import", "content": base64.b64encode(content).decode()})

    def test_backup_import_rejects_empty_invalid_or_oversized_upload(self):
        cases = [(b"", {}, 413), (b"not gzip", {}, 400),
                 (b"", {"Content-Length": str(admin.MAX_BACKUP_UPLOAD + 1)}, 413)]
        with patch.object(admin, "manager_action") as manager:
            for content, headers, expected in cases:
                with self.subTest(headers=headers, content=content):
                    code, _ = self.upload_backup(content, headers)
                    self.assertEqual(code, expected)
        manager.assert_not_called()

    def test_backup_import_requires_authentication_and_same_origin(self):
        with patch.object(admin, "manager_action") as manager:
            with patch.object(admin, "authorized", return_value=False):
                code, _ = self.upload_backup()
                self.assertEqual(code, 401)
            for headers, expected in [({"Origin": "https://evil.example"}, 403),
                                      ({"Sec-Fetch-Site": "cross-site"}, 403),
                                      ({"Content-Type": "application/json"}, 415)]:
                code, _ = self.upload_backup(headers=headers)
                self.assertEqual(code, expected)
        manager.assert_not_called()

    def test_backup_import_refuses_running_tasks(self):
        task = Mock()
        task.snapshot.return_value = {"pending": True}
        with patch.dict(admin.SECKILL_TASKS, {"running": task}, clear=True), patch.object(admin, "manager_action") as manager:
            code, body = self.upload_backup()
        self.assertEqual(code, 400)
        self.assertIn("请先停止", json.loads(body)["error"])
        manager.assert_not_called()

    def test_json_management_cannot_bypass_backup_upload_checks(self):
        with patch.object(admin, "manager_action") as manager:
            code, _ = self.request("/api/manage", {"action": "backup-import", "content": "YQ=="})
        self.assertEqual(code, 400)
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
