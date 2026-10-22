"""Node enrollment, generated installation commands and a real agent process."""
import base64
import http.client
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch

import admin
import egress
import egress_agent
import test_admin


def pending_node(node_id="egress-test"):
    return {"id": node_id, "name": "出口节点 1", "proxy_url": "", "health_url": "",
            "listen_port": 48787, "enabled": True, "pending_registration": True}


class EgressEnrollmentTests(unittest.TestCase):
    setUp = test_admin.AdminAPITests.setUp
    request = test_admin.AdminAPITests.request
    get_request = test_admin.AdminAPITests.get_request

    def prepare(self, node=None, register_url="https://watch.example.com"):
        code, body = self.request("/api/egress/install-command", {"node": node or pending_node(), "register_url": register_url})
        self.assertEqual(code, 200, body)
        return json.loads(body)

    def payload(self, node_id="egress-test"):
        return {"node_id": node_id, "name": "Agent name", "public_ip": "203.0.113.9",
                "proxy_url": "http://203.0.113.9:8787", "health_url": "http://203.0.113.9:8787/health", "token": "ignored-agent-token"}

    def test_defaults_generate_reusable_node_scoped_secret_and_keep_other_settings(self):
        current = admin.default_config()
        current["interval_seconds"] = 30
        current["telegram"] = {"bot_token": "stored-secret", "chat_id": "123", "disabled": False}
        admin.save_config(current)
        output = self.prepare()
        stored = admin.current_config()
        node = stored["egress"]["nodes"][0]
        self.assertGreaterEqual(len(node["token"]), 40)
        self.assertEqual(stored["telegram"], current["telegram"])
        self.assertEqual(stored["interval_seconds"], 30)
        self.assertEqual(output["node"]["token"], "")
        self.assertTrue(output["node"]["token_configured"])
        code, body = self.get_request("/api/egress")
        self.assertEqual(code, 200)
        self.assertNotIn(node["token"].encode(), body)
        next_command = self.prepare(output["node"])
        self.assertEqual(output["command"], next_command["command"])
        another = self.prepare(pending_node("egress-second"))
        self.assertNotEqual(output["command"], another["command"])

    def test_command_keeps_shell_metacharacters_as_encoded_data(self):
        node = pending_node()
        node["name"] = "香港 ' $(touch /tmp/should-not-exist); `id`"
        node["listen_port"] = 9876
        output = self.prepare(node)
        self.assertNotIn("\n", output["command"])
        self.assertNotIn("\r", output["command"])
        args = shlex.split(output["command"])
        self.assertEqual(args[:2], ["sh", "-c"])
        self.assertEqual(args[3], "sh")
        settings = json.loads(base64.b64decode(args[4], validate=True))
        self.assertEqual(settings["EGRESS_NODE_NAME"], node["name"])
        self.assertEqual(settings["EGRESS_LISTEN_PORT"], 9876)
        self.assertNotIn(node["name"], args[2])
        self.assertEqual(settings["EGRESS_REGISTER_URL"], "https://watch.example.com")

    def test_localhost_commands_use_offline_connection_info(self):
        for url in ["http://localhost:8765", "http://127.0.0.1:8765", "http://[::1]:8765"]:
            with self.subTest(url=url):
                output = self.prepare(register_url=url)
                self.assertFalse(output["auto_register"])
                self.assertEqual(output["register_url"], "")

    def test_invalid_parameters_and_unauthorized_generation_do_not_save(self):
        for node in [None, [], "bad", {**pending_node(), "listen_port": 65536}]:
            code, _ = self.request("/api/egress/install-command", {"node": node})
            self.assertEqual(code, 400)
        self.assertFalse(self.config.exists())
        with patch.object(admin, "authorized", return_value=False):
            code, _ = self.request("/api/egress/install-command", {"node": pending_node()})
        self.assertEqual(code, 401)
        self.assertFalse(self.config.exists())

    def test_agent_registration_preserves_disabled_state_name_token_and_routes(self):
        node = {**pending_node(), "enabled": False}
        self.prepare(node)
        current = admin.current_config()
        current["egress"]["routes"]["dmit"] = node["id"]
        admin.save_config(current)
        token = current["egress"]["nodes"][0]["token"]
        with patch.object(admin, "authorized", return_value=False):
            code, _ = self.request("/api/egress/register", self.payload(), {"Authorization": "Bearer " + token})
        self.assertEqual(code, 200)
        stored = admin.current_config()["egress"]
        self.assertFalse(stored["nodes"][0]["enabled"])
        self.assertEqual(stored["nodes"][0]["name"], node["name"])
        self.assertEqual(stored["nodes"][0]["token"], token)
        self.assertFalse(stored["nodes"][0]["pending_registration"])
        self.assertEqual(stored["routes"], {"dmit": node["id"]})

    def test_wrong_unknown_and_cross_node_credentials_cannot_register(self):
        self.prepare()
        self.prepare(pending_node("other"))
        token = admin.current_config()["egress"]["nodes"][0]["token"]
        original = self.config.read_bytes()
        with patch.dict(os.environ, {"EGRESS_REGISTER_TOKEN": ""}):
            for node_id, supplied in [("egress-test", "bad"), ("other", token), ("missing", token)]:
                code, _ = self.request("/api/egress/register", self.payload(node_id), {"Authorization": "Bearer " + supplied})
                self.assertEqual(code, 400)
        self.assertEqual(self.config.read_bytes(), original)

    def test_registration_busy_and_stale_draft_cannot_undo_discovered_addresses(self):
        output = self.prepare()
        token = admin.current_config()["egress"]["nodes"][0]["token"]
        with admin.MUTATION_LOCK:
            code, _ = self.request("/api/egress/register", self.payload(), {"Authorization": "Bearer " + token})
        self.assertEqual(code, 409)
        code, _ = self.request("/api/egress/register", self.payload(), {"Authorization": "Bearer " + token})
        self.assertEqual(code, 200)
        self.prepare(output["node"])
        stale = admin.default_config()
        stale["egress"]["nodes"] = [output["node"]]
        with patch.object(admin, "manager_action", return_value={"ok": True}):
            code, _ = self.request("/api/config", stale)
        self.assertEqual(code, 200)
        stored = admin.current_config()["egress"]["nodes"][0]
        self.assertEqual(stored["proxy_url"], "http://203.0.113.9:8787")
        self.assertFalse(stored["pending_registration"])

    def test_offline_import_uses_node_secret_and_returns_redacted_status(self):
        self.prepare(register_url="http://localhost:8765")
        token = admin.current_config()["egress"]["nodes"][0]["token"]
        code, _ = self.request("/api/egress/import", {**self.payload(), "token": "bad"})
        self.assertEqual(code, 400)
        code, body = self.request("/api/egress/import", {**self.payload(), "token": token})
        self.assertEqual(code, 200)
        self.assertNotIn(token.encode(), body)
        self.assertEqual(json.loads(body)["egress"]["nodes"][0]["public_ip"], "203.0.113.9")

    def test_probe_failure_persists_node_error_for_status_display(self):
        self.prepare(register_url="http://localhost:8765")
        with patch.object(admin.egress, "probe", side_effect=ValueError("代理连接失败")):
            code, body = self.request("/api/egress/probe", {"node_id": "egress-test"})
        self.assertEqual(code, 400)
        self.assertIn("代理连接失败", body.decode())
        self.assertEqual(admin.current_config()["egress"]["nodes"][0]["last_error"], "代理连接失败")

    def test_real_agent_loads_installed_config_registers_and_authenticates_health(self):
        self.prepare()
        token = admin.current_config()["egress"]["nodes"][0]["token"]
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        settings = {"EGRESS_NODE_ID": "egress-test", "EGRESS_NODE_NAME": "Agent",
                    "EGRESS_TOKEN": token, "EGRESS_PUBLIC_IP": "203.0.113.10", "EGRESS_LISTEN_HOST": "127.0.0.1",
                    "EGRESS_LISTEN_PORT": port, "EGRESS_REGISTER_URL": f"http://127.0.0.1:{self.server.server_port}"}
        config = Path(self.temp.name) / "agent.json"
        config.write_text(json.dumps(settings), encoding="utf-8")
        env = {key: value for key, value in os.environ.items() if not key.startswith("EGRESS_")}
        env["EGRESS_CONFIG"] = str(config)
        process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("egress_agent.py"))], env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if not admin.current_config()["egress"]["nodes"][0]["pending_registration"]:
                    break
                self.assertIsNone(process.poll(), "Agent failed to start from installed JSON configuration")
                time.sleep(0.05)
            self.assertFalse(admin.current_config()["egress"]["nodes"][0]["pending_registration"])
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            try:
                connection.request("GET", "/health")
                self.assertEqual(connection.getresponse().status, 401)
                connection.close()
                connection.request("GET", "/health", headers={"Authorization": "Bearer " + token})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read())["public_ip"], "203.0.113.10")
            finally:
                connection.close()
        finally:
            process.terminate()
            process.wait(timeout=5)


class EgressAgentTests(unittest.TestCase):
    def test_ip_discovery_rejects_error_pages_and_caches_valid_ip(self):
        invalid = Mock(text="<html>error</html>")
        valid = Mock(text="203.0.113.7\n")
        with patch.object(egress_agent, "PUBLIC_IP", ""), patch.object(egress_agent, "_ip_cache", (0, "")), patch.object(egress_agent.requests, "get", side_effect=[invalid, valid]) as fetch:
            self.assertEqual(egress_agent.public_ip(), "203.0.113.7")
            self.assertEqual(egress_agent.public_ip(), "203.0.113.7")
            self.assertEqual(fetch.call_count, 2)

    def test_connection_info_formats_ipv6_and_refuses_missing_ip(self):
        with patch.object(egress_agent, "public_ip", return_value="2001:db8::1"), patch.object(egress_agent, "setting", return_value=""):
            self.assertEqual(egress_agent.connection_info()["health_url"], "http://[2001:db8::1]:48787/health")
        with patch.object(egress_agent, "public_ip", return_value=""), patch.object(egress_agent, "setting", return_value=""):
            with self.assertRaises(ValueError):
                egress_agent.connection_info()

    def test_pending_nodes_cannot_route_through_the_main_server(self):
        value = egress.normalize({"nodes": [pending_node()], "routes": {"dmit": "egress-test"}})
        with self.assertRaisesRegex(RuntimeError, "没有代理地址"):
            egress.resolve(value, "dmit")


if __name__ == "__main__":
    unittest.main()
