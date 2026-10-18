"""Unrestricted reverse-proxy domains still require auth and same-origin writes."""
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
    def setUp(self):
        test_admin.AdminAPITests.setUp(self)
        users = Path(self.temp.name) / "users.json"
        users.write_text(json.dumps({"version": 1, "users": {"admin": auth.password_record("initial random password")}}))
        for setting in (patch.dict(os.environ, {"ADMIN_USERS_FILE": str(users)}),
                        patch.object(admin, "authorized", side_effect=auth.authorized)):
            setting.start()
            self.addCleanup(setting.stop)
        self.credential = "Basic " + base64.b64encode(b"admin:initial random password").decode()

    def get(self, host, path="/", authorization=""):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        self.addCleanup(connection.close)
        connection.request("GET", path, headers={"Host": host, "Authorization": authorization})
        response = connection.getresponse()
        return response.status, response.getheaders(), response.read()

    def post(self, host, path, payload, **headers):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        self.addCleanup(connection.close)
        connection.request("POST", path, json.dumps(payload), {"Host": host, "Authorization": self.credential, "Content-Type": "application/json", **headers})
        response = connection.getresponse()
        return response.status, response.read()

    def test_new_domains_ignore_legacy_allowlist_and_require_valid_login(self):
        for host in ("new.example", "other.example:8443", "192.0.2.10:8765"):
            with self.subTest(host=host):
                status, headers, _ = self.get(host)
                self.assertEqual(status, 200)
                self.assertNotIn("WWW-Authenticate", dict(headers))
                self.assertIn(b"login-form", self.get(host)[2])
                self.assertEqual(self.get(host, authorization="Basic invalid")[0], 200)
                status, _, body = self.get(host, authorization=self.credential)
                self.assertEqual(status, 200)
                self.assertIn(b"HostWatch", body)
                status, _, body = self.get(host, "/api/config", self.credential)
                self.assertEqual(status, 200)
                self.assertIn("products", json.loads(body))

    def test_login_page_creates_cookie_session_and_logout_revokes_it(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        self.addCleanup(connection.close)
        connection.request("POST", "/api/auth/login", json.dumps({"username": "admin", "password": "initial random password"}), {"Host": "new.example", "Content-Type": "application/json"})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        cookie = next(value for name, value in response.getheaders() if name.casefold() == "set-cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        response.read()

        connection.request("GET", "/api/config", headers={"Host": "new.example", "Cookie": cookie.split(";", 1)[0]})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        response.read()

        connection.request("POST", "/api/auth/logout", headers={"Host": "new.example", "Cookie": cookie.split(";", 1)[0]})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn("Max-Age=0", dict(response.getheaders())["Set-Cookie"])
        response.read()

        connection.request("GET", "/api/config", headers={"Host": "new.example", "Cookie": cookie.split(";", 1)[0]})
        response = connection.getresponse()
        self.assertEqual(response.status, 401)
        response.read()

    def test_old_allowlist_including_wildcard_does_not_prevent_startup(self):
        for hosts in ("*", "localhost,old.example", ""):
            with self.subTest(hosts=hosts), patch.dict(os.environ, {"ADMIN_HOST": "127.0.0.1", "ADMIN_PORT": "8765", "ADMIN_ALLOWED_HOSTS": hosts}), patch.object(admin, "ThreadingHTTPServer") as server:
                admin.main()
                server.assert_called_once_with(("127.0.0.1", 8765), admin.Handler)
                server.return_value.serve_forever.assert_called_once_with()

    def test_same_origin_actions_work_on_new_domains_but_cross_site_is_rejected(self):
        host = "new.example:8443"
        with patch.object(admin, "manager_action", return_value={"ok": True}) as manager:
            self.assertEqual(self.post(host, "/api/manage", {"action": "monitor-stop"}, Origin="https://" + host)[0], 200)
            manager.assert_called_once_with({"action": "monitor-stop"})
            manager.reset_mock()
            for headers in ({"Origin": "https://other.example"}, {"Sec-Fetch-Site": "cross-site"}):
                self.assertEqual(self.post(host, "/api/manage", {"action": "monitor-stop"}, **headers)[0], 403)
            manager.assert_not_called()

    def test_missing_credentials_preserve_local_tunnel_and_explain_domain_login_setup(self):
        with patch.object(auth, "users_path", return_value=None), patch.dict(os.environ, {"ADMIN_PASSWORD": ""}):
            for host in ("127.0.0.1:8765", "localhost:8765", "[::1]:8765"):
                self.assertEqual(self.get(host)[0], 200)
            status, _, body = self.get("new.example")
            self.assertEqual(status, 503)
            self.assertIn(b"hw init-admin", body)
            self.assertNotIn(b"new.example", body)
            status, _, body = self.get("new.example", "/api/config")
            self.assertEqual(status, 503)
            self.assertEqual(json.loads(body)["code"], "admin_auth_not_configured")

    def test_node_registration_uses_node_token_on_any_domain_without_basic_login(self):
        with patch.object(auth, "users_path", return_value=None), patch.dict(os.environ, {"ADMIN_PASSWORD": ""}), patch.object(admin, "register_egress_node", return_value={"ok": True}) as register:
            status, _ = self.post("migrated.example", "/api/egress/register", {"node_id": "test"}, Authorization="Bearer node-secret")
            self.assertEqual(status, 200)
            register.assert_called_once_with({"node_id": "test"}, "Bearer node-secret")

    def test_network_peer_must_still_be_loopback(self):
        handler = object.__new__(admin.Handler)
        handler.client_address = ("192.0.2.1", 1234)
        with patch.object(handler, "send_error") as error:
            self.assertFalse(handler._secure())
            error.assert_called_once_with(403)


if __name__ == "__main__":
    unittest.main()
