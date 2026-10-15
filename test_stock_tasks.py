from contextlib import nullcontext
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import admin
import monitor


def product():
    return {"id": "test", "name": "test", "adapter": "html", "url": "https://shop.example.com/item", "stock_selector": ".stock", "request_mode": "inherit", "enabled": False}


class StockTaskTests(unittest.TestCase):
    def task(self, browser=False):
        return admin.StockTestTask("task-1234567890123456", admin.validate_config({"products": [product()]})["products"][0], admin.normalize_fetcher({"mode": "flaresolverr" if browser else "requests"}))

    def test_stop_terminates_real_worker_without_waiting_for_request_timeout(self):
        task = self.task()
        real_popen = subprocess.Popen
        ready = threading.Event()

        def spawn(*args, **kwargs):
            process = real_popen([sys.executable, "-c", "import sys,time; sys.stdin.read(); time.sleep(30)"], **kwargs)
            ready.set()
            return process

        with patch.object(admin.subprocess, "Popen", side_effect=spawn):
            thread = threading.Thread(target=task.run)
            thread.start()
            self.assertTrue(ready.wait(3))
            task.cancel()
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertIsNotNone(task.process.poll())
        self.assertTrue(task.snapshot()["cancelled"])
        self.assertFalse(task.snapshot()["pending"])

    def test_normal_result_and_no_test_history_side_effects(self):
        task = self.task()
        process = Mock()
        process.communicate.return_value = (json.dumps({"ok": True, "available": 4, "stock_mode": "quantity"}), None)
        process.poll.return_value = 0
        with patch.object(admin.subprocess, "Popen", return_value=process), patch.object(admin, "save_config") as save, patch.object(monitor, "write_state") as history, patch.object(monitor, "telegram_request") as notify:
            task.run()
        self.assertEqual(task.snapshot()["available"], 4)
        for action in (save, history, notify):
            action.assert_not_called()

    def test_browser_has_dedicated_session_and_is_destroyed_after_result(self):
        task = self.task(True)
        process = Mock()
        process.communicate.return_value = (json.dumps({"ok": True, "available": 0, "stock_mode": "quantity"}), None)
        process.poll.return_value = 0
        response = Mock(ok=True)
        response.json.return_value = {"status": "ok", "session": task.session_id}
        with patch.object(monitor, "browser_request_slot", return_value=nullcontext()), patch.object(monitor, "check_browser_memory"), patch.object(admin.requests, "post", return_value=response) as post, patch.object(admin.subprocess, "Popen", return_value=process), patch.object(admin, "destroy_test_browser", return_value="") as destroy:
            task.run()
        self.assertEqual(post.call_args.kwargs["json"], {"cmd": "sessions.create", "session": task.session_id})
        submitted = json.loads(process.communicate.call_args.args[0])
        self.assertEqual(submitted["fetcher"]["_test_browser_session"], task.session_id)
        self.assertTrue(submitted["fetcher"]["_browser_slot_held"])
        destroy.assert_called_once_with(task.fetcher["flaresolverr_url"], task.session_id, False)

    def test_stop_during_browser_creation_prevents_worker_and_cleans_created_session(self):
        task = self.task(True)
        creating, release = threading.Event(), threading.Event()

        def create(*args, **kwargs):
            creating.set()
            release.wait(3)
            response = Mock(ok=True)
            response.json.return_value = {"status": "ok"}
            return response

        with patch.object(monitor, "browser_request_slot", return_value=nullcontext()), patch.object(monitor, "check_browser_memory"), patch.object(admin.requests, "post", side_effect=create), patch.object(admin.subprocess, "Popen") as process, patch.object(admin, "destroy_test_browser", return_value="") as destroy:
            thread = threading.Thread(target=task.run)
            thread.start()
            self.assertTrue(creating.wait(3))
            task.cancel()
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        process.assert_not_called()
        destroy.assert_called_once()
        self.assertTrue(task.snapshot()["cancelled"])

    def test_low_memory_prevents_browser_creation(self):
        task = self.task(True)
        with patch.object(monitor, "browser_request_slot", return_value=nullcontext()), patch.object(monitor.Path, "read_text", return_value="MemAvailable: 120000 kB\n"), patch.object(admin.requests, "post") as post, patch.object(admin.subprocess, "Popen") as process:
            task.run()
        self.assertIn("内存不足", task.snapshot()["error"])
        post.assert_not_called()
        process.assert_not_called()

    def test_worker_crash_or_timeout_is_reaped(self):
        for timeout in (True, False):
            task = self.task()
            process = Mock()
            process.communicate.side_effect = [subprocess.TimeoutExpired("worker", 10), ("", None)] if timeout else [("", None)]
            process.poll.return_value = 1
            with self.subTest(timeout=timeout), patch.object(admin.subprocess, "Popen", return_value=process):
                task.run()
                self.assertFalse(task.snapshot()["ok"])
                self.assertFalse(task.snapshot()["pending"])
                if timeout:
                    process.kill.assert_called_once()

    def test_cleanup_failure_is_not_reported_as_resources_released(self):
        response = Mock()
        response.json.return_value = {"status": "error", "message": "The session doesn't exist."}
        with patch.object(admin.requests, "post", return_value=response), patch.object(admin.time, "sleep"):
            self.assertEqual(admin.destroy_test_browser("http://127.0.0.1:8191/v1", "own-session"), "")
            self.assertIn("未能确认", admin.destroy_test_browser("http://127.0.0.1:8191/v1", "own-session", creation_uncertain=True))

    def test_real_browser_worker_success_and_cancellation_cleanup(self):
        for cancel in (False, True):
            commands = []
            requested, destroyed = threading.Event(), threading.Event()

            class BrowserService(BaseHTTPRequestHandler):
                def do_POST(self):
                    value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    commands.append(value)
                    result = {"status": "ok"}
                    if value["cmd"] == "request.get":
                        requested.set()
                        if cancel:
                            destroyed.wait(5)
                        result["solution"] = {"status": 200, "url": value["url"], "response": '<p class="stock">4</p>'}
                    elif value["cmd"] == "sessions.destroy":
                        destroyed.set()
                    body = json.dumps(result).encode()
                    try:
                        self.send_response(200)
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    except (BrokenPipeError, ConnectionResetError):
                        pass

                def log_message(self, *args):
                    pass

            server = ThreadingHTTPServer(("127.0.0.1", 0), BrowserService)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            task = self.task(True)
            task.fetcher["flaresolverr_url"] = f"http://127.0.0.1:{server.server_port}/v1"
            try:
                with patch.object(monitor, "browser_request_slot", return_value=nullcontext()), patch.object(monitor, "check_browser_memory"):
                    thread = threading.Thread(target=task.run)
                    thread.start()
                    self.assertTrue(requested.wait(5))
                    if cancel:
                        task.cancel()
                    thread.join(5)
                    self.assertFalse(thread.is_alive())
                self.assertTrue(destroyed.is_set())
                self.assertEqual([value["cmd"] for value in commands], ["sessions.create", "request.get", "sessions.destroy"])
                self.assertTrue(all(value["session"] == task.session_id for value in commands))
                if cancel:
                    self.assertTrue(task.snapshot()["cancelled"])
                else:
                    self.assertEqual(task.snapshot()["available"], 4)
            finally:
                destroyed.set()
                server.shutdown()
                server.server_close()

    def test_only_one_test_runs_and_registry_is_bounded(self):
        with patch.dict(admin.TEST_TASKS, {}, clear=True), patch.object(admin.threading.Thread, "start"):
            submitted = {"task_id": "a1234567890123456", "product": product()}
            self.assertTrue(admin.start_stock_test(submitted)["pending"])
            with self.assertRaisesRegex(ValueError, "已有商品测试"):
                admin.start_stock_test({**submitted, "task_id": "b1234567890123456"})
            for i in range(30):
                for task in admin.TEST_TASKS.values():
                    task.done.set()
                admin.start_stock_test({**submitted, "task_id": f"task-{i:016}"})
            self.assertLessEqual(len(admin.TEST_TASKS), 20)

    @unittest.skipUnless(sys.platform == "linux", "Debian shared browser lock")
    def test_browser_lock_prevents_overlap_between_processes(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"VMISS_CONFIG": str(Path(directory) / "config.json")}):
            with monitor.browser_request_slot():
                result = subprocess.run([sys.executable, "-c", "from monitor import browser_request_slot;\nwith browser_request_slot(): pass"], capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("另一个浏览器请求", result.stderr)


if __name__ == "__main__":
    unittest.main()
