"""VMISS publishes exact counts only for some Lagom product cards."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import admin
import catalog
import monitor


URL = "https://app.vmiss.com/store/cn-hong-kong-bgp"


def card(pid="55", name="CN.HK.BGP.Elite", quantity=None, attrs="", href=None):
    slug = name.rsplit(".", 1)[-1].lower()
    href = href if href is not None else URL + "/" + slug
    return (f'<div class="package" id="product{pid}"><h3 class="package-title">{name}</h3>'
            '<div class="package-content"><p>2 Cores<br>4096 MB<br>40GB SSD<br>150Mbps Port<br>1600GB Bandwidth<br>1 IPv4 Included</p></div>'
            '<div class="price-amount">$30.00 CAD</div><div class="price-cycle">Monthly</div>'
            f'<a class="btn btn-lg btn-primary btn-order-now {attrs}" href="{href}" id="product{pid}-order-button">Order Now</a>'
            + (f'<div class="package-qty">{quantity}</div>' if quantity is not None else '') + '</div>')


def page(*cards):
    return '<h1>CN - HongKong - BGP</h1><div class="section products">' + ''.join(cards) + '</div>'


class VMISSStockTests(unittest.TestCase):
    def test_region_is_normalized_for_display_and_notifications(self):
        self.assertEqual(monitor.normalize_vmiss_region("CN - HongKong - BGP"), "中国香港 · BGP")
        self.assertEqual(monitor.normalize_vmiss_region("JP - Tokyo - Softbank"), "日本东京 · Softbank")
        self.assertEqual(monitor.normalize_vmiss_region("New custom region"), "New custom region")

    def test_repeated_price_nodes_are_read_once(self):
        body = page(card()).replace(
            '<div class="price-amount">$30.00 CAD</div><div class="price-cycle">Monthly</div>',
            '<div class="price-amount">$30.00 CAD</div><div class="price-cycle">Monthly</div>'
            '<div class="price-amount">$30.00 CAD</div><div class="price-cycle">Monthly</div>')
        product = monitor.parse_products(body, URL, ["product55"])[0]
        self.assertEqual(product.price, "$30.00 CAD / Monthly")

    def test_real_page_shape_supports_mixed_exact_and_unpublished_stock(self):
        body = page(card("50", "CN.HK.BGP.Basic", "0 Available", "disabled"),
                    card("53", "CN.HK.BGP.Core", "1,234 Available"),
                    card(), card("56", "CN.HK.BGP.Ultra"))
        products = monitor.parse_products(body, URL, [])
        self.assertEqual([(p.available, p.stock_mode) for p in products],
                         [(0, "quantity"), (1234, "quantity"), (1, "availability"), (1, "availability")])
        self.assertEqual(products[-2].url, URL + "/elite")
        self.assertEqual(products[-2].specs, "2 Cores · 4096 MB · 40GB SSD · 1 IPv4 Included")
        self.assertEqual(products[-2].traffic, "150Mbps Port · 1600GB Bandwidth")
        # Existing selectors and IDs still refer to the same product.
        self.assertEqual(monitor.parse_products(body, URL, ["product55"]), [products[-2]])

    def test_unpublished_stock_respects_all_disabled_button_forms(self):
        for attrs in ('disabled', '" disabled="disabled', '" aria-disabled="true'):
            with self.subTest(attrs=attrs):
                product = monitor.parse_products(page(card(attrs=attrs)), URL, ["product55"])[0]
                self.assertEqual((product.available, product.stock_mode), (0, "availability"))
        product = monitor.parse_products(page(card(quantity="0 Available")), URL, ["product55"])[0]
        self.assertEqual(product.available, 0)  # Explicit zero always wins.

    def test_invalid_or_conflicting_quantity_never_falls_back_to_button_availability(self):
        for qty, attrs in [("", ""), ("Unknown", ""), ("--", ""), ("1,,2 Available", ""), ("3 Available", "disabled")]:
            with self.subTest(qty=qty, attrs=attrs), self.assertRaises(monitor.MonitorError):
                monitor.parse_products(page(card(quantity=qty, attrs=attrs)), URL, [])

    def test_missing_or_invalid_order_link_cannot_report_available(self):
        for href in ("", "#", "?a=add", "javascript:void(0)", "https://elsewhere.example/order", "https://user:pass@app.vmiss.com/store/test/elite", URL, "/"):
            with self.subTest(href=href), self.assertRaises(monitor.MonitorError):
                monitor.parse_products(page(card(href=href)), URL, [])
        body = page(card()).replace('class="btn btn-lg btn-primary btn-order-now "', 'class="other"')
        with self.assertRaises(monitor.MonitorError):
            monitor.parse_products(body, URL, [])

    def test_catalog_preserves_availability_mode_and_purchase_url(self):
        navigation = '<a href="/store/cn-hong-kong-bgp">CN - HongKong - BGP</a>'
        with patch.object(monitor, "fetch_page", side_effect=[(navigation, URL), (page(card()), URL)]):
            row = catalog.vmiss_catalog(Mock(), "")[2][0]
        self.assertEqual((row["available"], row["stock_mode"], row["error"]), (1, "availability", ""))
        self.assertEqual(row["purchase_url"], URL + "/elite")
        self.assertEqual(row["watch"]["pid"], "55")

    def test_monitor_updates_all_plans_and_sends_restock_without_fake_quantity(self):
        products = [{"id": pid, "adapter": "vmiss", "provider": "VMISS", "name": name,
                     "category_url": URL, "selector": "product" + pid, "pid": pid, "enabled": True}
                    for pid, name in [("50", "CN.HK.BGP.Basic"), ("55", "CN.HK.BGP.Elite"), ("56", "CN.HK.BGP.Ultra")]]
        config = admin.validate_config({"enabled": True, "products": products})
        with tempfile.TemporaryDirectory() as folder:
            state, status = Path(folder) / "state.json", Path(folder) / "status.json"
            monitor.write_state(state, {p["id"]: {"available": 0, "notify_enabled": True} for p in products})
            body = page(card("50", "CN.HK.BGP.Basic", "0 Available", "disabled"), card(), card("56", "CN.HK.BGP.Ultra"))
            with patch.object(monitor, "fetch_page", return_value=(body, URL)) as fetch, patch.object(monitor, "telegram_request", return_value={"ok": True, "result": {"message_id": 101}}) as send:
                monitor.check_once(Mock(), config, state, status, "token", "chat")
            fetch.assert_called_once()
            result = json.loads(status.read_text(encoding="utf-8"))
            self.assertEqual(result["state"], "ok")
            self.assertEqual([p["available"] for p in result["products"]], [0, 1, 1])
            self.assertEqual(result["products"][1]["stock_mode"], "availability")
            self.assertEqual(send.call_count, 2)
            for call in send.call_args_list:
                self.assertIn("有货", call.kwargs["text"])
                self.assertNotIn("剩余 1 台", call.kwargs["text"])
                self.assertNotIn("未知", call.kwargs["text"])


if __name__ == "__main__":
    unittest.main()
