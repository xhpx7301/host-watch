import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import admin


class AdminAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "config.json"
        for setting in (patch.object(admin, "CONFIG_PATH", self.config),
                        patch.object(admin, "authorized", return_value=True),
                        patch.dict(os.environ, {"ADMIN_ALLOWED_HOSTS": "127.0.0.1"})):
            setting.start()
            self.addCleanup(setting.stop)
        self.server = admin.ThreadingHTTPServer(("127.0.0.1", 0), admin.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, path, payload, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        self.addCleanup(connection.close)
        connection.request("POST", path, json.dumps(payload), {"Content-Type": "application/json", **(headers or {})})
        response = connection.getresponse()
        return response.status, response.read()

    def test_save_applies_service_and_returns_redacted_config(self):
        value = admin.default_config()
        value["telegram"] = {"bot_token": "private-token", "chat_id": "123"}
        with patch.object(admin, "manager_action", return_value={"ok": True}) as manager:
            status, body = self.request("/api/config", value)
        self.assertEqual(status, 200)
        self.assertNotIn(b"private-token", body)
        self.assertTrue(json.loads(body)["service"]["ok"])
        manager.assert_called_once_with({"action": "monitor-apply"})
        self.assertEqual(json.loads(self.config.read_text())["telegram"]["bot_token"], "private-token")

    def test_save_failure_does_not_apply_service(self):
        with patch.object(admin, "save_config", side_effect=OSError("disk full")), patch.object(admin, "manager_action") as manager:
            status, _ = self.request("/api/config", admin.default_config())
        self.assertEqual(status, 400)
        manager.assert_not_called()

    def test_saved_config_is_preserved_when_service_fails(self):
        with patch.object(admin, "manager_action", side_effect=ValueError("service error")):
            status, body = self.request("/api/config", admin.default_config())
        self.assertEqual(status, 200)
        self.assertTrue(self.config.exists())
        self.assertFalse(json.loads(body)["service"]["ok"])
        self.assertIn("service error", json.loads(body)["service"]["error"])

    def test_cross_site_and_non_json_requests_cannot_control_services(self):
        for headers in ({"Content-Type": "text/plain"}, {"Sec-Fetch-Site": "cross-site"}, {"Origin": "https://evil.example"}):
            with self.subTest(headers=headers), patch.object(admin, "manager_action") as manager:
                status, _ = self.request("/api/manage", {"action": "monitor-stop"}, headers)
                self.assertIn(status, (403, 415))
                manager.assert_not_called()

    def test_start_rejects_invalid_monitor_config(self):
        with patch.object(admin, "manager_action") as manager:
            status, _ = self.request("/api/manage", {"action": "monitor-start"})
        self.assertEqual(status, 400)
        manager.assert_not_called()

    def test_authentication_is_required_for_management(self):
        with patch.object(admin, "authorized", return_value=False), patch.object(admin, "manager_action") as manager:
            status, _ = self.request("/api/manage", {"action": "update"})
        self.assertEqual(status, 401)
        manager.assert_not_called()


if __name__ == "__main__":
    unittest.main()
