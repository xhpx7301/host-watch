"""BandwagonHost catalogue and affiliate contracts."""
from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import admin
import bwh
import catalog
import monitor
from affiliates import affiliate_purchase_url


PAYLOAD = {
    "schema_version": 1,
    "locations": [
        {"city": "Los Angeles", "country": "United States", "datacenters": [
            {"id": "USCA_9", "name": "Coresite LA2", "tiers": ["ecommerce"]},
        ]},
        {"city": "Tokyo", "country": "Japan", "datacenters": [
            {"id": "JPOS_1", "name": "Equinix OS1", "tiers": ["basic"]},
        ]},
    ],
    "offers": [
        {
            "product_id": 87,
            "name": "SPECIAL 20G KVM PROMO V5",
            "tier_ids": ["ecommerce"],
            "location": {"city": "Los Angeles", "country": "United States", "datacenter_id": "USCA_9", "datacenter_name": "Coresite LA2"},
            "specifications": {"storage_gb": 20, "ram_mb": 1024, "vcpu_count": 2, "transfer_gb_per_month": 1000, "port_speed_mbps": 2500},
            "price": {"amount_cents": 4999, "currency": "USD", "billing_period": "quarterly"},
            "available": True,
            "cart_url": "https://bandwagonhost.com/cart.php?a=add&pid=87&billingcycle=quarterly&configoption%5B17%5D=55",
        },
        {
            "product_id": 87,
            "name": "SPECIAL 20G KVM PROMO V5",
            "tier_ids": ["ecommerce"],
            "location": {"city": "Los Angeles", "country": "United States", "datacenter_id": "USCA_9", "datacenter_name": "Coresite LA2"},
            "specifications": {"storage_gb": 20, "ram_mb": 1024, "vcpu_count": 2, "transfer_gb_per_month": 1000, "port_speed_mbps": 2500},
            "price": {"amount_cents": 16999, "currency": "USD", "billing_period": "annually"},
            "available": False,
            "cart_url": "https://bandwagonhost.com/cart.php?a=add&pid=87&billingcycle=annually&configoption%5B17%5D=55",
        },
    ],
}


class BandwagonHostTests(unittest.TestCase):
    def setUp(self):
        self.body = json.dumps(PAYLOAD)

    def fetch_page(self, *args, **kwargs):
        return self.body, bwh.CATALOG_URL

    def test_normalize_url_and_chinese_scopes(self):
        url = "https://bwh81.net/order/ecommerce/Los%20Angeles/USCA_9"
        self.assertEqual(bwh.normalize_catalog_url(url), url)
        self.assertEqual(bwh.scope_from_url(url), "ecommerce:USCA_9")
        scopes = bwh._scopes(PAYLOAD)
        self.assertIn({"id": "ecommerce:USCA_9", "name": "美国 · 洛杉矶 · Coresite LA2 · 电商 VPS（USCA_9）"}, scopes)
        self.assertIn({"id": "basic:JPOS_1", "name": "日本 · 东京 · Equinix OS1 · 基础 VPS（JPOS_1）"}, scopes)

    def test_directory_parses_period_and_inventory_as_distinct_rows(self):
        with patch("monitor.fetch_page", side_effect=self.fetch_page):
            data = catalog.load_catalog("bwh", "ecommerce:USCA_9", {"mode": "requests"})
        self.assertEqual(data["provider"], "bwh")
        self.assertEqual(len(data["rows"]), 2)
        available, sold = data["rows"]
        self.assertEqual(available["available"], 1)
        self.assertEqual(sold["available"], 0)
        self.assertEqual(available["specs"], "2 核 · 1024 MB · 20 GB")
        self.assertEqual(available["traffic"], "1000 GB流量 · 2500 Mbps")
        self.assertEqual(available["price"], "USD 49.99 / 季付")
        self.assertEqual(available["region"], "美国 · 洛杉矶")
        self.assertEqual(available["line"], "Coresite LA2")
        self.assertEqual(available["watch"]["category_url"], "https://bwh81.net/order/ecommerce/Los%20Angeles/USCA_9")
        self.assertNotEqual(available["key"], sold["key"])

    def test_inspect_requires_exact_offer_key(self):
        item = {"bwh_offer_key": "87:USCA_9:quarterly:4999"}
        with patch("monitor.fetch_page", side_effect=self.fetch_page):
            product = bwh.inspect(Mock(), item, {"mode": "requests"})
        self.assertEqual(product.key, item["bwh_offer_key"])
        self.assertEqual(product.available, 1)
        with patch("monitor.fetch_page", side_effect=self.fetch_page):
            with self.assertRaises(monitor.ProductUnlisted):
                bwh.inspect(Mock(), {"bwh_offer_key": "87:USCA_9:monthly:4999"}, {"mode": "requests"})

    def test_config_and_affiliate_keep_product_parameters(self):
        item = {"id": "bwh-one", "adapter": "bwh", "provider": "搬瓦工", "name": "20G", "category_url": bwh.PRODUCT_URL,
                "bwh_offer_key": "87:USCA_9:quarterly:4999", "pid": "87", "enabled": True}
        config = admin.validate_config({"products": [item], "affiliates": {"bwh": "https://bwh81.net/aff.php?aff=84938"}})
        self.assertEqual(config["affiliates"]["bwh"], "84938")
        link = affiliate_purchase_url(PAYLOAD["offers"][0]["cart_url"], "bwh", config, item)
        query = parse_qs(urlsplit(link).query)
        self.assertEqual(urlsplit(link).path, "/aff.php")
        self.assertEqual(query["aff"], ["84938"])
        self.assertEqual(query["pid"], ["87"])
        self.assertEqual(query["billingcycle"], ["quarterly"])


if __name__ == "__main__":
    unittest.main()
