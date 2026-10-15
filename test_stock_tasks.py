from contextlib import nullcontext
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import admin
import monitor


def product():
    return {"id": "test", "name": "test", "adapter": "html", "url": "https://shop.example.com/item", "stock_selector": ".stock", "request_mode": "inherit", "enabled": False}


class StockTaskTests(unittest.TestCase):
    def setUp(self):
        credentials = patch.object(admin, "telegram_credentials", return_value=("", ""))
        credentials.start()
        self.addCleanup(credentials.stop)

    def task(self, browser=False):
        return admin.StockTestTask("task-1234567890123456", admin.validate_config({"products": [product()]})["products"][0], admin.normalize_fetcher({"mode": "flaresolverr" if browser else "requests"}))

    def test_stop_terminates_real_worker_without_waiting_for_request_timeout(self):
        task = self.task()
        real_popen = subprocess.Popen
        ready = threading.Event()
        workers = []

        def spawn(*args, **kwargs):
            process = real_popen([sys.executable, "-c", "import sys,time; sys.stdin.read(); time.sleep(30)"], **kwargs)
            workers.append(process)
            ready.set()
            return process

        with patch.object(admin.subprocess, "Popen", side_effect=spawn):
            thread = threading.Thread(target=task.run)
            thread.start()
            self.assertTrue(ready.wait(3))
            task.cancel()
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertIsNotNone(workers[0].poll())
        self.assertTrue(task.snapshot()["cancelled"])
        self.assertFalse(task.snapshot()["pending"])

    def test_stop_kills_worker_that_ignores_sigterm(self):
        task = self.task()
        real_popen = subprocess.Popen
        workers = []
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / "ready"
            code = "import signal,time,pathlib; signal.signal(signal.SIGTERM, signal.SIG_IGN); pathlib.Path(" + repr(str(ready)) + ").touch(); time.sleep(30)"

            def spawn(*args, **kwargs):
                child = real_popen([sys.executable, "-c", code], **kwargs)
                workers.append(child)
                return child

            with patch.object(admin.subprocess, "Popen", side_effect=spawn):
                thread = threading.Thread(target=task.run)
                thread.start()
                try:
                    deadline = time.monotonic() + 5
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(ready.exists())
                    task.cancel()
                    thread.join(3)
                    self.assertFalse(thread.is_alive())
                    self.assertTrue(task.snapshot()["cancelled"])
                finally:
                    for child in workers:
                        admin.kill_test_process(child)
                    thread.join(3)

    def test_stop_before_delayed_start_never_launches_child(self):
        task_id = "early-stop-1234567890123456"
        with patch.dict(admin.TEST_TASKS, {}, clear=True), patch.dict(admin.STOPPED_TEST_IDS, {}, clear=True), patch.object(admin.subprocess, "Popen") as spawn:
            self.assertTrue(admin.stop_stock_test(task_id)["cancelled"])
            admin.start_stock_test({"task_id": task_id, "product": product()})
            task = admin.TEST_TASKS[task_id]
            self.assertTrue(task.done.wait(3))
            self.assertTrue(task.snapshot()["cancelled"])
        spawn.assert_not_called()

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

    def test_manual_results_send_once_including_sold_out_and_errors(self):
        for result, expected in [
            ({"ok": True, "available": 4, "stock_mode": "quantity"}, "库存：4 件"),
            ({"ok": True, "available": 0, "stock_mode": "quantity"}, "库存：缺货"),
            ({"ok": True, "available": 1, "stock_mode": "availability"}, "库存：有货"),
            ({"ok": False, "error": "HTTP 403"}, "测试失败：HTTP 403"),
        ]:
            task = self.task()
            process = Mock()
            process.communicate.return_value = (json.dumps(result), None)
            process.poll.return_value = 0
            with self.subTest(result=result), patch.object(admin.subprocess, "Popen", return_value=process), patch.object(admin, "telegram_credentials", return_value=("test-token", "123")), patch.object(admin, "telegram_call") as send:
                task.run()
                self.assertEqual(task.snapshot()["notification"]["status"], "sent")
                for _ in range(3):
                    task.snapshot()
                send.assert_called_once()
                self.assertEqual(send.call_args.args, ("test-token", "sendMessage"))
                self.assertEqual(send.call_args.kwargs["chat_id"], "123")
                self.assertIn(expected, send.call_args.kwargs["text"])
                self.assertIn("商品：test", send.call_args.kwargs["text"])
                self.assertIn("检查时间：", send.call_args.kwargs["text"])

    def test_cancelled_task_never_sends_result(self):
        task = self.task()
        task.cancel()
        with patch.object(admin, "telegram_call") as send, patch.object(admin.subprocess, "Popen") as spawn:
            task.run()
        send.assert_not_called()
        spawn.assert_not_called()
        self.assertEqual(task.snapshot()["notification"]["status"], "skipped")

    def test_akile_worker_and_notification_report_actual_catalog_match(self):
        resolved = monitor.Product("1017", "MOLite-高级竞技A", 0,
            "https://akile.ai/shop/server?type=traffic&areaId=14&nodeId=38&planId=1017",
            vendor="Akile", area_id="14", area_name="中国澳门", node_name="MOLite")
        output = io.StringIO()
        submitted = {"product": {"adapter": "akile"}, "fetcher": {"mode": "requests"}}
        with patch.object(admin.sys, "stdin", io.StringIO(json.dumps(submitted))), patch.object(admin.sys, "stdout", output), patch.object(monitor, "inspect_product", return_value=resolved):
            admin.stock_test_worker()
        result = json.loads(output.getvalue())
        self.assertEqual(result["matched_product"], {"area_id": "14", "area_name": "中国澳门", "node_name": "MOLite", "plan_name": "MOLite-高级竞技A"})
        self.assertEqual(result["available"], 0)
        result.update(checked_at="2026-10-15T17:00:00+08:00", duration_seconds=0.1)
        with patch.object(admin, "telegram_credentials", return_value=("test-token", "123")), patch.object(admin, "telegram_call") as send:
            notification = admin.send_stock_test_result({"name": "MOLite", "adapter": "akile"}, result)
        self.assertEqual(notification["status"], "sent")
        message = send.call_args.kwargs["text"]
        for expected in ["中国澳门", "ID=14", "MOLite-高级竞技A", resolved.url, "库存：缺货"]:
            self.assertIn(expected, message)

    def test_soy_test_result_and_message_include_checked_group(self):
        url = "https://www.vps.soy/cart?fid=1&gid=2"
        resolved = monitor.Product("日本软银", "日本软银", 0, url, vendor="VPS.SOY", catalog_url=url)
        output = io.StringIO()
        with patch.object(admin.sys, "stdin", io.StringIO(json.dumps({"product": {}, "fetcher": {}}))), patch.object(admin.sys, "stdout", output), patch.object(monitor, "inspect_product", return_value=resolved):
            admin.stock_test_worker()
        result = json.loads(output.getvalue())
        self.assertEqual(result["catalog_url"], url)
        result.update(checked_at="2026-10-15", duration_seconds=0.1)
        with patch.object(admin, "telegram_credentials", return_value=("test-token", "123")), patch.object(admin, "telegram_call") as send:
            self.assertEqual(admin.send_stock_test_result({"name": "日本软银", "adapter": "vps_soy"}, result)["status"], "sent")
        self.assertIn("检查分类：" + url, send.call_args.kwargs["text"])

    def test_missing_bot_and_delivery_failure_preserve_stock_result(self):
        for credentials, error, status in [(("", ""), None, "unconfigured"), (("test-token", "123"), admin.TelegramAPIError("无法连接 Telegram"), "failed")]:
            task = self.task()
            process = Mock()
            process.communicate.return_value = (json.dumps({"ok": True, "available": 4, "stock_mode": "quantity"}), None)
            process.poll.return_value = 0
            with self.subTest(status=status), patch.object(admin.subprocess, "Popen", return_value=process), patch.object(admin, "telegram_credentials", return_value=credentials), patch.object(admin, "telegram_call", side_effect=error) as send:
                task.run()
            self.assertTrue(task.done.is_set())
            self.assertTrue(task.snapshot()["ok"])
            self.assertEqual(task.snapshot()["available"], 4)
            self.assertEqual(task.snapshot()["notification"]["status"], status)
            if status == "unconfigured":
                send.assert_not_called()

    def test_delivery_phase_does_not_block_status_and_cannot_cancel_finished_check(self):
        task = self.task()
        sending, release = threading.Event(), threading.Event()
        process = Mock()
        process.communicate.return_value = (json.dumps({"ok": True, "available": 0, "stock_mode": "quantity"}), None)
        process.poll.return_value = 0

        def send(*args, **kwargs):
            sending.set()
            release.wait(3)

        with patch.object(admin.subprocess, "Popen", return_value=process), patch.object(admin, "telegram_credentials", return_value=("test-token", "123")), patch.object(admin, "telegram_call", side_effect=send):
            thread = threading.Thread(target=task.run)
            thread.start()
            try:
                self.assertTrue(sending.wait(3))
                self.assertTrue(task.snapshot()["notifying"])
                task.cancel()
                self.assertFalse(task.cancelled.is_set())
            finally:
                release.set()
                thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(task.snapshot()["notification"]["status"], "sent")

    def test_browser_has_dedicated_session_and_is_destroyed_after_result(self):
        task = self.task(True)
        with patch.object(monitor, "browser_request_slot", return_value=nullcontext()), patch.object(monitor, "check_browser_memory"), patch.object(task, "run_process", side_effect=[{"ok": True}, {"ok": True, "available": 0, "stock_mode": "quantity"}, {"ok": True}]) as run:
            task.run()
        create, check, destroy = run.call_args_list
        self.assertEqual(create.args[1]["session"], task.session_id)
        submitted = check.args[1]
        self.assertEqual(submitted["fetcher"]["_test_browser_session"], task.session_id)
        self.assertTrue(submitted["fetcher"]["_browser_slot_held"])
        self.assertEqual(destroy.args[1]["command"], "destroy")
        self.assertEqual(destroy.args[1]["session"], task.session_id)
        self.assertFalse(destroy.args[1]["creation_uncertain"])
        self.assertFalse(destroy.kwargs["cancellable"])

    def test_stop_during_browser_creation_prevents_worker_and_cleans_created_session(self):
        task = self.task(True)
        creating, release = threading.Event(), threading.Event()

        def run(mode, payload, timeout, cancellable=True):
            if payload["command"] == "destroy":
                self.assertTrue(payload["creation_uncertain"])
                return {"ok": False, "warning": admin.BROWSER_CLEANUP_WARNING}
            creating.set()
            release.wait(3)
            return {"ok": not task.cancelled.is_set()}

        with patch.object(monitor, "browser_request_slot", return_value=nullcontext()), patch.object(monitor, "check_browser_memory"), patch.object(admin.subprocess, "Popen") as process, patch.object(task, "run_process", side_effect=run) as child:
            thread = threading.Thread(target=task.run)
            thread.start()
            self.assertTrue(creating.wait(3))
            task.cancel()
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        process.assert_not_called()
        self.assertEqual(child.call_count, 2)
        self.assertTrue(task.snapshot()["cancelled"])
        self.assertTrue(task.done.is_set())
        self.assertIn("未能确认", task.snapshot()["warning"])

    def test_stuck_browser_initialization_is_killed_and_cleanup_is_bounded(self):
        task = self.task(True)
        real_popen = subprocess.Popen
        creating = threading.Event()
        workers = []

        def spawn(args, **kwargs):
            child = real_popen([sys.executable, "-c", "import sys,time; sys.stdin.read(); time.sleep(30)"], **kwargs)
            workers.append(child)
            creating.set()
            return child

        with patch.object(monitor, "browser_request_slot", return_value=nullcontext()), patch.object(monitor, "check_browser_memory"), patch.object(admin.subprocess, "Popen", side_effect=spawn), patch.object(admin, "BROWSER_CLEANUP_SECONDS", 0.2):
            thread = threading.Thread(target=task.run)
            thread.start()
            try:
                self.assertTrue(creating.wait(3))
                task.cancel()
                thread.join(3)
                self.assertFalse(thread.is_alive())
            finally:
                for child in workers:
                    admin.kill_test_process(child)
                thread.join(3)
        self.assertTrue(task.done.is_set())
        self.assertTrue(task.snapshot()["cancelled"])
        self.assertIn("未能确认", task.snapshot()["warning"])
        self.assertEqual(len(workers), 2)  # Creation and cleanup, never stock checking.
        self.assertTrue(all(child.poll() is not None for child in workers))

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
            process.poll.return_value = None if timeout else 1
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
                    try:
                        # WSL reads dependencies from the Windows-mounted disk;
                        # allow imports to finish before measuring cancellation.
                        self.assertTrue(requested.wait(15), task.snapshot())
                        if cancel:
                            task.cancel()
                        thread.join(10)
                        self.assertFalse(thread.is_alive(), task.snapshot())
                    finally:
                        task.cancel()
                        destroyed.set()
                        thread.join(10)
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


class StockTestTelegramTests(unittest.TestCase):
    def test_explicit_test_uses_saved_bot_even_when_automatic_notifications_disabled(self):
        saved = admin.default_config()
        saved["telegram"] = {"bot_token": "saved-token", "chat_id": "123", "disabled": True}
        result = {"ok": True, "available": 1, "stock_mode": "quantity", "checked_at": "2026-10-15T17:00:00+08:00", "duration_seconds": 0.5, "purchase_url": "https://shop.example.com/item"}
        with patch.object(admin, "current_config", return_value=saved), patch.object(admin, "telegram_call") as send:
            notification = admin.send_stock_test_result(product(), result)
        self.assertEqual(notification["status"], "sent")
        self.assertEqual(send.call_args.args, ("saved-token", "sendMessage"))
        self.assertEqual(send.call_args.kwargs["chat_id"], "123")
        self.assertIn("商品入口：https://shop.example.com/item", send.call_args.kwargs["text"])
        self.assertNotIn("parse_mode", send.call_args.kwargs)

    def test_api_error_does_not_expose_saved_token_in_notification_status(self):
        response = Mock(ok=False)
        response.json.return_value = {"ok": False, "description": "invalid private-token"}
        result = {"ok": False, "error": "HTTP 403", "checked_at": "2026-10-15", "duration_seconds": 0.5}
        with patch.object(admin, "telegram_credentials", return_value=("private-token", "123")), patch.object(admin.requests, "post", return_value=response):
            notification = admin.send_stock_test_result(product(), result)
        self.assertEqual(notification["status"], "failed")
        self.assertNotIn("private-token", json.dumps(notification))


if __name__ == "__main__":
    unittest.main()
