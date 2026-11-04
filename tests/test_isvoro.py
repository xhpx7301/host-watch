import copy
import html
import http.client
import io
import json
import os
import threading
import unittest
from unittest.mock import Mock, patch

import admin
import monitor
from notifications import network_text


def item(**overrides):
    return {"id": "hk-mini", "name": "HK PROD MiNi", "adapter": "isvoro", "provider": "Isvoro",
            "type_id": "24", "category_id": "27", "product_id": "188", "enabled": True,
            "request_mode": "requests", **overrides}


def directory():
    # Shape of public GET categories, categories/HK-prod and products/hkpordmini.
    return {
        "categories": [{"id": 24, "slug": "cnhk", "name": "中国香港", "children": [
            {"id": 27, "slug": "HK-prod", "name": "HK PROD"},
            {"id": 56, "slug": "hklite", "name": "HK Lite"}]},
            {"id": 48, "slug": "dedicated", "name": "物理机", "children": []}],
        "categories/HK-prod": {"id": 27, "slug": "HK-prod", "name": "HK PROD", "products": [
            {"id": 188, "slug": "hkpordmini", "name": "HK PROD MiNi", "category_id": 27},
            {"id": 189, "slug": "hkprodlite", "name": "HK PROD Lite", "category_id": 27}]},
        "products/hkpordmini": {"id": 188, "slug": "hkpordmini", "name": "HK PROD MiNi",
            "stock": 30, "ip_available": True, "plans": [{"id": 361, "name": "月"}, {"id": 362, "name": "年"}], "settings": []},
    }


class IsvoroTests(unittest.TestCase):
    def inspect(self, data, product=None):
        with patch.object(monitor, "fetch_isvoro_json", side_effect=lambda session, path, fetcher: data[path]):
            return monitor.inspect_product(Mock(), product or item(), {})

    def test_full_hierarchy_real_stock_and_purchase_entry(self):
        result = self.inspect(directory())
        self.assertEqual((result.available, result.name, result.vendor), (30, "HK PROD MiNi", "Isvoro"))
        self.assertEqual(result.url, "https://isvoro.com/buy/HK-prod?product=188")
        self.assertEqual(result.match_details["type_name"], "中国香港")
        self.assertEqual(result.match_details["category_name"], "HK PROD")
        self.assertIn("地区/线路：</b>HK PROD", monitor.notification_text(result, 0, result.url))

    def test_network_settings_match_storefront_bandwidth_units(self):
        for speed_fields, expected in [
            ({"ratelimit": "12.5"}, "300 GB流量 · 100 Mbps"),
            ({"ratelimit": "37.5"}, "300 GB流量 · 300 Mbps"),
            ({"bandwidth_speed": "500", "ratelimit": "12.5"}, "300 GB流量 · 500 Mbps"),
            ({"port_speed": "1000"}, "300 GB流量 · 1000 Mbps"),
            ({"ratelimit": "invalid", "bandwidth": "100"}, "300 GB流量 · 100 Mbps"),
            ({}, "300 GB流量"),
        ]:
            settings = {"bandwidth_limit": "300", **speed_fields}
            for as_list in [False, True]:
                with self.subTest(settings=settings, as_list=as_list):
                    data = directory()
                    data["products/hkpordmini"]["settings"] = [{"key": k, "value": v} for k, v in settings.items()] if as_list else settings
                    result = self.inspect(data)
                    self.assertEqual(result.traffic, expected)
                    self.assertIn(network_text(expected), monitor.notification_text(result, 0, result.url))

    def test_missing_network_settings_are_not_unlimited(self):
        for value in [None, "", True, -1, "NaN", "inf"]:
            with self.subTest(value=value):
                self.assertEqual(monitor.isvoro_network({"bandwidth_limit": value, "ratelimit": value}), "—")

    def test_type_line_and_product_cannot_cross_match(self):
        for overrides in [{"type_id": "48"}, {"category_id": "999"}, {"product_id": "999"}]:
            with self.subTest(overrides=overrides), self.assertRaisesRegex(monitor.MonitorError, "无法唯一匹配"):
                self.inspect(directory(), item(**overrides))
        data = directory()
        data["categories/HK-prod"]["products"][0]["category_id"] = 56
        with self.assertRaisesRegex(monitor.MonitorError, "归属.*不一致"):
            self.inspect(data)

    def test_duplicate_ids_never_choose_first(self):
        for level in ["type", "line", "product"]:
            data = directory()
            rows = {"type": data["categories"], "line": data["categories"][0]["children"],
                    "product": data["categories/HK-prod"]["products"]}[level]
            rows.append(copy.deepcopy(rows[0]))
            with self.subTest(level=level), self.assertRaisesRegex(monitor.MonitorError, "找到 2 项"):
                self.inspect(data)

    def test_same_name_in_other_line_is_not_a_match(self):
        data = directory()
        data["categories/hklite"] = {"id": 56, "slug": "hklite", "name": "HK Lite", "products": [
            {"id": 999, "slug": "othermini", "name": "HK PROD MiNi", "category_id": 56}]}
        with self.assertRaisesRegex(monitor.MonitorError, "套餐.*找到 0 项"):
            self.inspect(data, item(category_id="56"))

    def test_stock_unlimited_ip_exhaustion_and_sold_out(self):
        for stock, ip, count, mode in [(0, True, 0, "quantity"), (30, False, 0, "quantity"),
                (None, True, 1, "availability"), (None, False, 0, "availability")]:
            data = directory()
            data["products/hkpordmini"].update(stock=stock, ip_available=ip)
            result = self.inspect(data)
            self.assertEqual((result.available, result.stock_mode), (count, mode))

    def test_missing_and_invalid_stock_is_unknown_not_sold_out(self):
        for stock in [True, -1, "30", 0.5, {}]:
            data = directory()
            data["products/hkpordmini"]["stock"] = stock
            with self.subTest(stock=stock), self.assertRaisesRegex(monitor.MonitorError, "库存不是"):
                self.inspect(data)
        data = directory()
        del data["products/hkpordmini"]["stock"]
        with self.assertRaises(monitor.MonitorError):
            self.inspect(data)

    def test_detail_identity_hidden_invalid_ip_and_presale_rejected(self):
        for changes in [{"id": 999}, {"hidden": True}, {"ip_available": "false"}, {"plans": []},
                {"settings": [{"key": "presale_enabled", "value": "1"}]}, {"settings": {"presale_enabled": True}}]:
            data = directory()
            data["products/hkpordmini"].update(changes)
            with self.subTest(changes=changes), self.assertRaises(monitor.MonitorError):
                self.inspect(data)

    def test_leaf_type_uses_own_category(self):
        data = directory()
        data["categories/dedicated"] = {"id": 48, "slug": "dedicated", "name": "物理机", "products": [
            {"id": 188, "slug": "hkpordmini", "name": "HK PROD MiNi", "category_id": 48}]}
        result = self.inspect(data, item(type_id="48", category_id="48"))
        self.assertIn("/buy/dedicated?product=188", result.url)

    def test_http_json_browser_wrapper_failure_and_path_restriction(self):
        for mode in ["requests", "flaresolverr"]:
            body = json.dumps({"code": 0, "data": directory()["categories"]})
            if mode == "flaresolverr":
                body = "<html><pre>" + html.escape(body) + "</pre></html>"
            with patch.object(monitor, "fetch_page", return_value=(body, "")) as fetch:
                result = monitor.fetch_isvoro_json(Mock(), "categories", {"mode": mode})
            self.assertEqual(len(result), 2)
            self.assertEqual(fetch.call_args.args[1], "https://isvoro.com/api/v1/categories")
            self.assertEqual(fetch.call_args.args[3], "isvoro.com")
        for body in ["<html>loading</html>", "{}", '{"code":true,"data":[]}', '{"code":403,"data":[]}']:
            with patch.object(monitor, "fetch_page", return_value=(body, "")), self.assertRaises(monitor.MonitorError):
                monitor.fetch_isvoro_json(Mock(), "categories", {})
        for path in ["../orders", "categories/a?x=1", "https://evil.example"]:
            with self.assertRaises(monitor.MonitorError):
                monitor.fetch_isvoro_json(Mock(), path, {})

    def test_malformed_catalog_and_hidden_entries(self):
        for bad in [None, {}, [None], [{"id": True, "name": "x", "slug": "x"}],
                    [{"id": 1, "name": "x", "slug": "../x"}]]:
            with self.subTest(bad=bad), self.assertRaises(monitor.MonitorError):
                monitor.isvoro_categories(bad)
        self.assertEqual(monitor.isvoro_categories([{"hidden": True}]), [])

    def test_unicode_slug_from_public_catalog_is_accepted_without_path_escape(self):
        entry = {"id": 60, "name": "HK-Apex", "slug": "HK‑Apex"}
        self.assertEqual(monitor.isvoro_categories([entry])[0]["slug"], "HK‑Apex")
        spaced = {"id": 214, "name": "HK apex lite", "slug": "HK apex lite"}
        self.assertEqual(monitor.isvoro_categories([spaced])[0]["slug"], "HK apex lite")
        for path in ["categories/HK‑Apex", "products/HK‑Apex", "products/HK apex lite"]:
            with patch.object(monitor, "fetch_page", return_value=(('{"code":0,"data":[]}', ""))) as fetch:
                monitor.fetch_isvoro_json(Mock(), path, {})
            self.assertIn(path, fetch.call_args.args[1])

    def test_config_validation_and_disabled_single_inspection(self):
        saved = admin.validate_config({"products": [item(enabled=False)]})["products"][0]
        self.assertEqual(saved["type_id"], "24")
        self.assertEqual(self.inspect(directory(), saved).available, 30)
        for key in ["type_id", "category_id", "product_id"]:
            for invalid in ["", True, "0", "../1"]:
                with self.subTest(key=key, invalid=invalid), self.assertRaises(ValueError):
                    admin.validate_config({"products": [item(**{key: invalid})]})

    def test_each_round_shares_reads_and_respects_request_override(self):
        data = directory()
        with patch.object(monitor, "fetch_isvoro_json", side_effect=lambda session, path, fetcher: data[path]) as fetch, patch.object(monitor, "read_state", return_value={"hk-mini": {"available": 0}}), patch.object(monitor, "write_state"), patch.object(monitor, "write_status") as status, patch.object(monitor, "telegram_request", return_value={"ok": True, "result": {"message_id": 101}}) as send:
            monitor.check_once(Mock(), {"products": [item(), item(id="second")]}, None, None, "mock", "123")
        self.assertEqual(fetch.call_count, 3)
        send.assert_called_once()
        self.assertIn("中国香港", send.call_args.kwargs["text"])
        self.assertEqual(status.call_args.args[1]["products"][0]["stock_mode"], "quantity")
        with patch.object(monitor, "fetch_isvoro_json", side_effect=lambda session, path, fetcher: data[path]) as fetch, patch.object(monitor, "read_state", return_value={}), patch.object(monitor, "write_state"), patch.object(monitor, "write_status"):
            monitor.check_once(Mock(), {"products": [item(), item(id="second", request_mode="flaresolverr")]}, None, None, "", "")
        self.assertEqual(fetch.call_count, 6)

    def test_worker_and_telegram_preserve_three_level_match(self):
        result = self.inspect(directory())
        output = io.StringIO()
        with patch.object(admin.sys, "stdin", io.StringIO(json.dumps({"product": item(), "fetcher": {}}))), patch.object(admin.sys, "stdout", output), patch.object(monitor, "inspect_product", return_value=result):
            admin.stock_test_worker()
        snapshot = json.loads(output.getvalue())
        self.assertEqual(snapshot["matched_product"]["product_id"], "188")
        snapshot.update(checked_at="2026-10-15", duration_seconds=0.2)
        with patch.object(admin, "telegram_credentials", return_value=("mock-token", "123")), patch.object(admin, "telegram_call") as send:
            self.assertEqual(admin.send_stock_test_result(item(), snapshot)["status"], "sent")
        for expected in ["中国香港", "HK PROD", "HK PROD MiNi", "ID=24", "ID=27", "ID=188", result.url]:
            self.assertIn(expected, send.call_args.kwargs["text"])

    def test_unlimited_status_uses_availability_and_unknown_never_writes_history(self):
        data = directory()
        data["products/hkpordmini"]["stock"] = None
        with patch.object(monitor, "fetch_isvoro_json", side_effect=lambda session, path, fetcher: data[path]), patch.object(monitor, "read_state", return_value={}), patch.object(monitor, "write_state"), patch.object(monitor, "write_status") as status:
            monitor.check_once(Mock(), {"products": [item()]}, None, None, "", "")
        self.assertEqual(status.call_args.args[1]["products"][0]["stock_mode"], "availability")
        del data["products/hkpordmini"]["stock"]
        with patch.object(monitor, "fetch_isvoro_json", side_effect=lambda session, path, fetcher: data[path]), patch.object(monitor, "read_state", return_value={}), patch.object(monitor, "write_state") as history, patch.object(monitor, "telegram_request", return_value={"ok": True, "result": {"message_id": 101}}) as send, self.assertRaises(monitor.MonitorError):
            monitor.check_once(Mock(), {"products": [item()]}, None, None, "mock-token", "123")
        history.assert_not_called()
        send.assert_not_called()

    def test_admin_catalog_endpoint_is_read_only_and_sanitized(self):
        with patch.object(admin, "authorized", return_value=True), patch.dict(os.environ, {"ADMIN_ALLOWED_HOSTS": "127.0.0.1"}):
            server = admin.ThreadingHTTPServer(("127.0.0.1", 0), admin.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.object(monitor, "fetch_isvoro_json", return_value=directory()["categories/HK-prod"]), patch.object(admin, "save_config") as save:
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
                    connection.request("GET", "/api/isvoro/category/HK-prod")
                    response = connection.getresponse()
                    payload = json.loads(response.read())
                    connection.close()
                self.assertEqual(response.status, 200)
                self.assertEqual(payload["category"]["products"][0], {"id": 188, "slug": "hkpordmini", "name": "HK PROD MiNi"})
                save.assert_not_called()
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
