import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import admin
import monitor
from affiliates import DEFAULT_AFFILIATES, PROVIDERS, affiliate_purchase_url
import test_admin


class AffiliateTests(unittest.TestCase):
    def test_defaults_migrate_legacy_and_explicit_blanks_persist(self):
        self.assertEqual(admin.validate_config({})["affiliates"], DEFAULT_AFFILIATES)
        self.assertEqual(admin.validate_config({})["affiliate_notes"], {key: "" for key in PROVIDERS})
        self.assertEqual(admin.validate_config({"affiliate_id": "321"})["affiliates"]["vmiss"], "321")
        blank = {key: "" for key in PROVIDERS}
        config = admin.validate_config({"affiliate_id": "321", "affiliates": blank})
        self.assertEqual(config["affiliate_id"], "")
        self.assertEqual(admin.validate_config(config)["affiliates"], blank)

    def test_affiliate_notes_are_normalized_and_validated(self):
        notes = {key: f"{key} account · 10%" for key in PROVIDERS}
        config = admin.validate_config({"affiliate_notes": notes})
        self.assertEqual(config["affiliate_notes"], notes)
        for payload in [
            {"affiliate_notes": []},
            {"affiliate_notes": {"unknown": "note"}},
            {"affiliate_notes": {"vmiss": 10}},
            {"affiliate_notes": {"vmiss": "x" * 241}},
        ]:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                admin.validate_config(payload)

    def test_full_referral_links_are_normalized(self):
        config = admin.validate_config({"affiliates": {
            "vmiss": "https://app.vmiss.com/aff.php?aff=5340",
            "akile": "https://akile.ai/register?aff_code=" + DEFAULT_AFFILIATES["akile"],
            "isvoro": "https://isvoro.com/?aff=c57baee34737",
        }})
        self.assertEqual(config["affiliates"], DEFAULT_AFFILIATES)

    def test_rejects_invalid_affiliates_and_order(self):
        for payload in [
            {"affiliates": []}, {"affiliates": {"unknown": "id"}},
            {"affiliates": {"akile": "https://evil.example/?aff_code=id"}},
            {"affiliates": {"isvoro": "https://isvoro.com/?aff=one&aff=two"}},
            {"affiliates": {"vmiss": "abc"}}, {"affiliates": {"isvoro": "bad&aff=code"}},
            {"affiliates": {"isvoro": "https://isvoro.com/?missing=id"}},
            {"affiliates": {"akile": 123}},
            {"merchant_order": ["akile", "akile"]}, {"merchant_order": ["fake"]},
            {"merchant_order": "akile"}, {"merchant_order": [[]]},
        ]:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                admin.validate_config(payload)

    def test_partial_merchant_order_appends_missing_vendors(self):
        config = admin.validate_config({"merchant_order": ["isvoro", "akile"]})
        self.assertEqual(config["merchant_order"], ["isvoro", "akile", "vmiss", "vps_soy", "narwhal", "dmit"])
        self.assertEqual(admin.validate_config(config)["merchant_order"], config["merchant_order"])

    def test_deep_links_keep_exact_product_parameters_and_replace_old_code(self):
        for provider, raw, expected in [
            ("akile", "https://akile.ai/shop/server?type=traffic&areaId=6&nodeId=41&planId=1047&aff_code=old#plan", {"type": ["traffic"], "areaId": ["6"], "nodeId": ["41"], "planId": ["1047"], "aff_code": ["new"]}),
            ("isvoro", "https://isvoro.com/buy/HK-prod?product=188&aff=old&aff=older", {"product": ["188"], "aff": ["new"]}),
            ("vps_soy", "https://www.vps.soy/cart?fid=1&gid=8&pid=11", {"fid": ["1"], "gid": ["8"], "pid": ["11"], "aff": ["new"]}),
        ]:
            with self.subTest(provider=provider):
                result = affiliate_purchase_url(raw, provider, {"affiliates": {provider: "new"}})
                parsed = urlsplit(result)
                self.assertEqual(parse_qs(parsed.query), expected)
                self.assertEqual(parsed.path, urlsplit(raw).path)
                self.assertEqual(parsed.fragment, urlsplit(raw).fragment)
                plain = affiliate_purchase_url(result, provider, {"affiliates": {provider: ""}})
                self.assertNotIn("aff=", plain)
                self.assertNotIn("aff_code=", plain)

    def test_vmiss_uses_product_id_or_group_id_and_current_aff(self):
        config = admin.validate_config({})
        product = monitor.Product("p", "Mini", 1, "https://app.vmiss.com/store/test/mini")
        for ids in [{"pid": "44"}, {"gid": "12"}]:
            item = {"adapter": "vmiss", **ids}
            query = parse_qs(urlsplit(monitor.purchase_link(product, item, config)).query)
            self.assertEqual(query, {"aff": ["5340"], **{k: [v] for k, v in ids.items()}})

    def test_never_adds_aff_to_unrelated_hosts_or_generic_products(self):
        config = {"affiliates": {"akile": "new"}}
        for raw in ["https://akile.ai.evil.example/item", "http://akile.ai/shop", "https://akile.ai:8765/shop", "https://user@akile.ai/shop"]:
            self.assertEqual(affiliate_purchase_url(raw, "akile", config), raw)
        raw = "https://shop.example.com/?aff=custom"
        self.assertEqual(affiliate_purchase_url(raw, "html", config), raw)

    def test_manual_test_telegram_uses_latest_saved_aff(self):
        config = admin.validate_config({"affiliates": {"akile": "latest"}})
        product = {"adapter": "akile", "name": "Mini"}
        result = {"ok": True, "available": 1, "checked_at": "now", "duration_seconds": 1,
                  "purchase_url": "https://akile.ai/shop/server?areaId=6&nodeId=41&planId=1047&aff_code=old"}
        with patch.object(admin, "telegram_credentials", return_value=("token", "123")), patch.object(admin, "current_config", return_value=config), patch.object(admin, "telegram_call") as telegram:
            self.assertEqual(admin.send_stock_test_result(product, result)["status"], "sent")
        message = telegram.call_args.kwargs["text"]
        self.assertIn("aff_code=latest", message)
        self.assertIn("planId=1047", message)
        self.assertNotIn("aff_code=old", message)


class AffiliateAPITests(unittest.TestCase):
    setUp = test_admin.AdminAPITests.setUp
    request = test_admin.AdminAPITests.request

    def test_affiliate_and_merchant_order_save_reload_and_legacy_client(self):
        value = admin.default_config()
        value["affiliates"]["akile"] = "changed"
        value["affiliates"]["vmiss"] = ""
        value["affiliate_notes"]["akile"] = "主账号 · 10%"
        value["merchant_order"] = list(reversed(PROVIDERS))
        with patch.object(admin, "manager_action", return_value={"ok": True}):
            code, body = self.request("/api/config", value)
            self.assertEqual(code, 200)
            saved = json.loads(body)["config"]
            self.assertEqual(saved["affiliates"], value["affiliates"])
            self.assertEqual(saved["affiliate_notes"], value["affiliate_notes"])
            self.assertEqual(saved["merchant_order"], value["merchant_order"])
            self.assertEqual(saved["affiliate_id"], "")
            legacy = {k: v for k, v in saved.items() if k not in ("affiliates", "affiliate_notes", "merchant_order")}
            legacy["affiliate_id"] = "999"
            code, body = self.request("/api/config", legacy)
        self.assertEqual(code, 200)
        actual = json.loads(body)["config"]
        self.assertEqual(actual["affiliates"]["vmiss"], "999")
        self.assertEqual(actual["affiliates"]["akile"], "changed")
        self.assertEqual(actual["affiliate_notes"]["akile"], "主账号 · 10%")
        self.assertEqual(actual["merchant_order"], value["merchant_order"])
        self.assertEqual(admin.current_config()["affiliates"], actual["affiliates"])


if __name__ == "__main__":
    unittest.main()
