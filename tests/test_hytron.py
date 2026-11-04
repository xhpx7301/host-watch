import unittest
from unittest.mock import patch

import admin
import hytron


NODE_ID = "b89b7004-3621-4a17-957e-3f79f3ef57a5"


def node(**overrides):
    value = {
        "id": NODE_ID,
        "name": "Hytron-HK-WPC-12-Server-2",
        "location": "Hong Kong",
        "location_code": "HKG",
        "has_capacity": True,
        "needs_sync": False,
        "pricing": {
            "price_per_cpu_hour": "0.00200000",
            "price_per_gb_ram_hour": "0.00200000",
            "price_per_gb_disk_hour": "0.00050000",
            "price_per_ip_hour": "0.00550000",
            "price_per_ipv6_hour": "0.00010000",
            "price_per_gb_traffic": "0.00060000",
            "setup_fee": "0.50000000",
        },
        "capacity": {
            "cpu": {"available": 10},
            "ram_mb": {"available": 10240},
            "disk_gb": {"available": 100},
            "ipv4": {"available": 2},
            "ipv6": {"available": 0},
            "vm_slots": {"available": 10},
        },
        "templates": [{"is_orderable": True, "capacity_verified": True, "min_disk_gb": 3}],
    }
    value.update(overrides)
    return value


CAPS = {
    "hard_min_cpu": 1,
    "hard_min_ram_mb": 512,
    "hard_min_disk_gb": 3,
    "ratio_disk_min_gb": 5,
    "ratio_disk_per_cpu_gb": 50,
    "ratio_disk_per_ram_gb": 50,
    "ratio_ipv4_min": 1,
    "max_disk_gb": 50,
    "max_ipv4": 2,
    "hard_max_cpu": 128,
    "hard_max_ram_mb": 1048576,
    "hard_max_ipv6_prefix64": 64,
}


class HytronTests(unittest.TestCase):
    def test_directory_exposes_node_capacity_without_resource_caps_request(self):
        with patch.object(hytron, "fetch_catalog", return_value=[node()]), patch.object(hytron, "fetch_resource_caps") as fetch_caps:
            scopes, selected, rows = hytron.directory(None)
        self.assertEqual(selected, "all")
        self.assertEqual([row["id"] for row in scopes], ["all", "HKG"])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["available"], 1)
        self.assertEqual(row["stock_mode"], "availability")
        self.assertEqual(row["node_capacity"], {"cpu": 10, "ram_mb": 10240, "disk_gb": 100, "ipv4": 2, "ipv6": 0, "vm_slots": 10})
        self.assertIn("CPU 10 核", row["specs"])
        self.assertEqual(row["watch"]["notify_resources"], ["stock", "cpu", "ram_mb", "disk_gb", "ipv4", "ipv6", "vm_slots"])
        self.assertEqual(row["watch"]["adapter"], "hytron")
        self.assertEqual(row["watch"]["node_id"], NODE_ID)
        fetch_caps.assert_not_called()
        self.assertEqual(row["purchase_url"], "https://hyvps.hytron.io/order")

    def test_node_capacity_flag_controls_availability_and_sync_is_unknown(self):
        item = {"node_id": NODE_ID}
        found = hytron.inspect(None, item, catalog=[node(has_capacity=False)], caps=CAPS)
        self.assertEqual(found.available, 0)
        found = hytron.inspect(None, item, catalog=[node(needs_sync=True)], caps=CAPS)
        self.assertIsNone(found.available)

    def test_node_directory_does_not_require_orderable_template(self):
        with patch.object(hytron, "fetch_catalog", return_value=[node(templates=[])]), patch.object(hytron, "fetch_resource_caps") as fetch_caps:
            _, _, rows = hytron.directory(None)
        self.assertEqual(rows[0]["available"], 1)
        fetch_caps.assert_not_called()
        config = admin.default_config()
        config["products"] = [{"id": "hytron-node", "name": "Hytron node", "adapter": "hytron", "node_id": "invalid", "enabled": True}]
        with self.assertRaisesRegex(ValueError, "Hytron 目录选择"):
            admin.validate_config(config)

    def test_valid_hytron_product_config_normalizes(self):
        config = admin.default_config()
        resources = {"cpu": 2, "ram_mb": 4096, "disk_gb": 20, "ipv4": 1, "ipv6": 2}
        config["products"] = [{"id": "hytron-node", "name": "Hytron node", "adapter": "hytron", "node_id": NODE_ID, "resources": resources, "enabled": True}]
        normalized = admin.validate_config(config)["products"][0]
        self.assertEqual(normalized["provider"], "Hytron")
        self.assertEqual(normalized["node_id"], NODE_ID)
        self.assertEqual(normalized["resources"], resources)

    def test_custom_resource_combination_checks_all_node_capacities(self):
        selected = {"cpu": 2, "ram_mb": 4096, "disk_gb": 20, "ipv4": 1, "ipv6": 1}
        current_node = node()
        current_node["capacity"]["ipv6"]["available"] = 2
        result = hytron.inspect(None, {"node_id": NODE_ID, "resources": selected},
            catalog=[current_node], caps={**CAPS, "max_disk_gb": 100, "max_ipv4": 4})
        self.assertEqual(result.available, 2)
        self.assertTrue(result.specs.startswith("2 核 · 4 GB · 20 GB"))
        self.assertEqual(result.match_details["resources"], selected)

    def test_custom_resource_combination_requires_public_address(self):
        config = admin.default_config()
        config["products"] = [{"id": "hytron-node", "name": "Hytron node", "adapter": "hytron", "node_id": NODE_ID,
            "resources": {"cpu": 1, "ram_mb": 1024, "disk_gb": 3, "ipv4": 0, "ipv6": 0}}]
        with self.assertRaisesRegex(ValueError, "至少需要一个 IPv4 或 IPv6"):
            admin.validate_config(config)

    def test_resource_caps_are_fetched_for_selected_cpu_and_memory(self):
        from urllib.parse import parse_qs, urlsplit

        with patch("monitor.fetch_page", return_value=(__import__("json").dumps(CAPS), hytron.RESOURCE_CAPS_URL + "?cpu=2&ram_mb=4096")) as fetch:
            result = hytron.fetch_resource_caps(None, cpu=2, ram_mb=4096)
        self.assertEqual(result, CAPS)
        self.assertEqual(parse_qs(urlsplit(fetch.call_args.args[1]).query), {"cpu": ["2"], "ram_mb": ["4096"]})


if __name__ == "__main__":
    unittest.main()
