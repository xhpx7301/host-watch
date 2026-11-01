import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import admin


def config():
    value = admin.default_config()
    value["vps_seckill"]["accounts"] = [dict(
        id="akile-1", name="Main Akile", provider="akile", username="user@example.com", password="secret")]
    value["account_balance_notifications"] = dict(
        enabled=True, interval_seconds=300, account_ids=["akile-1"], low_balance_enabled=True,
        low_balance_threshold=10, balance_change_enabled=True)
    value["telegram"] = dict(bot_token="token", chat_id="123", disabled=False)
    return admin.validate_config(value)


def profile_response(data):
    response = Mock(status_code=200)
    response.json.return_value = {"status_code": 0, "data": data}
    return response


class AccountBalanceTests(unittest.TestCase):
    def test_config_accepts_supported_accounts_and_rejects_invalid_rules(self):
        self.assertEqual(config()["account_balance_notifications"]["account_ids"], ["akile-1"])
        for changes in [
            {"interval_seconds": 29},
            {"low_balance_threshold": float("nan")},
            {"enabled": True, "account_ids": [], "low_balance_enabled": True},
            {"enabled": True, "account_ids": ["akile-1"], "low_balance_enabled": False,
             "balance_change_enabled": False},
        ]:
            value = admin.default_config()
            value["vps_seckill"]["accounts"] = config()["vps_seckill"]["accounts"]
            value["account_balance_notifications"] = {**config()["account_balance_notifications"], **changes}
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                admin.validate_config(value)
        value = config()
        value["vps_seckill"]["accounts"] = []
        normalized = admin.validate_config(value)
        self.assertFalse(normalized["account_balance_notifications"]["enabled"])
        self.assertEqual(normalized["account_balance_notifications"]["account_ids"], [])

    def test_profile_balance_is_verified_and_cents_are_preserved(self):
        session = Mock()
        session.get.return_value = profile_response({"email": "USER@example.com", "money": 1234})
        with patch.object(admin, "_akile_market_session", return_value=session):
            self.assertEqual(admin._read_akile_balance(config(), "akile-1"), 1234)
        session.close.assert_called_once()
        session.get.return_value = profile_response({"email": "other@example.com", "money": 1234})
        with patch.object(admin, "_akile_market_session", return_value=session), self.assertRaises(ValueError):
            admin._read_akile_balance(config(), "akile-1")

    def test_low_balance_and_balance_change_triggers_are_deduplicated(self):
        settings = config()["account_balance_notifications"]
        self.assertEqual(admin._account_balance_triggers(None, 900, settings), ["余额低于阈值"])
        previous = {"balance_cents": 900, "below_threshold": True, "pending": []}
        self.assertEqual(admin._account_balance_triggers(previous, 800, settings), ["余额变化"])
        self.assertEqual(admin._account_balance_triggers(
            {**previous, "pending": ["余额变化"]}, 800, settings), ["余额变化"])
        self.assertEqual(admin._account_balance_triggers(previous, 1200, settings), ["余额变化"])

    def test_failed_account_read_keeps_saved_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            state_path = Path(directory) / "state-account-balance.json"
            state_path.write_text(json.dumps({"accounts": {"akile-1": {
                "balance_cents": 500, "below_threshold": True, "pending": [], "updated_at": "old"}}}), encoding="utf-8")
            with patch.object(admin, "CONFIG_PATH", path), \
                    patch.object(admin, "current_config", return_value=config()), \
                    patch.object(admin, "_read_akile_balance", side_effect=ValueError("temporary failure")):
                result = admin.refresh_account_balances()
            self.assertEqual(result["accounts"][0]["balance"], 5.0)
            self.assertIn("temporary failure", result["accounts"][0]["error"])
            saved = json.loads(state_path.read_text(encoding="utf-8"))["accounts"]["akile-1"]
            self.assertEqual(saved["balance_cents"], 500)
            self.assertEqual(saved["updated_at"], "old")


if __name__ == "__main__":
    unittest.main()
