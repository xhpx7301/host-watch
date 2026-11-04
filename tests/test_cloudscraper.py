"""Exercise cloudscraper routing, isolation and failures without live challenges."""
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, Mock, patch

import cloudscraper
from cloudscraper.exceptions import CloudflareChallengeError, CloudflareCode1020, CloudflareLoopProtection
import requests

import admin
import catalog
import monitor
from affiliates import PROVIDERS
import test_admin
from test_akile import catalog as akile_fixture
from test_isvoro import directory as isvoro_fixture
from test_monitor import html_product


def response(body='<p class="stock">3</p>', status=200, url="https://shop.example.com/item", headers=None):
    value = requests.Response()
    value.status_code = status
    value.url = url
    value.headers.update(headers or {})
    value._content = body.encode("utf-8")
    value.encoding = "utf-8"
    return value


class CloudscraperTransportTests(unittest.TestCase):
    def setUp(self):
        self.scraper = MagicMock()
        self.scraper.__enter__.return_value = self.scraper
        self.scraper.get.return_value = response()
        patcher = patch("cloudscraper.create_scraper", return_value=self.scraper)
        self.create = patcher.start()
        self.addCleanup(patcher.stop)
        self.session = requests.Session()
        self.addCleanup(self.session.close)
        self.session.headers["Authorization"] = "secret-auth"
        self.session.cookies.set("shared", "secret-cookie")

    def test_dispatch_isolated_session_uses_bounded_native_solver_and_closes(self):
        with patch("monitor.fetch_with_requests") as plain, patch("monitor.fetch_with_flaresolverr") as browser:
            product = monitor.fetch_html_stock(self.session, html_product(), {"mode": "cloudscraper", "wait_seconds": 30})
        self.assertEqual(product.available, 3)
        self.create.assert_called_once_with(interpreter="native", delay=5, solveDepth=1, doubleDown=False, debug=False)
        self.scraper.get.assert_called_once_with("https://shop.example.com/item", timeout=(10, 25), allow_redirects=True)
        self.scraper.__exit__.assert_called_once()
        self.assertEqual(self.session.headers["Authorization"], "secret-auth")
        self.assertEqual(dict(self.session.cookies), {"shared": "secret-cookie"})
        self.scraper.headers.update.assert_not_called()
        self.scraper.cookies.update.assert_not_called()
        plain.assert_not_called()
        browser.assert_not_called()

    def test_returned_http_challenge_rate_limit_and_redirect_remain_errors(self):
        for result, kind, label in [
            (response("Enable JavaScript and cookies to continue", 200), monitor.SiteBlocked, "安全验证"),
            (response(status=403), monitor.SiteBlocked, "403"),
            (response(status=429), monitor.SiteBlocked, "429"),
            (response(headers={"CF-Mitigated": "challenge"}), monitor.SiteBlocked, "安全验证"),
            (response("<html>Performing security verification</html>", 403), monitor.SiteBlocked, "安全验证"),
            (response(status=500), monitor.MonitorError, "500"),
            (response(url="https://elsewhere.example/secret"), monitor.MonitorError, "重定向离开"),
        ]:
            with self.subTest(label=label):
                self.scraper.get.return_value = result
                with self.assertRaisesRegex(kind, label):
                    monitor.fetch_with_cloudscraper("https://shop.example.com/item", "shop.example.com")
        self.assertEqual(self.scraper.__exit__.call_count, 7)

    def test_solver_exceptions_stop_without_exposing_urls_or_cookies(self):
        for error in [CloudflareChallengeError, CloudflareCode1020, CloudflareLoopProtection]:
            with self.subTest(error=error):
                self.scraper.get.side_effect = error("secret-cookie https://target.example/?token=secret-auth")
                with self.assertRaises(monitor.SiteBlocked) as caught:
                    monitor.fetch_with_cloudscraper("https://shop.example.com/item", "shop.example.com")
                self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(self.scraper.get.call_count, 3)

    def test_timeout_and_network_errors_are_sanitized(self):
        for error, label in [(requests.Timeout, "超时"), (requests.ConnectionError, "失败")]:
            with self.subTest(error=error):
                self.scraper.get.side_effect = error("https://target.example/?token=secret")
                with self.assertRaisesRegex(monitor.MonitorError, label) as caught:
                    monitor.fetch_with_cloudscraper("https://shop.example.com/item", "shop.example.com")
                self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(self.scraper.__exit__.call_count, 2)

    def test_missing_dependency_and_interpreter_failure_have_actionable_errors(self):
        with patch.dict(sys.modules, {"cloudscraper": None}), self.assertRaisesRegex(monitor.RequestServiceError, "更新 HostWatch"):
            monitor.fetch_with_cloudscraper("https://shop.example.com/item", "shop.example.com")
        self.create.side_effect = RuntimeError("secret environment detail")
        with self.assertRaisesRegex(monitor.RequestServiceError, "解释器") as caught:
            monitor.fetch_with_cloudscraper("https://shop.example.com/item", "shop.example.com")
        self.assertNotIn("secret", str(caught.exception))

    def test_real_published_scraper_parses_stock_and_uses_generated_headers(self):
        # Leave the actual CloudScraper session, native setup and response
        # processing intact; replace only network I/O.
        with patch("cloudscraper.create_scraper", wraps=cloudscraper.CloudScraper.create_scraper), patch.object(cloudscraper.CloudScraper, "perform_request", return_value=response()) as network:
            product = monitor.fetch_html_stock(self.session, html_product(), {"mode": "cloudscraper"})
        self.assertEqual(product.available, 3)
        network.assert_called_once()

    def test_custom_timeouts_and_delay_reach_library_without_changing_browser_options(self):
        fetcher = admin.normalize_fetcher({"mode": "cloudscraper", "timeout_seconds": 180, "wait_seconds": 30,
            "cloudscraper": {"connect_timeout_seconds": 4, "read_timeout_seconds": 42, "challenge_delay_seconds": 0}})
        monitor.fetch_page(self.session, "https://shop.example.com/item", fetcher, "shop.example.com")
        self.create.assert_called_once_with(interpreter="native", delay=0, solveDepth=1, doubleDown=False, debug=False)
        self.scraper.get.assert_called_once_with("https://shop.example.com/item", timeout=(4, 42), allow_redirects=True)

    def test_catalog_session_reuses_clearance_and_closes_after_scope(self):
        fetcher = admin.normalize_fetcher({"mode": "cloudscraper"})
        with monitor.cloudscraper_request_session(fetcher) as shared:
            self.assertIs(shared["_cloudscraper_session"], self.scraper)
            monitor.fetch_with_cloudscraper("https://shop.example.com/store", "shop.example.com", shared)
            monitor.fetch_with_cloudscraper("https://shop.example.com/store/mini", "shop.example.com", shared)
        self.create.assert_called_once_with(interpreter="native", delay=5, solveDepth=1, doubleDown=False, debug=False)
        self.assertEqual(self.scraper.get.call_count, 2)
        self.scraper.close.assert_called_once_with()

    def test_probe_reports_real_status_and_byte_size_after_validation(self):
        self.scraper.get.return_value = response("<title>Example</title>", status=201, url="https://example.com/")
        result = admin.cloudscraper_probe("baseline", {"mode": "cloudscraper"})
        self.assertEqual(result["status_code"], 201)
        self.assertEqual(result["response_bytes"], len(b"<title>Example</title>"))
        self.assertEqual(result["title"], "Example")
        self.assertEqual(result["library_version"], "1.2.71")
        self.assertEqual(result["url"], "https://example.com/")


class CloudscraperConfigurationTests(unittest.TestCase):
    def test_options_have_compatible_defaults_and_reject_invalid_values(self):
        self.assertEqual(admin.normalize_fetcher({})["cloudscraper"], {"connect_timeout_seconds": 10, "read_timeout_seconds": 25, "challenge_delay_seconds": 5})
        for key, low, high in (("connect_timeout_seconds", 1, 30), ("read_timeout_seconds", 1, 120), ("challenge_delay_seconds", 0, 15)):
            for number in (low, high):
                value = {"cloudscraper": {key: number}}
                self.assertEqual(admin.normalize_fetcher(value)["cloudscraper"][key], number)
            for invalid in (low - 1, high + 1, True, "5", 1.5, None):
                with self.subTest(key=key, invalid=invalid), self.assertRaises(ValueError):
                    admin.normalize_fetcher({"cloudscraper": {key: invalid}})
        for invalid in ([], "native", 5):
            with self.assertRaises(ValueError):
                admin.normalize_fetcher({"cloudscraper": invalid})

    def test_probe_task_skips_browser_telegram_and_uses_cloudscraper_timeout_budget(self):
        product = {"id": admin.CLOUDSCRAPER_TEST_ID, "adapter": "html", "_request_probe": True, "target": "dmit"}
        fetcher = admin.normalize_fetcher({"mode": "cloudscraper", "timeout_seconds": 10,
            "cloudscraper": {"connect_timeout_seconds": 30, "read_timeout_seconds": 120, "challenge_delay_seconds": 15}})
        task = admin.StockTestTask("probe-task-1234567890123456", product, fetcher)
        with patch.object(task, "run_process", return_value={"ok": True, "status_code": 200}) as run, patch.object(admin, "send_stock_test_result") as notify, patch("monitor.browser_request_slot") as slot, patch("monitor.write_state") as history, patch.object(admin, "save_config") as save:
            task.run()
        self.assertEqual(run.call_args.args[2], 335)
        self.assertFalse(task.snapshot()["notifying"])
        self.assertEqual(task.snapshot()["notification"]["status"], "skipped")
        self.assertTrue(task.done.is_set())
        for operation in (notify, slot, history, save):
            operation.assert_not_called()

    def test_probe_worker_uses_real_transport_and_does_not_parse_inventory(self):
        value = {"product": {"_request_probe": True, "target": "baseline"}, "fetcher": admin.normalize_fetcher({"mode": "cloudscraper"})}
        output = io.StringIO()
        with patch.object(admin.sys, "stdin", io.StringIO(json.dumps(value))), patch.object(admin.sys, "stdout", output), patch("cloudscraper.CloudScraper.perform_request", return_value=response("<title>Read only</title>", url="https://example.com/")), patch.object(monitor, "inspect_product") as inspect:
            admin.stock_test_worker()
        result = json.loads(output.getvalue())
        self.assertTrue(result["ok"])
        self.assertEqual(result["title"], "Read only")
        self.assertNotIn("available", result)
        inspect.assert_not_called()

    def test_probe_cancels_real_worker_without_waiting_for_read_timeout(self):
        task = admin.StockTestTask("probe-stop-1234567890123456", {"id": admin.CLOUDSCRAPER_TEST_ID, "adapter": "html", "_request_probe": True}, admin.normalize_fetcher({"mode": "cloudscraper"}))
        real_popen = subprocess.Popen
        started = threading.Event()
        children = []
        def spawn(*args, **kwargs):
            child = real_popen([sys.executable, "-c", "import sys,time;sys.stdin.read();time.sleep(30)"], **kwargs)
            children.append(child)
            started.set()
            return child
        with patch.object(admin.subprocess, "Popen", side_effect=spawn), patch.object(admin, "send_stock_test_result") as notify:
            thread = threading.Thread(target=task.run)
            thread.start()
            try:
                self.assertTrue(started.wait(3))
                task.cancel()
                thread.join(3)
                self.assertFalse(thread.is_alive())
                self.assertIsNotNone(children[0].poll())
                self.assertTrue(task.snapshot()["cancelled"])
            finally:
                for child in children:
                    admin.kill_test_process(child)
                thread.join(3)
        notify.assert_not_called()
    def test_global_provider_and_legacy_product_modes_survive_roundtrip(self):
        modes = {p: "cloudscraper" for p in (*PROVIDERS, "html")}
        value = {"fetcher": {"mode": "cloudscraper", "provider_modes": modes}, "products": [html_product(request_mode="cloudscraper")]}
        config = admin.validate_config(value)
        self.assertEqual(admin.validate_config(config), config)
        self.assertEqual(config["fetcher"]["provider_modes"], modes)
        self.assertEqual(config["products"][0]["request_mode"], "cloudscraper")
        self.assertEqual(admin.default_config()["fetcher"]["mode"], "requests")
        for provider in (*PROVIDERS, "html"):
            policy = {"mode": "flaresolverr", "provider_modes": {provider: "cloudscraper"}}
            self.assertEqual(monitor.product_fetcher(policy, {"adapter": provider, "request_mode": "requests"})["mode"], "cloudscraper")
        self.assertEqual(monitor.product_fetcher({"mode": "cloudscraper"}, {"adapter": "html", "request_mode": "inherit"})["mode"], "cloudscraper")

    def test_test_task_does_not_start_browser_or_cleanup_session(self):
        task = admin.StockTestTask("cloud-test-1234567890123456", html_product(), admin.normalize_fetcher({"mode": "cloudscraper"}))
        with patch.object(task, "run_process", return_value={"ok": True, "available": 3}) as run, patch.object(admin, "send_stock_test_result", return_value={"status": "unconfigured"}), patch("monitor.browser_request_slot") as slot, patch("monitor.check_browser_memory") as memory:
            task.run()
        self.assertFalse(task.browser)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0], "--stock-test-worker")
        self.assertEqual(run.call_args.args[1]["fetcher"]["mode"], "cloudscraper")
        slot.assert_not_called()
        memory.assert_not_called()
        self.assertTrue(task.done.is_set())

    def test_all_catalogue_workers_receive_saved_policy_without_browser_session(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as temp:
                policy = admin.normalize_fetcher({"provider_modes": {provider: "cloudscraper"}})
                service = catalog.CatalogService(lambda: Path(temp) / "catalog.json", lambda: policy)
                task = {"provider": provider, "scope": "", "cancelled": False}
                with patch.object(service, "worker", return_value='{"ok":true}') as worker, patch("monitor.browser_request_slot") as slot:
                    service.fetch_result(task)
                self.assertEqual(worker.call_count, 1)
                self.assertEqual(worker.call_args.args[1], "--worker")
                self.assertEqual(worker.call_args.args[2]["fetcher"]["mode"], "cloudscraper")
                slot.assert_not_called()

    def test_discovery_adapters_use_selected_transport_including_isvoro_cache(self):
        with patch("monitor.fetch_akile_catalog", return_value=akile_fixture()) as fetch:
            catalog.load_catalog("akile", fetcher={"mode": "cloudscraper"})
        self.assertEqual(fetch.call_args.args[1]["mode"], "cloudscraper")
        fixtures = isvoro_fixture()
        fixtures["categories/HK-prod"]["products"] = fixtures["categories/HK-prod"]["products"][:1]
        with patch("monitor.fetch_isvoro_json", side_effect=lambda session, path, fetcher: fixtures[path]) as fetch:
            catalog.load_catalog("isvoro", "24:27", {"mode": "cloudscraper"})
        self.assertTrue(all(call.args[2]["mode"] == "cloudscraper" for call in fetch.call_args_list))
        paths = [call.args[1] for call in fetch.call_args_list]
        self.assertEqual(len(paths), len(set(paths)))

    def test_auto_monitor_preserves_history_after_verification_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            state, status = Path(temp) / "state.json", Path(temp) / "status.json"
            baseline = '{"products":{"vps-2g":{"available":0}}}'
            state.write_text(baseline, encoding="utf-8")
            config = admin.validate_config({"enabled": True, "products": [html_product()], "fetcher": {"mode": "cloudscraper"}})
            with patch("monitor.fetch_with_cloudscraper", side_effect=monitor.SiteBlocked("verification failed")), patch("monitor.telegram_request") as send, self.assertRaises(monitor.SiteBlocked):
                monitor.check_once(Mock(), config, state, status, "token", "123")
            send.assert_not_called()
            self.assertEqual(state.read_text(encoding="utf-8"), baseline)


class CloudscraperAPITests(unittest.TestCase):
    setUp = test_admin.AdminAPITests.setUp
    request = test_admin.AdminAPITests.request

    def test_cloudscraper_options_roundtrip_and_old_clients_preserve_them(self):
        config = admin.default_config()
        config["fetcher"]["cloudscraper"] = {"connect_timeout_seconds": 4, "read_timeout_seconds": 42, "challenge_delay_seconds": 0}
        with patch.object(admin, "manager_action", return_value={"ok": True}):
            code, body = self.request("/api/config", config)
            self.assertEqual(code, 200)
            submitted = json.loads(body)["config"]
            expected = submitted["fetcher"].pop("cloudscraper")
            code, body = self.request("/api/config", submitted)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["config"]["fetcher"]["cloudscraper"], expected)

    def test_probe_uses_selected_target_current_options_and_forces_cloudscraper(self):
        task_id = "probe-api-1234567890123456"
        with patch.dict(admin.TEST_TASKS, {}, clear=True), patch.object(admin.StockTestTask, "run"), patch.object(admin, "manager_action") as manage:
            code, body = self.request("/api/products/test", {"task_id": task_id, "kind": "cloudscraper_probe", "target": "narwhal", "fetcher": {"mode": "flaresolverr", "provider_modes": {"html": "requests"}, "cloudscraper": {"read_timeout_seconds": 44}}})
            self.assertEqual(code, 200)
            task = admin.TEST_TASKS[task_id]
            self.assertTrue(task.product["_request_probe"])
            self.assertEqual(task.product["target"], "narwhal")
            self.assertEqual(task.fetcher["mode"], "cloudscraper")
            self.assertEqual(task.fetcher["cloudscraper"]["read_timeout_seconds"], 44)
            self.assertFalse(task.browser)
            code, _ = self.request("/api/products/test/stop", {"task_id": task_id})
            self.assertEqual(code, 200)
            self.assertTrue(task.cancelled.is_set())
        manage.assert_not_called()

    def test_probe_rejects_arbitrary_targets_and_busy_test_reuses_existing_limits(self):
        with patch.object(admin.StockTestTask, "run"):
            code, _ = self.request("/api/products/test", {"task_id": "invalid-target-1234567890123456", "kind": "cloudscraper_probe", "target": "http://127.0.0.1"})
            self.assertEqual(code, 400)
            with patch.dict(admin.TEST_TASKS, {}, clear=True):
                code, _ = self.request("/api/products/test", {"task_id": "first-probe-1234567890123456", "kind": "cloudscraper_probe", "target": "dmit"})
                self.assertEqual(code, 200)
                code, _ = self.request("/api/products/test", {"task_id": "second-probe-1234567890123456", "kind": "cloudscraper_probe", "target": "baseline"})
                self.assertEqual(code, 400)
                admin.TEST_TASKS["first-probe-1234567890123456"].cancel()

    def test_save_and_single_test_accept_cloudscraper_and_stop_is_available(self):
        config = admin.default_config()
        config["fetcher"]["mode"] = "cloudscraper"
        config["fetcher"]["provider_modes"] = {"dmit": "cloudscraper"}
        with patch.object(admin, "manager_action", return_value={"ok": True}):
            code, body = self.request("/api/config", config)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["config"]["fetcher"]["mode"], "cloudscraper")
        task_id = "cloud-api-1234567890123456"
        with patch.dict(admin.TEST_TASKS, {}, clear=True), patch.object(admin.StockTestTask, "run"):
            code, _ = self.request("/api/products/test", {"task_id": task_id, "product": html_product(), "fetcher": {"mode": "cloudscraper"}})
            self.assertEqual(code, 200)
            self.assertFalse(admin.TEST_TASKS[task_id].browser)
            code, _ = self.request("/api/products/test/stop", {"task_id": task_id})
            self.assertEqual(code, 200)
            self.assertTrue(admin.TEST_TASKS[task_id].cancelled.is_set())

    def test_cloudscraper_blocked_worker_returns_structured_error(self):
        # The real standalone worker must handle library errors instead of
        # printing a traceback or leaving the UI waiting for completion.
        script = '''import json,sys
from io import StringIO
from unittest.mock import patch
import admin
from cloudscraper.exceptions import CloudflareChallengeError
value=json.loads(sys.stdin.read())
with patch('sys.stdin',StringIO(json.dumps(value))), patch('cloudscraper.create_scraper',side_effect=CloudflareChallengeError('secret')):
    admin.stock_test_worker()
'''
        result = subprocess.run([sys.executable, "-c", script], input=json.dumps({"product": html_product(), "fetcher": {"mode": "cloudscraper"}}), text=True, encoding="utf-8", capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertFalse(output["ok"])
        self.assertNotIn("secret", output["error"])


if __name__ == "__main__":
    unittest.main()
