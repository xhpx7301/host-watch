"""Merchant browser parameters must reach every caller without leaking to peers."""
import http.client
import json
from contextlib import closing
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import admin
import catalog
import monitor
import requests


DMIT_OPTIONS = {"timeout_seconds": 25, "wait_seconds": 0, "disable_media": True}
VMISS_OPTIONS = {"timeout_seconds": 120, "wait_seconds": 5, "disable_media": False}


def settings():
    return admin.normalize_fetcher({"mode": "flaresolverr", "timeout_seconds": 60,
        "wait_seconds": 2, "disable_media": False,
        "provider_flaresolverr": {"dmit": DMIT_OPTIONS, "vmiss": VMISS_OPTIONS}})


class ProviderFlareSettingsTests(unittest.TestCase):
    def test_legacy_and_partial_overrides_have_compatible_defaults(self):
        self.assertEqual(admin.normalize_fetcher({})["provider_flaresolverr"], {})
        value = admin.normalize_fetcher({"timeout_seconds": 100, "wait_seconds": 3,
            "provider_flaresolverr": {"dmit": {"disable_media": True}}})
        self.assertEqual(value["provider_flaresolverr"]["dmit"], {
            "timeout_seconds": 100, "wait_seconds": 3, "disable_media": True})

    def test_invalid_merchant_parameters_are_rejected(self):
        for value in [[], None, {"unknown": DMIT_OPTIONS}, {"dmit": []},
                {"dmit": {"flaresolverr_url": "http://other/v1"}}]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                admin.normalize_fetcher({"provider_flaresolverr": value})
        for key, invalid_values in {
            "timeout_seconds": [9, 181, True, None, 10.5],
            "wait_seconds": [-1, 31, True, None, "2", 2.5],
            "disable_media": [0, 1, "true", None],
        }.items():
            for invalid in invalid_values:
                with self.subTest(key=key, value=invalid), self.assertRaisesRegex(ValueError, "dmit"):
                    admin.normalize_fetcher({"provider_flaresolverr": {"dmit": {key: invalid}}})

    def test_effective_parameters_inherit_override_and_survive_repeat_resolution(self):
        original = settings()
        resolved = monitor.product_fetcher(original, {"adapter": "dmit"})
        self.assertEqual({key: resolved[key] for key in DMIT_OPTIONS}, DMIT_OPTIONS)
        for provider, expected in [("dmit", DMIT_OPTIONS), ("vmiss", VMISS_OPTIONS),
                ("akile", {"timeout_seconds": 60, "wait_seconds": 2, "disable_media": False})]:
            result = monitor.product_fetcher(resolved, {"adapter": provider})
            self.assertEqual({key: result[key] for key in expected}, expected)
        self.assertEqual(original["timeout_seconds"], 60)
        for mode in ["requests", "cloudscraper"]:
            result = monitor.product_fetcher({**original, "provider_modes": {"dmit": mode}}, {"adapter": "dmit"})
            self.assertEqual(result["mode"], mode)
            self.assertEqual(result["timeout_seconds"], 60)
        inherited = {**original, "mode": "requests", "provider_modes": {"dmit": "flaresolverr"}}
        routed = monitor.product_fetcher(inherited, {"adapter": "dmit"})
        self.assertEqual(monitor.product_fetcher(routed, {"adapter": "akile"})["mode"], "requests")

    def test_real_browser_payload_uses_selected_merchant_parameters(self):
        session = Mock()
        session.post.return_value = Mock(ok=True, status_code=200)
        session.post.return_value.json.return_value = {"status": "ok", "solution": {
            "status": 200, "url": "https://www.dmit.io/pages/pricing", "response": "<h1>Pricing</h1>"}}
        value = monitor.product_fetcher(settings(), {"adapter": "dmit"})
        monitor._fetch_with_flaresolverr(session, "https://www.dmit.io/pages/pricing", value, "www.dmit.io")
        payload = session.post.call_args.kwargs["json"]
        self.assertEqual(payload["maxTimeout"], 25000)
        self.assertNotIn("waitInSeconds", payload)
        self.assertTrue(payload["disableMedia"])
        self.assertEqual(session.post.call_args.kwargs["timeout"], (10, 40))

    def test_login_bootstrap_copies_only_same_site_browser_state(self):
        session = requests.Session()
        response = Mock(ok=True, status_code=200)
        response.json.return_value = {"status": "ok", "solution": {
            "status": 200, "url": "https://app.vmiss.com/login", "response": "<form>",
            "userAgent": "Mozilla/5.0 browser", "cookies": [
                {"name": "cf_clearance", "value": "clear", "domain": ".app.vmiss.com", "path": "/"},
                {"name": "other", "value": "skip", "domain": ".attacker.example", "path": "/"},
            ]}}
        control = Mock(headers={}, cookies=Mock())
        control.post.return_value = response
        fetcher = {"mode": "flaresolverr", "flaresolverr_url": "http://127.0.0.1:8191/v1",
                   "timeout_seconds": 30, "wait_seconds": 0, "disable_media": True,
                   "_copy_browser_state": True}
        monitor._fetch_with_flaresolverr(control, "https://app.vmiss.com/login", fetcher, "app.vmiss.com")
        self.assertEqual(session.cookies.get("cf_clearance"), None)
        # The helper is exercised with a real session so cookie-domain filtering
        # is covered independently of the mocked FlareSolverr control client.
        monitor._copy_browser_state(session, response.json.return_value["solution"], "app.vmiss.com")
        self.assertEqual(session.cookies.get("cf_clearance"), "clear")
        self.assertIsNone(session.cookies.get("other"))
        self.assertEqual(session.headers["User-Agent"], "Mozilla/5.0 browser")

    def test_browser_state_copy_preserves_authenticated_dmit_session(self):
        session = requests.Session()
        session.headers["User-Agent"] = "Mozilla/5.0 authenticated"
        session.cookies.set("WHMCSP8D3Nbb7msEy", "logged-in", domain="www.dmit.io", path="/")
        session.cookies.set("cf_app_sid", "authenticated", domain="www.dmit.io", path="/")
        session._hostwatch_preserve_user_agent = True
        session._hostwatch_preserve_cookie_names = ["WHMCSP8D3Nbb7msEy", "cf_app_sid"]
        monitor._copy_browser_state(session, {
            "userAgent": "Mozilla/5.0 anonymous browser",
            "cookies": [
                {"name": "WHMCSP8D3Nbb7msEy", "value": "anonymous", "domain": "www.dmit.io", "path": "/"},
                {"name": "cf_app_sid", "value": "anonymous", "domain": "www.dmit.io", "path": "/"},
                {"name": "cf_clearance", "value": "fresh", "domain": ".dmit.io", "path": "/"},
            ],
        }, "www.dmit.io")
        self.assertEqual(session.headers["User-Agent"], "Mozilla/5.0 authenticated")
        self.assertEqual(session.cookies.get("WHMCSP8D3Nbb7msEy"), "logged-in")
        self.assertEqual(session.cookies.get("cf_app_sid"), "authenticated")
        self.assertEqual(session.cookies.get("cf_clearance"), "fresh")

    def test_catalog_route_and_worker_use_the_same_effective_parameters(self):
        options = settings()
        route = monitor.fetcher_route(options, {"adapter": "dmit"})
        self.assertEqual({key: route[key] for key in DMIT_OPTIONS}, DMIT_OPTIONS)
        with patch("dmit.directory", return_value=([], "all", [])) as directory:
            data = catalog.load_catalog("dmit", fetcher=options)
        passed = directory.call_args.args[2]
        self.assertEqual({key: passed[key] for key in DMIT_OPTIONS}, DMIT_OPTIONS)
        self.assertEqual(data["route"], route)
        self.assertNotIn("flaresolverr_url", data["route"])

    def test_manual_test_budget_monitor_and_seckill_stock_check_resolve_overrides(self):
        watched = {"id": "tiny", "adapter": "dmit", "pid": "253", "provider": "DMIT", "name": "Tiny"}
        task = admin.StockTestTask("merchant-flare-test-123456", watched, settings())
        self.assertEqual({key: task.fetcher[key] for key in DMIT_OPTIONS}, DMIT_OPTIONS)
        with patch("monitor.browser_request_slot"), patch("monitor.check_browser_memory"), patch.object(task, "run_process", return_value={"ok": True}) as run, patch.object(admin, "send_stock_test_result", return_value={"status": "skipped"}):
            task.run()
        self.assertEqual(run.call_args_list[1].args[0], "--stock-test-worker")
        self.assertEqual(run.call_args_list[1].args[2], 65)
        product = monitor.Product("dmit:253", "Tiny", 0, "https://www.dmit.io/cart.php?a=add&pid=253", vendor="DMIT")
        with patch("dmit.inspect", return_value=product) as inspect:
            found = {}
            monitor.collect_inventory(Mock(), {"fetcher": settings()}, [watched], found, {})
            monitor.inspect_product(Mock(), watched, settings())
            rush_config = {"products": [watched], "fetcher": settings(), "vps_seckill": {
                "items": [{"id": "rush", "product_id": "tiny", "quantity": 1}]}}
            rush = admin.SeckillTask("merchant-flare-rush-123456", rush_config, "manual")
            with patch.object(rush, "_submit") as submit:
                rush.run()
            submit.assert_not_called()
        self.assertEqual(inspect.call_count, 3)
        for call in inspect.call_args_list:
            self.assertEqual({key: call.args[2][key] for key in DMIT_OPTIONS}, DMIT_OPTIONS)


class ProviderFlareAPITests(unittest.TestCase):
    def test_save_reload_preserve_for_old_client_and_explicitly_restore_inheritance(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(admin, "CONFIG_PATH", Path(temp) / "config.json"), patch.object(admin, "authorized", return_value=True), patch.object(admin, "manager_action", return_value={"ok": True}):
            server = admin.ThreadingHTTPServer(("127.0.0.1", 0), admin.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                def request(method, payload=None):
                    with closing(http.client.HTTPConnection("127.0.0.1", server.server_port)) as connection:
                        connection.request(method, "/api/config", json.dumps(payload) if payload is not None else None, {"Content-Type": "application/json"})
                        response = connection.getresponse()
                        return response.status, json.loads(response.read())
                value = admin.default_config()
                value["fetcher"] = settings()
                self.assertEqual(request("POST", value)[0], 200)
                status, saved = request("GET")
                self.assertEqual(status, 200)
                self.assertEqual(saved["fetcher"]["provider_flaresolverr"]["dmit"], DMIT_OPTIONS)
                old_client = {**saved, "fetcher": {key: val for key, val in saved["fetcher"].items() if key != "provider_flaresolverr"}}
                old_client["fetcher"]["timeout_seconds"] = 90
                self.assertEqual(request("POST", old_client)[0], 200)
                self.assertEqual(request("GET")[1]["fetcher"]["provider_flaresolverr"]["dmit"], DMIT_OPTIONS)
                old_client["fetcher"]["provider_flaresolverr"] = {}
                self.assertEqual(request("POST", old_client)[0], 200)
                self.assertEqual(request("GET")[1]["fetcher"]["provider_flaresolverr"], {})
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
