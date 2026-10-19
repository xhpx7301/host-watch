import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup

import admin
import monitor
from notifications import hardware_text, network_text, price_text, purchase_button, stock_message


class NotificationTests(unittest.TestCase):
    def message(self, **overrides):
        return stock_message(**{"vendor": "Isvoro", "name": "HK CN2 Core", "available": 3, "stock_mode": "quantity",
            "region": "中国香港", "specs": "2 核 · 2048 MB · 30 GB", "traffic": "300 GB流量 · 100 Mbps",
            "price": "CNY 25 / 月；CNY 249 / 年", "line": "HK CN2", "old_count": 0,
            "checked_at": "2026-10-15 23:00:00 CST", "purchase_url": "https://isvoro.com/buy/HKCN2?product=149&aff=example", **overrides})

    def test_rich_restock_details_and_aff_link(self):
        message = self.message(item={"remarks": "自用", "line_telecom": "CN2GT", "line_unicom": "4837", "line_mobile": "CMI"})
        soup = BeautifulSoup(message, "html.parser")
        text = soup.get_text()
        for value in ["补货提醒", "Isvoro · HK CN2 Core", "中国香港", "HK CN2", "自用", "电：CN2GT | 联：4837 | 移：CMI",
                      "2C/2G/30GB", "300GB@100Mbps", "¥25 / 月；¥249 / 年", "3 台", "由缺货变为有货"]:
            self.assertIn(value, text)
        self.assertIn("2026-10-15 23:00:00 北京时间", text)
        self.assertIsNone(soup.code)
        self.assertIn("🛒 购买链接：点击直达购买", soup.get_text())
        self.assertEqual(soup.a["href"], "https://isvoro.com/buy/HKCN2?product=149&aff=example")
        self.assertEqual({tag.name for tag in soup.find_all()}, {"b", "a"})

    def test_coupon_remains_copyable_while_hardware_is_plain_text(self):
        soup = BeautifulSoup(self.message(coupon_code="SAVE-2026"), "html.parser")
        self.assertEqual([tag.get_text() for tag in soup.find_all("code")], ["SAVE-2026"])
        self.assertNotIn("2C/2G/30GB", [tag.get_text() for tag in soup.find_all("code")])

    def test_unlimited_stock_does_not_invent_quantity_and_tests_are_explicit(self):
        self.assertIn("库存：有货（首次发现有货）", BeautifulSoup(self.message(stock_mode="availability", old_count=None), "html.parser").get_text())
        self.assertNotIn("台", BeautifulSoup(self.message(stock_mode="availability", old_count=None), "html.parser").get_text())
        self.assertIn("增加 2 台", self.message(old_count=1))
        for available, expected in [(3, "3 台"), (0, "缺货")]:
            text = BeautifulSoup(self.message(test=True, available=available, duration=0.5), "html.parser").get_text()
            self.assertIn("商品单次测试", text)
            self.assertIn(expected, text)
            self.assertNotIn("补货提醒", text)
            self.assertNotIn("增加", text)

    def test_external_fields_are_escaped_and_message_fits_telegram(self):
        injection = '<a href="https://evil.example">&bad</a>'
        message = self.message(name=injection, region=injection, line=injection, specs=injection, price=injection,
                               item={"remarks": injection, "line_telecom": injection})
        soup = BeautifulSoup(message, "html.parser")
        self.assertEqual(len(soup.find_all("a")), 1)
        self.assertIn(injection, soup.get_text())
        large = "😀" * 1000
        text = BeautifulSoup(self.message(vendor=large, name=large, region=large, specs=large, traffic=large, price=large,
            line=large, item={key: large for key in ["remarks", "line_telecom", "line_unicom", "line_mobile"]}, test=True, warning=large), "html.parser").get_text()
        self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 4096)
        for url in ["javascript:alert(1)", "https://user:pass@example.com", "not-a-url", "https://[invalid"]:
            self.assertEqual(purchase_button(url), {})
            self.assertIsNone(BeautifulSoup(self.message(purchase_url=url), "html.parser").a)

    def test_purchase_button_allows_order_action_label(self):
        button = purchase_button("https://akile.ai/console/finance/order/202610180001", "💳 立即付款")
        self.assertEqual(button["inline_keyboard"][0][0]["text"], "💳 立即付款")

    def test_formatters_keep_capacity_currency_and_network_units(self):
        for raw, expected in [("1 核 · 512 MB · 10 GB", "1C/0.5G/10GB"), ("4 核 · 4096 MB · 20 GB", "4C/4G/20GB"),
                              ("2 核 · 2 GB · 30 GB SSD", "2C/2G/30GB SSD"), ("CPU：1 核 · 内存：1 GB · 硬盘：10 GB", "1C/1G/10GB")]:
            self.assertEqual(hardware_text(raw), expected)
        for raw, expected in [("300 GB流量 · 100 Mbps", "300GB@100Mbps"), ("无限制 · 500 Mbps", "无限制@500Mbps"),
                              ("带宽：1 Gbps · 流量：1 TB", "1TB@1Gbps")]:
            self.assertEqual(network_text(raw), expected)
        for raw, expected in [("CNY 25 / 月", "¥25 / 月"), ("$5.00 CAD / Monthly", "CA$5.00 / 月\n折合 ¥25.5/月"),
                              ("USD 5", "$5\n折合 ¥36.0"), ("EUR 6.95 / 月", "€6.95 / 月\n折合 ¥54.2/月")]:
            self.assertEqual(price_text(raw), expected)
        self.assertEqual(price_text("CA$12.00 / 月 / CA$12.00 / 月"), "CA$12.00 / 月\n折合 ¥61.2/月")

    def test_manual_test_uses_live_metadata_and_current_aff_without_sending(self):
        config = admin.default_config()
        config["affiliates"]["isvoro"] = "updated-aff"
        result = {"ok": True, "name": "Live plan", "available": 2, "specs": "2 核 · 2048 MB · 30 GB", "traffic": "300 GB流量 · 100 Mbps",
            "price": "CNY 25 / 月", "region": "中国香港", "checked_at": "2026-10-15", "duration_seconds": 0.5,
            "purchase_url": "https://isvoro.com/buy/HKCN2?product=149&aff=old", "matched_product": {"category_name": "HK CN2", "category_id": "46"}}
        with patch.object(admin, "telegram_credentials", return_value=("mock-token", "123")), patch.object(admin, "current_config", return_value=config), patch.object(admin, "telegram_call") as send:
            response = admin.send_stock_test_result({"name": "Saved plan", "provider": "Isvoro", "adapter": "isvoro"}, result)
        self.assertEqual(response["status"], "sent")
        args = send.call_args.kwargs
        self.assertEqual(args["parse_mode"], "HTML")
        self.assertIn("Live plan", args["text"])
        self.assertIn("2C/2G/30GB", args["text"])
        self.assertTrue(args["reply_markup"]["inline_keyboard"][0][0]["url"].endswith("aff=updated-aff"))

    def test_none_match_details_legacy_product_is_supported(self):
        message = monitor.notification_text(monitor.Product("x", "Legacy", 1, "https://example.com"), 0, "https://example.com")
        self.assertIn("Legacy", message)

    def test_narwhal_notification_includes_instance_and_plan(self):
        product = monitor.Product("plan", "Mini", 1, "https://example.com", vendor="独角鲸云",
                                  node_name="HKT商宽", region="香港")
        message = monitor.notification_text(product, 0, product.url)
        text = BeautifulSoup(message, "html.parser").get_text()
        self.assertIn("独角鲸云 · HKT商宽 · Mini", text)

    def test_narwhal_notification_includes_region_host_expiry_and_all_configs(self):
        machine_id = "17141ff1-5623-4455-a7a5-91a0146f090e"
        selected_id = "fe7edd6f-a84c-43fc-a41a-6221b33410ff"
        plans = [
            {"id": selected_id, "machine_id": machine_id, "name": "中杯", "machine_name": "东京实例", "machine_region": "JP",
             "cpu": 1, "ram_mb": 128, "disk_gb": 1, "bandwidth_mbps": 100, "monthly_traffic_gb": 400,
             "price_monthly": 1, "status": "active", "machine_status": "online", "remaining": 2, "sold_out": False,
             "machine_expires_at": 1794960000},
            {"id": "ce7edd6f-a84c-43fc-a41a-6221b33410ff", "machine_id": machine_id, "name": "Mini", "machine_name": "东京实例", "machine_region": "JP",
             "cpu": 1, "ram_mb": 128, "disk_gb": 1, "bandwidth_mbps": 100, "monthly_traffic_gb": 400,
             "price_monthly": 0.5, "status": "active", "machine_status": "online", "remaining": 0, "sold_out": False,
             "machine_expires_at": 1794960000},
        ]
        product = monitor.parse_narwhal(plans, {"machine_id": machine_id, "plan_id": selected_id})
        message = monitor.notification_text(product, 0, product.url)
        text = BeautifulSoup(message, "html.parser").get_text()
        for value in ("🇯🇵 日本", "母鸡名称：东京实例", "母鸡到期时间：2026-11-18", "中杯 · $1/月 [剩 2 台]", "Mini · $0.5/月 [剩 1 台]"):
            self.assertIn(value, text)


if __name__ == "__main__":
    unittest.main()
