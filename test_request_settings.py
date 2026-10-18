import json
import unittest
from unittest.mock import Mock, patch

import admin
import catalog
import monitor
import test_admin


class MerchantRequestTests(unittest.TestCase):
    def test_merchant_choice_overrides_legacy_product_and_global(self):
        for provider in ("vmiss", "vps_soy", "akile", "isvoro", "dmit", "html"):
            item = {"adapter": provider, "request_mode": "flaresolverr"}
            fetcher = admin.normalize_fetcher({"mode": "flaresolverr", "provider_modes": {provider: "requests"}})
            self.assertEqual(monitor.product_fetcher(fetcher, item)["mode"], "requests")
            fetcher["mode"] = "requests"
            fetcher["provider_modes"][provider] = "flaresolverr"
            self.assertEqual(monitor.product_fetcher(fetcher, item)["mode"], "flaresolverr")

    def test_explicit_inherit_uses_global_and_missing_policy_preserves_legacy(self):
        item = {"adapter": "akile", "request_mode": "requests"}
        fetcher = admin.normalize_fetcher({"mode": "flaresolverr"})
        self.assertEqual(monitor.product_fetcher(fetcher, item)["mode"], "requests")
        fetcher["provider_modes"]["akile"] = "inherit"
        self.assertEqual(monitor.product_fetcher(fetcher, item)["mode"], "flaresolverr")
        self.assertEqual(monitor.product_fetcher(fetcher, {"adapter": "vmiss"})["mode"], "flaresolverr")

    def test_login_policy_is_independent_from_product_policy(self):
        fetcher = admin.normalize_fetcher({
            "mode": "requests",
            "provider_modes": {"dmit": "flaresolverr"},
            "provider_login_modes": {"dmit": "cloudscraper"},
        })
        self.assertEqual(monitor.product_fetcher(fetcher, {"adapter": "dmit"})["mode"], "flaresolverr")
        self.assertEqual(monitor.login_fetcher(fetcher, "dmit")["mode"], "cloudscraper")
        fetcher["provider_login_modes"]["dmit"] = "inherit"
        self.assertEqual(monitor.login_fetcher(fetcher, "dmit")["mode"], "flaresolverr")

    def test_rejects_invalid_policy(self):
        for value in [None, [], "requests", {"unknown": "requests"}, {"vmiss": "automatic"}, {"akile": []}, {"html": True}]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                admin.normalize_fetcher({"provider_modes": value})
        for value in [None, [], {"unknown": "requests"}, {"dmit": "automatic"}, {"dmit": []}]:
            with self.subTest(login_value=value), self.assertRaises(ValueError):
                admin.normalize_fetcher({"provider_login_modes": value})

    def test_browser_light_loading_defaults_to_light_and_requires_a_boolean(self):
        self.assertTrue(admin.normalize_fetcher({})["disable_media"])
        for enabled in (False, True):
            fetcher = admin.normalize_fetcher({"disable_media": enabled, "provider_modes": {"vmiss": "flaresolverr"}})
            self.assertIs(monitor.product_fetcher(fetcher, {"adapter": "vmiss"})["disable_media"], enabled)
        for invalid in (1, 0, "true", "false", None, []):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                admin.normalize_fetcher({"disable_media": invalid})

    def test_browser_task_uses_merchant_policy_for_session_lifecycle(self):
        fetcher = admin.normalize_fetcher({"mode": "flaresolverr", "provider_modes": {"akile": "requests"}})
        item = {"adapter": "akile", "id": "one", "request_mode": "flaresolverr"}
        task = admin.StockTestTask("task-1234567890123456", item, fetcher)
        self.assertFalse(task.browser)
        self.assertEqual(task.fetcher["mode"], "requests")
        fetcher["provider_modes"]["akile"] = "flaresolverr"
        item["request_mode"] = "requests"
        self.assertTrue(admin.StockTestTask("task-2234567890123456", item, fetcher).browser)

    def test_vmiss_unified_policy_batches_old_mixed_products(self):
        items = [{"id": str(i), "adapter": "vmiss", "category_url": "https://app.vmiss.com/store/test", "name": "Mini", "provider": "VMISS", "selector": "p", "gid": "1", "enabled": True, "request_mode": mode} for i, mode in enumerate(("requests", "flaresolverr"))]
        product = monitor.Product("p", "Mini", 0, "https://app.vmiss.com/store/test/p")
        with patch.object(monitor, "fetch_products", return_value=[product]) as fetch, patch.object(monitor, "read_state", return_value={}), patch.object(monitor, "write_state"), patch.object(monitor, "write_status"):
            monitor.check_once(Mock(), {"products": items, "fetcher": {"mode": "requests", "provider_modes": {"vmiss": "flaresolverr"}}}, None, None, "", "")
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.args[3]["mode"], "flaresolverr")

    def test_catalog_selection_inherits_merchant_policy(self):
        row = catalog.make_row("test", {"adapter": "akile", "name": "Mini", "provider": "Akile"}, "HK", "Mini", "", "")
        self.assertEqual(row["watch"]["request_mode"], "inherit")


class MerchantRequestAPITests(unittest.TestCase):
    setUp = test_admin.AdminAPITests.setUp
    request = test_admin.AdminAPITests.request

    def test_save_reload_and_old_client_preserve_merchant_policy(self):
        value = admin.default_config()
        value["fetcher"]["provider_modes"] = {"vmiss": "flaresolverr", "akile": "requests"}
        with patch.object(admin, "manager_action", return_value={"ok": True}):
            code, body = self.request("/api/config", value)
            self.assertEqual(code, 200)
            stored = json.loads(body)["config"]
            self.assertEqual(stored["fetcher"]["provider_modes"], value["fetcher"]["provider_modes"])
            del stored["fetcher"]["provider_modes"]
            stored["fetcher"]["wait_seconds"] = 2
            code, body = self.request("/api/config", stored)
        self.assertEqual(code, 200)
        self.assertEqual(admin.current_config()["fetcher"]["provider_modes"], value["fetcher"]["provider_modes"])

    def test_light_loading_persists_across_old_clients_and_can_be_disabled(self):
        value = admin.default_config()
        value["fetcher"]["disable_media"] = True
        with patch.object(admin, "manager_action", return_value={"ok": True}):
            code, body = self.request("/api/config", value)
            self.assertEqual(code, 200)
            old_client = json.loads(body)["config"]
            del old_client["fetcher"]["disable_media"]
            code, _ = self.request("/api/config", old_client)
            self.assertEqual(code, 200)
            self.assertTrue(admin.current_config()["fetcher"]["disable_media"])
            old_client["fetcher"]["disable_media"] = False
            code, _ = self.request("/api/config", old_client)
            self.assertEqual(code, 200)
            self.assertFalse(admin.current_config()["fetcher"]["disable_media"])

    def test_unsaved_single_test_receives_current_merchant_policy(self):
        item = {"id": "one", "adapter": "html", "name": "Test", "url": "https://example.com/item", "stock_selector": ".stock", "request_mode": "flaresolverr", "enabled": True}
        task_id = "policy-1234567890123456"
        with patch.dict(admin.TEST_TASKS, {}, clear=True), patch.object(admin.StockTestTask, "run"):
            code, _ = self.request("/api/products/test", {"task_id": task_id, "product": item, "fetcher": {"mode": "flaresolverr", "provider_modes": {"html": "requests"}}})
            self.assertEqual(code, 200)
            self.assertFalse(admin.TEST_TASKS[task_id].browser)
            self.assertEqual(admin.TEST_TASKS[task_id].fetcher["mode"], "requests")
        self.assertFalse(self.config.exists())


if __name__ == "__main__":
    unittest.main()
