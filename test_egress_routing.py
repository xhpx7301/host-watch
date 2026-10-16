"""Merchant routing must survive every read path and report blocked routes."""
import io
import json
from contextlib import nullcontext
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import requests
import admin
import catalog
import egress
import monitor
import test_admin


def settings(enabled=True, pending=False):
    node = {"id": "node-a", "name": "HY-248", "enabled": enabled,
            "proxy_url": "" if pending else "http://203.0.113.20:8787",
            "health_url": "" if pending else "http://203.0.113.20:8787/health",
            "public_ip": "203.0.113.20", "token": "private-token:/@", "pending_registration": pending}
    return {"mode": "requests", "egress": {"nodes": [node], "routes": {"akile": "node-a", "isvoro": "node-a", "vmiss": "node-a"}}}


class RoutingTests(unittest.TestCase):
    def test_routes_are_per_merchant_and_never_reuse_previous_proxy(self):
        routed = monitor.product_fetcher(settings(), {"adapter": "akile"})
        self.assertIn("203.0.113.20", routed["egress_proxy_url"])
        direct = monitor.product_fetcher(routed, {"adapter": "dmit"})
        self.assertNotIn("egress_proxy_url", direct)
        session = requests.Session()
        egress.apply_session(session, routed)
        egress.apply_session(session, direct)
        self.assertEqual(session.proxies, {})

    def test_disabled_pending_and_missing_token_are_controlled_errors(self):
        missing_token = settings()
        missing_token["egress"]["nodes"][0]["token"] = ""
        for value, error in [(settings(False), "已停用"), (settings(pending=True), "没有代理地址"), (missing_token, "未配置代理令牌")]:
            with self.subTest(error=error), self.assertRaisesRegex(monitor.MonitorError, error):
                monitor.product_fetcher(value, {"adapter": "akile"})

    def test_public_route_and_snapshot_never_include_proxy_credentials(self):
        value = settings()
        value["provider_modes"] = {"akile": "cloudscraper"}
        with patch("catalog.akile_catalog", return_value=([], "all", [])), patch("monitor.cloudscraper_request_session", side_effect=nullcontext):
            data = catalog.load_catalog("akile", fetcher=value)
        self.assertEqual(data["route"]["mode"], "cloudscraper")
        self.assertEqual(data["route"]["node_name"], "HY-248")
        for secret in ["private-token", "proxy_url", "health_url"]:
            self.assertNotIn(secret, json.dumps(data))

    def test_vmiss_batch_monitoring_uses_merchant_proxy(self):
        product = monitor.Product("mini", "Mini", 1, "https://app.vmiss.com/store/hk/mini")
        item = {"id": "mini", "adapter": "vmiss", "provider": "VMISS", "category_url": "https://app.vmiss.com/store/hk", "selector": "mini"}
        found = {}
        with patch("monitor.fetch_products", return_value=[product]) as fetch:
            monitor.collect_inventory(Mock(), {"fetcher": settings()}, [item], found, {})
        self.assertIn("203.0.113.20", fetch.call_args.args[3]["egress_proxy_url"])
        self.assertEqual(found["mini"].available, 1)

    def test_flaresolverr_proxy_object_auth_and_direct_control_connection(self):
        value = monitor.product_fetcher(settings(), {"adapter": "akile"})
        proxy = egress.browser_proxy(value)
        self.assertEqual(proxy, {"url": "http://203.0.113.20:8787", "username": "private-token:/@", "password": ""})
        session = requests.Session()
        session.proxies = {"http": value["egress_proxy_url"], "https": value["egress_proxy_url"]}
        response = Mock(ok=True, status_code=200)
        response.json.return_value = {"status": "ok", "solution": {"status": 200, "url": "https://akile.ai/", "response": "ok"}}
        value.update(flaresolverr_url="http://127.0.0.1:8191/v1", timeout_seconds=60)
        with patch.object(session, "post", return_value=response) as post:
            monitor._fetch_with_flaresolverr(session, "https://akile.ai/", value, "akile.ai")
        self.assertEqual(post.call_args.kwargs["json"]["proxy"], proxy)
        merged = session.merge_environment_settings(value["flaresolverr_url"], post.call_args.kwargs["proxies"], False, True, None)
        self.assertEqual(merged["proxies"]["http"], "")
        self.assertEqual(merged["proxies"]["https"], "")

    def test_browser_session_creation_receives_proxy_auth(self):
        proxy = egress.browser_proxy(monitor.product_fetcher(settings(), {"adapter": "isvoro"}))
        command = {"command": "create", "endpoint": "http://localhost:8191/v1", "session": "test", "proxy": proxy}
        response = Mock(ok=True)
        response.json.return_value = {"status": "ok"}
        with patch("sys.stdin", io.StringIO(json.dumps(command))), patch("sys.stdout", io.StringIO()), patch("admin.requests.post", return_value=response) as post:
            admin.browser_session_worker()
        self.assertEqual(post.call_args.kwargs["json"]["proxy"], proxy)

    def test_notification_does_not_inherit_last_merchant_route(self):
        session = Mock(proxies={"https": "http://private-token@node:8787"})
        session.post.return_value.json.return_value = {"ok": True}
        monitor.telegram_request(session, "bot-token", "sendMessage", chat_id="123", text="test")
        self.assertEqual(session.proxies, {})


class CatalogRoutingTests(unittest.TestCase):
    def wait(self, service):
        deadline = time.monotonic() + 3
        while service.task["pending"] and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(service.task["pending"])

    def test_blocked_routes_return_visible_error_without_network_or_cache_loss(self):
        for value in [settings(False), settings(pending=True)]:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "catalog.json"
                old = {"provider": "akile", "scope": "all", "fetched_at": "2026-10-16", "rows": [], "scopes": []}
                path.write_text(json.dumps({"akile:all": old}), encoding="utf-8")
                service = catalog.CatalogService(lambda: path, lambda: value)
                with patch.object(service, "worker") as worker:
                    service.refresh("akile")
                    self.wait(service)
                worker.assert_not_called()
                self.assertTrue(service.task["error"])
                self.assertEqual(service.snapshot("akile")["data"], old)
                self.assertTrue(service.snapshot("akile")["route"]["error"])
                self.assertNotIn("private-token", json.dumps(service.snapshot("akile")))

    def test_refresh_pins_saved_route_even_if_settings_change_before_worker_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            value = settings()
            service = catalog.CatalogService(lambda: Path(directory) / "catalog.json", lambda: value)
            with patch("catalog.threading.Thread") as thread:
                task = service.refresh("akile")
                value["egress"]["routes"].clear()
                args = thread.call_args.kwargs["args"]
            with patch.object(service, "worker", return_value=json.dumps({"ok": False, "error": "fixture failure"})) as worker:
                service.run(*args)
            self.assertEqual(task["route"]["node_id"], "node-a")
            self.assertIn("egress_proxy_url", worker.call_args.args[2]["fetcher"])
            self.assertEqual(service.snapshot("akile")["route"]["node_id"], "")

    def test_catalog_browser_creates_session_with_same_proxy_as_fetch_worker(self):
        value = settings()
        value.update(mode="flaresolverr", flaresolverr_url="http://localhost:8191/v1", timeout_seconds=60)
        service = catalog.CatalogService(lambda: Path("unused"), lambda: value)
        task = {"provider": "akile", "scope": "", "cancelled": False}
        with patch.object(service, "worker", return_value='{"ok":true}') as worker, patch("monitor.check_browser_memory"):
            service.fetch_result(task)
        commands = [call.args[2] for call in worker.call_args_list]
        self.assertIn("proxy", commands[0])
        self.assertEqual(commands[0]["proxy"], egress.browser_proxy(commands[1]["fetcher"]))


class RoutingAPITests(unittest.TestCase):
    setUp = test_admin.AdminAPITests.setUp
    request = test_admin.AdminAPITests.request
    get_request = test_admin.AdminAPITests.get_request

    def save_routing(self, enabled=True):
        value = admin.default_config()
        value["egress"] = settings(enabled)["egress"]
        admin.save_config(value)

    def test_isvoro_category_reads_follow_saved_route(self):
        self.save_routing()
        with patch("monitor.fetch_isvoro_json", return_value={"data": []}) as fetch, patch("monitor.isvoro_categories", return_value=[]):
            code, _ = self.get_request("/api/isvoro/categories")
        self.assertEqual(code, 200)
        self.assertIn("203.0.113.20", fetch.call_args.args[2]["egress_proxy_url"])

    def test_tests_use_saved_secrets_and_target_merchant_route(self):
        self.save_routing()
        process = Mock()
        process.communicate.return_value = ('{"ok":true}', None)
        process.poll.return_value = 0
        with patch.dict(admin.TEST_TASKS, {}, clear=True), patch("admin.subprocess.Popen", return_value=process):
            code, body = self.request("/api/products/test", {"task_id": "routed-probe-1234567890", "kind": "cloudscraper_probe", "target": "isvoro", "fetcher": {"egress": {"nodes": [], "routes": {}}}})
            task = admin.TEST_TASKS["routed-probe-1234567890"]
            self.assertTrue(task.done.wait(3))
        self.assertEqual(code, 200, body)
        self.assertEqual(task.fetcher["mode"], "cloudscraper")
        self.assertIn("private-token", task.fetcher["egress_proxy_url"])
        self.assertEqual(task.snapshot()["route"]["node_id"], "node-a")
        self.assertNotIn("private-token", json.dumps(task.snapshot()))

    def test_disabled_routes_fail_tests_and_categories_with_actionable_error(self):
        self.save_routing(False)
        with patch("monitor.fetch_isvoro_json") as fetch, patch("admin.subprocess.Popen") as process:
            code, body = self.get_request("/api/isvoro/categories")
            self.assertEqual(code, 502)
            self.assertIn("已停用", json.loads(body)["error"])
            code, body = self.request("/api/products/test", {"task_id": "blocked-probe-1234567890", "kind": "cloudscraper_probe", "target": "isvoro"})
            self.assertEqual(code, 400)
            self.assertIn("已停用", json.loads(body)["error"])
        fetch.assert_not_called()
        process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
