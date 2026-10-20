import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import admin


NOW = 1792382400.0


def listing(**changes):
    row = {
        "id": 901,
        "price": 12,
        "currency_code": "CNY",
        "status": "active",
        "service": {"product": {"name": "HK Mini"}, "billing_period": 1, "billing_unit": "month"},
        "server_info": {
            "hostname": "hk-901", "location_name": "中国香港", "plan_name": "HK Mini",
            "node": "HK-A", "renewal_price": 30, "billing_period": 1,
            "billing_unit": "month", "expires_at": "2026-11-17T00:00:00Z",
            "spec": {"cores": 2, "memory_mb": 2048, "disk_gb": 40, "bandwidth_mbps": 500, "traffic_gb": 1000},
        },
    }
    row.update(changes)
    return row


class IsvoroMarketTests(unittest.TestCase):
    def test_valuation_uses_nested_server_info(self):
        item = admin.isvoro_market_valuation(listing(), NOW)
        self.assertEqual(item["id"], "901")
        self.assertEqual(item["region"], "中国香港")
        self.assertEqual(item["node"], "HK-A")
        self.assertAlmostEqual(item["fair_value"], 30 * item["remaining_days"] / 30)
        self.assertAlmostEqual(item["delta"], item["fair_value"] - 12)
        self.assertAlmostEqual(item["rate"], item["delta"] / 30 * 100)

    def test_valuation_rejects_missing_or_expired_values(self):
        for change in [{"price": None}, {"server_info": {**listing()["server_info"], "expires_at": "bad"}}]:
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    admin.isvoro_market_valuation(listing(**change), NOW)
        self.assertIsNotNone(admin.isvoro_market_valuation(listing(server_info={**listing()["server_info"], "renewal_price": 0}), NOW)["valuation_error"])
        self.assertEqual(admin.isvoro_market_valuation(listing(server_info={**listing()["server_info"], "billing_period": 2}), NOW)["valuation_error"], "未知续费周期")

    def test_list_reads_meta_pagination_and_rejects_short_page(self):
        session = Mock()
        session.get.side_effect = [
            Mock(status_code=200, json=Mock(return_value={"code": 0, "data": [listing()], "meta": {"total": 2}})),
            Mock(status_code=200, json=Mock(return_value={"code": 0, "data": [listing(id=902)], "meta": {"total": 2}})),
        ]
        self.assertEqual([row["id"] for row in admin.isvoro_market_list(session, page_size=1)], [901, 902])
        self.assertEqual([call.kwargs["params"]["page"] for call in session.get.call_args_list], [1, 2])
        session.get.side_effect = [Mock(status_code=200, json=Mock(return_value={"code": 0, "data": [], "meta": {"total": 1}}))]
        with self.assertRaises(ValueError):
            admin.isvoro_market_list(session)

    def test_config_requires_isvoro_account_when_enabled_and_hides_password(self):
        value = admin.default_config()
        value["vps_seckill"]["accounts"] = [{"id": "iv", "name": "Isvoro", "provider": "isvoro",
            "username": "user@example.com", "password": "secret"}]
        value["isvoro_market"] = {"enabled": True, "interval_seconds": 60, "account_id": "iv", "items": [{
            "id": "rule", "target": "listing", "product_id": "901", "direction": "discount",
            "rate_threshold": 10, "amount_threshold": None, "enabled": True,
        }]}
        normalized = admin.validate_config(value)
        self.assertEqual(normalized["isvoro_market"]["account_id"], "iv")
        public = admin.public_config(normalized)
        self.assertNotIn("secret", json.dumps(public))

    def test_rule_match_and_alert_deduplication(self):
        item = admin.isvoro_market_valuation(listing(), NOW)
        rule = {"id": "r1", "target": "listing", "product_id": "901", "direction": "discount",
                "rate_threshold": 10, "amount_threshold": None, "match": "any", "action": "notify", "enabled": True}
        value = admin.default_config()
        value["vps_seckill"]["accounts"] = [{"id": "iv", "name": "Isvoro", "provider": "isvoro",
            "username": "user@example.com", "password": "secret"}]
        value["isvoro_market"] = {"enabled": True, "interval_seconds": 60, "account_id": "iv", "items": [rule]}
        value["telegram"] = {"bot_token": "token", "chat_id": "123", "disabled": False}
        config = admin.validate_config(value)
        self.assertTrue(admin._isvoro_market_rule_matches(config["isvoro_market"]["items"][0], item))
        with tempfile.TemporaryDirectory() as folder, patch.object(admin, "CONFIG_PATH", Path(folder) / "config.json"), \
                patch.object(admin, "telegram_call") as send:
            admin._evaluate_isvoro_market(config, [item])
            admin._evaluate_isvoro_market(config, [item])
            self.assertEqual(send.call_count, 1)

    def test_alert_uses_akile_layout_and_real_newlines(self):
        item = admin.isvoro_market_valuation(listing(), NOW)
        item.update(cpu=2, memory=2048, disk=40, flow=1000, bandwidth=500)
        value = admin.default_config()
        value["telegram"] = {"bot_token": "token", "chat_id": "123", "disabled": False}
        rule = {"id": "r-alert", "name": "HK 续费价", "target": "listing", "product_id": "901",
                "direction": "discount", "rate_threshold": 10, "amount_threshold": None,
                "match": "any", "action": "notify", "enabled": True}
        with patch.object(admin, "telegram_call") as send:
            self.assertTrue(admin._isvoro_market_alert(value, rule, item))
        message = send.call_args.kwargs["text"]
        labels = ["地区：", "节点：", "套餐：", "配置：", "网络：", "续费价格：", "到期时间：",
                  "售价：", "剩余价值：", "折价：", "触发规则：", "挂单 ID：", "说明：", "检查时间："]
        positions = [message.index(label) for label in labels]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("🌍", message)
        self.assertIn("2C/2G/40GB", message)
        self.assertIn("CNY 30.00/1个月", message)
        self.assertNotIn("\\n", message)
        self.assertNotIn("月付", message)

    def test_renewal_rule_matches_region_and_renewal_price(self):
        item = admin.isvoro_market_valuation(listing(), NOW)
        rule = {"id": "r-renewal", "target": "renewal", "product_id": "", "name": "",
                "plan_name": "", "region": "中国香港", "node": "", "renewal_price": 30,
                "direction": "discount", "rate_threshold": 10, "amount_threshold": None,
                "match": "any", "action": "notify", "enabled": True}
        self.assertTrue(admin._isvoro_market_rule_matches(rule, item))
        self.assertFalse(admin._isvoro_market_rule_matches({**rule, "region": "美国"}, item))
        self.assertFalse(admin._isvoro_market_rule_matches({**rule, "renewal_price": 29.99}, item))

    def test_renewal_rule_requires_isvoro_region_and_price(self):
        value = admin.default_config()
        value["vps_seckill"]["accounts"] = [{"id": "iv", "name": "Isvoro", "provider": "isvoro",
            "username": "user@example.com", "password": "secret"}]
        value["isvoro_market"] = {"enabled": False, "interval_seconds": 600, "account_id": "iv", "items": [{
            "id": "rule", "target": "renewal", "region": "中国香港", "renewal_price": 30,
            "direction": "discount", "rate_threshold": 10, "amount_threshold": None, "enabled": True,
        }]}
        normalized = admin.validate_config(value)
        self.assertEqual(normalized["isvoro_market"]["items"][0]["renewal_price"], 30.0)
        value["isvoro_market"]["items"][0]["renewal_price"] = None
        with self.assertRaises(ValueError):
            admin.validate_config(value)

    def test_account_status_only_probes_cached_session(self):
        value = admin.default_config()
        value["vps_seckill"]["accounts"] = [{"id": "iv", "name": "Isvoro", "provider": "isvoro",
            "username": "user@example.com", "password": "secret", "login_url": admin.ISVORO_LOGIN_URL}]
        with patch.object(admin, "current_config", return_value=admin.validate_config(value)), \
                patch("monitor.product_fetcher", return_value={}), patch.object(admin, "_get_merchant_session", return_value=None) as cached:
            result = admin.merchant_account_status("iv")
        self.assertFalse(result["ok"])
        self.assertIn("尚未建立", result["message"])
        cached.assert_called_once()


if __name__ == "__main__":
    unittest.main()
