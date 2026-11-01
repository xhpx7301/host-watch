import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import admin


def config():
    value = admin.default_config()
    value["vps_seckill"]["accounts"] = [
        {
            "id": "narwhal-1",
            "name": "Narwhal",
            "provider": "narwhal",
            "username": "narwhal@example.com",
            "password": "secret",
        },
        {
            "id": "hytron-1",
            "name": "Hytron",
            "provider": "hytron",
            "username": "hytron@example.com",
            "password": "secret",
        },
    ]
    value["account_balance_notifications"] = {
        "providers": {
            "narwhal": {
                "enabled": True,
                "interval_seconds": 300,
                "account_ids": ["narwhal-1"],
                "low_balance_enabled": True,
                "low_balance_threshold": 10,
                "balance_change_enabled": True,
            },
            "hytron": {
                "enabled": True,
                "interval_seconds": 600,
                "account_ids": ["hytron-1"],
                "low_balance_enabled": True,
                "low_balance_threshold": 20,
                "balance_change_enabled": True,
            },
        }
    }
    value["telegram"] = {"bot_token": "token", "chat_id": "123", "disabled": False}
    return admin.validate_config(value)


def response(payload):
    result = Mock(status_code=200)
    result.json.return_value = payload
    return result


class AccountBalanceTests(unittest.TestCase):
    def test_config_normalizes_supported_providers_and_currencies(self):
        normalized = config()["account_balance_notifications"]["providers"]
        self.assertEqual(normalized["narwhal"]["currency"], "USD")
        self.assertEqual(normalized["narwhal"]["currency_symbol"], "$")
        self.assertEqual(normalized["hytron"]["currency"], "USD")
        self.assertEqual(normalized["hytron"]["currency_symbol"], "$")
        self.assertEqual(normalized["narwhal"]["interval_seconds"], 300)
        self.assertEqual(normalized["hytron"]["interval_seconds"], 600)

    def test_balance_readers_convert_decimal_amounts_to_minor_units(self):
        narwhal_session = Mock()
        narwhal_session.get.return_value = response({
            "code": 0,
            "data": {"email": "narwhal@example.com", "money": "12.34"},
        })
        hytron_session = Mock()
        hytron_session.get.return_value = response({"data": {"balance": "7.89"}})
        accounts = config()
        with patch.object(admin, "_account_balance_session", side_effect=[
            (narwhal_session, accounts["vps_seckill"]["accounts"][0]),
            (hytron_session, accounts["vps_seckill"]["accounts"][1]),
        ]):
            self.assertEqual(admin._read_narwhal_balance(accounts, "narwhal-1"), 1234)
            self.assertEqual(admin._read_hytron_balance(accounts, "hytron-1"), 789)
        narwhal_session.get.assert_called_once_with(admin.NARWHAL_PROFILE_URL, timeout=(5, 20), allow_redirects=False)
        hytron_session.get.assert_called_once_with(admin.HYTRON_BALANCE_URL, timeout=(5, 20), allow_redirects=False)
        self.assertTrue(admin.HYTRON_BALANCE_URL.endswith("/billing/balance"))
        narwhal_session.close.assert_called_once()
        hytron_session.close.assert_called_once()

    def test_low_balance_and_balance_change_triggers_are_deduplicated(self):
        settings = config()["account_balance_notifications"]["providers"]["narwhal"]
        self.assertEqual(admin._account_balance_triggers(None, 900, settings), ["余额低于阈值"])
        previous = {"balance_cents": 900, "below_threshold": True, "pending": []}
        self.assertEqual(admin._account_balance_triggers(previous, 800, settings), ["余额变化"])
        self.assertEqual(admin._account_balance_triggers(
            {**previous, "pending": ["余额变化"]}, 800, settings), ["余额变化"])
        self.assertEqual(admin._account_balance_triggers(previous, 1200, settings), ["余额变化"])

    def test_failed_account_read_keeps_saved_baseline_and_merges_provider_results(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            state_path = Path(directory) / "state-account-balance.json"
            state_path.write_text(json.dumps({"accounts": {
                "narwhal:narwhal-1": {"balance_cents": 500, "below_threshold": True, "pending": [], "updated_at": "old"},
                "hytron:hytron-1": {"balance_cents": 789, "below_threshold": True, "pending": [], "updated_at": "old-hytron"},
            }}), encoding="utf-8")
            current = config()
            with patch.object(admin, "CONFIG_PATH", path), \
                    patch.object(admin, "current_config", return_value=current), \
                    patch.object(admin, "_read_provider_balance", side_effect=[ValueError("temporary failure"), 800]), \
                    patch.object(admin, "telegram_call"):
                result = admin.refresh_account_balances()
            by_provider = {(row["provider"], row["account_id"]): row for row in result["accounts"]}
            self.assertEqual(by_provider[("narwhal", "narwhal-1")]["balance"], 5.0)
            self.assertIn("temporary failure", by_provider[("narwhal", "narwhal-1")]["error"])
            self.assertEqual(by_provider[("hytron", "hytron-1")]["balance"], 8.0)
            saved = json.loads(state_path.read_text(encoding="utf-8"))["accounts"]
            self.assertEqual(saved["narwhal:narwhal-1"]["balance_cents"], 500)
            self.assertEqual(saved["narwhal:narwhal-1"]["updated_at"], "old")
            self.assertEqual(saved["hytron:hytron-1"]["balance_cents"], 800)

    def test_balance_test_notification_uses_account_balance_route_without_changing_state(self):
        current = config()
        with patch.object(admin, "current_config", return_value=current), \
                patch.object(admin, "effective_telegram", return_value=("token", "123")), \
                patch.object(admin, "telegram_call") as send:
            result = admin.send_account_balance_test_notification([
                {"provider": "narwhal", "account_id": "narwhal-1"},
            ])
        self.assertTrue(result["ok"])
        send.assert_called_once()
        self.assertIn("账户余额通知测试", send.call_args.kwargs["text"])
        self.assertIn("Narwhal", send.call_args.kwargs["text"])


if __name__ == "__main__":
    unittest.main()
