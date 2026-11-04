"""Exercise the real proxy, HTTP clients and TLS tunnel without external sites."""
import base64
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import cloudscraper
import requests

import egress
import egress_agent
import monitor


class TargetHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.headers_received.append(dict(self.headers))
        body = b"203.0.113.42" if self.path == "/ip" else b"<html><body>available</body></html>"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class QuietProxyHandler(egress_agent.ProxyHandler):
    def handle(self):
        try:
            super().handle()
        except ConnectionResetError:
            pass  # Clients close intentionally after rejected CONNECT/TLS.


class ProxyFixture:
    @classmethod
    def setUpClass(cls):
        cls.resources = ExitStack()
        cls.addClassCleanup(cls.resources.close)
        cls.root = Path(cls.resources.enter_context(tempfile.TemporaryDirectory()))
        cls.proxy = egress_agent.Server(("127.0.0.1", 0), QuietProxyHandler)
        cls.resources.callback(cls.proxy.server_close)
        cls.resources.callback(cls.proxy.shutdown)
        cls.resources.enter_context(patch.object(egress_agent, "TOKEN", "proxy-test-token+/@"))
        cls.resources.enter_context(patch.object(egress_agent, "NODE_ID", "local-test"))
        cls.resources.enter_context(patch.object(egress_agent, "PUBLIC_IP", "203.0.113.10"))
        threading.Thread(target=cls.proxy.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{cls.proxy.server_port}"
        cls.node = {"id": "local-test", "name": "Local test", "proxy_url": url,
                    "health_url": url + "/health", "token": "proxy-test-token+/@"}
        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
        cls.http.daemon_threads = True
        cls.http.headers_received = []
        cls.resources.callback(cls.http.server_close)
        cls.resources.callback(cls.http.shutdown)
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()

    def setUp(self):
        self.session = requests.Session()
        self.session.trust_env = False
        self.addCleanup(self.session.close)

class RealProxyTests(ProxyFixture, unittest.TestCase):
    def test_authenticated_http_proxy_forwards_body_and_strips_proxy_credentials(self):
        proxy = egress.connect_url(self.node)
        self.session.proxies = {"http": proxy, "https": proxy}
        response = self.session.get(f"http://127.0.0.1:{self.http.server_port}/", timeout=3)
        self.assertEqual(response.status_code, 200)
        self.assertIn("available", response.text)
        self.assertNotIn("Proxy-Authorization", self.http.headers_received[-1])

    def test_health_success_does_not_mean_legacy_proxy_url_authenticates(self):
        health = self.session.get(self.node["health_url"], headers={"Authorization": "Bearer " + self.node["token"]}, timeout=3)
        self.assertEqual(health.status_code, 200)
        # Reproduce the old token@host URL: requests drops authentication because
        # parsed.password is None, despite the node health check succeeding.
        proxy = egress.connect_url(self.node).replace(":@", "@")
        self.session.proxies = {"http": proxy, "https": proxy}
        with self.assertRaisesRegex(requests.exceptions.ProxyError, "407"):
            self.session.get(f"https://127.0.0.1:{self.http.server_port}/", timeout=3)

    def test_generated_url_produces_basic_proxy_auth_with_explicit_empty_password(self):
        headers = requests.adapters.HTTPAdapter().proxy_headers(egress.connect_url(self.node))
        encoded = headers["Proxy-Authorization"].split(" ", 1)[1]
        self.assertEqual(base64.b64decode(encoded).decode(), self.node["token"] + ":")


class RealTLSProxyTests(ProxyFixture, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        openssl = shutil.which("openssl")
        if not openssl and Path("D:/Git/usr/bin/openssl.exe").is_file():
            openssl = "D:/Git/usr/bin/openssl.exe"
        if not openssl:
            raise unittest.SkipTest("openssl is needed to create a temporary local TLS certificate")
        super().setUpClass()
        cls.cert, key = cls.root / "localhost.crt", cls.root / "localhost.key"
        subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-keyout", str(key), "-out", str(cls.cert), "-subj", "/CN=localhost",
                        "-addext", "subjectAltName=IP:127.0.0.1"], check=True, capture_output=True, timeout=20)
        cls.https = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
        cls.https.daemon_threads = True
        cls.https.headers_received = []
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cls.cert, key)
        cls.https.socket = context.wrap_socket(cls.https.socket, server_side=True)
        cls.resources.callback(cls.https.server_close)
        cls.resources.callback(cls.https.shutdown)
        threading.Thread(target=cls.https.serve_forever, daemon=True).start()
        cls.target_url = f"https://127.0.0.1:{cls.https.server_port}"

    def test_requests_reads_verified_https_through_authenticated_connect(self):
        self.session.verify = str(self.cert)
        body, final_url = monitor.fetch_page(self.session, self.target_url, {"mode": "requests", "egress_proxy_url": egress.connect_url(self.node)}, "127.0.0.1")
        self.assertIn("available", body)
        self.assertEqual(final_url, self.target_url + "/")
        self.assertNotIn("Proxy-Authorization", self.https.headers_received[-1])

    def test_cloudscraper_reads_verified_https_through_authenticated_connect(self):
        with cloudscraper.create_scraper(interpreter="native") as scraper:
            scraper.trust_env = False
            scraper.verify = str(self.cert)
            body, _ = monitor.fetch_with_cloudscraper(self.target_url, "127.0.0.1", {
                "mode": "cloudscraper", "egress_proxy_url": egress.connect_url(self.node), "_cloudscraper_session": scraper})
        self.assertIn("available", body)

    def test_probe_verifies_real_https_proxy_and_uses_ip_from_tunnel(self):
        session_factory = requests.Session
        def local_session():
            session = session_factory()
            session.verify = str(self.cert)
            return session
        with patch.object(egress, "IP_CHECK_URLS", (self.target_url + "/ip",)), patch.object(egress.requests, "Session", side_effect=local_session):
            result = egress.probe(self.node, timeout=3)
        self.assertTrue(result["proxy_verified"])
        self.assertEqual(result["public_ip"], "203.0.113.42")
        self.assertEqual(result["reported_public_ip"], "203.0.113.10")

    def test_probe_cannot_report_success_if_proxy_authentication_fails(self):
        legacy_url = egress.connect_url(self.node).replace(":@", "@")
        with patch.object(egress, "IP_CHECK_URLS", (self.target_url + "/ip",)), patch.object(egress, "connect_url", return_value=legacy_url):
            with self.assertRaisesRegex(ValueError, "节点在线.*407") as error:
                egress.probe(self.node, timeout=3)
        self.assertNotIn(self.node["token"], str(error.exception))

    def test_probe_keeps_tls_certificate_validation_enabled(self):
        with patch.object(egress, "IP_CHECK_URLS", (self.target_url + "/ip",)):
            with self.assertRaisesRegex(ValueError, "证书验证失败"):
                egress.probe(self.node, timeout=3)


class ProbeFailureTests(unittest.TestCase):
    node = {"id": "local-test", "name": "Local test", "proxy_url": "http://127.0.0.1:8787",
            "health_url": "http://127.0.0.1:8787/health", "token": "proxy-test-token"}

    def session(self):
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        health = Mock(status_code=200)
        health.json.return_value = {"node_id": "local-test", "public_ip": "203.0.113.10"}
        session.get.return_value = health
        return session, health

    def test_health_auth_errors_are_actionable_and_redacted(self):
        session, health = self.session()
        health.status_code = 401
        health.raise_for_status.side_effect = requests.HTTPError("sensitive URL")
        with patch.object(egress.requests, "Session", return_value=session), self.assertRaisesRegex(ValueError, "401") as error:
            egress.probe(self.node)
        self.assertNotIn("sensitive", str(error.exception))

    def test_ip_service_failure_falls_back_without_sending_health_credentials(self):
        session, health = self.session()
        result = Mock(text="203.0.113.42")
        session.get.side_effect = [health, requests.Timeout("sensitive URL"), result]
        with patch.object(egress.requests, "Session", return_value=session):
            output = egress.probe(self.node)
        self.assertTrue(output["proxy_verified"])
        self.assertEqual(session.get.call_count, 3)
        self.assertNotIn("headers", session.get.call_args_list[1].kwargs)
        self.assertFalse(session.trust_env)

    def test_both_ip_services_failing_never_uses_reported_ip_as_success(self):
        session, health = self.session()
        session.get.side_effect = [health, requests.Timeout("private-token"), requests.Timeout("private-token")]
        with patch.object(egress.requests, "Session", return_value=session), self.assertRaisesRegex(ValueError, "尚未验证 HTTPS 代理可用") as error:
            egress.probe(self.node)
        self.assertNotIn("private-token", str(error.exception))


if __name__ == "__main__":
    unittest.main()
