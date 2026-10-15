import unittest

from admin import validate_config
from monitor import MonitorError, Product, SiteBlocked, fetch_html_stock, notification_text


class FakeResponse:
    def __init__(self, body, url="https://shop.example.com/item", status_code=200):
        self.text = body
        self.url = url
        self.status_code = status_code


class FakeSession:
    def __init__(self, response):
        self.response = response

    def get(self, url, **kwargs):
        return self.response


def html_product(**overrides):
    item = {
        "id": "vps-2g",
        "adapter": "html",
        "provider": "ExampleHost",
        "name": "VPS 2 GB",
        "enabled": True,
        "url": "https://shop.example.com/item",
        "stock_selector": ".stock",
        "stock_mode": "quantity",
        "stock_pattern": r"([\d,]+)",
        "available_pattern": r"(?i)in stock|available|有货",
        "unavailable_pattern": r"(?i)out of stock|sold out|缺货|售罄",
        "purchase_url": "",
    }
    item.update(overrides)
    return item


class GenericAdapterTests(unittest.TestCase):
    def test_reads_quantity_from_unique_stock_element(self):
        product = fetch_html_stock(FakeSession(FakeResponse('<p class="stock">库存 1,200 台</p>')), html_product())
        self.assertEqual(product.available, 1200)
        self.assertEqual(product.vendor, "ExampleHost")

    def test_availability_prefers_unavailable_match(self):
        item = html_product(stock_mode="availability")
        product = fetch_html_stock(FakeSession(FakeResponse('<p class="stock">Out of stock</p>')), item)
        self.assertEqual(product.available, 0)

    def test_rejects_ambiguous_stock_selector(self):
        session = FakeSession(FakeResponse('<p class="stock">1</p><p class="stock">2</p>'))
        with self.assertRaisesRegex(MonitorError, "应精确匹配一个"):
            fetch_html_stock(session, html_product())

    def test_rejects_redirect_to_another_host(self):
        response = FakeResponse('<p class="stock">2</p>', url="https://elsewhere.example/item")
        with self.assertRaises(MonitorError):
            fetch_html_stock(FakeSession(response), html_product())

    def test_rejects_unknown_stock_text(self):
        item = html_product(stock_mode="availability")
        with self.assertRaisesRegex(MonitorError, "未匹配"):
            fetch_html_stock(FakeSession(FakeResponse('<p class="stock">Ask us</p>')), item)

    def test_manual_notification_names_vendor_and_link(self):
        product = Product("key", "VPS 2 GB", 1, "https://shop.example.com/item", vendor="ExampleHost", stock_mode="availability")
        message = notification_text(product, 0, "https://shop.example.com/order")
        self.assertIn("ExampleHost 检测到库存变化", message)
        self.assertIn("手动购买：https://shop.example.com/order", message)
        self.assertIn("由缺货变为有货", message)

    def test_accepts_generic_and_legacy_vmiss_configs(self):
        generic = validate_config({"products": [html_product()]})
        self.assertEqual(generic["products"][0]["adapter"], "html")

        legacy = validate_config({
            "products": [{
                "id": "legacy-vmiss",
                "name": "Legacy VMISS",
                "category_url": "https://app.vmiss.com/store/example",
                "selector": "product44",
                "gid": "12",
            }]
        })
        self.assertEqual(legacy["products"][0]["adapter"], "vmiss")

    def test_rejects_private_address_for_generic_monitoring(self):
        with self.assertRaisesRegex(ValueError, "内网或保留 IP"):
            validate_config({"products": [html_product(url="https://127.0.0.1/item")]})


if __name__ == "__main__":
    unittest.main()
