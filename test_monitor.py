import unittest
from unittest.mock import Mock, patch
import requests

from admin import validate_config
from monitor import MonitorError, Product, RequestServiceError, SiteBlocked, fetch_html_stock, notification_text, check_once, inspect_product


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
        self.assertIn("HostWatch · 补货提醒", message)
        self.assertIn("ExampleHost · VPS 2 GB", message)
        self.assertIn('href="https://shop.example.com/order"', message)
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


class BrowserRequestTests(unittest.TestCase):
    def setUp(self):
        self.fetcher = {"mode": "flaresolverr", "flaresolverr_url": "http://127.0.0.1:8191/v1", "timeout_seconds": 60, "wait_seconds": 3}
        self.session = Mock()
        self.payload = {"status": "ok", "solution": {"status": 200, "url": "https://shop.example.com/item", "response": '<p class="stock">3</p>', "headers": {}, "cookies": [{"name": "clearance", "value": "secret"}], "userAgent": "browser"}}
        self.session.post.return_value = Mock(ok=True, status_code=200)
        self.session.post.return_value.json.side_effect = lambda: self.payload

    def test_generic_browser_parses_stock_and_sends_wait_without_leaking_cookies(self):
        result = fetch_html_stock(self.session, html_product(), self.fetcher)
        self.assertEqual(result.available, 3)
        self.session.post.assert_called_once_with("http://127.0.0.1:8191/v1", json={"cmd": "request.get", "url": "https://shop.example.com/item", "maxTimeout": 60000, "waitInSeconds": 3}, timeout=(10, 78))
        self.session.headers.update.assert_not_called()
        self.session.cookies.set.assert_not_called()

    def test_product_override_uses_http_even_with_global_browser(self):
        self.session.get.return_value = FakeResponse('<p class="stock">4</p>')
        result = fetch_html_stock(self.session, html_product(request_mode="requests"), self.fetcher)
        self.assertEqual(result.available, 4)
        self.session.post.assert_not_called()

    def test_product_override_enables_browser_with_global_http(self):
        result = fetch_html_stock(self.session, html_product(request_mode="flaresolverr"), {**self.fetcher, "mode": "requests"})
        self.assertEqual(result.available, 3)

    def test_browser_redirect_still_must_stay_on_expected_host(self):
        self.payload["solution"]["url"] = "https://different.example/item"
        with self.assertRaisesRegex(MonitorError, "重定向离开"):
            fetch_html_stock(self.session, html_product(), self.fetcher)

    def test_browser_service_failures_are_not_reported_as_site_blocks(self):
        for failure, label in [(requests.ConnectionError(), "无法连接"), (requests.Timeout(), "超时"), (ValueError(), "非 JSON")]:
            with self.subTest(label=label):
                self.session.post.side_effect = failure if not isinstance(failure, ValueError) else None
                self.session.post.return_value.json.side_effect = failure
                with self.assertRaisesRegex(RequestServiceError, label):
                    fetch_html_stock(self.session, html_product(), self.fetcher)

    def test_captcha_error_does_not_expose_server_response_secrets(self):
        self.payload = {"status": "error", "message": "Captcha detected at https://example.com/?token=secret"}
        self.session.post.return_value.ok = False
        self.session.post.return_value.status_code = 500
        with self.assertRaises(SiteBlocked) as caught:
            fetch_html_stock(self.session, html_product(), self.fetcher)
        self.assertNotIn("secret", str(caught.exception))

    def test_challenge_header_and_rate_limit_have_specific_messages(self):
        self.payload["solution"]["headers"] = {"CF-Mitigated": "challenge", "CF-Ray": "123abc-HKG"}
        with self.assertRaisesRegex(SiteBlocked, "安全验证页.*123abc-HKG"):
            fetch_html_stock(self.session, html_product(), self.fetcher)
        self.payload["solution"]["status"] = 429
        with self.assertRaisesRegex(SiteBlocked, "429（请求限流）"):
            fetch_html_stock(self.session, html_product(), self.fetcher)

    def test_injected_challenge_script_does_not_block_valid_product_html(self):
        self.payload["solution"]["response"] += '<script src="/cdn-cgi/challenge-platform/cf-chl-test.js"></script>'
        self.assertEqual(fetch_html_stock(self.session, html_product(), self.fetcher).available, 3)

    def test_malformed_solution_is_a_service_error(self):
        self.payload["solution"]["status"] = "200"
        with self.assertRaises(RequestServiceError):
            fetch_html_stock(self.session, html_product(), self.fetcher)

    def test_vmiss_groups_separate_requests_by_product_mode(self):
        items = [{"id": str(i), "adapter": "vmiss", "category_url": "https://app.vmiss.com/store/test", "name": "test", "provider": "VMISS", "selector": "p", "gid": "1", "enabled": True, "request_mode": mode} for i, mode in enumerate(["requests", "flaresolverr"])]
        product = Product("p", "p", 0, "https://app.vmiss.com/store/test/p")
        with patch("monitor.fetch_products", return_value=[product]) as fetch, patch("monitor.read_state", return_value={}), patch("monitor.write_state"), patch("monitor.write_status"):
            check_once(self.session, {"products": items, "fetcher": self.fetcher}, None, None, "", "")
        self.assertEqual([call.args[3]["mode"] for call in fetch.call_args_list], ["requests", "flaresolverr"])

    def test_service_failure_stops_without_systemd_retry_and_records_error(self):
        from monitor import main
        with patch("sys.argv", ["monitor.py", "--once"]), patch("monitor.load_dotenv"), patch("monitor.read_config", return_value={"enabled": True, "telegram": {"disabled": True}}), patch("monitor.effective_telegram", return_value=("", "")), patch("monitor.check_once", side_effect=RequestServiceError("browser offline")), patch("monitor.write_status") as status:
            self.assertEqual(main(), 0)
        self.assertEqual(status.call_args.args[1]["state"], "error")
        self.assertEqual(status.call_args.args[1]["message"], "browser offline")

    def test_single_vmiss_product_requires_unique_match(self):
        item = {"adapter": "vmiss", "category_url": "https://app.vmiss.com/store/test", "selector": "p", "request_mode": "requests"}
        with patch("monitor.fetch_products", return_value=[]) as fetch:
            with self.assertRaisesRegex(MonitorError, "无法唯一匹配"):
                inspect_product(self.session, item, self.fetcher)
        self.assertEqual(fetch.call_args.args[2], ["p"])
        self.assertEqual(fetch.call_args.args[3]["mode"], "requests")


class IntervalTests(unittest.TestCase):
    def test_accepts_zero_and_intervals_below_five_minutes(self):
        for seconds in [0, 1, 5, 10, 30, 60, 299, 86400]:
            with self.subTest(seconds=seconds):
                self.assertEqual(validate_config({"interval_seconds": seconds})["interval_seconds"], seconds)

    def test_rejects_negative_fractional_and_invalid_intervals(self):
        for value in [-1, 0.5, True, None, "", 86401]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_config({"interval_seconds": value})

    def test_monitor_waits_exact_configured_interval_including_zero(self):
        from monitor import main
        for seconds in [0, 1, 5]:
            with self.subTest(seconds=seconds), patch("sys.argv", ["monitor.py"]), patch("monitor.load_dotenv"), patch("monitor.read_config", side_effect=[{"enabled": True, "interval_seconds": seconds, "telegram": {"disabled": True}}, {"enabled": False}]), patch("monitor.effective_telegram", return_value=("", "")), patch("monitor.check_once") as check, patch("monitor.time.sleep") as sleep:
                self.assertEqual(main(), 0)
                check.assert_called_once()
                sleep.assert_called_once_with(seconds)


if __name__ == "__main__":
    unittest.main()
