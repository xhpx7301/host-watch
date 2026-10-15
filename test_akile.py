import copy
import html
import json
import unittest
from unittest.mock import Mock, patch

from admin import validate_config
from monitor import (
    AKILE_STORE_URL, MonitorError, SiteBlocked, check_once,
    fetch_akile_catalog, inspect_product, parse_akile,
)


def item(tier="Mini", **overrides):
    name = f"HKL-TW-{tier}"
    return {"id": name, "name": name, "adapter": "akile", "provider": "Akile",
            "area_id": "6", "node_name": "HKL-TW", "selector": name,
            "enabled": True, "request_mode": "requests", **overrides}


def catalog():
    # Public GetVpsStoreV3 shape observed on 2026-10-15. Each tier has its
    # own stock and plan ID; group totals must never substitute for Mini stock.
    return {"status_code": 0, "data": {"areas": [{"id": 6, "nodes": [
        {"id": 41, "group_name": "HKL-TW", "plans": [
            {"id": 1047 + i, "plan_name": f"HKL-TW-{tier}", "stock": stock, "tag": "traffic"}
            for i, (tier, stock) in enumerate(zip(["Mini", "Starter", "Standard", "Pro"], [0, 1, 14, 16]))
        ]}
    ]}]}}


def plan(payload):
    return payload["data"]["areas"][0]["nodes"][0]["plans"][0]


class AkileTests(unittest.TestCase):
    def test_unlimited_traffic_keeps_bandwidth_and_unknown_is_not_unlimited(self):
        for flow, expected in [(0, "无限制"), ("0", "无限制"), ("无限制", "无限制"),
                               (300, "300 GB流量"), (None, "—"), ("", "—"), (False, "—"), (-1, "—")]:
            with self.subTest(flow=flow):
                payload = catalog()
                plan(payload).update(flow=flow, bandwidth=500)
                product = parse_akile(payload, item())
                self.assertEqual(product.traffic, expected + " · 500 Mbps")

    def test_each_tier_has_exact_stock_and_matching_purchase_link(self):
        payload = catalog()
        for i, (tier, stock) in enumerate(zip(["Mini", "Starter", "Standard", "Pro"], [0, 1, 14, 16])):
            with self.subTest(tier=tier):
                product = parse_akile(payload, item(tier))
                self.assertEqual(product.available, stock)
                self.assertEqual(product.name, f"HKL-TW-{tier}")
                self.assertEqual(product.vendor, "Akile")
                self.assertEqual(product.url, f"https://akile.ai/shop/server?type=traffic&areaId=6&nodeId=41&planId={1047+i}")

    def test_other_area_node_and_similarly_named_plan_do_not_match(self):
        payload = catalog()
        other = copy.deepcopy(payload["data"]["areas"][0])
        other["id"] = 3
        plan({"data": {"areas": [other]}})["stock"] = 999
        payload["data"]["areas"].append(other)
        node = payload["data"]["areas"][0]["nodes"][0]
        node["plans"].insert(0, {"id": 9, "plan_name": "HKL-TW-Mini-Plus", "stock": 99, "tag": "traffic"})
        self.assertEqual(parse_akile(payload, item(area_mode="manual")).available, 0)
        for override in [{"area_id": "99"}, {"node_name": "HKL"}, {"selector": "HKL-TW"}]:
            with self.subTest(override=override), self.assertRaisesRegex(MonitorError, "无法唯一匹配"):
                parse_akile(payload, item(area_mode="manual", **override))

    def test_missing_or_duplicate_area_node_or_plan_is_error(self):
        for level in ["area", "node", "plan"]:
            for duplicate in [True, False]:
                payload = catalog()
                area = payload["data"]["areas"][0]
                entries = {"area": payload["data"]["areas"], "node": area["nodes"], "plan": area["nodes"][0]["plans"]}[level]
                if duplicate:
                    entries.append(copy.deepcopy(entries[0]))
                else:
                    entries.clear()
                with self.subTest(level=level, duplicate=duplicate), self.assertRaisesRegex(MonitorError, "无法唯一匹配"):
                    parse_akile(payload, item(area_mode="manual"))

    def test_automatic_region_ignores_stale_url_area_and_reports_actual_match(self):
        payload = catalog()
        payload["data"]["areas"].append({"id": 14, "area_name": "中国澳门", "nodes": [
            {"id": 38, "group_name": "MOLite", "plans": [
                {"id": 1017, "plan_name": "MOLite-高级竞技A", "stock": 0, "tag": "traffic"}
            ]}
        ]})
        for area_id in ["6", "", "99"]:
            with self.subTest(area_id=area_id):
                product = parse_akile(payload, item(area_id=area_id, node_name=" MOLite ", selector="MOLite-高级竞技A"))
                self.assertEqual(product.available, 0)
                self.assertEqual((product.area_id, product.area_name, product.node_name), ("14", "中国澳门", "MOLite"))
                self.assertEqual(product.url, "https://akile.ai/shop/server?type=traffic&areaId=14&nodeId=38&planId=1017")
        with self.assertRaisesRegex(MonitorError, "实际位于.*ID=14"):
            parse_akile(payload, item(area_mode="manual", node_name="MOLite", selector="MOLite-高级竞技A"))

    def test_automatic_region_requires_unique_full_pair(self):
        payload = catalog()
        other = copy.deepcopy(payload["data"]["areas"][0])
        other["id"] = 14
        payload["data"]["areas"].append(other)
        with self.assertRaisesRegex(MonitorError, "找到 2 项.*指定地区"):
            parse_akile(payload, item())
        other["nodes"][0]["plans"] = [{"id": 1017, "plan_name": "HKL-TW-Mini-Plus", "stock": 99, "tag": "traffic"}]
        self.assertEqual(parse_akile(payload, item()).available, 0)
        for overrides in [{"node_name": "HKL"}, {"selector": "HKL-TW"}]:
            with self.subTest(overrides=overrides), self.assertRaisesRegex(MonitorError, "找到 0 项"):
                parse_akile(payload, item(**overrides))

    def test_automatic_region_rejects_malformed_nested_catalog(self):
        for level in ["nodes", "plans"]:
            payload = catalog()
            parent = payload["data"]["areas"][0]
            if level == "plans":
                parent = parent["nodes"][0]
            parent[level] = [None]
            with self.subTest(level=level), self.assertRaisesRegex(MonitorError, "列表结构异常"):
                parse_akile(payload, item())

    def test_missing_negative_boolean_fractional_or_text_stock_is_error(self):
        for stock in [None, -1, True, 0.5, "0", "unknown"]:
            payload = catalog()
            plan(payload)["stock"] = stock
            with self.subTest(stock=stock), self.assertRaisesRegex(MonitorError, "库存不是非负整数"):
                parse_akile(payload, item())
        payload = catalog()
        del plan(payload)["stock"]
        with self.assertRaises(MonitorError):
            parse_akile(payload, item())

    def test_api_failure_and_malformed_catalog_are_not_sold_out(self):
        malformed = [None, [], {}, {"status_code": True}, {"status_code": 401, "data": catalog()["data"]},
                     {"status_code": 0, "data": None}, {"status_code": 0, "data": {"areas": {}}}]
        for payload in malformed:
            with self.subTest(payload=payload), self.assertRaises(MonitorError):
                parse_akile(payload, item())

    def test_invalid_purchase_ids_and_tags_are_rejected(self):
        for key, value in [("id", 0), ("id", True), ("id", "1047"), ("tag", "unknown"), ("tag", []), ("tag", None)]:
            payload = catalog()
            plan(payload)[key] = value
            with self.subTest(key=key, value=value), self.assertRaisesRegex(MonitorError, "购买标识"):
                parse_akile(payload, item())

    def test_config_validates_without_url_css_or_affiliate(self):
        config = validate_config({"enabled": True, "products": [item(area_id=6)], "affiliates": {"vmiss": "", "akile": ""}})
        self.assertEqual(config["products"][0]["area_id"], "6")
        self.assertEqual(config["products"][0]["area_mode"], "auto")
        self.assertEqual(config["affiliate_id"], "")
        self.assertNotIn("url", config["products"][0])
        for override in [{"area_id": "0"}, {"area_id": "-3"}, {"area_id": "3&test=1"}, {"area_id": True}, {"node_name": ""}, {"selector": ""}]:
            with self.subTest(override=override), self.assertRaises(ValueError):
                validate_config({"products": [item(**override)]})

    def test_config_automatic_and_explicit_regions(self):
        for overrides in [{"area_mode": "manual", "area_id": ""}, {"area_mode": "unknown"}]:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                validate_config({"products": [item(**overrides)]})
        for mode, area_id in [("auto", ""), ("manual", "14")]:
            config = validate_config({"products": [item(area_mode=mode, area_id=area_id)]})
            self.assertEqual(config["products"][0]["area_mode"], mode)
            self.assertEqual(config["products"][0]["area_id"], area_id)

    def test_public_api_fetch_and_standalone_override(self):
        session = Mock()
        session.get.return_value = Mock(status_code=200, text=json.dumps(catalog()), url=AKILE_STORE_URL, headers={})
        product = inspect_product(session, item(enabled=False), {"mode": "flaresolverr"})
        self.assertEqual(product.available, 0)
        session.get.assert_called_once_with(AKILE_STORE_URL, timeout=(10, 25), allow_redirects=True)
        session.post.assert_not_called()

    def test_browser_json_wrapper_and_invalid_json(self):
        body = "<html><body><pre>" + html.escape(json.dumps(catalog())) + "</pre></body></html>"
        with patch("monitor.fetch_page", return_value=(body, AKILE_STORE_URL)) as fetch:
            self.assertEqual(fetch_akile_catalog(Mock(), {"mode": "flaresolverr"}), catalog())
            self.assertEqual(fetch.call_args.args[3], "api.akile.ai")
        for body in ["<html>Loading...</html>", "{} broken"]:
            with self.subTest(body=body), patch("monitor.fetch_page", return_value=(body, AKILE_STORE_URL)), self.assertRaisesRegex(MonitorError, "JSON"):
                fetch_akile_catalog(Mock(), {"mode": "requests"})

    def test_blocked_or_redirected_api_is_error(self):
        session = Mock()
        session.get.return_value = Mock(status_code=429, text="", url=AKILE_STORE_URL, headers={})
        with self.assertRaises(SiteBlocked):
            inspect_product(session, item(), {})
        session.get.return_value = Mock(status_code=200, text=json.dumps(catalog()), url="https://other.example/api", headers={})
        with self.assertRaisesRegex(MonitorError, "重定向离开"):
            inspect_product(session, item(), {})

    def test_shared_catalog_restock_notifications_and_distinct_request_modes(self):
        payload = catalog()
        plan(payload)["stock"] = 2
        products = [item(tier) for tier in ["Mini", "Starter", "Standard", "Pro"]]
        previous = {product["id"]: {"available": 0 if i == 0 else [0, 1, 14, 16][i]} for i, product in enumerate(products)}
        with patch("monitor.fetch_akile_catalog", return_value=payload) as fetch, patch("monitor.read_state", return_value=previous), patch("monitor.write_state"), patch("monitor.write_status") as status, patch("monitor.telegram_request", return_value={"ok": True, "result": {"message_id": 101}}) as notify:
            check_once(Mock(), {"products": [dict(product, area_id="") for product in products]}, None, None, "bot", "chat")
        fetch.assert_called_once()
        notify.assert_called_once()
        self.assertIn("HKL-TW-Mini", notify.call_args.kwargs["text"])
        self.assertIn("Akile", notify.call_args.kwargs["text"])
        button = notify.call_args.kwargs["reply_markup"]["inline_keyboard"][0][0]
        self.assertNotIn("VMISS", button["text"])
        self.assertIn("planId=1047", button["url"])
        self.assertEqual(len(status.call_args.args[1]["products"]), 4)
        products[-1]["request_mode"] = "flaresolverr"
        with patch("monitor.fetch_akile_catalog", return_value=payload) as fetch, patch("monitor.read_state", return_value=previous), patch("monitor.write_state"), patch("monitor.write_status"):
            check_once(Mock(), {"products": products}, None, None, "", "")
        self.assertEqual(fetch.call_count, 2)

    def test_missing_monitored_plan_preserves_baseline_and_other_products(self):
        with patch("monitor.fetch_akile_catalog", return_value=catalog()), patch("monitor.read_state", return_value={}), patch("monitor.write_state") as write, patch("monitor.write_status") as status, patch("monitor.telegram_request", return_value={"ok": True, "result": {"message_id": 101}}) as notify:
            check_once(Mock(), {"products": [item(), item(id="removed", selector="HKL-TW-Removed")]}, None, None, "bot", "chat")
        self.assertEqual(status.call_args.args[1]["state"], "partial")
        self.assertIsNone(status.call_args.args[1]["products"][1]["available"])
        write.assert_called_once()
        notify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
