import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import admin
import catalog
import monitor
from monitor import MonitorError, ProductUnlisted, parse_narwhal


MACHINE_ID = "17141ff1-5623-4455-a7a5-91a0146f090e"
PLAN_ID = "fe7edd6f-a84c-43fc-a41a-6221b33410ff"
OTHER_MACHINE_ID = "27141ff1-5623-4455-a7a5-91a0146f090e"
OTHER_PLAN_ID = "ce7edd6f-a84c-43fc-a41a-6221b33410ff"


def plan(**changes):
    return {
        "id": PLAN_ID,
        "machine_id": MACHINE_ID,
        "name": "中杯",
        "machine_name": "东京实例",
        "machine_region": "JP",
        "cpu": 1,
        "ram_mb": 128,
        "disk_gb": 1,
        "bandwidth_mbps": 100,
        "monthly_traffic_gb": 400,
        "price_monthly": 1,
        "status": "active",
        "machine_status": "online",
        "remaining": 2,
        "sold_out": False,
        **changes,
    }


def item(**changes):
    return {"id": "watch-1", "name": "中杯", "adapter": "narwhal", "machine_id": MACHINE_ID, "plan_id": PLAN_ID, **changes}


class NarwhalTests(unittest.TestCase):
    def test_parser_tracks_exact_plan_quantity_and_purchase_url(self):
        result = parse_narwhal([plan()], item())
        self.assertEqual(result.available, 2)
        self.assertEqual(result.vendor, "独角鲸云")
        self.assertIn("plan_id=" + PLAN_ID, result.url)
        self.assertEqual(result.match_details["machine_id"], MACHINE_ID)


    def test_parser_treats_sold_out_as_zero_even_if_remaining_is_positive(self):
        result = parse_narwhal([plan(remaining=3, sold_out=True)], item())
        self.assertEqual(result.available, 0)

    def test_parser_treats_zero_remaining_without_sold_out_as_unlimited(self):
        result = parse_narwhal([plan(remaining=0, sold_out=False)], item())
        self.assertEqual(result.available, 1)
        self.assertEqual(result.stock_mode, "availability")


    def test_parser_rejects_untrusted_stock_values(self):
        for bad in (None, -1, True, 1.5, "2"):
            with self.subTest(stock=bad), self.assertRaises(MonitorError):
                parse_narwhal([plan(remaining=bad)], item())


    def test_parser_keeps_removed_plan_configured(self):
        with self.assertRaises(ProductUnlisted):
            parse_narwhal([], item())


    def test_config_requires_valid_narwhal_machine_and_plan_uuids(self):
        config = admin.default_config()
        config["products"] = [item(enabled=True)]
        self.assertEqual(admin.validate_config(config)["products"][0]["adapter"], "narwhal")
        config["products"] = [item(machine_id="invalid")]
        with self.assertRaisesRegex(ValueError, "独角鲸云目录"):
            admin.validate_config(config)

    def test_fetch_checks_complete_response_and_requests_selected_machine(self):
        payload = {"code": 0, "data": {"total": 1, "plans": [plan()]}}
        with patch.object(monitor, "fetch_page", return_value=(json.dumps(payload), monitor.NARWHAL_PLANS_URL)) as fetch:
            result = monitor.fetch_narwhal_plans(Mock(), MACHINE_ID, {"mode": "requests"})
        self.assertEqual(result, [plan()])
        self.assertIn("machine_id=" + MACHINE_ID, fetch.call_args.args[1])

        payload["data"]["total"] = 2
        with patch.object(monitor, "fetch_page", return_value=(json.dumps(payload), monitor.NARWHAL_PLANS_URL)):
            with self.assertRaisesRegex(MonitorError, "目录不完整"):
                monitor.fetch_narwhal_plans(Mock(), MACHINE_ID, {"mode": "requests"})

    def test_catalog_scopes_plans_by_instance(self):
        other = plan(id=OTHER_PLAN_ID, machine_id=OTHER_MACHINE_ID, machine_name="洛杉矶实例")
        with patch.object(monitor, "fetch_narwhal_plans", return_value=[plan(), other]):
            scopes, selected, rows = catalog.narwhal_catalog(Mock(), MACHINE_ID)
        self.assertEqual(selected, MACHINE_ID)
        self.assertEqual({scope["id"] for scope in scopes}, {MACHINE_ID, OTHER_MACHINE_ID})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["watch"]["machine_id"], MACHINE_ID)
        self.assertEqual(rows[0]["watch"]["plan_id"], PLAN_ID)

    def test_restock_from_selected_catalog_plan_sends_instance_and_purchase_link(self):
        payload = [plan(remaining=0, sold_out=True)]
        with patch.object(monitor, "fetch_narwhal_plans", return_value=payload):
            row = catalog.narwhal_catalog(Mock(), MACHINE_ID)[2][0]
        config = admin.validate_config({"enabled": True, "products": [{**row["watch"], "id": "watch-mini"}]})
        with tempfile.TemporaryDirectory() as directory, patch.object(monitor, "fetch_narwhal_plans", return_value=payload), patch.object(monitor, "telegram_request", return_value={"ok": True, "result": {"message_id": 101}}) as send:
            state, status = Path(directory) / "state.json", Path(directory) / "status.json"
            monitor.check_once(Mock(), config, state, status, "token", "123")
            send.assert_not_called()
            payload[0].update(remaining=2, sold_out=False)
            monitor.check_once(Mock(), config, state, status, "token", "123")
            send.assert_called_once()
            self.assertEqual(send.call_args.args[2], "sendMessage")
            self.assertIn("东京实例", send.call_args.kwargs["text"])
            link = send.call_args.kwargs["reply_markup"]["inline_keyboard"][0][0]["url"]
            self.assertIn(PLAN_ID, link)
            monitor.check_once(Mock(), config, state, status, "token", "123")
            self.assertEqual(send.call_count, 1)

    def test_changing_plan_or_instance_invalidates_shared_inventory_cache(self):
        key = monitor.inventory_cache_key(item(), {})
        self.assertNotEqual(key, monitor.inventory_cache_key(item(plan_id=OTHER_PLAN_ID), {}))
        self.assertNotEqual(key, monitor.inventory_cache_key(item(machine_id=OTHER_MACHINE_ID), {}))
