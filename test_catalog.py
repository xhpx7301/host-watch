import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import admin
import catalog
import monitor
from test_akile import catalog as akile_fixture
from test_isvoro import directory


class CatalogParsingTests(unittest.TestCase):
    def test_akile_rows_keep_exact_plan_identity_and_unknown_stock(self):
        payload = akile_fixture()
        node = payload["data"]["areas"][0]["nodes"][0]
        node["plans"][0].update(cpu=1, memory=1024, disk=10, price_datas=[{"price": 2499, "cycle": 1}])
        node["plans"][1]["stock"] = "4"
        node["plans"][2]["is_pre_sale"] = True
        payload["data"]["areas"][0]["nodes"].append({"id": 90, "group_name": "Empty", "plans": None})
        with patch("monitor.fetch_akile_catalog", return_value=payload):
            scopes, scope, rows = catalog.akile_catalog(Mock(), "")
        self.assertEqual(scope, "all")
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["available"], 0)
        self.assertEqual(rows[0]["watch"]["area_mode"], "manual")
        self.assertEqual(rows[0]["watch"]["area_id"], "6")
        self.assertEqual(rows[0]["price"], "¥24.99 / 1个月")
        self.assertIsNone(rows[1]["available"])
        self.assertIsNone(rows[2]["available"])
        for i, row in enumerate(rows):
            admin.validate_config({"products": [{**row["watch"], "id": str(i)}]})

    def test_isvoro_uses_detail_ip_stock_and_keeps_hierarchy(self):
        fixtures = directory()
        fixtures["categories/HK-prod"]["products"] = fixtures["categories/HK-prod"]["products"][:1]
        fixtures["products/hkpordmini"].update(ip_available=False, settings=[{"key": "cores", "value": "2"}])
        with patch("monitor.fetch_isvoro_json", side_effect=lambda session, path, fetcher: fixtures[path]):
            scopes, scope, rows = catalog.isvoro_catalog(Mock(), "")
        self.assertEqual(scope, "24:27")
        self.assertEqual(rows[0]["available"], 0)
        self.assertEqual(rows[0]["watch"]["product_id"], "188")
        self.assertEqual(rows[0]["watch"]["type_id"], "24")
        self.assertIn("2 核", rows[0]["specs"])

    def test_network_metadata_survives_unknown_stock_in_both_catalogs(self):
        payload = akile_fixture()
        payload["data"]["areas"][0]["nodes"][0]["plans"][0].update(stock="unknown", flow=0, bandwidth=500)
        with patch("monitor.fetch_akile_catalog", return_value=payload):
            row = catalog.akile_catalog(Mock(), "")[2][0]
        self.assertIsNone(row["available"])
        self.assertEqual(row["traffic"], "无限制 · 500 Mbps")
        fixtures = directory()
        fixtures["categories/HK-prod"]["products"] = fixtures["categories/HK-prod"]["products"][:1]
        fixtures["products/hkpordmini"].update(stock="unknown", settings={"bandwidth_limit": "300", "ratelimit": "12.5"})
        with patch("monitor.fetch_isvoro_json", side_effect=lambda session, path, fetcher: fixtures[path]):
            row = catalog.isvoro_catalog(Mock(), "")[2][0]
        self.assertIsNone(row["available"])
        self.assertEqual(row["traffic"], "300 GB流量 · 100 Mbps")

    def test_isvoro_block_stops_further_detail_requests(self):
        fixtures = directory()
        calls = []
        def fetch(session, path, fetcher):
            calls.append(path)
            if path.startswith("products/"):
                raise monitor.SiteBlocked("HTTP 429")
            return fixtures[path]
        with patch("monitor.fetch_isvoro_json", side_effect=fetch), self.assertRaises(monitor.SiteBlocked):
            catalog.isvoro_catalog(Mock(), "")
        self.assertEqual(sum(p.startswith("products/") for p in calls), 1)

    def test_isvoro_hk_bgp_all_plans_expose_actual_bandwidth(self):
        fixtures = directory()
        fixtures["categories"][0]["children"].append({"id": 47, "slug": "hkbgp", "name": "HK BGP"})
        products = []
        expected = []
        # Public HK BGP settings: traffic is GB; ratelimit is MB/s.
        for identifier, name, flow, rate, mbps in [(159, "Core", "500", "37.5", 300),
                (160, "Standard", "1024", "62.5", 500), (161, "Pro", "2048", "125", 1000)]:
            slug = "hkbgp" + name.lower()
            products.append({"id": identifier, "name": "HK BGP " + name, "slug": slug, "category_id": 47})
            fixtures["products/" + slug] = {**products[-1], "stock": 22, "ip_available": True,
                "plans": [{"name": "月", "prices": [{"currency_code": "CNY", "price": 29}]}],
                "settings": [{"key": "bandwidth_limit", "value": flow}, {"key": "ratelimit", "value": rate},
                    {"key": "overlimit_speed_mbps", "value": "0.125"}]}
            expected.append(f"{flow} GB流量 · {mbps} Mbps")
        fixtures["categories/hkbgp"] = {"id": 47, "slug": "hkbgp", "name": "HK BGP", "products": products}
        with patch("monitor.fetch_isvoro_json", side_effect=lambda session, path, fetcher: fixtures[path]):
            data = catalog.load_catalog("isvoro", "24:47")
        self.assertEqual([row["traffic"] for row in data["rows"]], expected)
        self.assertEqual(data["metadata_version"], catalog.METADATA_VERSION)
        self.assertTrue(all(row["available"] == 22 for row in data["rows"]))

    def test_vmiss_generates_pid_from_card_and_rejects_wrong_scope_redirect(self):
        navigation = '<a href="/store/test">Hong Kong</a><a href="https://elsewhere.example/store/evil">Ignore</a>'
        card = '<div class="section products"><div class="package" id="product44"><h3 class="package-title">Mini</h3><div class="package-qty">3 Available</div><a class="btn-order-now" href="/store/test/mini">Buy</a><div class="price-amount">$5 CAD</div><div class="price-cycle">Monthly</div></div></div>'
        with patch("monitor.fetch_page", side_effect=[(navigation, "https://app.vmiss.com/store"), (card, "https://app.vmiss.com/store/test")]):
            scopes, scope, rows = catalog.vmiss_catalog(Mock(), "")
        self.assertEqual([scope["id"] for scope in scopes], ["all", "test"])
        self.assertEqual(rows[0]["watch"]["pid"], "44")
        self.assertEqual(rows[0]["available"], 3)
        self.assertEqual(rows[0]["watch"]["selector"], "product44")
        with patch("monitor.fetch_page", side_effect=[(navigation, "https://app.vmiss.com/store"), (card, "https://app.vmiss.com/store/other")]), self.assertRaisesRegex(monitor.MonitorError, "不一致"):
            catalog.vmiss_catalog(Mock(), "")

    def test_vmiss_separates_inline_resources_even_when_stock_is_unknown(self):
        navigation = '<a href="/store/hk-bgp">CN - HongKong - BGP</a>'
        resources = ["1 Core", "1024MB", "10GB SSD", "100Mbps Port", "300GB Bandwidth", "1 IPv4 Included"]
        for content in (" ".join(resources), "<ul>" + "".join("<li>" + value + "</li>" for value in resources) + "</ul>"):
            for quantity in ("0 Available", "Unknown"):
                with self.subTest(content=content, quantity=quantity):
                    card = ('<div class="section products"><div class="package" id="product44"><h3 class="package-title">CN.HK.BGP.Basic</h3>'
                            '<div class="package-content">' + content + '</div><div class="package-qty">' + quantity + '</div>'
                            '<a class="btn-order-now" href="/store/hk-bgp/basic">Buy</a><div class="price-amount">CA$5.00</div><div class="price-cycle">Monthly</div></div></div>')
                    with patch("monitor.fetch_page", side_effect=[(navigation, "https://app.vmiss.com/store"), (card, "https://app.vmiss.com/store/hk-bgp")]):
                        row = catalog.vmiss_catalog(Mock(), "")[2][0]
                    self.assertEqual(row["specs"], "1 Core · 1024MB · 10GB SSD · 1 IPv4 Included")
                    self.assertEqual(row["traffic"], "100Mbps Port · 300GB Bandwidth")
                    self.assertEqual(row["price"], "CA$5.00 / Monthly")
                    self.assertEqual(row["available"], 0 if quantity.startswith("0") else None)
                    self.assertEqual(row["watch"]["pid"], "44")

    def test_vmiss_load_catalog_reuses_cloudscraper_session_for_discovery_and_scope(self):
        navigation = '<a href="/store/test">Hong Kong</a>'
        card = '<div class="section products"><div class="package" id="product44"><h3 class="package-title">Mini</h3><div class="package-qty">3 Available</div><a class="btn-order-now" href="/store/test/mini">Buy</a></div></div>'
        fetchers = []

        def fetch_page(_session, _url, fetcher, _host):
            fetchers.append(fetcher)
            return (navigation, "https://app.vmiss.com/store") if len(fetchers) == 1 else (card, "https://app.vmiss.com/store/test")

        scraper = Mock()
        with patch("cloudscraper.create_scraper", return_value=scraper), patch("monitor.fetch_page", side_effect=fetch_page):
            catalog.load_catalog("vmiss", fetcher={"mode": "cloudscraper"})
        self.assertEqual(len(fetchers), 2)
        self.assertIs(fetchers[0]["_cloudscraper_session"], fetchers[1]["_cloudscraper_session"])
        scraper.close.assert_called_once_with()

    def test_soy_discovery_does_not_accept_arbitrary_onclick(self):
        navigation = '<div class="secondgroup_item" onclick="window.location.href=\'/cart?fid=1&amp;gid=8\'">深港 CNIX</div><div class="secondgroup_item" onclick="fetch(\'https://bad.example/\')">Ignore</div>'
        card = '<div class="cartitem"><div class="card-body"><h4>Mini</h4><div class="card-text">CPU 1</div></div><p class="card-text">库存：-3</p><div class="card-footer"><a href="javascript:void(0)">Buy</a></div></div>'
        with patch("monitor.fetch_vps_soy_page", side_effect=[(navigation, "https://www.vps.soy/cart"), (navigation + card, "https://www.vps.soy/cart?fid=1&gid=8")]):
            scopes, scope, rows = catalog.soy_catalog(Mock(), "")
        self.assertEqual([scope["id"] for scope in scopes], ["all", "1:8"])
        self.assertEqual(scope, "1:8")
        self.assertEqual(rows[0]["available"], 0)
        self.assertEqual(rows[0]["watch"]["selector"], "Mini")

    def test_soy_unknown_stock_still_has_clean_configuration_and_network(self):
        from test_vps_soy import card
        navigation = '<div class="secondgroup_item" onclick="window.location.href=\'/cart?fid=1&amp;gid=8\'">深港 CNIX</div>'
        body = navigation + card(stock="未知").replace("CPU: 1 RAM: 512 MB", "CPU：1 核<br>内存：512 MB<br>硬盘：5 GB<br>带宽：1 Gbps<br>流量：200 GB<br>端口：500 个<br>架构：KVM<br>网络：CNIX<br>禁止滥用")
        url = "https://www.vps.soy/cart?fid=1&gid=8"
        with patch("monitor.fetch_vps_soy_page", side_effect=[(navigation, url), (body, url)]):
            row = catalog.soy_catalog(Mock(), "1:8")[2][0]
        self.assertIsNone(row["available"])
        self.assertEqual(row["specs"], "CPU：1 核 · 内存：512 MB · 硬盘：5 GB")
        self.assertEqual(row["traffic"], "流量：200 GB · 带宽：1 Gbps")

    def test_vmiss_watch_without_affiliate_uses_direct_link(self):
        item = {"id": "one", "adapter": "vmiss", "provider": "VMISS", "name": "Mini", "selector": "product44", "category_url": "https://app.vmiss.com/store/test", "pid": "44", "enabled": True}
        config = admin.validate_config({"enabled": True, "products": [item], "affiliates": {"vmiss": ""}})
        product = monitor.Product("mini", "Mini", 1, "https://app.vmiss.com/store/test/mini")
        self.assertEqual(monitor.purchase_link(product, item, config), product.url)
        self.assertIn("aff=123", monitor.purchase_link(product, item, {**config, "affiliates": {"vmiss": "123"}}))

    def test_selected_catalog_product_sends_restock_then_edits_quantity_and_aff(self):
        payload = akile_fixture()
        with patch("monitor.fetch_akile_catalog", return_value=payload):
            row = catalog.akile_catalog(Mock(), "")[2][0]
        config = admin.validate_config({"enabled": True, "products": [{**row["watch"], "id": "watch-mini"}]})
        with tempfile.TemporaryDirectory() as temp, patch("monitor.fetch_akile_catalog", return_value=payload), patch("monitor.telegram_request", return_value={"ok": True, "result": {"message_id": 101}}) as send:
            state, status = Path(temp) / "state.json", Path(temp) / "status.json"
            monitor.check_once(Mock(), config, state, status, "token", "123")
            send.assert_not_called()
            payload["data"]["areas"][0]["nodes"][0]["plans"][0]["stock"] = 2
            monitor.check_once(Mock(), config, state, status, "token", "123")
            send.assert_called_once()
            self.assertEqual(send.call_args.args[2], "sendMessage")
            self.assertIn("HKL-TW-Mini", send.call_args.kwargs["text"])
            link = send.call_args.kwargs["reply_markup"]["inline_keyboard"][0][0]["url"]
            self.assertIn("planId=1047", link)
            self.assertIn("aff_code=" + config["affiliates"]["akile"], link)
            snapshot = json.loads(status.read_text(encoding="utf-8"))["products"][0]
            self.assertEqual(snapshot["purchase_url"], link)
            self.assertNotIn("aff_code", snapshot["url"])
            monitor.check_once(Mock(), config, state, status, "token", "123")
            self.assertEqual(send.call_count, 1)
            config["affiliates"]["akile"] = "updated-code"
            payload["data"]["areas"][0]["nodes"][0]["plans"][0]["stock"] = 3
            monitor.check_once(Mock(), config, state, status, "token", "123")
            self.assertEqual(send.call_count, 2)
            self.assertEqual(send.call_args.args[2], "editMessageText")
            self.assertEqual(send.call_args.kwargs["message_id"], 101)
            self.assertIn("aff_code=updated-code", send.call_args.kwargs["text"])


class CatalogServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "catalog.json"
        self.service = catalog.CatalogService(lambda: self.path)
        self.value = {"provider": "akile", "scope": "all", "scopes": [], "rows": [], "fetched_at": "2026-10-15T20:00:00+08:00", "unknown": 0}

    def wait(self):
        deadline = time.monotonic() + 3
        while self.service.task["pending"] and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.service.task["pending"])

    def test_refresh_caches_atomically_and_failure_preserves_snapshot(self):
        process = Mock()
        process.communicate.return_value = (json.dumps({"ok": True, "data": self.value}), None)
        process.poll.return_value = 0
        with patch("catalog.subprocess.Popen", return_value=process):
            self.service.refresh("akile")
            self.wait()
        self.assertEqual(self.service.snapshot("akile")["data"], self.value)
        process.communicate.return_value = (json.dumps({"ok": False, "error": "HTTP 429"}), None)
        with patch("catalog.subprocess.Popen", return_value=process):
            self.service.refresh("akile")
            self.wait()
        self.assertIn("429", self.service.task["error"])
        self.assertEqual(self.service.snapshot("akile")["data"], self.value)

    def test_akile_region_snapshots_are_derived_from_one_full_catalog(self):
        full = {**self.value, "scopes": [{"id": "all", "name": "全部地区"}, {"id": "6", "name": "香港"}, {"id": "3", "name": "台湾"}],
            "rows": [
                {"key": "hk", "available": 2, "watch": {"area_id": "6"}},
                {"key": "tw", "available": None, "watch": {"area_id": "3"}},
            ]}
        self.path.write_text(json.dumps({"akile:all": full}), encoding="utf-8")
        hong_kong = self.service.snapshot("akile", "6")["data"]
        taiwan = self.service.snapshot("akile", "3")["data"]
        self.assertEqual([row["key"] for row in hong_kong["rows"]], ["hk"])
        self.assertEqual(hong_kong["scope"], "6")
        self.assertEqual(hong_kong["unknown"], 0)
        self.assertEqual([row["key"] for row in taiwan["rows"]], ["tw"])
        self.assertEqual(taiwan["unknown"], 1)
        self.assertEqual(self.service.snapshot("akile", "999")["data"], None)
        self.assertEqual(self.service.snapshot("akile")["data"], full)

    def test_non_akile_categories_are_derived_from_full_catalog_when_mapped(self):
        full = {**self.value, "provider": "vps_soy", "scope": "all",
                "scopes": [{"id": "all", "name": "全部公开套餐"}, {"id": "1:8", "name": "深港"}],
                "rows": [{"key": "mini", "available": 1, "watch": {"catalog_scope": "1:8"}}]}
        self.path.write_text(json.dumps({"vps_soy:all": full}), encoding="utf-8")
        scoped = self.service.snapshot("vps_soy", "1:8")["data"]
        self.assertEqual(scoped["scope"], "1:8")
        self.assertEqual([row["key"] for row in scoped["rows"]], ["mini"])

    def test_akile_region_refresh_fetches_and_caches_all_areas(self):
        full = {**self.value, "scopes": [{"id": "all", "name": "全部地区"}, {"id": "6", "name": "香港"}], "rows": [{"key": "hk", "watch": {"area_id": "6"}}]}
        with patch.object(self.service, "fetch_result", return_value=json.dumps({"ok": True, "data": full})) as fetch:
            task = self.service.refresh("akile", "6")
            self.wait()
        self.assertEqual(task["scope"], "6")
        self.assertEqual(fetch.call_args.args[0]["fetch_scope"], "all")
        self.assertEqual(self.service.snapshot("akile", "6")["data"]["rows"], full["rows"])

    def test_stop_is_responsive_and_does_not_publish_cancelled_results(self):
        entered, released = threading.Event(), threading.Event()
        process = Mock()
        def communicate(*args, **kwargs):
            entered.set()
            released.wait(2)
            return json.dumps({"ok": True, "data": self.value}), None
        process.communicate.side_effect = communicate
        process.kill.side_effect = released.set
        process.poll.return_value = 0
        with patch("catalog.subprocess.Popen", return_value=process):
            self.service.refresh("akile")
            self.assertTrue(entered.wait(1))
            with self.assertRaises(catalog.CatalogBusy):
                self.service.refresh("isvoro")
            self.assertTrue(self.service.stop()["cancelled"])
            self.wait()
        self.assertFalse(self.path.exists())
        process.kill.assert_called()

    def test_old_isvoro_snapshot_requests_refresh_without_losing_old_data(self):
        old = {**self.value, "provider": "isvoro", "scope": "24:47", "rows": [{"traffic": "500 GB流量"}]}
        updated = {**old, "scope": "24:46", "metadata_version": catalog.METADATA_VERSION}
        self.path.write_text(json.dumps({"old": old, "updated": updated, "akile": self.value}), encoding="utf-8")
        result = self.service.snapshot("isvoro", "24:47")
        self.assertEqual(result["data"], old)
        self.assertTrue(result["needs_refresh"])
        self.assertFalse(self.service.snapshot("isvoro", "24:46")["needs_refresh"])
        self.assertFalse(self.service.snapshot("akile")["needs_refresh"])
        self.assertFalse(self.service.snapshot("isvoro", "24:56")["needs_refresh"])
        # A failed or stopped refresh must leave the old snapshot available.
        self.service.task = {"provider": "isvoro", "pending": False, "cancelled": True}
        self.assertEqual(self.service.snapshot("isvoro", "24:47")["data"], old)

    def test_validation_rejects_urls_and_unknown_providers(self):
        for provider, scope in [("other", ""), ("isvoro", "../../etc"), ("vmiss", "https://bad.example"), ("akile", [])]:
            with self.subTest(provider=provider), self.assertRaises(ValueError):
                self.service.refresh(provider, scope)


class CatalogAPITests(unittest.TestCase):
    def setUp(self):
        self.patches = [patch.object(admin, "authorized", return_value=True), patch.dict(os.environ, {"ADMIN_ALLOWED_HOSTS": "127.0.0.1"})]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.server = admin.ThreadingHTTPServer(("127.0.0.1", 0), admin.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, payload=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        self.addCleanup(connection.close)
        connection.request(method, path, json.dumps(payload) if payload is not None else None, headers or {"Content-Type": "application/json"})
        response = connection.getresponse()
        return response.status, response.read()

    def test_catalog_read_refresh_stop_and_script_serving(self):
        with patch.object(admin.CATALOG, "snapshot", return_value={"data": None, "task": None}) as snapshot:
            code, body = self.request("GET", "/api/catalog?provider=vps_soy&scope=1%3A8")
            self.assertEqual(code, 200)
            snapshot.assert_called_once_with("vps_soy", "1:8")
        with patch.object(admin.CATALOG, "refresh", return_value={"pending": True}) as refresh:
            code, body = self.request("POST", "/api/catalog/refresh", {"provider": "akile"})
            self.assertEqual(code, 200)
            refresh.assert_called_once_with("akile", "")
        with patch.object(admin.CATALOG, "stop", return_value={"pending": False}) as stop:
            code, _ = self.request("POST", "/api/catalog/stop", {})
            self.assertEqual(code, 200)
            stop.assert_called_once()
        code, body = self.request("GET", "/catalog.js")
        self.assertEqual(code, 200)
        self.assertIn(b"catalog-refresh", body)

    def test_cross_site_refresh_rejected_and_invalid_source_is_400(self):
        with patch.object(admin.CATALOG, "refresh") as refresh:
            code, _ = self.request("POST", "/api/catalog/refresh", {"provider": "akile"}, {"Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"})
            self.assertEqual(code, 403)
            refresh.assert_not_called()
        code, _ = self.request("GET", "/api/catalog?provider=not-supported")
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
