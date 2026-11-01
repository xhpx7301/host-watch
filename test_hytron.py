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
}


class HytronTests(unittest.TestCase):
    def test_directory_exposes_node_stock_and_hourly_price(self):
        with patch.object(hytron, "fetch_catalog", return_value=[node()]), patch.object(hytron, "fetch_resource_caps", return_value=CAPS):
            scopes, selected, rows = hytron.directory(None)
        self.assertEqual(selected, "all")
        self.assertEqual([row["id"] for row in scopes], ["all", "HKG"])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["available"], 2)
        self.assertEqual(row["stock_mode"], "quantity")
        self.assertEqual(row["watch"]["adapter"], "hytron")
        self.assertEqual(row["watch"]["node_id"], NODE_ID)
        self.assertIn("/小时", row["price"])
        self.assertEqual(row["purchase_url"], "https://hyvps.hytron.io/order")

    def test_nodes_without_capacity_are_sold_out(self):
        item = {"node_id": NODE_ID}
        found = hytron.inspect(None, item, catalog=[node(has_capacity=False)], caps=CAPS)
        self.assertEqual(found.available, 0)

    def test_unorderable_node_is_unknown_and_cannot_be_added_to_watch_config(self):
        with patch.object(hytron, "fetch_catalog", return_value=[node(templates=[])]), patch.object(hytron, "fetch_resource_caps", return_value=CAPS):
            _, _, rows = hytron.directory(None)
        self.assertIsNone(rows[0]["available"])
        config = admin.default_config()
        config["products"] = [{"id": "hytron-node", "name": "Hytron node", "adapter": "hytron", "node_id": "invalid", "enabled": True}]
        with self.assertRaisesRegex(ValueError, "Hytron 目录选择"):
            admin.validate_config(config)

    def test_valid_hytron_product_config_normalizes(self):
        config = admin.default_config()
        config["products"] = [{"id": "hytron-node", "name": "Hytron node", "adapter": "hytron", "node_id": NODE_ID, "enabled": True}]
        normalized = admin.validate_config(config)["products"][0]
        self.assertEqual(normalized["provider"], "Hytron")
        self.assertEqual(normalized["node_id"], NODE_ID)


if __name__ == "__main__":
    unittest.main()
