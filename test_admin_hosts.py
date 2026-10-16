"""Host allowlist errors must distinguish reverse-proxy setup from login."""
import base64
import http.client
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import admin
import auth
import test_admin


class HostGateTests(unittest.TestCase):
    setUp = test_admin.AdminAPITests.setUp

    def get(self, host, path="/", authorization=""):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        self.addCleanup(connection.close)
        connection.request("GET", path, headers={"Host": host, "Authorization": authorization})
        response = connection.getresponse()
        return response.status, response.getheaders(), response.read()

    def test_unconfigured_reverse_proxy_domain_has_actionable_403_page(self):
        with patch.object(admin, "authorized") as authorize:
            status, _, body = self.get("hw.6990699.xyz")
        self.assertEqual(status, 403)
        self.assertIn("访问域名尚未配置", body.decode())
        self.assertIn(b"hw admin-domain", body)
        authorize.assert_not_called()

    def test_host_diagnostic_does_not_reflect_untrusted_host_into_html(self):
        status, _, body = self.get("untrusted.example")
        self.assertEqual(status, 403)
        self.assertNotIn(b"untrusted.example", body)

    def test_api_host_rejection_is_json_with_specific_error_code(self):
        status, headers, body = self.get("hw.6990699.xyz", "/api/config")
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["code"], "admin_host_not_allowed")
        self.assertIn("application/json", dict(headers)["Content-Type"])

    def test_allowed_domain_requires_login_before_serving_admin_page(self):
        users = Path(self.temp.name) / "users.json"
        users.write_text(json.dumps({"version": 1, "users": {"admin": auth.password_record("initial random password")}}))
        with patch.dict(os.environ, {"ADMIN_ALLOWED_HOSTS": "127.0.0.1,localhost,hw.6990699.xyz", "ADMIN_USERS_FILE": str(users)}), patch.object(admin, "authorized", side_effect=auth.authorized):
            status, headers, _ = self.get("hw.6990699.xyz")
            self.assertEqual(status, 401)
            self.assertIn("WWW-Authenticate", dict(headers))
            credential = "Basic " + base64.b64encode(b"admin:initial random password").decode()
            status, _, body = self.get("hw.6990699.xyz", authorization=credential)
            self.assertEqual(status, 200)
            self.assertIn(b"HostWatch", body)
            self.assertEqual(self.get("other.example", authorization=credential)[0], 403)


if __name__ == "__main__":
    unittest.main()
