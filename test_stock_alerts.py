"""Exercise persistent Telegram restock cycles through the real Bot API client."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
from bs4 import BeautifulSoup

import admin
import monitor
from test_monitor import html_product


class RestockMessageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state, self.status = Path(temp.name) / "state.json", Path(temp.name) / "status.json"
        self.config = admin.validate_config({"enabled": True, "products": [html_product()], "notify_current_stock_on_start": True})
        self.session = Mock()
        self.calls, self.message_id, self.clock = [], 100, 1000
        self.fail = None
        self.session.post.side_effect = self.post
        self.session.get.side_effect = AssertionError("Bot mutation must use POST")
        self.fetch = patch.object(monitor, "fetch_html_stock")
        self.fetcher = self.fetch.start()
        self.addCleanup(self.fetch.stop)
        timer = patch("stock_alerts.time.time", side_effect=lambda: self.clock)
        timer.start()
        self.addCleanup(timer.stop)

    def post(self, endpoint, *, json, timeout):
        method = endpoint.rsplit("/", 1)[-1]
        self.calls.append((method, json))
        if self.fail:
            self.fail(method, json)
        self.message_id += method == "sendMessage"
        response = Mock()
        response.json.return_value = {"ok": True, "result": {"message_id": json.get("message_id", self.message_id)}}
        return response

    def tick(self, count, *, token="123:secret", chat="456", error=None):
        self.clock += 1
        self.fetcher.side_effect = error
        self.fetcher.return_value = monitor.Product("p", "Live plan", count, "https://shop.example.com/order", vendor="ExampleHost",
            specs="1 核 · 512 MB · 10 GB", traffic="200 GB · 100 Mbps", price="CNY 25 / 月")
        monitor.check_once(self.session, self.config, self.state, self.status, token, chat)

    def entry(self):
        return monitor.read_state(self.state)["vps-2g"]

    def text(self, index=-1):
        return BeautifulSoup(self.calls[index][1]["text"], "html.parser").get_text()

    def methods(self):
        return [method for method, payload in self.calls]

    def test_one_new_message_per_cycle_increases_decreases_and_sellout_edit(self):
        self.tick(0)
        self.tick(2)
        first_id = self.entry()["telegram"]["message_id"]
        self.tick(5)
        self.assertIn("有货 · 剩余 5 台", self.text())
        self.tick(1)
        self.tick(0)
        self.assertIn("❌ HostWatch · 已售罄", self.text())
        self.assertIn("库存：售罄", self.text())
        self.assertNotIn("增加 -", self.text())
        self.assertEqual(self.methods(), ["sendMessage", "editMessageText", "editMessageText", "editMessageText"])
        self.assertTrue(all(payload["message_id"] == first_id for method, payload in self.calls[1:]))
        self.tick(0)
        self.assertEqual(len(self.calls), 4)
        self.tick(3)
        self.assertEqual(self.methods()[-1], "sendMessage")
        self.assertNotEqual(self.entry()["telegram"]["message_id"], first_id)
        self.assertEqual(self.methods().count("sendMessage"), 2)

    def test_first_stock_off_is_baseline_and_quantity_increase_is_quiet(self):
        self.config["notify_current_stock_on_start"] = False
        self.tick(2)
        self.tick(7)
        self.assertEqual(self.calls, [])
        self.tick(0)
        self.tick(1)
        self.assertEqual(self.methods(), ["sendMessage"])

    def test_upgrade_keeps_existing_baseline_and_waits_for_next_restock(self):
        monitor.write_state(self.state, {"vps-2g": {"available": 2, "notify_enabled": True}})
        self.tick(5)
        self.assertEqual(self.calls, [])
        self.tick(0)
        self.tick(3)
        self.assertEqual(self.methods(), ["sendMessage"])

    def test_manual_test_does_not_replace_or_edit_automatic_message(self):
        self.tick(2)
        baseline = self.state.read_bytes()
        result = {"ok": True, "available": 5, "name": "Manual plan", "checked_at": "test", "duration_seconds": 0.1}
        with patch.object(admin, "telegram_credentials", return_value=("123:secret", "456")), patch.object(admin, "current_config", return_value=self.config), patch.object(admin, "telegram_call") as send:
            admin.send_stock_test_result(self.config["products"][0], result)
        self.assertEqual(send.call_args.args[1], "sendMessage")
        self.assertEqual(self.state.read_bytes(), baseline)
        self.tick(3)
        self.assertEqual(self.methods(), ["sendMessage", "editMessageText"])

    def test_failure_of_one_vendor_does_not_mark_already_checked_product_uncertain(self):
        self.config["products"].append(html_product(id="second"))
        self.tick(2)
        good = self.fetcher.return_value
        self.fetcher.side_effect = [good, monitor.SiteBlocked("blocked")]
        with self.assertRaises(monitor.SiteBlocked):
            monitor.check_once(self.session, self.config, self.state, self.status, "123:secret", "456")
        self.assertEqual(self.methods(), ["sendMessage", "sendMessage", "editMessageText"])
        second = monitor.read_state(self.state)["second"]["telegram"]["message_id"]
        self.assertEqual(self.calls[-1][1]["message_id"], second)

    def test_first_stock_on_sends_once_and_restart_preserves_message_and_throttles(self):
        self.tick(2)
        message_id = self.entry()["telegram"]["message_id"]
        self.tick(2)  # Reconstruct the tracker and read the real file each time.
        self.assertEqual(self.methods(), ["sendMessage"])
        self.clock += 60
        self.tick(2)
        self.assertEqual(self.methods(), ["sendMessage", "editMessageText"])
        self.assertEqual(self.calls[-1][1]["message_id"], message_id)
        self.assertIn("最后成功检查", self.text())
        self.assertNotIn("secret", self.state.read_text(encoding="utf-8"))

    def test_availability_only_does_not_invent_quantities(self):
        self.tick(1)
        product = self.fetcher.return_value
        self.fetcher.return_value = monitor.replace(product, stock_mode="availability")
        monitor.check_once(self.session, self.config, self.state, self.status, "123:secret", "456")
        text = self.text()
        self.assertIn("库存：有货", text)
        self.assertNotIn("台", text)

    def test_fetch_failure_marks_original_uncertain_and_recovery_edits_it(self):
        self.tick(2)
        success_time = self.entry()["telegram"]["snapshot"]["checked_at"]
        for error in (monitor.SiteBlocked("HTTP 403 secret"), requests.Timeout("secret")):
            with self.assertRaises(type(error)):
                self.tick(0, error=error)
            self.assertEqual(self.entry()["available"], 2)
            self.assertEqual(self.entry()["telegram"]["snapshot"]["checked_at"], success_time)
            self.assertIn("库存暂未确认", self.text())
            self.assertNotIn("secret", self.text())
            self.assertNotIn("已售罄", self.text())
        self.assertEqual(len(self.calls), 2)  # Repeat failures don't re-edit.
        self.tick(2)
        self.assertEqual(self.methods(), ["sendMessage", "editMessageText", "editMessageText"])
        self.assertNotIn("暂未确认", self.text())

    def test_unlisted_product_keeps_baseline_and_updates_original_to_uncertain(self):
        self.tick(3)
        self.tick(0, error=monitor.ProductUnlisted("下架"))
        self.assertEqual(self.entry()["available"], 3)
        self.assertIn("库存暂未确认", self.text())
        self.assertEqual(json.loads(self.status.read_text(encoding="utf-8"))["state"], "partial")
        self.tick(4)
        self.assertEqual(self.methods().count("sendMessage"), 1)
        self.assertIn("剩余 4 台", self.text())

    def test_deleted_message_does_not_create_replacement_until_next_restock(self):
        self.tick(2)
        def deleted(method, payload):
            raise monitor.TelegramMessageUnavailable("原通知已删除")
        self.fail = deleted
        self.tick(3)
        self.assertTrue(self.entry()["telegram"]["uneditable"])
        self.fail = None
        self.tick(4)
        self.tick(0)
        self.assertEqual(self.methods().count("sendMessage"), 1)
        self.tick(2)
        self.assertEqual(self.methods().count("sendMessage"), 2)

    def test_failed_send_retries_without_stock_increase(self):
        self.fail = lambda *args: (_ for _ in ()).throw(requests.Timeout("secret"))
        self.tick(2)
        self.assertTrue(self.entry()["telegram"]["pending_send"])
        self.assertEqual(json.loads(self.status.read_text(encoding="utf-8"))["state"], "partial")
        self.fail = None
        self.tick(2)
        self.assertTrue(self.entry()["telegram"]["open"])
        self.tick(2)
        self.assertEqual(self.methods(), ["sendMessage", "sendMessage"])

    def test_failed_edit_retries_and_never_sends_replacement(self):
        self.tick(2)
        self.fail = lambda *args: (_ for _ in ()).throw(requests.Timeout())
        self.tick(5)
        self.assertEqual(self.entry()["available"], 5)
        self.fail = None
        self.tick(5)
        self.assertEqual(self.methods(), ["sendMessage", "editMessageText", "editMessageText"])
        self.assertIn("剩余 5 台", self.text())

    def test_failed_final_edit_is_retried_before_new_restock_even_after_repeated_failure(self):
        self.tick(2)
        first_id = self.entry()["telegram"]["message_id"]
        self.fail = lambda *args: (_ for _ in ()).throw(requests.Timeout())
        self.tick(0)
        self.assertTrue(self.entry()["telegram"]["pending_close"])
        self.tick(4)
        self.tick(6)
        self.fail = None
        self.tick(6)
        self.assertEqual(self.calls[-2][0], "editMessageText")
        self.assertEqual(self.calls[-2][1]["message_id"], first_id)
        self.assertIn("库存：售罄", self.text(-2))
        self.assertEqual(self.calls[-1][0], "sendMessage")
        self.assertEqual(self.methods().count("sendMessage"), 2)

    def test_partial_batch_success_is_not_resent_when_another_product_retries(self):
        self.config["products"].append(html_product(id="second"))
        def fail_second(method, payload):
            if method == "sendMessage" and len(self.calls) == 2:
                raise requests.ConnectionError()
        self.fail = fail_second
        self.tick(2)
        first_id = self.entry()["telegram"]["message_id"]
        self.fail = None
        self.tick(2)
        self.assertEqual(self.entry()["telegram"]["message_id"], first_id)
        self.assertEqual(self.methods(), ["sendMessage", "sendMessage", "sendMessage"])
        self.tick(2)
        self.assertEqual(len(self.calls), 3)

    def test_failed_send_that_sells_out_before_retry_is_not_sent_as_in_stock(self):
        self.fail = lambda *args: (_ for _ in ()).throw(requests.Timeout())
        self.tick(2)
        self.fail = None
        self.tick(0)
        self.assertNotIn("telegram", self.entry())
        self.assertEqual(len(self.calls), 1)
        self.tick(1)
        self.assertEqual(self.methods(), ["sendMessage", "sendMessage"])

    def test_bot_or_chat_change_never_edits_previous_target(self):
        for token, chat in (("123:other-secret", "456"), ("123:secret", "999")):
            with self.subTest(chat=chat):
                self.state.unlink(missing_ok=True)
                self.calls.clear()
                self.tick(2)
                self.tick(3, token=token, chat=chat)
                self.assertEqual(self.methods(), ["sendMessage"])
                self.tick(0, token=token, chat=chat)
                self.tick(2, token=token, chat=chat)
                self.assertEqual(self.methods(), ["sendMessage", "sendMessage"])
                self.assertEqual(self.calls[-1][1]["chat_id"], chat)

    def test_unsubscribe_and_disabled_telegram_do_not_edit_messages(self):
        self.tick(2)
        product = self.config["products"][0]
        product.update(watch_enabled=True, notify_enabled=False)
        self.tick(4)
        self.assertNotIn("telegram", self.entry())
        self.assertEqual(len(self.calls), 1)
        product["notify_enabled"] = True
        self.tick(4)
        self.assertEqual(len(self.calls), 2)  # First-stock option on resubscribe.
        self.config["telegram"]["disabled"] = True
        self.tick(0)
        self.assertEqual(len(self.calls), 2)

    def test_aff_and_metadata_changes_update_same_message_and_keyboard(self):
        self.tick(2)
        self.config["products"][0]["purchase_url"] = "https://shop.example.com/new-order"
        self.config["products"][0]["remarks"] = "<b>自用</b>"
        self.tick(2)
        self.assertEqual(self.methods(), ["sendMessage", "editMessageText"])
        self.assertEqual(self.calls[-1][1]["reply_markup"]["inline_keyboard"][0][0]["url"], "https://shop.example.com/new-order")
        self.assertIn("&lt;b&gt;自用&lt;/b&gt;", self.calls[-1][1]["text"])


class TelegramEditAPITests(unittest.TestCase):
    def test_not_modified_and_deleted_errors_are_distinguished_without_leaking_tokens(self):
        for description, expected in (("message is not modified", None), ("message to edit not found", monitor.TelegramMessageUnavailable),
                                      ("message can't be edited", monitor.TelegramMessageUnavailable), ("bad unrelated secret", monitor.MonitorError)):
            with self.subTest(description=description):
                session = Mock()
                session.post.return_value.json.return_value = {"ok": False, "error_code": 400, "description": "Bad Request: " + description}
                session.post.return_value.raise_for_status.side_effect = requests.HTTPError("secret-token")
                if expected:
                    with self.assertRaises(expected) as caught:
                        monitor.telegram_request(session, "secret-token", "editMessageText", chat_id="123", message_id=1, text="test")
                    self.assertNotIn("secret", str(caught.exception))
                else:
                    self.assertTrue(monitor.telegram_request(session, "secret-token", "editMessageText", chat_id="123", message_id=1, text="test")["ok"])
                session.get.assert_not_called()

    def test_real_monitor_entrypoint_keeps_exception_types_and_installed_module_importable(self):
        # Running monitor.py as __main__ must use that module's own client and
        # exception classes, rather than importing a second copy of monitor.
        script = '''import runpy,sys
from unittest.mock import Mock,patch
with patch.object(sys,'argv',['monitor.py','--once']), patch('requests.Session') as factory:
    session=factory.return_value
    response=session.post.return_value
    response.json.return_value={'ok':True,'result':{'message_id':99}}
    page=session.get.return_value
    page.status_code=200;page.url='https://shop.example.com/item';page.headers={};page.text='<p class="stock">2</p>'
    runpy.run_path('monitor.py',run_name='__main__')
'''
        with tempfile.TemporaryDirectory() as temp:
            import os
            config = admin.validate_config({"enabled": True, "notify_current_stock_on_start": True, "products": [html_product()],
                "telegram": {"bot_token": "123:fake", "chat_id": "456"}})
            path = Path(temp)
            (path / "config.json").write_text(json.dumps(config), encoding="utf-8")
            env = {**os.environ, "VMISS_CONFIG": str(path / "config.json"), "STATE_FILE": str(path / "state.json"), "VMISS_STATUS": str(path / "status.json")}
            result = subprocess.run([sys.executable, "-X", "utf8", "-c", script], env=env, capture_output=True, text=True, encoding="utf-8", timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            state = monitor.read_state(path / "state.json")
            self.assertEqual(state["vps-2g"]["telegram"]["message_id"], 99)
            script = script.replace("response.json.return_value=", "import requests;response.raise_for_status.side_effect=requests.Timeout('secret');response.json.return_value=")
            # Force an edit after the new tracker is constructed under __main__.
            saved = monitor.read_state(path / "state.json")
            saved["vps-2g"]["telegram"]["signature"] = "force-edit"
            monitor.write_state(path / "state.json", saved)
            result = subprocess.run([sys.executable, "-X", "utf8", "-c", script], env=env, capture_output=True, text=True, encoding="utf-8", timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads((path / "status.json").read_text(encoding="utf-8"))["state"], "partial")


if __name__ == "__main__":
    unittest.main()
