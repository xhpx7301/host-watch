"""Small authenticated HTTP CONNECT proxy for a HostWatch egress server.

Run this file on a sub-server. It exposes the sub-server's public IP as the
request source for the main HostWatch server. Keep the listening port behind a
firewall and set EGRESS_TOKEN to a long random value.
"""
from __future__ import annotations

import base64
import json
import os
import select
import socket
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import requests


NODE_ID = os.getenv("EGRESS_NODE_ID", "egress-node")
TOKEN = os.getenv("EGRESS_TOKEN", "")
PUBLIC_IP = os.getenv("EGRESS_PUBLIC_IP", "")
REGISTER_URL = os.getenv("EGRESS_REGISTER_URL", "")
REGISTER_TOKEN = os.getenv("EGRESS_REGISTER_TOKEN", TOKEN)
LISTEN_HOST = os.getenv("EGRESS_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.getenv("EGRESS_LISTEN_PORT", "8787"))
NODE_NAME = os.getenv("EGRESS_NODE_NAME", NODE_ID)


def auth_ok(handler) -> bool:
    value = handler.headers.get("Proxy-Authorization", "")
    if not value.startswith("Basic "):
        return False
    try:
        username, _ = base64.b64decode(value[6:], validate=True).decode().split(":", 1)
    except (ValueError, UnicodeError):
        return False
    return bool(TOKEN) and username == TOKEN


def public_ip() -> str:
    if PUBLIC_IP:
        return PUBLIC_IP
    for endpoint in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            response = requests.get(endpoint, timeout=(3, 5))
            value = response.text.strip()
            if value:
                return value
        except requests.RequestException:
            continue
    return "unknown"


def relay(left: socket.socket, right: socket.socket) -> None:
    left.setblocking(False)
    right.setblocking(False)
    sockets = [left, right]
    try:
        while sockets:
            readable, _, exceptional = select.select(sockets, [], sockets, 60)
            if exceptional:
                return
            if not readable:
                return
            for source in readable:
                target = right if source is left else left
                data = source.recv(64 * 1024)
                if not data:
                    return
                target.sendall(data)
    except (OSError, ValueError):
        return
    finally:
        for sock in (left, right):
            try:
                sock.close()
            except OSError:
                pass


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def unauthorized(self):
        self.send_response(407)
        self.send_header("Proxy-Authenticate", "Basic realm=HostWatch-Egress")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            if self.headers.get("Authorization", "") != f"Bearer {TOKEN}":
                self.send_response(401); self.send_header("Content-Length", "0"); self.end_headers(); return
            body = json.dumps({"ok": True, "node_id": NODE_ID, "name": NODE_NAME, "public_ip": public_ip(), "checked_at": time.time()}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
        self.proxy_http()

    def do_POST(self):
        self.proxy_http()

    def do_CONNECT(self):
        if not auth_ok(self):
            self.unauthorized(); return
        try:
            host, port_text = self.path.rsplit(":", 1)
            port = int(port_text)
            upstream = socket.create_connection((host, port), timeout=20)
        except (OSError, ValueError):
            self.send_error(502, "upstream connection failed"); return
        self.send_response(200, "Connection Established")
        self.send_header("Content-Length", "0")
        self.end_headers()
        relay(self.connection, upstream)

    def proxy_http(self):
        if not auth_ok(self):
            self.unauthorized(); return
        parsed = urlsplit(self.path)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            self.send_error(400, "absolute URL required"); return
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            upstream = socket.create_connection((parsed.hostname, port), timeout=20)
            path = parsed.path or "/"
            if parsed.query: path += "?" + parsed.query
            headers = []
            for key, value in self.headers.items():
                if key.lower() in {"proxy-authorization", "proxy-connection", "connection", "keep-alive"}: continue
                headers.append(f"{key}: {value}\r\n".encode())
            upstream.sendall((f"{self.command} {path} HTTP/1.1\r\n").encode() + b"".join(headers) + b"Connection: close\r\n\r\n")
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length: upstream.sendall(self.rfile.read(min(length, 8 * 1024 * 1024)))
            relay(upstream, self.connection)
        except (OSError, ValueError):
            self.send_error(502, "upstream request failed")

    def log_message(self, format, *args):
        return


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def register_once():
    if not REGISTER_URL:
        return
    public_host = os.getenv("EGRESS_PUBLIC_HOST", "").strip() or public_ip()
    payload = {"node_id": NODE_ID, "name": NODE_NAME, "public_ip": public_ip(), "proxy_url": f"http://{public_host}:{LISTEN_PORT}", "health_url": f"http://{public_host}:{LISTEN_PORT}/health", "token": TOKEN}
    try:
        requests.post(REGISTER_URL.rstrip("/") + "/api/egress/register", json=payload, headers={"Authorization": f"Bearer {REGISTER_TOKEN}"}, timeout=(5, 10)).raise_for_status()
    except requests.RequestException:
        pass


def main():
    if not TOKEN:
        raise SystemExit("EGRESS_TOKEN must be set")
    def register_loop():
        while True:
            register_once()
            time.sleep(30)
    threading.Thread(target=register_loop, daemon=True).start()
    server = Server((LISTEN_HOST, LISTEN_PORT), ProxyHandler)
    print(f"HostWatch egress agent {NODE_ID} listening on {LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
