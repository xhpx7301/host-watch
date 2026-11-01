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

    def test_legacy_narwhal_currency_is_migrated_to_usd(self):
        value = config()
        value["account_balance_notifications"]["providers"]["narwhal"]["currency"] = "CNY"
        value["account_balance_notifications"]["providers"]["narwhal"]["currency_symbol"] = "¥"
        normalized = admin.validate_config(value)["account_balance_notifications"]["providers"]["narwhal"]
        self.assertEqual(normalized["currency"], "USD")
        self.assertEqual(normalized["currency_symbol"], "$")

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

    def test_balance_alert_shows_account_name_merchant_and_official_topup_button(self):
        current = config()
        current["vps_seckill"]["accounts"][0]["name"] = "主用账户"
        current["account_balance_notifications"]["providers"]["hytron"]["enabled"] = False
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(admin, "CONFIG_PATH", Path(directory) / "config.json"), \
                patch.object(admin, "current_config", return_value=current), \
                patch.object(admin, "_read_provider_balance", return_value=500), \
                patch.object(admin, "effective_telegram", return_value=("token", "123")), \
                patch.object(admin, "telegram_call") as send:
            result = admin.refresh_account_balances()

        self.assertEqual(len(result["accounts"]), 1)
        text = send.call_args.kwargs["text"]
        self.assertIn("🏪 <b>商家：</b>独角鲸云", text)
        self.assertIn("👤 <b>账户：</b>主用账户", text)
        self.assertNotIn("narwhal@example.com", text)
        self.assertIn("👤 <b>账户：</b>主用账户\n💰 <b>当前余额：</b>$5.00", text)
        self.assertIn("🕒 <b>检查时间：</b>", text)
        button = send.call_args.kwargs["reply_markup"]["inline_keyboard"][0][0]
        self.assertEqual(button["text"], "💳 前往官网充值余额")
        self.assertEqual(button["url"], "https://dash.fuckip.me/")

    def test_topup_keyboard_uses_each_selected_merchant_site(self):
        keyboard = admin._account_balance_topup_keyboard(["narwhal", "hytron", "narwhal"])
        buttons = keyboard["inline_keyboard"][0]
        self.assertEqual([button["url"] for button in buttons], [
            "https://dash.fuckip.me/", "https://hyvps.hytron.io/",
        ])
        self.assertEqual(buttons[1]["text"], "💳 前往Hytron官网充值余额")

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
        with patch.object(admin, "ACCOUNT_BALANCE_CACHE", {"accounts": []}), \
                patch.object(admin, "current_config", return_value=current), \
                patch.object(admin, "effective_telegram", return_value=("token", "123")), \
                patch.object(admin, "telegram_call") as send:
            result = admin.send_account_balance_test_notification([
                {"provider": "narwhal", "account_id": "narwhal-1"},
            ])
        self.assertTrue(result["ok"])
        send.assert_called_once()
        self.assertIn("账户余额通知测试", send.call_args.kwargs["text"])
        self.assertIn("Narwhal", send.call_args.kwargs["text"])
        self.assertIn("🏪 <b>商家：</b>独角鲸云", send.call_args.kwargs["text"])
        self.assertNotIn("narwhal@example.com", send.call_args.kwargs["text"])
        text = send.call_args.kwargs["text"]
        self.assertIn("👤 <b>账户：</b>Narwhal\n💰 <b>当前余额：</b>尚未查询", text)
        self.assertIn("🕒 <b>测试时间：</b>", text)
        self.assertEqual(send.call_args.kwargs["reply_markup"]["inline_keyboard"][0][0]["url"],
                         "https://dash.fuckip.me/")

    def test_email_account_label_is_not_auto_linkable_in_telegram(self):
        label = admin._telegram_account_label("user@example.com")
        self.assertIn("user&#8203;@example.com", label)
        self.assertNotIn("user@example.com", label)


if __name__ == "__main__":
    unittest.main()
