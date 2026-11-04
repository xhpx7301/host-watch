import json
import http.client
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import admin


def config():
    value = admin.default_config()
    value["telegram"] = {"bot_token": "default-token", "chat_id": "100", "disabled": False}
    value["telegram_routes"]["market"] = {
        "mode": "custom", "bot_token": "market-token", "chat_id": "200"}
    value["telegram_routes"]["account_balance"] = {"mode": "off", "bot_token": "", "chat_id": ""}
    return admin.validate_config(value)


class TelegramRouteTests(unittest.TestCase):
    def test_categories_use_their_assigned_bot(self):
        value = config()
        self.assertEqual(admin.effective_telegram(value, "stock"), ("default-token", "100"))
        self.assertEqual(admin.effective_telegram(value, "seckill"), ("default-token", "100"))
        self.assertEqual(admin.effective_telegram(value, "market"), ("market-token", "200"))
        self.assertEqual(admin.effective_telegram(value, "account_balance"), ("", ""))
        self.assertEqual(admin.effective_telegram({**value, "telegram": {**value["telegram"], "disabled": True}},
                                                  "market"), ("", ""))

    def test_routes_validate_modes_and_complete_credentials(self):
        for route in [
            {"mode": "other", "bot_token": "", "chat_id": ""},
            {"mode": "custom", "bot_token": "only-token", "chat_id": ""},
            {"mode": "custom", "bot_token": "token", "chat_id": "bad id"},
        ]:
            value = admin.default_config()
            value["telegram_routes"]["stock"] = route
            with self.subTest(route=route), self.assertRaises(ValueError):
                admin.validate_config(value)

    def test_public_config_never_returns_route_tokens(self):
        public = admin.public_config(config())
        encoded = json.dumps(public)
        self.assertNotIn("default-token", encoded)
        self.assertNotIn("market-token", encoded)
        self.assertTrue(public["telegram_routes"]["market"]["bot_token_configured"])
        self.assertEqual(public["telegram_routes"]["market"]["chat_id"], "200")

    def test_route_credentials_use_selected_bot(self):
        with patch.object(admin, "current_config", return_value=config()):
            self.assertEqual(admin.telegram_credentials(category="market"), ("market-token", "200"))
            self.assertEqual(admin.telegram_credentials(category="stock"), ("default-token", "100"))

    def test_config_save_preserves_masked_route_token(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            for setting in (patch.object(admin, "CONFIG_PATH", config_path),
                            patch.object(admin, "authorized", return_value=True),
                            patch.object(admin, "manager_action", return_value={"ok": True}),
                            patch.dict(os.environ, {"ADMIN_ALLOWED_HOSTS": "127.0.0.1"})):
                setting.start()
                self.addCleanup(setting.stop)
            admin.save_config(config())
            server = admin.ThreadingHTTPServer(("127.0.0.1", 0), admin.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            submitted = admin.public_config(config())
            submitted["telegram_routes"]["market"] = {"mode": "custom", "chat_id": "200",
                "bot_token_configured": True, "clear": False}
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            self.addCleanup(connection.close)
            connection.request("POST", "/api/config", json.dumps(submitted),
                {"Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 200, response.read().decode("utf-8"))
            saved = admin.current_config()
            self.assertEqual(saved["telegram_routes"]["market"]["bot_token"], "market-token")


if __name__ == "__main__":
    unittest.main()
