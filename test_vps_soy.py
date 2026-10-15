import unittest
from unittest.mock import Mock, patch

from admin import validate_config
from monitor import MonitorError, check_once, inspect_product, parse_vps_soy
from notifications import hardware_text, network_text


URL = "https://www.vps.soy/cart"


def item(name="深港IX-MINI", **overrides):
    return {"id": name, "name": name, "adapter": "vps_soy", "provider": "VPS.SOY", "url": URL, "selector": name, "enabled": True, "request_mode": "requests", **overrides}


def card(name="深港IX-MINI", stock="-3", href="javascript:void(0)", sold_out=True):
    # Minimal fixture matching the public Yunyoo cart structure observed on
    # 2026-10-15: name in h4, separate description and inventory, disabled link.
    image = '<img src="/themes/cart/yunyoo/assets/img/saleout.svg">' if sold_out else ""
    return f'<div class="card cartitem"><div class="card-body"><h4>{name}</h4><div class="card-text">CPU: 1 RAM: 512 MB</div>{image}</div><div><p class="card-text">库存： {stock}</p><a class="cart-num">4.50</a></div><div class="card-footer"><a href="{href}">立即购买</a></div></div>'


class VPSSoyTests(unittest.TestCase):
    def test_hardware_fields_exclude_ports_architecture_and_store_notices(self):
        for cpu, memory, disk, flow, hardware, network in [
            (1, "512 MB", "10 GB", "500 GB", "1C/0.5G/10GB", "500GB@1Gbps"),
            (2, "1 GB", "20 GB", "1 TB", "2C/1G/20GB", "1TB@1Gbps"),
            (1, "512 MB", "5 GB", "200 GB", "1C/0.5G/5GB", "200GB@1Gbps"),
        ]:
            description = f"CPU：{cpu} 核<br>内存：{memory}<br>硬盘：{disk}<br>带宽：1 Gbps<br>流量：{flow}<br>端口：500 个<br>架构：KVM<br>网络：CNIX<br><br>禁止一切违规滥用行为！！！"
            page = card().replace("CPU: 1 RAM: 512 MB", description)
            with self.subTest(memory=memory, disk=disk):
                product = parse_vps_soy(page, URL, "深港IX-MINI")
                self.assertEqual(hardware_text(product.specs), hardware)
                self.assertEqual(network_text(product.traffic), network)
                for excluded in ["端口", "500 个", "KVM", "CNIX", "禁止"]:
                    self.assertNotIn(excluded, product.specs + product.traffic)
                self.assertEqual(product.available, 0)

    def test_partial_configuration_does_not_invent_hardware_or_bandwidth(self):
        product = parse_vps_soy(card(), URL, "深港IX-MINI")
        self.assertEqual(hardware_text(product.specs), "1C/0.5G")
        self.assertEqual(product.traffic, "")
        description = "架构：KVM 端口：500 个 网络：CNIX 年付赠送 1 CPU 2 GB 内存"
        product = parse_vps_soy(card().replace("CPU: 1 RAM: 512 MB", description), URL, "深港IX-MINI")
        self.assertEqual(product.specs, "")

    def test_negative_stock_is_sold_out_and_uses_catalog_entry(self):
        page = card("深港IX-PRO") + card() + card("深港IX-MINI-PLUS", "8", "/cart?action=configureproduct&pid=99", False)
        product = parse_vps_soy(page, URL, "深港IX-MINI")
        self.assertEqual(product.available, 0)
        self.assertEqual(product.name, "深港IX-MINI")
        self.assertEqual(product.vendor, "VPS.SOY")
        self.assertEqual(product.url, URL)

    def test_positive_stock_uses_safe_product_link_without_affiliate(self):
        product = parse_vps_soy(card(stock="1,200", href="/cart?action=configureproduct&pid=42", sold_out=False), URL, "深港IX-MINI")
        self.assertEqual(product.available, 1200)
        self.assertEqual(product.url, "https://www.vps.soy/cart?action=configureproduct&pid=42")

    def test_missing_and_duplicate_titles_do_not_report_sold_out(self):
        for page in [card("深港IX-PRO"), card() + card()]:
            with self.subTest(page=page), self.assertRaisesRegex(MonitorError, "无法唯一匹配"):
                parse_vps_soy(page, URL, "深港IX-MINI")

    def test_missing_malformed_or_ambiguous_stock_is_error(self):
        for page in [card(stock="未知"), card(stock="1,,000"), card().replace("库存：", "余量："), card().replace('</p>', '</p><p class="card-text">库存：1</p>')]:
            with self.subTest(page=page), self.assertRaises(MonitorError):
                parse_vps_soy(page, URL, "深港IX-MINI")

    def test_positive_stock_conflicts_with_sold_out_or_disabled_link(self):
        for page in [card(stock="3"), card(stock="3", sold_out=False), card(stock="3", sold_out=False, href="/cart?pid=42").replace('<a href=', '<a disabled href=')]:
            with self.subTest(page=page), self.assertRaisesRegex(MonitorError, "冲突|不可用"):
                parse_vps_soy(page, URL, "深港IX-MINI")

    def test_external_or_unsafe_purchase_link_is_rejected(self):
        for link in ["https://evil.example/cart", "http://www.vps.soy/cart", "https://www.vps.soy:8443/cart", "https://user:secret@www.vps.soy/cart"]:
            with self.subTest(link=link), self.assertRaises(MonitorError):
                parse_vps_soy(card(stock="2", href=link, sold_out=False), URL, "深港IX-MINI")

    def test_config_needs_no_css_selector_or_affiliate(self):
        config = validate_config({"enabled": True, "products": [item()], "affiliates": {"vmiss": "", "vps_soy": ""}})
        self.assertEqual(config["products"][0]["selector"], "深港IX-MINI")
        self.assertEqual(config["affiliate_id"], "")
        for url in ["https://evil.example/cart", "https://www.vps.soy/login", "http://www.vps.soy/cart", "https://www.vps.soy:8443/cart"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_config({"products": [item(url=url)]})

    def test_category_url_preserves_groups_and_normalizes_browser_hostname(self):
        for url in ["https://vps.soy/cart?gid=2&fid=1", URL + "?fid=1&gid=2"]:
            config = validate_config({"products": [item(url=url)]})
            self.assertEqual(config["products"][0]["url"], URL + "?fid=1&gid=2")
        for query in ["gid=0", "gid=-2", "gid=", "gid=2&gid=8", "fid=1&fid=2", "fid=hello", "gid=2abc", "gid=12345678901", "action=configureproduct&pid=1", "pid=1"]:
            with self.subTest(query=query), self.assertRaises(ValueError):
                validate_config({"products": [item(url=URL + "?" + query)]})

    def test_grouped_single_test_and_sold_out_entry_keep_region(self):
        url = URL + "?fid=1&gid=2"
        with patch("monitor.fetch_page", return_value=(card("日本软银", "0"), url)) as fetch:
            result = inspect_product(Mock(), item("日本软银", url=url), {})
        self.assertEqual(fetch.call_args.args[1], url)
        self.assertEqual(result.url, url)
        self.assertEqual(result.catalog_url, url)
        self.assertEqual(result.available, 0)
        with self.assertRaisesRegex(MonitorError, "FID=1、GID=2.*本页套餐：日本软银"):
            parse_vps_soy(card("日本软银", "0"), url, "深港IX-MINI")

    def test_redirect_cannot_silently_drop_or_change_requested_group(self):
        for final in [URL, URL + "?fid=1&gid=8", URL + "?fid=2&gid=2", URL + "?fid=1&gid=2&gid=8"]:
            with self.subTest(final=final), patch("monitor.fetch_page", return_value=(card(), final)), self.assertRaisesRegex(MonitorError, "FID/GID.*不一致"):
                inspect_product(Mock(), item(url=URL + "?fid=1&gid=2"), {})

    def test_distinct_groups_never_share_stock_but_same_group_shares_fetch(self):
        products = [item("MINI", id="one", url=URL + "?fid=1&gid=2"),
                    item("MINI", id="two", url=URL + "?fid=1&gid=8"),
                    item("PRO", id="three", url=URL + "?gid=2&fid=1")]
        def page(session, url, *args):
            if "gid=2" in url:
                return card("MINI", "0") + card("PRO", "0"), url
            return card("MINI", "5", "/cart?pid=8", False), url
        with patch("monitor.fetch_page", side_effect=page) as fetch, patch("monitor.read_state", return_value={}), patch("monitor.write_state"), patch("monitor.write_status") as status:
            check_once(Mock(), {"products": products}, None, None, "", "")
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual([p["available"] for p in status.call_args.args[1]["products"]], [0, 5, 0])

    def test_single_test_uses_product_request_override(self):
        with patch("monitor.fetch_page", return_value=(card(), URL)) as fetch:
            result = inspect_product(Mock(), item(), {"mode": "flaresolverr"})
        self.assertEqual(result.available, 0)
        self.assertEqual(fetch.call_args.args[2]["mode"], "requests")

    def test_same_page_is_fetched_once_and_restock_notification_has_correct_vendor(self):
        products = [item(), item("深港IX-PRO")]
        page = card(stock="2", href="/cart?pid=42", sold_out=False) + card("深港IX-PRO")
        with patch("monitor.fetch_page", return_value=(page, URL)) as fetch, patch("monitor.read_state", return_value={"深港IX-MINI": {"available": 0}}), patch("monitor.write_state"), patch("monitor.write_status") as status, patch("monitor.telegram_request") as notify:
            check_once(Mock(), {"products": products}, None, None, "bot", "chat")
        fetch.assert_called_once()
        notify.assert_called_once()
        self.assertIn("VPS.SOY", notify.call_args.kwargs["text"])
        self.assertIn("https://www.vps.soy/cart?pid=42", notify.call_args.kwargs["text"])
        self.assertNotIn("VMISS", notify.call_args.kwargs["reply_markup"]["inline_keyboard"][0][0]["text"])
        self.assertEqual(status.call_args.args[1]["products"][0]["available"], 2)


if __name__ == "__main__":
    unittest.main()
