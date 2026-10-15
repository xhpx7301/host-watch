"""DMIT fixture contracts; real inventory is never inferred from HTTP failures."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import admin
import catalog
import dmit
import monitor
from affiliates import affiliate_purchase_url


def card(pid="183", name="LAX.Pro.WEE", stock="0 Available", order="", extra=""):
    return f'''<div class="product" id="product{pid}">
      <h3 class="product-title">{name}</h3><div class="qty">{stock}</div>
      <ul><li>1 vCPU</li><li>1024 MB Memory</li><li>20 GB NVMe SSD</li>
      <li>1 TB Transfer</li><li>1 Gbps</li></ul>
      <div class="price">$39.90 / year</div>{order}{extra}</div>'''


def buy(pid="183", label="Order Now", disabled=False):
    return f'<a href="/cart.php?a=add&amp;pid={pid}" {"disabled" if disabled else ""}>{label}</a>'


def item(pid="183", **extra):
    return {"id": "dmit-" + pid, "adapter": "dmit", "provider": "DMIT", "name": "DMIT Watch", "pid": pid,
            "category_url": dmit.PRICING_URL, "enabled": True, **extra}


def pricing_card(sold=False, pid="253", loc="lax", hw="as3", net="premium", name="TINY", onclick=None):
    # Reduced from the public pricing HTML read via cloudscraper on 2026-10-16.
    action = '<button class="btn-order btn-order-disabled" disabled type="button">Out of Stock</button>' if sold else f'''<button class="btn-order" onclick="{onclick or "if (!window.__cfRLUnblockHandlers) return false; window.location.href='/cart.php?a=add&pid=" + pid + "'"}">Order Now</button>'''
    return f'''<div class="plan-group" data-loc="{loc}" data-hw="{hw}" data-net="{net}" style="display:none">
      <div class="plan-cards"><div class="plan-card"><div class="plan-card-name">{name}</div>
      <div class="plan-card-specs"><div class="plan-spec"><i class="fas fa-microchip"></i>1 vCore</div>
      <div class="plan-spec"><i class="fas fa-memory"></i>2GB</div>
      <div class="plan-spec"><i class="fas fa-hdd"></i>20GB SSD</div>
      <div class="plan-spec"><i class="fas fa-exchange-alt"></i>1000GB</div>
      <div class="plan-spec"><i class="fas fa-bolt"></i>1Gbps</div></div>
      <div class="plan-card-price">$ 10.90<span>/Monthly</span></div>
      <div class="plan-card-action">{action}</div></div></div></div>'''


class DMITCurrentPricingTests(unittest.TestCase):
    def test_current_theme_parses_regions_platforms_lines_and_icon_metadata(self):
        body = pricing_card() + pricing_card(sold=True, hw="an4") + pricing_card(pid="259", net="eyeball") + pricing_card(pid="270", loc="tyo", net="tier1")
        rows = dmit.parse_cards(body, dmit.PRICING_URL)
        self.assertEqual([r["available"] for r in rows], [1, 0, 1, 1])
        self.assertEqual(rows[0]["plan_key"], "LAX:AS3:PREMIUM:TINY")
        self.assertEqual(rows[0]["name"], "LAX.AS3.Pro.TINY")
        self.assertEqual(rows[0]["specs"], "CPU：1 核 · 内存：2GB · 硬盘：20GB SSD")
        self.assertEqual(rows[0]["traffic"], "流量：1000GB · 带宽：1Gbps")
        self.assertEqual(rows[1]["pid"], "")
        self.assertEqual(rows[1]["plan_key"], "LAX:AN4:PREMIUM:TINY")
        self.assertEqual(rows[3]["region"], "日本 · 东京")

    def test_onclick_is_never_executed_and_unknown_actions_remain_unknown(self):
        for action in ["fetch('https://evil.example/')", "window.location.href='https://evil.example/cart.php?a=add&pid=253'", "window.location.href='/cart.php?a=add&pid=253'; extraCode()"]:
            with self.subTest(action=action):
                row = dmit.parse_cards(pricing_card(onclick=action), dmit.PRICING_URL)[0]
                self.assertIsNone(row["available"])
                self.assertEqual(row["pid"], "")
        with self.assertRaisesRegex(monitor.MonitorError, "重复"):
            dmit.parse_cards(pricing_card() + pricing_card(), dmit.PRICING_URL)

    def test_sold_out_without_pid_is_trackable_and_restocks_to_exact_pid_aff(self):
        with patch("monitor.fetch_page", return_value=(pricing_card(sold=True), dmit.PRICING_URL)):
            row = catalog.load_catalog("dmit")["rows"][0]
        config = admin.validate_config({"enabled": True, "products": [{**row["watch"], "id": "notify", "watch_enabled": False, "notify_enabled": True}]})
        self.assertEqual(config["products"][0]["pid"], "")
        self.assertEqual(config["products"][0]["plan_key"], "LAX:AS3:PREMIUM:TINY")
        with tempfile.TemporaryDirectory() as temp, patch("monitor.fetch_page") as fetch, patch("monitor.telegram_request") as send:
            state, status = Path(temp) / "state.json", Path(temp) / "status.json"
            fetch.return_value = (pricing_card(sold=True), dmit.PRICING_URL)
            monitor.check_once(Mock(), config, state, status, "token", "123")
            send.assert_not_called()
            fetch.return_value = (pricing_card(), dmit.PRICING_URL)
            monitor.check_once(Mock(), config, state, status, "token", "123")
            send.assert_called_once()
            link = send.call_args.kwargs["reply_markup"]["inline_keyboard"][0][0]["url"]
            self.assertEqual(parse_qs(urlsplit(link).query), {"aff": ["22544"], "pid": ["253"]})
            monitor.check_once(Mock(), config, state, status, "token", "123")
            self.assertEqual(send.call_count, 1)

    def test_known_pid_survives_sold_out_but_cannot_silently_switch_product(self):
        watched = item("253", plan_key="LAX:AS3:PREMIUM:TINY")
        for body, expected in [(pricing_card(sold=True), 0), (pricing_card(), 1)]:
            with patch("monitor.fetch_page", return_value=(body, dmit.PRICING_URL)):
                self.assertEqual(monitor.inspect_product(Mock(), watched, {"mode": "cloudscraper"}).available, expected)
        with patch("monitor.fetch_page", return_value=(pricing_card(pid="999"), dmit.PRICING_URL)), self.assertRaisesRegex(monitor.MonitorError, "PID.*不一致"):
            monitor.inspect_product(Mock(), watched, {})


class DMITParsingTests(unittest.TestCase):
    def test_quantity_and_clean_metadata(self):
        row = dmit.parse_cards(card(stock="1,234 Available", order=buy()), dmit.PRICING_URL)[0]
        self.assertEqual(row["available"], 1234)
        self.assertEqual(row["stock_mode"], "quantity")
        self.assertEqual(row["region"], "美国 · 洛杉矶")
        self.assertEqual(row["line"], "Pro")
        self.assertEqual(row["specs"], "CPU：1 核 · 内存：1024 MB · 硬盘：20 GB")
        self.assertEqual(row["traffic"], "流量：1 TB · 带宽：1 Gbps")
        self.assertEqual(row["price"], "$39.90 / year")

    def test_availability_sold_out_and_unknown_are_distinct(self):
        for stock, button, expected in [("", buy(), None), ("In Stock", buy(), 1), ("Out of Stock", buy(label="Out of Stock", disabled=True), 0), ("Sold Out", "", 0), ("0 Available", "", 0), ("", "", None), ("--", buy(), None), ("1,,2 Available", buy(), None), ("0 Available", buy(), None), ("Sold Out", buy(), None), ("3 Available", buy(label="Sold Out", disabled=True), None)]:
            with self.subTest(stock=stock, button=button):
                row = dmit.parse_cards(card(stock=stock, order=button), dmit.PRICING_URL)[0]
                self.assertEqual(row["available"], expected)
                self.assertEqual(bool(row["error"]), expected is None)

    def test_custom_pricing_wrapper_and_nested_package(self):
        body = '<div class="row"><div class="col"><h3>HKG.EB.TINY</h3><span class="stock">In Stock</span><ul><li>1 vCPU</li><li>Memory: 512 MB</li><li>Disk: 10 GB</li><li>Transfer: unlimited</li></ul>' + buy("228") + '</div></div>'
        row = dmit.parse_cards(body, dmit.PRICING_URL)[0]
        self.assertEqual(row["pid"], "228")
        self.assertEqual(row["region"], "中国香港")
        self.assertIn("内存：512 MB", row["specs"])
        self.assertIn("流量：无限制", row["traffic"])
        body = '<div class="product" id="product183"><div class="package">' + card() + '</div></div>'
        self.assertEqual(len(dmit.parse_cards(body, dmit.PRICING_URL)), 1)

    def test_duplicate_pid_is_rejected_and_same_title_different_pid_is_allowed(self):
        with self.assertRaisesRegex(monitor.MonitorError, "重复"):
            dmit.parse_cards(card() + card(), dmit.PRICING_URL)
        self.assertEqual(len(dmit.parse_cards(card() + card("228"), dmit.PRICING_URL)), 2)

    def test_external_and_malformed_order_links_cannot_indicate_in_stock(self):
        for href in ["https://evil.example/cart.php?a=add&pid=183", "http://www.dmit.io/cart.php?a=add&pid=183", "https://user@www.dmit.io/cart.php?a=add&pid=183", "https://www.dmit.io:8000/cart.php?a=add&pid=183", "https://www.dmit.io:invalid/cart.php?a=add&pid=183", "/cart.php?a=add&pid=183&pid=228"]:
            with self.subTest(href=href):
                row = dmit.parse_cards(card(stock="", order=f'<a href="{href}">Order Now</a>'), dmit.PRICING_URL)[0]
                self.assertIsNone(row["available"])

    def test_challenge_and_login_pages_never_create_inventory(self):
        with self.assertRaises(monitor.SiteBlocked):
            dmit.parse_cards('<title>Just a moment...</title>Enable JavaScript and cookies to continue', dmit.PRICING_URL)
        self.assertEqual(dmit.parse_cards('<h1>Login</h1><a href="cart.php?a=add&pid=183">Cart</a>', dmit.PRICING_URL), [])

    def test_category_url_rejects_cart_actions_and_credentials(self):
        for url in ["https://www.dmit.io/cart.php?a=add&pid=183", "https://www.dmit.io/cart.php?a=checkout", "https://www.dmit.io/cart.php?gid=1&gid=2", "https://www.dmit.io/cart.php", "https://www.dmit.io:9000/pages/pricing", "https://user@www.dmit.io/pages/pricing", "https://dmit.io/pages/pricing", "https://www.dmit.io/pages/pricing#fake", "https://www.dmit.io/store/../../admin"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                dmit.normalize_catalog_url(url)
        self.assertEqual(dmit.normalize_catalog_url("https://www.dmit.io/cart.php?gid=2&currency=1"), "https://www.dmit.io/cart.php?currency=1&gid=2")
        self.assertEqual(dmit.normalize_catalog_url("https://www.dmit.io/store/lax-pro"), "https://www.dmit.io/store/lax-pro")

    def test_request_rejects_redirect_to_wrong_category(self):
        with patch("monitor.fetch_page", return_value=(card(), "https://www.dmit.io/cart.php?gid=2")), self.assertRaisesRegex(monitor.MonitorError, "不一致"):
            dmit.fetch_catalog(Mock(), dmit.PRICING_URL, {"mode": "requests"})


class DMITIntegrationTests(unittest.TestCase):
    def test_config_defaults_migration_and_explicit_blank_aff(self):
        config = admin.validate_config({"merchant_order": ["akile"], "affiliates": {"vmiss": ""}, "products": [item()]})
        self.assertEqual(config["affiliates"]["dmit"], "22544")
        self.assertEqual(config["merchant_order"][-1], "dmit")
        self.assertEqual(config["affiliates"]["vmiss"], "")
        self.assertEqual(admin.validate_config({**config, "affiliates": {"dmit": ""}})["affiliates"]["dmit"], "")
        self.assertEqual(admin.validate_config({"affiliates": {"dmit": "https://www.dmit.io/aff.php?aff=22544"}})["affiliates"]["dmit"], "22544")
        for value in [item(pid="abc"), item(category_url="https://www.dmit.io/cart.php?a=add&pid=183")]:
            with self.assertRaises(ValueError):
                admin.validate_config({"products": [value]})
        with self.assertRaises(ValueError):
            admin.validate_config({"affiliates": {"dmit": "abc"}})

    def test_affiliate_links_keep_pid_and_use_current_code(self):
        raw = "https://www.dmit.io/cart.php?a=add&pid=183&aff=old"
        for code in ["22544", "9988", ""]:
            result = affiliate_purchase_url(raw, "dmit", {"affiliates": {"dmit": code}}, item())
            query = parse_qs(urlsplit(result).query)
            self.assertEqual(query["pid"], ["183"])
            if code:
                self.assertEqual(urlsplit(result).path, "/aff.php")
                self.assertEqual(query["aff"], [code])
            else:
                self.assertNotIn("aff", query)
                self.assertEqual(urlsplit(result).path, "/cart.php")
        external = "https://elsewhere.example/cart.php?a=add&pid=183"
        self.assertEqual(affiliate_purchase_url(external, "dmit", admin.default_config(), item()), external)

    def test_directory_can_join_watch_or_notify_and_unknown_rows_keep_metadata(self):
        body = card() + card("228", "TYO.EB.TINY", stock="In Stock", order=buy("228")) + card("229", "HKG.Pro.TINY", stock="待确认")
        fetcher = admin.normalize_fetcher({"provider_modes": {"dmit": "flaresolverr"}})
        with patch("monitor.fetch_page", return_value=(body, dmit.PRICING_URL)) as fetch:
            data = catalog.load_catalog("dmit", "", fetcher)
        self.assertEqual(fetch.call_args.args[2]["mode"], "flaresolverr")
        self.assertEqual(data["scope"], "all")
        self.assertEqual(data["unknown"], 1)
        self.assertIn("TYO", [s["id"] for s in data["scopes"]])
        row = data["rows"][2]
        self.assertIsNone(row["available"])
        self.assertIn("1 Gbps", row["traffic"])
        self.assertIn("pid=229", row["purchase_url"])
        for role in ("watch", "notify"):
            config = admin.validate_config({"products": [{**row["watch"], "id": "new", "watch_enabled": role == "watch", "notify_enabled": role == "notify"}]})
            self.assertTrue(monitor.product_membership(config["products"][0], role))
        with patch("monitor.fetch_page", return_value=(body, dmit.PRICING_URL)):
            filtered = catalog.load_catalog("dmit", "TYO")
        self.assertEqual([r["watch"]["pid"] for r in filtered["rows"]], ["228"])

    def test_directory_discovers_public_categories_without_fetching_order_links(self):
        nav = '<a href="/cart.php?gid=2">Tokyo</a><a href="/cart.php?a=add&pid=183">Order</a><a href="https://evil.example/store/no">Ignore</a>'
        url = "https://www.dmit.io/cart.php?gid=2"
        with patch("monitor.fetch_page", side_effect=[(nav, dmit.PRICING_URL), (card(), url)]) as fetch:
            data = catalog.load_catalog("dmit")
        self.assertEqual(data["scope"], "gid:2")
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(data["rows"][0]["watch"]["category_url"], url)
        for call in fetch.call_args_list:
            self.assertNotIn("a=add", call.args[1])

    def test_inspect_matches_pid_and_applies_unsaved_policy(self):
        body = card(stock="2 Available") + card("228", name="LAX.Pro.WEE", stock="8 Available")
        fetcher = admin.normalize_fetcher({"provider_modes": {"dmit": "flaresolverr"}})
        with patch("monitor.fetch_page", return_value=(body, dmit.PRICING_URL)) as fetch:
            product = monitor.inspect_product(Mock(), item("228"), fetcher)
        self.assertEqual(product.available, 8)
        self.assertEqual(product.vendor, "DMIT")
        self.assertEqual(fetch.call_args.args[2]["mode"], "flaresolverr")

    def test_batch_reads_once_and_only_notifies_subscribed_restock(self):
        products = [item(watch_enabled=True, notify_enabled=False), item("228", watch_enabled=False, notify_enabled=True)]
        config = admin.validate_config({"products": products, "enabled": True})
        with tempfile.TemporaryDirectory() as temp, patch("monitor.telegram_request") as send, patch("monitor.fetch_page") as fetch:
            state, status = Path(temp) / "state.json", Path(temp) / "status.json"
            fetch.return_value = (card() + card("228"), dmit.PRICING_URL)
            monitor.check_once(Mock(), config, state, status, "token", "123")
            self.assertEqual(fetch.call_count, 1)
            send.assert_not_called()
            fetch.return_value = (card(stock="5 Available") + card("228", stock="2 Available"), dmit.PRICING_URL)
            monitor.check_once(Mock(), config, state, status, "token", "123")
            self.assertEqual(fetch.call_count, 2)
            send.assert_called_once()
            message = send.call_args.kwargs
            self.assertIn("DMIT", message["text"])
            url = message["reply_markup"]["inline_keyboard"][0][0]["url"]
            self.assertEqual(parse_qs(urlsplit(url).query), {"aff": ["22544"], "pid": ["228"]})
            rows = json.loads(status.read_text(encoding="utf-8"))["products"]
            self.assertTrue(all(row["specs"] and row["traffic"] and row["price"] for row in rows))
            monitor.check_once(Mock(), config, state, status, "token", "123")
            self.assertEqual(send.call_count, 1)

    def test_unlisted_and_blocked_preserve_history_and_do_not_notify(self):
        config = admin.validate_config({"products": [item()], "enabled": True})
        with tempfile.TemporaryDirectory() as temp, patch("monitor.telegram_request") as send, patch("monitor.fetch_page") as fetch:
            state, status = Path(temp) / "state.json", Path(temp) / "status.json"
            fetch.return_value = (card(), dmit.PRICING_URL)
            monitor.check_once(Mock(), config, state, status, "token", "123")
            baseline = state.read_bytes()
            fetch.return_value = (card("228"), dmit.PRICING_URL)
            monitor.check_once(Mock(), config, state, status, "token", "123")
            self.assertEqual(json.loads(state.read_text())["products"]["dmit-183"]["available"], 0)
            self.assertIsNone(json.loads(status.read_text(encoding="utf-8"))["products"][0]["available"])
            baseline = state.read_bytes()
            fetch.side_effect = monitor.SiteBlocked("HTTP 403")
            with self.assertRaises(monitor.SiteBlocked):
                monitor.check_once(Mock(), config, state, status, "token", "123")
            self.assertEqual(state.read_bytes(), baseline)
            send.assert_not_called()


class DMITCatalogBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "catalog.json"
        self.fetcher = admin.normalize_fetcher({"provider_modes": {"dmit": "flaresolverr"}})
        self.service = catalog.CatalogService(lambda: self.path, lambda: self.fetcher)
        self.task = {"provider": "dmit", "scope": "", "cancelled": False}

    def test_browser_lock_memory_named_session_and_cleanup(self):
        with patch("monitor.browser_request_slot") as slot, patch("monitor.check_browser_memory") as memory, patch.object(self.service, "worker", side_effect=['{"ok":true}', '{"ok":true,"data":{}}', '{"ok":true}']) as worker:
            self.assertTrue(json.loads(self.service.fetch_result(self.task))["ok"])
        slot.assert_called_once()
        memory.assert_called_once()
        payloads = [call.args[2] for call in worker.call_args_list]
        self.assertEqual(payloads[0]["command"], "create")
        self.assertEqual(payloads[2]["command"], "destroy")
        self.assertEqual(payloads[1]["fetcher"]["_test_browser_session"], payloads[0]["session"])
        self.assertTrue(payloads[1]["fetcher"]["_browser_slot_held"])
        self.assertFalse(worker.call_args_list[2].kwargs["cancellable"])

    def test_low_memory_never_launches_browser(self):
        with patch("monitor.browser_request_slot"), patch("monitor.check_browser_memory", side_effect=monitor.RequestServiceError("内存不足")), patch.object(self.service, "worker") as worker, self.assertRaisesRegex(catalog.CatalogFailure, "内存不足"):
            self.service.fetch_result(self.task)
        worker.assert_not_called()

    def test_cancel_after_creation_still_destroys_browser_and_keeps_snapshot(self):
        self.service.task = self.task
        self.task.update(pending=True, error="")
        self.path.write_text('{"previous":{}}', encoding="utf-8")
        def worker(path, mode, payload, *args, **kwargs):
            if payload.get("command") == "create":
                return '{"ok":true}'
            if mode == "--worker":
                self.task["cancelled"] = True
                return '{}'
            return '{"ok":true}'
        with patch("monitor.browser_request_slot"), patch("monitor.check_browser_memory"), patch.object(self.service, "worker", side_effect=worker) as run:
            self.service.run(self.task)
        self.assertEqual(run.call_args_list[-1].args[2]["command"], "destroy")
        self.assertFalse(self.task["pending"])
        self.assertEqual(self.path.read_text(), '{"previous":{}}')

    def test_failed_cleanup_is_visible_even_after_stop(self):
        with patch("monitor.browser_request_slot"), patch("monitor.check_browser_memory"), patch.object(self.service, "worker", side_effect=['{"ok":true}', '{"ok":true,"data":{}}', '{"ok":false}']):
            self.service.fetch_result(self.task)
        self.assertIn("浏览器", self.task["error"])


if __name__ == "__main__":
    unittest.main()
