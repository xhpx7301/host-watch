"""Order-only seckill regressions: no live merchant requests or payments."""
import copy
import json
import time
import unittest
from unittest.mock import Mock, patch

import requests
import admin
from monitor import Product
from test_monitor import html_product
from test_admin import AdminAPITests as _AdminAPIBase


def configuration(quantity=2):
    value = admin.default_config()
    value['products'] = [html_product(id='plan')]
    value['vps_seckill'].update(items=[{'id': 'one', 'product_id': 'plan',
        'target_url': 'https://shop.example.com/buy', 'submit_url': 'https://shop.example.com/orders',
        'account_id': 'account', 'quantity': quantity, 'auto_submit': True}],
        accounts=[{'id': 'account', 'name': 'Main', 'username': 'user', 'password': 'private-password',
            'login_url': 'https://shop.example.com/account-check', 'auto_submit': True}])
    return admin.validate_config(value)


def response(data=None, status=200):
    result = Mock(status_code=status)
    result.json.return_value = data
    return result


class VmissResponse:
    def __init__(self, text="", status_code=200, url="https://app.vmiss.com/cart.php"):
        self.text = text
        self.content = text.encode()
        self.status_code = status_code
        self.url = url
        self.headers = {}


def vmiss_config_page():
    return """<form id="frmConfigureProduct">
      <input type="hidden" name="configure" value="true"><input type="hidden" name="i" value="0">
      <input type="radio" name="billingcycle" value="monthly" checked>
      <select name="configoption[35]"><option value="180">AlmaLinux 9</option><option value="173">Debian 12</option></select>
      <input type="text" name="hostname" value=""><input type="password" name="rootpw" value="N/A">
      <input type="text" name="ns1prefix" value="ns1"><input type="text" name="ns2prefix" value="ns2">
    </form>"""


def vmiss_checkout_page():
    return """<form method="post" action="/cart.php?a=checkout" id="frmCheckout">
      <input type="hidden" name="token" value="checkout-token"><input type="hidden" name="submit" value="true">
      <input type="radio" name="custtype" value="existing" checked>
      <input type="text" name="loginemail" value="user@example.com"><input type="password" name="loginpassword">
      <input type="radio" name="applycredit" value="1" checked><input type="radio" name="applycredit" value="0">
      <input type="radio" name="paymentmethod" value="stripe" checked><input type="radio" name="paymentmethod" value="motionpayalipay">
      <input type="tel" name="ccnumber"><input type="checkbox" name="accepttos">
    </form>
    <form method="post" action="cart.php?a=checkout"><input type="hidden" name="token" value="checkout-token"><input name="promocode"><button name="validatepromo">Validate Code</button></form>"""


def vmiss_invoice_page(status="Unpaid", product="US.LA.CMIN2.Elite", system="Debian 12", coupon="10%OFF"):
    return f'<main><h1>View Invoice</h1><span class="status">{status}</span><div class="invoice-item">{product}<br>Operating System: {system}<br>优惠码: {coupon}</div></main>'


class OrderTaskTests(unittest.TestCase):
    def setUp(self):
        self.config = configuration()
        self.sessions = []
        self.auth_checks = 0
        self.auth_valid = True
        self.capability = {'protocol': admin.ORDER_PROTOCOL, 'order_only': True, 'automatic_payment': False}
        self.order_result = {'protocol': admin.ORDER_PROTOCOL, 'status': 'unpaid', 'order_id': 'order-123', 'quantity': 2,
            'payment_url': 'https://payments.example/charge'}
        self.order_exception = None
        self.stock = Mock(return_value=Product('plan', 'Plan', 3, 'https://shop.example.com/buy'))
        self.factory = patch.object(admin.requests, 'Session', side_effect=self.session).start()
        self.addCleanup(patch.stopall)
        patch('monitor.inspect_product', self.stock).start()
        self.task = None

    def session(self):
        session = Mock(headers={}, proxies={})
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        session.get.side_effect = lambda *a, **kw: response(self.capability)
        def post(url, **kwargs):
            payload = kwargs['json']
            if payload['action'] == 'check_account':
                self.auth_checks += 1
                return response({'protocol': admin.ORDER_PROTOCOL, 'authenticated': self.auth_valid,
                    'username': 'user'})
            if self.order_exception:
                raise self.order_exception
            return response(self.order_result)
        session.post.side_effect = post
        self.sessions.append(session)
        return session

    def run_task(self, mode='rapid', stop_on_wait=True):
        self.task = admin.SeckillTask('seckill-testing-123456', self.config, mode)
        if stop_on_wait:
            self.task.cancelled.wait = Mock(side_effect=lambda *args: self.task.cancel())
        self.task.run()
        return self.task.snapshot()

    def orders(self):
        return [call for session in self.sessions for call in session.post.call_args_list
            if call.kwargs['json']['action'] == 'create_unpaid_order']

    def test_quantity_defaults_and_rejects_invalid_values(self):
        item = self.config['vps_seckill']['items'][0]
        del item['quantity']
        self.assertEqual(admin.validate_config(self.config)['vps_seckill']['items'][0]['quantity'], 1)
        for quantity in (0, -1, 101, True, 1.5, '2', None):
            with self.subTest(quantity=quantity), self.assertRaisesRegex(ValueError, '下单数量'):
                item['quantity'] = quantity
                admin.validate_config(self.config)

    def test_rapid_checks_once_and_creates_only_unpaid_order_with_quantity_coupon(self):
        self.config['vps_seckill']['items'][0]['auto_submit'] = False
        self.config['vps_seckill']['accounts'][0]['auto_submit'] = False
        self.config['vps_seckill']['items'][0]['coupon_code'] = 'SPECIAL'
        self.config['vps_seckill']['items'][0].update({
            'operating_system': 'Debian 12', 'billing_cycle': 'monthly', 'payment_method': 'alipay'})
        result = self.run_task()
        self.assertEqual(self.stock.call_count, 1)
        self.assertEqual(self.auth_checks, 1)
        self.assertEqual(result['items'][0]['status'], 'submitted')
        self.assertEqual(result['items'][0]['payment_status'], 'unpaid')
        order = self.orders()[0]
        payload = order.kwargs['json']
        self.assertEqual(payload['quantity'], 2)
        self.assertEqual(payload['coupon_code'], 'SPECIAL')
        self.assertEqual(payload['operating_system'], 'Debian 12')
        self.assertEqual(payload['billing_cycle'], 'monthly')
        self.assertEqual(payload['payment_method'], 'alipay')
        self.assertEqual(payload['payment_mode'], 'manual')
        self.assertIs(payload['auto_pay'], False)
        self.assertNotIn('password', payload)
        self.assertEqual(order.kwargs['headers']['Idempotency-Key'], payload['idempotency_key'])
        self.assertNotIn('payments.example', str(result))
        for session in self.sessions:
            for call in session.get.call_args_list + session.post.call_args_list:
                self.assertFalse(call.kwargs['allow_redirects'])
                self.assertNotIn('payments.example', call.args[0])

    def test_submitted_unpaid_order_sends_telegram_notification(self):
        with patch.object(admin, 'effective_telegram', return_value=('test-token', '123')) as credentials, \
             patch.object(admin, 'telegram_call') as send:
            result = self.run_task()
        self.assertEqual(result['items'][0]['telegram_status'], 'sent')
        credentials.assert_called_once_with(self.config)
        self.assertEqual(send.call_args.args[:2], ('test-token', 'sendMessage'))
        payload = send.call_args.kwargs
        self.assertEqual(payload['chat_id'], '123')
        self.assertIn('订单号：<code>order-123</code>', payload['text'])
        self.assertIn('状态：待付款', payload['text'])
        self.assertEqual(payload['reply_markup']['inline_keyboard'][0][0]['text'], '💳 立即付款')
        self.assertEqual(payload['reply_markup']['inline_keyboard'][0][0]['url'], 'https://shop.example.com/buy')

    def test_seckill_test_checks_page_and_account_without_creating_order(self):
        checked = {'ok': True, 'status': 'valid', 'message': '账户认证正常', 'account_id': 'account', 'checked_at': '2026-10-16T12:00:00+08:00'}
        with patch.object(admin, 'current_config', return_value=self.config), \
             patch.object(admin, 'check_order_account', return_value=checked), \
             patch('monitor.inspect_product', self.stock):
            result = admin.seckill_test({'mode': 'rapid'})
        self.assertTrue(result['ok'])
        self.assertTrue(result['items'][0]['site_access']['ok'])
        self.assertTrue(result['items'][0]['page_recognition']['ok'])
        self.assertTrue(result['items'][0]['account']['ok'])
        self.assertEqual(self.orders(), [])

    def test_seckill_test_rejects_auto_submission_without_unpaid_endpoint(self):
        self.config['vps_seckill']['items'][0]['submit_url'] = ''
        checked = {'ok': True, 'status': 'valid', 'message': '账户认证正常', 'account_id': 'account', 'checked_at': '2026-10-16T12:00:00+08:00'}
        with patch.object(admin, 'current_config', return_value=self.config), \
             patch.object(admin, 'check_order_account', return_value=checked), \
             patch('monitor.inspect_product', self.stock):
            result = admin.seckill_test({'mode': 'rapid'})
        self.assertFalse(result['ok'])
        submission = result['items'][0]['order_submission']
        self.assertFalse(submission['ok'])
        self.assertIn('未配置商家待付款下单适配', submission['message'])
        self.assertEqual(self.orders(), [])

    def test_precise_rechecks_account_and_stock_before_submission(self):
        result = self.run_task('precise')
        self.assertEqual(self.stock.call_count, 2)
        self.assertEqual(self.auth_checks, 2)
        self.assertEqual(result['items'][0]['status'], 'submitted')

    def test_precise_does_not_submit_if_stock_disappears(self):
        self.stock.side_effect = [Product('p', 'P', 3, 'https://shop.example.com/buy'), Product('p', 'P', 1, 'https://shop.example.com/buy')]
        self.run_task('precise')
        self.assertEqual(self.stock.call_count, 2)
        self.assertEqual(self.orders(), [])

    def test_precise_stops_if_account_expires_after_initial_check(self):
        def inspect(*args):
            self.auth_valid = False
            return Product('p', 'P', 3, 'https://shop.example.com/buy')
        self.stock.side_effect = inspect
        result = self.run_task('precise')
        self.assertEqual(self.orders(), [])
        self.assertEqual(result['items'][0]['status'], 'blocked')

    def test_manual_never_submits_even_when_both_auto_buttons_enabled(self):
        result = self.run_task('manual')
        self.assertEqual(self.stock.call_count, 1)
        self.assertEqual(self.orders(), [])
        self.assertEqual(result['items'][0]['status'], 'ready')
        self.assertEqual(result['items'][0]['quantity'], 2)
        self.assertEqual(self.auth_checks, 1)
        self.assertTrue(all(not session.get.called for session in self.sessions))

    def test_quantity_insufficient_does_not_create_partial_order(self):
        self.stock.return_value = Product("p", "P", 1, "https://shop.example.com/buy")
        self.run_task()
        self.assertEqual(self.orders(), [])

    def test_boolean_availability_allows_requested_quantity_for_final_server_validation(self):
        self.stock.return_value = Product("p", "P", 1, "https://shop.example.com/buy", stock_mode="availability")
        result = self.run_task()
        self.assertEqual(self.orders()[0].kwargs['json']['quantity'], 2)
        self.assertEqual(result['items'][0]['status'], 'submitted')

    def test_invalid_account_blocks_order(self):
        self.auth_valid = False
        result = self.run_task()
        self.assertEqual(result['items'][0]['status'], 'blocked')
        self.assertEqual(self.orders(), [])
        self.stock.assert_not_called()

    def test_order_preferences_are_optional_but_validated(self):
        item = self.config['vps_seckill']['items'][0]
        for field, value in [('billing_cycle', 'weekly'), ('payment_method', 'paypal')]:
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, '无效'):
                item[field] = value
                admin.validate_config(self.config)
        item['billing_cycle'] = ''
        item['payment_method'] = ''
        normalized = admin.validate_config(self.config)
        self.assertEqual(normalized['vps_seckill']['items'][0]['operating_system'], '')
        self.assertEqual(normalized['vps_seckill']['items'][0]['billing_cycle'], '')
        self.assertEqual(normalized['vps_seckill']['items'][0]['payment_method'], '')

    def test_checkout_without_explicit_no_payment_contract_is_blocked(self):
        for capability in ({}, {**self.capability, 'automatic_payment': True}):
            with self.subTest(capability=capability):
                self.capability = capability
                result = self.run_task()
                self.assertEqual(result['items'][0]['status'], 'blocked')
                self.assertEqual(self.orders(), [])
        self.assertEqual(self.auth_checks, 0)

    def test_timeout_after_post_stops_without_retry_or_secret_in_logs(self):
        self.order_exception = requests.Timeout('private-password https://secret.example')
        result = self.run_task()
        self.assertEqual(result['items'][0]['status'], 'uncertain')
        self.assertEqual(len(self.orders()), 1)
        self.assertNotIn('private-password', str(result))
        self.assertNotIn('secret.example', str(result))
        self.assertTrue(self.task.done.is_set())

    def test_html_redirect_paid_or_wrong_quantity_are_never_reported_as_unpaid_success(self):
        for data in ({}, {**self.order_result, 'status': 'paid'}, {**self.order_result, 'quantity': 1},
                     {**self.order_result, 'quantity': True}):
            with self.subTest(data=data):
                self.order_result = data
                result = self.run_task()
                self.assertEqual(result['items'][0]['status'], 'uncertain')
        with self.assertRaises(ValueError):
            admin.order_json(response({}, 302))
        html = response()
        html.json.side_effect = ValueError('html')
        with self.assertRaisesRegex(ValueError, '未返回 JSON'):
            admin.order_json(html)

    def test_multiple_items_use_isolated_sessions_and_submit_once_each(self):
        second = copy.deepcopy(self.config['vps_seckill']['items'][0])
        second['id'] = 'two'
        self.config['vps_seckill']['items'].append(second)
        result = self.run_task()
        self.assertEqual(len(self.sessions), 4)
        self.assertEqual(len(self.orders()), 2)
        self.assertEqual(len(result['items']), 2)
        self.assertNotEqual(self.orders()[0].kwargs['json']['idempotency_key'], self.orders()[1].kwargs['json']['idempotency_key'])
        self.assertTrue(all(not session.trust_env for session in self.sessions))
        self.sessions[0].post.assert_not_called()
        self.sessions[2].post.assert_not_called()

    def test_stop_during_stock_check_prevents_order_post(self):
        def inspect(*args):
            self.task.cancel()
            return Product('p', 'P', 3, 'https://shop.example.com/buy')
        self.stock.side_effect = inspect
        self.run_task()
        self.assertEqual(self.orders(), [])


class VmissOrderTests(OrderTaskTests):
    def test_native_checkout_prefills_coupon_and_alipay_without_payment_redirect(self):
        session = Mock()
        config = VmissResponse(vmiss_config_page(), url="https://app.vmiss.com/cart.php?a=confproduct&i=0")
        checkout = VmissResponse(vmiss_checkout_page(), url="https://app.vmiss.com/cart.php?a=checkout")
        after_coupon = VmissResponse(vmiss_checkout_page(), url="https://app.vmiss.com/cart.php?a=checkout")
        invoice = VmissResponse("", status_code=302, url="https://app.vmiss.com/cart.php?a=checkout")
        invoice.headers = {"Location": "/viewinvoice.php?id=123"}
        invoice_page = VmissResponse(vmiss_invoice_page(), url="https://app.vmiss.com/viewinvoice.php?id=123")
        session.get.side_effect = [config, checkout, after_coupon, invoice_page]
        session.post.side_effect = [VmissResponse(""), VmissResponse("", status_code=302), invoice]
        item = {"product_id": "product60", "coupon_code": "10%OFF", "operating_system": "Debian 12",
                "billing_cycle": "monthly", "payment_method": "alipay"}
        result = admin.vmiss_order(session, item, {"username": "user@example.com"},
                                   Product("product60", "US.LA.CMIN2.Elite", 1, "https://app.vmiss.com/store/test"),
                                   "https://app.vmiss.com/store/test", "abc123")
        self.assertEqual(result["order_id"], "vmiss-invoice-123")
        self.assertEqual(result["payment_status"], "unpaid")
        self.assertEqual(session.get.call_args_list[-1].args[0], "https://app.vmiss.com/viewinvoice.php?id=123")
        config_data = session.post.call_args_list[0].kwargs["data"]
        self.assertIn(("billingcycle", "monthly"), config_data)
        self.assertIn(("configoption[35]", "173"), config_data)
        promo_data = session.post.call_args_list[1].kwargs["data"]
        self.assertIn(("promocode", "10%OFF"), promo_data)
        order_data = session.post.call_args_list[2].kwargs["data"]
        self.assertIn(("paymentmethod", "motionpayalipay"), order_data)
        self.assertIn(("applycredit", "0"), order_data)
        self.assertIn(("accepttos", "on"), order_data)
        self.assertNotIn("ccnumber", {name for name, _ in order_data})

    def test_native_checkout_rejects_invoice_without_unpaid_status(self):
        session = Mock()
        config = VmissResponse(vmiss_config_page(), url="https://app.vmiss.com/cart.php?a=confproduct&i=0")
        checkout = VmissResponse(vmiss_checkout_page(), url="https://app.vmiss.com/cart.php?a=checkout")
        invoice = VmissResponse("", status_code=302, url="https://app.vmiss.com/cart.php?a=checkout")
        invoice.headers = {"Location": "/viewinvoice.php?id=123"}
        paid_page = VmissResponse(vmiss_invoice_page("Paid"), url="https://app.vmiss.com/viewinvoice.php?id=123")
        session.get.side_effect = [config, checkout, paid_page]
        session.post.side_effect = [VmissResponse(""), invoice]
        with self.assertRaisesRegex(ValueError, "未确认未付款状态"):
            admin.vmiss_order(session, {"quantity": 1}, {"username": "user@example.com"},
                              Product("product60", "US.LA.CMIN2.Elite", 1, "https://app.vmiss.com/store/test"),
                              "https://app.vmiss.com/store/test", "abc123")

    def test_native_checkout_rejects_invoice_for_different_product(self):
        session = Mock()
        config = VmissResponse(vmiss_config_page(), url="https://app.vmiss.com/cart.php?a=confproduct&i=0")
        checkout = VmissResponse(vmiss_checkout_page(), url="https://app.vmiss.com/cart.php?a=checkout")
        invoice = VmissResponse("", status_code=302, url="https://app.vmiss.com/cart.php?a=checkout")
        invoice.headers = {"Location": "/viewinvoice.php?id=123"}
        wrong_page = VmissResponse(vmiss_invoice_page(product="US.LA.OTHER.Elite"), url="https://app.vmiss.com/viewinvoice.php?id=123")
        session.get.side_effect = [config, checkout, wrong_page]
        session.post.side_effect = [VmissResponse(""), invoice]
        with self.assertRaisesRegex(ValueError, "账单项目未匹配本次商品"):
            admin.vmiss_order(session, {"quantity": 1}, {"username": "user@example.com"},
                              Product("product60", "US.LA.CMIN2.Elite", 1, "https://app.vmiss.com/store/test"),
                              "https://app.vmiss.com/store/test", "abc123")

    def test_native_checkout_finds_unpaid_invoice_from_invoice_list(self):
        session = Mock()
        config = VmissResponse(vmiss_config_page(), url="https://app.vmiss.com/cart.php?a=confproduct&i=0")
        checkout = VmissResponse(vmiss_checkout_page(), url="https://app.vmiss.com/cart.php?a=checkout")
        order_response = VmissResponse("<html>Order accepted</html>", url="https://app.vmiss.com/cart.php?a=checkout")
        invoice_list = VmissResponse(
            '<table><tr><td><a href="/viewinvoice.php?id=456">VMISS-2026</a></td><td>Unpaid</td></tr></table>',
            url="https://app.vmiss.com/clientarea.php?action=invoices")
        invoice_page = VmissResponse(vmiss_invoice_page(), url="https://app.vmiss.com/viewinvoice.php?id=456")
        session.get.side_effect = [config, checkout, invoice_list, invoice_page]
        session.post.side_effect = [VmissResponse(""), order_response]
        result = admin.vmiss_order(session, {"quantity": 1}, {"username": "user@example.com"},
                                   Product("product60", "US.LA.CMIN2.Elite", 1, "https://app.vmiss.com/store/test"),
                                   "https://app.vmiss.com/store/test", "abc123")
        self.assertEqual(result["order_id"], "vmiss-invoice-456")
        self.assertEqual(result["payment_status"], "unpaid")
        self.assertEqual(session.get.call_args_list[-2].args[0], admin.VMISS_INVOICES_URL)
        self.assertEqual(session.get.call_args_list[-1].args[0], "https://app.vmiss.com/viewinvoice.php?id=456")

    def test_native_checkout_matches_requested_invoice_when_list_has_old_unpaid_invoice(self):
        session = Mock()
        config = VmissResponse(vmiss_config_page(), url="https://app.vmiss.com/cart.php?a=confproduct&i=0")
        checkout = VmissResponse(vmiss_checkout_page(), url="https://app.vmiss.com/cart.php?a=checkout")
        order_response = VmissResponse("<html>Order accepted</html>", url="https://app.vmiss.com/cart.php?a=checkout")
        invoice_list = VmissResponse(
            '<table><tr><td><a href="/viewinvoice.php?id=111">Old</a></td><td>Unpaid</td></tr>'
            '<tr><td><a href="/viewinvoice.php?id=456">New</a></td><td>Unpaid</td></tr></table>',
            url="https://app.vmiss.com/clientarea.php?action=invoices")
        old_invoice = VmissResponse(vmiss_invoice_page(product="US.LA.OTHER.Elite"), url="https://app.vmiss.com/viewinvoice.php?id=111")
        new_invoice = VmissResponse(vmiss_invoice_page(), url="https://app.vmiss.com/viewinvoice.php?id=456")
        session.get.side_effect = [config, checkout, invoice_list, old_invoice, new_invoice]
        session.post.side_effect = [VmissResponse(""), order_response]
        result = admin.vmiss_order(session, {"quantity": 1}, {"username": "user@example.com"},
                                   Product("product60", "US.LA.CMIN2.Elite", 1, "https://app.vmiss.com/store/test"),
                                   "https://app.vmiss.com/store/test", "abc123")
        self.assertEqual(result["order_id"], "vmiss-invoice-456")
        self.assertEqual(result["payment_status"], "unpaid")

    def test_native_checkout_polls_when_new_invoice_is_not_visible_yet(self):
        session = Mock()
        config = VmissResponse(vmiss_config_page(), url="https://app.vmiss.com/cart.php?a=confproduct&i=0")
        checkout = VmissResponse(vmiss_checkout_page(), url="https://app.vmiss.com/cart.php?a=checkout")
        empty_list = VmissResponse("<table></table>", url=admin.VMISS_INVOICES_URL)
        delayed_list = VmissResponse(
            '<div class="invoice-row"><a href="/viewinvoice.php?id=789">VMISS-2026</a>'
            '<span class="status unpaid"></span></div>', url=admin.VMISS_INVOICES_URL)
        invoice_page = VmissResponse(vmiss_invoice_page(), url="https://app.vmiss.com/viewinvoice.php?id=789")
        session.get.side_effect = [config, checkout, empty_list, delayed_list, invoice_page]
        session.post.side_effect = [VmissResponse(""), VmissResponse(vmiss_checkout_page(), url=checkout.url),
                                     VmissResponse("<html>Order accepted</html>", url=checkout.url)]
        item = {"quantity": 1, "coupon_code": "10%OFF", "operating_system": "Debian 12",
                "billing_cycle": "monthly", "payment_method": "alipay"}
        with patch.object(admin.time, "sleep") as sleep:
            result = admin.vmiss_order(session, item, {"username": "user@example.com"},
                                       Product("product60", "US.LA.CMIN2.Elite", 1, "https://app.vmiss.com/store/test"),
                                       "https://app.vmiss.com/store/test", "abc123")
        self.assertEqual(result["order_id"], "vmiss-invoice-789")
        self.assertEqual(result["payment_status"], "unpaid")
        self.assertEqual(session.post.call_count, 3)
        sleep.assert_called_once_with(admin.VMISS_INVOICE_POLL_DELAY)

    def test_native_checkout_verifies_completed_order_detail_response_before_invoice_list(self):
        session = Mock()
        config = VmissResponse(vmiss_config_page(), url="https://app.vmiss.com/cart.php?a=confproduct&i=0")
        checkout = VmissResponse(vmiss_checkout_page(), url="https://app.vmiss.com/cart.php?a=checkout")
        completed = VmissResponse(
            '<main><h1>View Invoice</h1><strong>VMISS-2026611667</strong>'
            '<span class="status">Unpaid</span><div class="invoice-item">'
            'US.LA.CMIN2.Elite<br>Operating System: Debian 12<br>优惠码: 10%OFF</div></main>',
            url="https://app.vmiss.com/cart.php?a=checkout")
        session.get.side_effect = [config, checkout]
        session.post.side_effect = [VmissResponse(""), VmissResponse(vmiss_checkout_page(), url=checkout.url), completed]
        result = admin.vmiss_order(session, {"quantity": 1, "coupon_code": "10%OFF",
                                             "operating_system": "Debian 12", "billing_cycle": "monthly",
                                             "payment_method": "alipay"}, {"username": "user@example.com"},
                                   Product("product60", "US.LA.CMIN2.Elite", 1, "https://app.vmiss.com/store/test"),
                                   "https://app.vmiss.com/store/test", "abc123")
        self.assertEqual(result["order_id"], "vmiss-invoice-2026611667")
        self.assertEqual(result["payment_status"], "unpaid")
        self.assertEqual(session.get.call_count, 2)

    def test_native_checkout_rejects_multi_quantity_instead_of_misreporting(self):
        with self.assertRaisesRegex(ValueError, "单台"):
            admin.vmiss_order(Mock(), {"quantity": 2}, {}, None,
                              "https://app.vmiss.com/store/test", "abc123")

    def test_account_and_order_endpoints_must_share_origin(self):
        self.config['vps_seckill']['items'][0]['submit_url'] = 'https://other.example/order'
        result = self.run_task()
        self.assertEqual(result['items'][0]['status'], 'blocked')
        self.assertEqual(self.orders(), [])
        self.assertEqual(self.auth_checks, 0)

    def test_akile_native_payload_uses_debian12_monthly_and_acknowledgements(self):
        plan = {"id": 1047, "plan_name": "TWLite-Mini", "cpu": 1, "memory": 2048,
                "disk": 10, "flow": 1500, "bandwidth": 1000, "price_datas": [{"id": 1, "price": 2499, "cycle": 1}]}
        product = Product("1047", "TWLite-Mini", 20, "https://akile.ai/shop/server?areaId=6&nodeId=41&planId=1047",
                          vendor="Akile", area_id="6", area_name="中国台湾", node_name="TWLite",
                          match_details={"area_id": 6, "area_name": "中国台湾", "node_id": 41,
                                         "node_name": "TWLite", "plan_id": 1047, "plan": plan})
        payload = admin.akile_order_payload({"quantity": 1, "akile_protocol_agreed": True,
            "akile_broadcast_ip_ack": True}, product, product.url)
        fields = {field["key"]: field["value"] for field in payload["order_data"]}
        self.assertEqual(payload["order_plan_id"], 1047)
        self.assertEqual(payload["order_type"], "1")
        self.assertEqual(fields["template_uuid"], admin.AKILE_DEBIAN_TEMPLATES["Debian 12"])
        self.assertEqual(fields["cycle"], "1")
        self.assertEqual(fields["system_name"], "Debian 12")
        self.assertEqual(fields["node_name"], "TWLite")

    def test_akile_order_result_accepts_unpaid_order_number_only(self):
        result = admin.akile_order_result(response({"status_code": 0, "data": {"order_no": "202610170001"}}))
        self.assertEqual(result, {"order_id": "202610170001",
            "order_url": "https://akile.ai/console/finance/order/202610170001", "quantity": 1})
        with self.assertRaises(ValueError):
            admin.akile_order_result(response({"status_code": 0, "data": {"payment_url": "https://pay.example"}}))
        with self.assertRaisesRegex(ValueError, "订单号格式无效"):
            admin.akile_order_result(response({"status_code": 0, "data": {"order_no": "../../payment"}}))

    def test_akile_submit_notifies_with_official_order_page_and_never_payment_url(self):
        task = admin.SeckillTask("akile-seckill-test", admin.default_config(), "rapid")
        item = {"id": "akile-item", "product_id": "1047", "label": "TWLite-Mini", "quantity": 1,
                "akile_protocol_agreed": True, "akile_broadcast_ip_ack": True}
        product = {"adapter": "akile", "provider": "Akile", "name": "TWLite-Mini"}
        inspected = Product("1047", "TWLite-Mini", 1,
            "https://akile.ai/shop/server?areaId=6&nodeId=41&planId=1047", vendor="Akile",
            area_id="6", area_name="中国台湾", node_name="TWLite",
            match_details={"area_name": "中国台湾", "node_name": "TWLite", "plan_id": 1047,
                           "plan": {"id": 1047, "plan_name": "TWLite-Mini"}})
        session = Mock(headers={})
        session.post.return_value = response({"status_code": 0, "data": {"order_no": "202610170001",
            "payment_url": "https://pay.example/charge"}})
        session.get.return_value = response({"status_code": 0, "data": {"order_no": "202610170001",
            "order_status": 1, "order_type": 1}})
        with patch.object(admin, "effective_telegram", return_value=("bot", "chat")), \
             patch.object(admin, "telegram_call") as telegram:
            result = task._submit(session, item, {}, inspected.url, product, inspected=inspected)
        order_url = "https://akile.ai/console/finance/order/202610170001"
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(result["url"], order_url)
        self.assertEqual(result["telegram_status"], "sent")
        self.assertEqual(session.post.call_args.args[0], admin.AKILE_ORDER_URL)
        self.assertEqual(session.get.call_args.args[0], admin.AKILE_ORDER_DETAIL_URL)
        self.assertEqual(session.get.call_args.kwargs["params"], {"orderNo": "202610170001"})
        self.assertNotIn("payment_url", session.post.call_args.kwargs["json"])
        self.assertEqual(telegram.call_args.kwargs["reply_markup"]["inline_keyboard"][0][0]["url"], order_url)

    def test_akile_order_confirmation_rejects_paid_or_mismatched_details(self):
        session = Mock()
        session.get.return_value = response({"status_code": 0, "data": {"order_no": "202610170001", "order_status": 2}})
        with self.assertRaisesRegex(ValueError, "已支付"):
            admin.akile_confirm_order(session, "202610170001")
        session.get.return_value = response({"status_code": 0, "data": {"order_no": "202610170002", "order_status": 1}})
        with self.assertRaisesRegex(ValueError, "不匹配"):
            admin.akile_confirm_order(session, "202610170001")


class BwhOrderTests(unittest.TestCase):
    @staticmethod
    def config_page():
        return '<form method="post" action="/cart.php?a=confproduct&i=0"><input type="hidden" name="token" value="csrf"><input type="hidden" name="configure" value="true"><select name="billingcycle"><option value="annually" selected>Annual</option></select><select name="configoption[17]"><option value="158" selected>EUNL_1</option></select></form>'

    @staticmethod
    def checkout_page():
        return '<form method="post" action="/cart.php?a=checkout"><input type="hidden" name="token" value="csrf"><input type="hidden" name="submit" value="true"><input type="hidden" name="custtype" value="new"><input type="radio" name="paymentmethod" value="paypal" checked><input type="radio" name="paymentmethod" value="payssionalipaycn"><input type="checkbox" name="accepttos"><input name="ccnumber"><input name="cccvv"></form>'

    def test_creates_unpaid_invoice_with_alipay_and_never_payment_redirect(self):
        session = Mock()
        config = VmissResponse(self.config_page(), url='https://bwh81.net/cart.php?a=confproduct&i=0')
        checkout = VmissResponse(self.checkout_page(), url='https://bwh81.net/cart.php?a=checkout')
        redirect = VmissResponse('', status_code=302, url='https://bwh81.net/cart.php?a=checkout')
        redirect.headers = {'Location': '/billing/24290771'}
        invoice = VmissResponse('<h3>Invoice 24290771: Unpaid</h3><p>SPECIAL 20G KVM PROMO V5 - CN2 GIA ECOMMERCE</p><p>Location: NL - Amsterdam DC1 (EUNL_1)</p>', url='https://bwh81.net/billing/24290771')
        session.get.side_effect = [config, checkout, invoice]
        session.post.side_effect = [config, redirect]
        product = Product('87:EUNL_1:annually:16999', 'SPECIAL 20G KVM PROMO V5 - CN2 GIA ECOMMERCE · 年付', 1,
                          'https://bwh81.net/cart.php?a=add&pid=87&billingcycle=annually&configoption%5B17%5D=158')
        result = admin.bwh_order(session, {'quantity': 1, 'billing_cycle': 'annually', 'payment_method': 'alipay'},
                                 {'username': 'user@example.com'}, product, product.url)
        self.assertEqual(result['order_id'], '24290771')
        self.assertEqual(result['payment_status'], 'unpaid')
        payload = session.post.call_args_list[-1].kwargs['data']
        self.assertEqual(payload['paymentmethod'], 'payssionalipaycn')
        self.assertEqual(payload['accepttos'], 'on')
        self.assertNotIn('ccnumber', payload)
        self.assertNotIn('payment', session.get.call_args_list[-1].args[0])

    def test_accepts_invoice_link_and_pending_status_variant(self):
        response = VmissResponse('<a href="/billing/24290771">View invoice</a><span>Pending</span>')
        self.assertEqual(admin._bwh_invoice_id_from_page(response), '24290771')


class IsvoroOrderTests(unittest.TestCase):
    def test_creates_unpaid_debian12_alipay_order(self):
        session = requests.Session()
        session.post = Mock(side_effect=[response({'code': 0, 'data': {}}), response({'code': 0, 'data': {'invoice_id': 'inv-188-361'}})])
        session.get = Mock(return_value=response({'code': 0, 'data': [{'id': 65, 'name': '支付宝', 'extension': 'Antom', 'enabled': True}]}))
        session.cookies.set('pmt_csrf', 'csrf-token')
        product = Product('188', 'HK PROD MiNi', 1, 'https://isvoro.com/buy/HK-prod?product=188', vendor='Isvoro',
                          match_details={'product_id': '188', 'plans': [{'id': 361, 'billing_period': 1, 'billing_unit': 'month', 'hidden': False}],
                                         'checkout_fields': [{'name': 'os', 'required': True, 'options': [{'value': 22, 'label': 'Debian12'}]}]})
        result = admin.isvoro_order(session, {'quantity': 1, 'billing_cycle': 'monthly', 'payment_method': 'alipay'}, {'username': 'user@example.com'}, product, product.url)
        self.assertEqual(result['order_id'], 'inv-188-361')
        self.assertEqual(result['os'], 'Debian12')
        self.assertEqual(result['payment_method'], 'alipay')
        self.assertEqual(session.post.call_args_list[0].kwargs['json']['checkout_config'], {'os': '22'})
        self.assertEqual(session.post.call_args_list[0].kwargs['headers']['X-CSRF-Token'], 'csrf-token')
        self.assertEqual(session.post.call_args_list[1].kwargs['json'], {'gateway_id': 65})


class DMitOrderTests(unittest.TestCase):
    def test_dmit_order_uses_requested_defaults_and_alipay(self):
        class Resp:
            def __init__(self, body, url): self.content = body.encode(); self.text = body; self.status_code = 200; self.url = url
        session = requests.Session()
        config = Resp('<form id="frmConfigureProduct" action="/cart.php?a=confproduct&i=0"><input name="configure" value="true"><input name="hostname"><select name="billingcycle"><option value="monthly" selected>Monthly</option></select><input name="configoption[29]" value="472"><input name="configoption[118]" value="0"></form>', 'https://www.dmit.io/cart.php?a=confproduct&i=0')
        checkout = Resp('<form action="/cart.php?a=checkout"><input name="token" value="csrf"><input name="paymentmethod" value="alipay"></form>', 'https://www.dmit.io/cart.php?a=checkout')
        invoice = Resp('<h1>Invoice 1171903: Unpaid</h1><p>HKG.AS3.Pro.TINY</p>', 'https://www.dmit.io/viewinvoice.php?id=1171903')
        session.get = Mock(side_effect=[config, checkout]); session.post = Mock(side_effect=[checkout, invoice])
        product = Product('dmit:HKG:AN5:PREMIUM:MINI', 'HKG.AS3.Pro.MINI', 1, 'https://www.dmit.io/cart.php?a=add&pid=125', vendor='DMIT')
        result = admin._dmit_order(session, {'quantity': 1}, {'username': 'user@example.com'}, product, product.url, {'mode': 'requests'})
        self.assertEqual(result['order_id'], '1171903')
        payload = session.post.call_args_list[0].kwargs['data']
        self.assertEqual(payload['billingcycle'], 'monthly')
        self.assertEqual(payload['hostname'], 'pz')
        self.assertEqual(payload['configoption[29]'], '471')
        self.assertEqual(payload['configoption[118]'], '0')
        self.assertEqual(payload['customfield[1025]'], '')


class AccountVerificationTests(unittest.TestCase):
    def setUp(self):
        # Authentication caches are intentionally process-global in production;
        # isolate account-check examples so one test cannot reuse another's
        # short-lived session.
        for cache in (admin.BWH_VERIFICATION_CHALLENGES, admin.MERCHANT_AUTH_SESSIONS):
            for value in cache.values():
                session = value.get('session') if isinstance(value, dict) else None
                if session:
                    session.close()
            cache.clear()

    def test_requires_positive_authentication_and_matching_identity(self):
        account = configuration()['vps_seckill']['accounts'][0]
        for data in ({}, {'authenticated': True},
                     {'protocol': admin.ORDER_PROTOCOL, 'authenticated': True, 'username': 'other'}):
            with self.subTest(data=data):
                session = Mock()
                session.post.return_value = response(data)
                result = admin.check_order_account(session, account)
                self.assertFalse(result['ok'])
                self.assertEqual(result['status'], 'unknown')
        session.post.return_value = response({}, 401)
        self.assertEqual(admin.check_order_account(session, account)['status'], 'invalid')
        session.post.side_effect = requests.ConnectionError('private-password')
        self.assertNotIn('private-password', str(admin.check_order_account(session, account)))

    def test_vmiss_form_login_uses_csrf_and_requires_logged_in_marker(self):
        account = configuration()['vps_seckill']['accounts'][0]
        account.update(provider='vmiss', login_url='https://app.vmiss.com/login')
        page = Mock(status_code=200, url='https://app.vmiss.com/login', content=b'<form action="/login" method="post"><input type="hidden" name="token" value="csrf"><input name="username"><input name="password"></form>')
        redirected = Mock(status_code=302, headers={'Location': '/clientarea.php'})
        logged_in = Mock(status_code=200, url='https://app.vmiss.com/clientarea.php', content=b'<a href="/logout.php">Logout</a>')
        session = Mock()
        session.get.side_effect = [page, logged_in]
        session.post.return_value = redirected
        result = admin.check_order_account(session, account)
        self.assertEqual(result['status'], 'valid')
        self.assertTrue(result['ok'])
        self.assertEqual(session.post.call_args.args[0], 'https://app.vmiss.com/login')
        self.assertEqual(session.post.call_args.kwargs['data'], {'token': 'csrf', 'username': 'user', 'password': 'private-password'})
        self.assertFalse(session.post.call_args.kwargs['allow_redirects'])
        self.assertEqual(session.post.call_args.kwargs['headers']['Origin'], 'https://app.vmiss.com')
        self.assertEqual(session.post.call_args.kwargs['headers']['Referer'], 'https://app.vmiss.com/login')
        self.assertIn('Mozilla/5.0', session.post.call_args.kwargs['headers']['User-Agent'])

    def test_vmiss_form_login_submits_remember_me_when_offered(self):
        account = configuration()['vps_seckill']['accounts'][0]
        account.update(provider='vmiss', login_url='https://app.vmiss.com/login')
        page = Mock(status_code=200, url='https://app.vmiss.com/login', content=b'<form action="/login" method="post"><input type="hidden" name="token" value="csrf"><input name="username"><input name="password"><input type="checkbox" name="rememberme" value="yes"></form>')
        redirected = Mock(status_code=302, headers={'Location': '/clientarea.php'})
        logged_in = Mock(status_code=200, url='https://app.vmiss.com/clientarea.php', content=b'<a href="/logout.php">Logout</a>')
        session = Mock()
        session.get.side_effect = [page, logged_in]
        session.post.return_value = redirected
        result = admin.check_order_account(session, account)
        self.assertEqual(result['status'], 'valid')
        self.assertEqual(session.post.call_args.kwargs['data']['rememberme'], 'yes')

    def test_vmiss_login_get_403_is_reported_as_access_block(self):
        account = configuration()['vps_seckill']['accounts'][0]
        account.update(provider='vmiss', login_url='https://app.vmiss.com/login')
        blocked = Mock(status_code=403, headers={'CF-Ray': 'abc123'}, url='https://app.vmiss.com/login')
        session = Mock(headers={})
        session.get.return_value = blocked
        result = admin.check_order_account(session, account)
        self.assertEqual(result['status'], 'unknown')
        self.assertIn('GET 返回 HTTP 403', result['message'])
        self.assertIn('abc123', result['message'])

    def test_dmit_cloudflare_challenge_is_reported_before_login_form(self):
        account = configuration()['vps_seckill']['accounts'][0]
        account.update(provider='dmit', login_url=admin.MERCHANT_LOGIN_ADAPTERS['dmit']['url'])
        blocked = Mock(status_code=403, headers={'cf-mitigated': 'challenge', 'CF-Ray': 'ray-123'}, url=admin.MERCHANT_LOGIN_ADAPTERS['dmit']['url'])
        blocked.text = '<title>Just a moment...</title><div>Cloudflare</div>'
        session = Mock(headers={})
        session.get.return_value = blocked
        result = admin.check_order_account(session, account)
        self.assertEqual(result['status'], 'unknown')
        self.assertIn('Cloudflare 安全挑战', result['message'])
        self.assertIn('出口 IP', result['message'])
        session.post.assert_not_called()

    def test_vmiss_flaresolverr_login_reuses_same_site_browser_state(self):
        account = configuration()['vps_seckill']['accounts'][0]
        account.update(provider='vmiss', login_url='https://app.vmiss.com/login')
        page = b'<form action="/login" method="post"><input type="hidden" name="token" value="csrf"><input name="username"><input name="password"></form>'
        redirected = Mock(status_code=302, headers={'Location': '/clientarea.php'})
        logged_in = Mock(status_code=200, url='https://app.vmiss.com/clientarea.php', content=b'<a href="/logout.php">Logout</a>')
        session = Mock(headers={}, cookies=Mock())
        session.get.return_value = logged_in
        session.post.return_value = redirected
        with patch('monitor.fetch_page', return_value=(page.decode(), 'https://app.vmiss.com/login')) as fetch:
            result = admin.check_order_account(session, account, {'mode': 'flaresolverr'})
        self.assertTrue(result['ok'])
        fetch.assert_called_once()
        self.assertTrue(fetch.call_args.args[2]['_copy_browser_state'])

    def test_vps_soy_login_error_and_cross_origin_form_are_not_success(self):
        account = configuration()['vps_seckill']['accounts'][0]
        account.update(provider='vps_soy', login_url='https://www.vps.soy/login')
        page = Mock(status_code=200, url='https://www.vps.soy/login', content=b'<form action="/login?action=email"><input type="hidden" name="token" value="csrf"><input name="email"><input name="password"></form>')
        failed = Mock(status_code=200, url='https://www.vps.soy/login', content=b'<form><input name="email"><input name="password"></form><div>Invalid password</div>')
        session = Mock()
        session.get.return_value = page
        session.post.return_value = failed
        self.assertEqual(admin.check_order_account(session, account)['status'], 'invalid')
        self.assertEqual(session.post.call_args.kwargs['data']['email'], 'user')
        page.content = b'<form action="https://attacker.example/login"><input type="hidden" name="token" value="csrf"><input name="email"><input name="password"></form>'
        session.get.return_value = page
        session.get.side_effect = None
        session.post.reset_mock()
        result = admin.check_order_account(session, account)
        self.assertEqual(result['status'], 'unknown')
        session.post.assert_not_called()

    def test_bandwagon_form_login_uses_login_origin_token_and_logout_marker(self):
        account = configuration()['vps_seckill']['accounts'][0]
        account.update(provider='bwh', login_url='https://bwh81.net/clientarea.php')
        page = Mock(status_code=200, url='https://bwh81.net/clientarea.php', content=b'<form action="dologin.php"><input type="hidden" name="token" value="csrf"><input type="hidden" name="loginorigin" value="clientarea"><input name="username"><input name="password"><input type="checkbox" name="rememberme"></form>')
        redirected = Mock(status_code=302, headers={'Location': '/clientarea.php'})
        logged_in = Mock(status_code=200, url='https://bwh81.net/clientarea.php', content=b'<a href="logout.php">Logout</a>')
        session = Mock()
        session.get.side_effect = [page, logged_in]
        session.post.return_value = redirected
        result = admin.check_order_account(session, account)
        self.assertEqual(result['status'], 'valid')
        self.assertEqual(session.post.call_args.kwargs['data'], {'token': 'csrf', 'loginorigin': 'clientarea', 'rememberme': 'on', 'username': 'user', 'password': 'private-password'})

    def test_bandwagon_browser_auth_is_not_reported_as_logged_in(self):
        account = configuration()['vps_seckill']['accounts'][0]
        account.update(provider='bwh', login_url='https://bwh81.net/clientarea.php')
        page = Mock(status_code=200, url='https://bwh81.net/clientarea.php', content=b'<form action="dologin.php"><input name="username"><input name="password"></form>')
        redirected = Mock(status_code=302, headers={'Location': '/browser_auth.php'})
        browser_auth = Mock(status_code=200, url='https://bwh81.net/browser_auth.php', content=b'<a href="logout.php">Logout</a><input name="browser_auth_code"><p>Email verification code</p>')
        session = Mock()
        session.get.side_effect = [page, browser_auth]
        session.post.return_value = redirected
        result = admin.check_order_account(session, account)
        self.assertEqual(result['status'], 'unknown')
        self.assertIn('邮箱验证码', result['message'])

    def test_bandwagon_email_code_finishes_original_login_session(self):
        account = configuration()['vps_seckill']['accounts'][0]
        account.update(provider='bwh', login_url='https://bwh81.net/clientarea.php')
        login_page = Mock(status_code=200, url='https://bwh81.net/clientarea.php', content=b'<form action="dologin.php"><input name="username"><input name="password"></form>')
        redirect = Mock(status_code=302, url='https://bwh81.net/dologin.php', headers={'Location': '/browser_auth.php'})
        code_page = Mock(status_code=200, url='https://bwh81.net/browser_auth.php', content=b'<form action="browser_auth.php"><input type="hidden" name="token" value="csrf"><input name="browser_auth_code"></form>')
        code_redirect = Mock(status_code=302, url='https://bwh81.net/browser_auth.php', headers={'Location': '/clientarea.php'})
        logged_in = Mock(status_code=200, url='https://bwh81.net/clientarea.php', content=b'<a href="logout.php">Logout</a>')
        session = Mock(headers={})
        session.get.side_effect = [login_page, code_page, code_page, logged_in]
        session.post.side_effect = [redirect, code_redirect]
        first = admin.check_order_account(session, account)
        self.assertTrue(first['verification_required'])
        self.assertNotIn('private-password', str(first))
        second = admin._merchant_submit_verification(session, account, admin.MERCHANT_LOGIN_ADAPTERS['bwh'], first['_verification_url'], '123456')
        self.assertTrue(second['ok'])
        self.assertEqual(session.post.call_args_list[-1].kwargs['data'], {'token': 'csrf', 'browser_auth_code': '123456'})

    def test_dmit_form_login_uses_builtin_adapter(self):
        account = configuration()['vps_seckill']['accounts'][0]
        account.update(provider='dmit', login_url=admin.MERCHANT_LOGIN_ADAPTERS['dmit']['url'])
        page = Mock(status_code=200, url=admin.MERCHANT_LOGIN_ADAPTERS['dmit']['url'], content=b'<form action="/login" method="post"><input type="hidden" name="token" value="csrf"><input name="username"><input name="password"></form>')
        redirected = Mock(status_code=302, headers={'Location': '/clientarea.php'})
        logged_in = Mock(status_code=200, url='https://www.dmit.io/clientarea.php', content=b'<a href="logout.php">Logout</a>')
        session = Mock()
        session.get.side_effect = [page, logged_in]
        session.post.return_value = redirected
        result = admin.check_order_account(session, account)
        self.assertEqual(result['status'], 'valid')
        self.assertEqual(session.post.call_args.args[0], 'https://www.dmit.io/login')

    def test_isvoro_api_login_verifies_profile_and_caches_session(self):
        account = configuration()['vps_seckill']['accounts'][0]
        account.update(provider='isvoro', username='user@example.com', password='pw', login_url=admin.ISVORO_LOGIN_URL)
        login = response({'code': 0, 'data': {'user': {'email': 'user@example.com'}}})
        profile = response({'code': 0, 'data': {'email': 'user@example.com', 'id': 42}})
        session = requests.Session()
        session.post = Mock(return_value=login)
        session.get = Mock(return_value=profile)
        with patch('admin._store_merchant_session') as store:
            result = admin.check_order_account(session, account)
        self.assertTrue(result['ok'])
        self.assertEqual(result['status'], 'valid')
        self.assertEqual(session.post.call_args.kwargs['json'], {'email': 'user@example.com', 'password': 'pw'})
        store.assert_called_once_with('isvoro', account, None, session)

    def test_isvoro_manual_cookie_is_verified_and_never_returned(self):
        account = configuration()['vps_seckill']['accounts'][0]
        account.update(provider='isvoro', username='user@example.com', password='pw', login_url=admin.ISVORO_LOGIN_URL,
                       _session_cookie='pmt_access=secret; pmt_csrf=csrf')
        profile = response({'code': 0, 'data': {'email': 'user@example.com', 'id': 42}})
        session = requests.Session()
        session.get = Mock(return_value=profile)
        with patch('admin._store_merchant_session') as store:
            result = admin.check_order_account(session, account)
        self.assertTrue(result['ok'])
        self.assertTrue(result['session_reused'])
        self.assertEqual(session.cookies.get('pmt_access'), 'secret')
        self.assertNotIn('secret', json.dumps(result))
        store.assert_called_once()

    def test_isvoro_cached_session_refreshes_access_cookie(self):
        account = configuration()['vps_seckill']['accounts'][0]
        account.update(provider='isvoro', username='user@example.com', password='pw', login_url=admin.ISVORO_LOGIN_URL)
        route = {'egress_node_id': 'route-isvoro'}
        source = requests.Session()
        source.cookies.set('pmt_refresh', 'refresh-token')
        source.cookies.set('pmt_csrf', 'csrf-token')
        admin._store_merchant_session('isvoro', account, route, source)
        cached = admin._get_merchant_session('isvoro', account, route)
        expired = response({'code': 401}, status=401)
        profile = response({'code': 0, 'data': {'email': 'user@example.com', 'id': 42}})
        cached.get = Mock(side_effect=[expired, profile])
        cached.post = Mock(return_value=response({'code': 0}))
        result = admin.check_order_account(requests.Session(), account, route)
        self.assertTrue(result['ok'])
        self.assertTrue(result['session_reused'])
        cached.post.assert_called_once_with(admin.ISVORO_API_BASE_URL + '/auth/refresh', timeout=(5, 20), allow_redirects=False,
                                            headers={'Accept': 'application/json', 'Content-Type': 'application/json', 'X-CSRF-Token': 'csrf-token'})

    def test_isvoro_auth_session_cache_ttl_is_thirty_days(self):
        account = configuration()['vps_seckill']['accounts'][0]
        account.update(provider='isvoro', username='user@example.com', password='pw', login_url=admin.ISVORO_LOGIN_URL)
        route = {'egress_node_id': 'route-isvoro'}
        source = requests.Session()
        key = admin._merchant_session_key('isvoro', account, route)
        with patch('admin.time.time', return_value=1000):
            admin._store_merchant_session('isvoro', account, route, source)
        self.assertEqual(admin.ISVORO_AUTH_SESSION_TTL, 30 * 24 * 60 * 60)
        self.assertEqual(admin.MERCHANT_AUTH_SESSIONS[key]['expires_at'], 1000 + 30 * 24 * 60 * 60)
        admin._remove_merchant_session('isvoro', account, route)

    def test_bwh_auth_session_cache_ttl_is_thirty_days(self):
        account = configuration()['vps_seckill']['accounts'][0]
        account.update(provider='bwh', username='user@example.com', password='pw', login_url=admin.MERCHANT_LOGIN_ADAPTERS['bwh']['url'])
        route = {'egress_node_id': 'route-bwh'}
        source = requests.Session()
        key = admin._merchant_session_key('bwh', account, route)
        with patch('admin.time.time', return_value=2000):
            admin._store_merchant_session('bwh', account, route, source)
        self.assertEqual(admin.BWH_AUTH_SESSION_TTL, 30 * 24 * 60 * 60)
        self.assertEqual(admin.MERCHANT_AUTH_SESSIONS[key]['expires_at'], 2000 + 30 * 24 * 60 * 60)
        admin._remove_merchant_session('bwh', account, route)

    def test_other_native_merchant_auth_cache_ttl_is_twenty_four_hours(self):
        account = configuration()['vps_seckill']['accounts'][0]
        account.update(provider='vmiss', username='user@example.com', password='pw', login_url=admin.MERCHANT_LOGIN_ADAPTERS['vmiss']['url'])
        route = {'egress_node_id': 'route-vmiss'}
        source = requests.Session()
        key = admin._merchant_session_key('vmiss', account, route)
        with patch('admin.time.time', return_value=3000):
            admin._store_merchant_session('vmiss', account, route, source)
        self.assertEqual(admin.MERCHANT_AUTH_SESSION_TTL, 30 * 24 * 60 * 60)
        self.assertEqual(admin.MERCHANT_AUTH_SESSIONS[key]['expires_at'], 3000 + 30 * 24 * 60 * 60)
        admin._remove_merchant_session('vmiss', account, route)

    def test_bwh_manual_cookie_is_verified_and_cached_without_password(self):
        account = configuration()['vps_seckill']['accounts'][0]
        account.update(provider='bwh', username='user@example.com', password='', login_url=admin.MERCHANT_LOGIN_ADAPTERS['bwh']['url'],
                       _session_cookie='WHMCSUser=opaque-session; currency=USD')
        page = Mock(status_code=200, url=admin.MERCHANT_LOGIN_ADAPTERS['bwh']['url'],
                    content=b'<a href="/logout.php">Logout</a>', headers={})
        session = requests.Session()
        session.get = Mock(return_value=page)
        with patch('admin._store_merchant_session') as store:
            result = admin.check_order_account(session, account)
        self.assertTrue(result['ok'])
        self.assertTrue(result['session_reused'])
        self.assertEqual(session.cookies.get('WHMCSUser'), 'opaque-session')
        self.assertNotIn('opaque-session', json.dumps(result))
        store.assert_called_once_with('bwh', account, None, session)

    def test_bwh_manual_cookie_is_not_kept_in_saved_config(self):
        account = configuration()['vps_seckill']['accounts'][0]
        account.update(provider='bwh', username='user@example.com', password='', login_url=admin.MERCHANT_LOGIN_ADAPTERS['bwh']['url'],
                       session_cookie='WHMCSUser=opaque-session')
        config = admin.default_config()
        config['vps_seckill']['accounts'] = [account]
        normalized = admin.validate_config(config)['vps_seckill']['accounts'][0]
        self.assertNotIn('session_cookie', normalized)
        self.assertNotIn('opaque-session', json.dumps(normalized))

    def test_bwh_cached_manual_cookie_session_can_be_reused_without_password(self):
        account = configuration()['vps_seckill']['accounts'][0]
        account.update(provider='bwh', username='user@example.com', password='', login_url=admin.MERCHANT_LOGIN_ADAPTERS['bwh']['url'])
        route = {'egress_node_id': 'route-bwh'}
        source = requests.Session()
        source.cookies.set('WHMCSUser', 'opaque-session', domain='bwh81.net', path='/')
        admin._store_merchant_session('bwh', account, route, source)
        cached = admin._get_merchant_session('bwh', account, route)
        with patch('admin._merchant_authenticated', return_value=True):
            result = admin.check_order_account(requests.Session(), account, route)
        self.assertTrue(result['ok'])
        self.assertTrue(result['session_reused'])
        self.assertIn('30 天', result['message'])
        self.assertEqual(cached.cookies.get('WHMCSUser'), 'opaque-session')
        admin._remove_merchant_session('bwh', account, route)

    def test_dmit_manual_cookie_is_verified_and_cached_without_password(self):
        account = configuration()['vps_seckill']['accounts'][0]
        account.update(provider='dmit', username='user@example.com', password='', login_url=admin.MERCHANT_LOGIN_ADAPTERS['dmit']['url'],
                       _session_cookie='WHMCSUser=opaque-session; currency=USD')
        page = Mock(status_code=200, url=admin.MERCHANT_LOGIN_ADAPTERS['dmit']['url'],
                    content=b'<a href="/logout.php">Logout</a>', headers={})
        session = requests.Session()
        session.get = Mock(return_value=page)
        with patch('admin._store_merchant_session') as store:
            result = admin.check_order_account(session, account)
        self.assertTrue(result['ok'])
        self.assertTrue(result['session_reused'])
        self.assertEqual(session.cookies.get('WHMCSUser'), 'opaque-session')
        self.assertNotIn('opaque-session', json.dumps(result))
        store.assert_called_once_with('dmit', account, None, session)

    def test_dmit_manual_cookie_is_domain_scoped_and_not_persisted(self):
        session = requests.Session()
        self.assertTrue(admin._import_dmit_session_cookie(session, 'WHMCSUser=opaque-session'))
        cookie = next(cookie for cookie in session.cookies if cookie.name == 'WHMCSUser')
        self.assertEqual(cookie.domain, '.dmit.io')
        self.assertTrue(cookie.domain_specified)
        account = configuration()['vps_seckill']['accounts'][0]
        account.update(provider='dmit', username='user@example.com', password='', login_url=admin.MERCHANT_LOGIN_ADAPTERS['dmit']['url'],
                       session_cookie='WHMCSUser=opaque-session')
        config = admin.default_config()
        config['vps_seckill']['accounts'] = [account]
        normalized = admin.validate_config(config)['vps_seckill']['accounts'][0]
        self.assertNotIn('session_cookie', normalized)
        self.assertNotIn('opaque-session', json.dumps(normalized))

    def test_dmit_cookie_probe_uses_account_details_url_and_browser_user_agent(self):
        account = configuration()['vps_seckill']['accounts'][0]
        account.update(provider='dmit', username='user@example.com', password='', login_url=admin.MERCHANT_LOGIN_ADAPTERS['dmit']['url'],
                       _session_cookie='WHMCSUser=opaque-session', _session_user_agent='Mozilla/5.0 CustomBrowser')
        page = Mock(status_code=200, url='https://www.dmit.io/clientarea.php?action=details',
                    content=b'<a href="/logout.php">Logout</a>', headers={})
        session = requests.Session()
        session.get = Mock(return_value=page)
        with patch('admin._store_merchant_session'):
            result = admin.check_order_account(session, account)
        self.assertTrue(result['ok'])
        self.assertEqual(session.headers['User-Agent'], 'Mozilla/5.0 CustomBrowser')
        self.assertEqual(session.get.call_args.args[0], 'https://www.dmit.io/clientarea.php?action=details')

    def test_known_merchant_url_is_canonical_and_unsupported_auth_stays_unknown(self):
        value = admin.default_config()
        value['vps_seckill']['accounts'] = [{'id': 'v', 'name': 'VMISS', 'provider': 'VMISS', 'username': 'user', 'password': 'pw', 'login_url': 'https://attacker.example/collect'}]
        account = admin.validate_config(value)['vps_seckill']['accounts'][0]
        self.assertEqual(account['provider'], 'vmiss')
        self.assertEqual(account['login_url'], 'https://app.vmiss.com/login')
        account.update(provider='dmit', login_url='')
        session = Mock()
        result = admin.check_order_account(session, account)
        self.assertEqual(result['status'], 'unknown')
        session.get.assert_not_called()

    def test_bandwagon_cached_auth_session_is_reused_for_next_check(self):
        account = configuration()['vps_seckill']['accounts'][0]
        account.update(provider='bwh', login_url='https://bwh81.net/clientarea.php')
        first_page = Mock(status_code=200, url='https://bwh81.net/clientarea.php', content=b'<form action="dologin.php"><input name="username"><input name="password"></form>')
        redirect = Mock(status_code=302, url='https://bwh81.net/dologin.php', headers={'Location': '/clientarea.php'})
        logged_in = Mock(status_code=200, url='https://bwh81.net/clientarea.php', content=b'<a href="logout.php">Logout</a>')
        session = requests.Session()
        with patch('admin._store_merchant_session') as store:
            session.get = Mock(side_effect=[first_page, logged_in])
            session.post = Mock(return_value=redirect)
            first = admin.check_order_account(session, account)
            self.assertTrue(first['ok'])
            store.assert_called_once_with('bwh', account, None, session)

    def test_bandwagon_cached_auth_state_copies_into_task_session(self):
        source = requests.Session()
        source.cookies.set('session', 'verified')
        target = requests.Session()
        admin._copy_bwh_session(source, target)
        self.assertEqual(target.cookies.get('session'), 'verified')

    def test_bandwagon_cached_auth_session_skips_password_login(self):
        account = configuration()['vps_seckill']['accounts'][0]
        account.update(provider='bwh', login_url='https://bwh81.net/clientarea.php')
        source = requests.Session()
        source.cookies.set('bwh_session', 'verified')
        route = {'egress_node_id': 'bwh-route'}
        admin._store_merchant_session('bwh', account, route, source)
        target = requests.Session()
        with patch.object(admin, '_merchant_authenticated', return_value=True) as authenticated, \
             patch.object(admin, '_merchant_form_login') as login:
            result = admin.check_order_account(target, account, route)
        self.assertTrue(result['ok'])
        self.assertTrue(result['session_reused'])
        self.assertEqual(target.cookies.get('bwh_session'), 'verified')
        authenticated.assert_called_once()
        login.assert_not_called()
        admin._remove_merchant_session('bwh', account, route)

    def test_vmiss_cached_auth_session_skips_password_login(self):
        account = configuration()['vps_seckill']['accounts'][0]
        account.update(provider='vmiss', login_url='https://app.vmiss.com/login')
        source = requests.Session()
        source.cookies.set('vmiss_session', 'cached')
        admin._store_merchant_session('vmiss', account, {'egress_node_id': 'route-a'}, source)
        target = requests.Session()
        with patch.object(admin, '_merchant_authenticated', return_value=True) as authenticated, \
             patch.object(admin, '_merchant_form_login') as login:
            result = admin.check_order_account(target, account, {'egress_node_id': 'route-a'})
        self.assertTrue(result['ok'])
        self.assertTrue(result['session_reused'])
        self.assertEqual(target.cookies.get('vmiss_session'), 'cached')
        authenticated.assert_called_once()
        login.assert_not_called()
        admin._remove_merchant_session('vmiss', account, {'egress_node_id': 'route-a'})

    def test_browser_solved_cached_session_skips_second_challenge_probe(self):
        account = configuration()['vps_seckill']['accounts'][0]
        account.update(provider='vmiss', login_url='https://app.vmiss.com/login')
        route = {'egress_node_id': 'route-a', 'mode': 'flaresolverr'}
        source = requests.Session()
        source.cookies.set('vmiss_session', 'browser-solved')
        admin._store_merchant_session('vmiss', account, route, source)
        target = requests.Session()
        with patch.object(admin, '_merchant_authenticated', return_value=False) as authenticated, \
             patch.object(admin, '_merchant_form_login') as login:
            result = admin.check_order_account(target, account, route)
        self.assertTrue(result['ok'])
        self.assertTrue(result['session_reused'])
        authenticated.assert_not_called()
        login.assert_not_called()
        self.assertEqual(target.cookies.get('vmiss_session'), 'browser-solved')
        admin._remove_merchant_session('vmiss', account, route)

    def test_merchant_auth_cache_expires_and_isolated_by_route(self):
        account = configuration()['vps_seckill']['accounts'][0]
        account.update(provider='vmiss', login_url='https://app.vmiss.com/login')
        source = requests.Session()
        admin._store_merchant_session('vmiss', account, {'egress_node_id': 'route-a'}, source)
        self.assertIsNotNone(admin._get_merchant_session('vmiss', account, {'egress_node_id': 'route-a'}))
        self.assertIsNone(admin._get_merchant_session('vmiss', account, {'egress_node_id': 'route-b'}))
        key = admin._merchant_session_key('vmiss', account, {'egress_node_id': 'route-a'})
        admin.MERCHANT_AUTH_SESSIONS[key]['expires_at'] = 0
        self.assertIsNone(admin._get_merchant_session('vmiss', account, {'egress_node_id': 'route-a'}))

    def test_merchant_auth_cache_renews_after_successful_reuse(self):
        account = configuration()['vps_seckill']['accounts'][0]
        account.update(provider='vmiss', login_url='https://app.vmiss.com/login')
        route = {'egress_node_id': 'route-a'}
        source = requests.Session()
        source.cookies.set('vmiss_session', 'cached')
        admin._store_merchant_session('vmiss', account, route, source)
        key = admin._merchant_session_key('vmiss', account, route)
        admin.MERCHANT_AUTH_SESSIONS[key]['expires_at'] = time.time() + 1
        old_expiry = admin.MERCHANT_AUTH_SESSIONS[key]['expires_at']
        with patch.object(admin, '_merchant_authenticated', return_value=True):
            result = admin.check_order_account(requests.Session(), account, route)
        self.assertTrue(result['session_reused'])
        self.assertGreater(admin.MERCHANT_AUTH_SESSIONS[key]['expires_at'], old_expiry)
        admin._remove_merchant_session('vmiss', account, route)

    def test_account_cache_can_be_cleared_across_routes(self):
        account = configuration()['vps_seckill']['accounts'][0]
        account.update(provider='vmiss', login_url='https://app.vmiss.com/login')
        for route in ({'egress_node_id': 'route-a'}, {'egress_node_id': 'route-b'}):
            admin._store_merchant_session('vmiss', account, route, requests.Session())
        self.assertEqual(admin._clear_merchant_sessions_for_account(account), 2)
        self.assertIsNone(admin._get_merchant_session('vmiss', account, {'egress_node_id': 'route-a'}))

    def test_order_notification_uses_product_name_when_item_label_is_missing(self):
        value = configuration()
        value['products'][0]['name'] = 'CN.HK.BGP.Pro'
        task = admin.SeckillTask('notification-label-test', value, 'rapid')
        with patch.object(admin, 'effective_telegram', return_value=('token', 'chat')), \
             patch.object(admin, 'telegram_call') as send:
            self.assertEqual(task._notify_unpaid_order({'product_id': 'plan'}, 'invoice-1', 1, 'https://app.vmiss.com/viewinvoice.php?id=1'), 'sent')
        self.assertIn('CN.HK.BGP.Pro', send.call_args.kwargs['text'])

    def test_akile_cached_auth_session_skips_api_login(self):
        account = configuration()['vps_seckill']['accounts'][0]
        account.update(provider='akile', login_url=admin.AKILE_LOGIN_URL)
        source = requests.Session()
        source.headers['Authorization'] = 'cached-token'
        source._hostwatch_akile_user_id = 42
        admin._store_merchant_session('akile', account, {'egress_node_id': 'route-a'}, source)
        target = requests.Session()
        with patch.object(admin, '_akile_authenticated', return_value=True) as authenticated, \
             patch.object(admin, '_akile_login') as login:
            result = admin.check_order_account(target, account, {'egress_node_id': 'route-a'})
        self.assertTrue(result['ok'])
        self.assertTrue(result['session_reused'])
        self.assertEqual(target.headers['Authorization'], 'cached-token')
        self.assertEqual(target._hostwatch_akile_user_id, 42)
        authenticated.assert_called_once()
        login.assert_not_called()
        admin._remove_merchant_session('akile', account, {'egress_node_id': 'route-a'})


class AccountCheckAPITests(unittest.TestCase):
    setUp = _AdminAPIBase.setUp
    request = _AdminAPIBase.request

    def test_saved_password_used_and_result_redacted_with_item_route(self):
        config = configuration()
        self.config.write_text(json.dumps(config), encoding='utf-8')
        checked = {'ok': True, 'status': 'valid', 'message': 'OK'}
        with patch.object(admin, 'check_order_account', return_value=checked) as check, patch.object(admin.egress, 'apply_session') as route:
            status, body = self.request('/api/seckill/account-check', {'account_id': 'account', 'item_id': 'one'})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)['ok'])
        self.assertEqual(check.call_args.args[1]['password'], 'private-password')
        self.assertNotIn(b'private-password', body)
        route.assert_called_once()
        self.assertEqual(json.loads(self.config.read_text()), config)

    def test_requires_saved_binding_and_authentication(self):
        self.config.write_text(json.dumps(configuration()), encoding='utf-8')
        with patch.object(admin, 'check_order_account') as check:
            status, _ = self.request('/api/seckill/account-check', {'account_id': 'account', 'item_id': 'missing'})
            self.assertEqual(status, 400)
            check.assert_not_called()
            with patch.object(admin, 'authorized', return_value=False):
                status, _ = self.request('/api/seckill/account-check', {'account_id': 'account', 'item_id': 'one'})
                self.assertEqual(status, 401)

    def test_account_check_can_run_without_a_product_binding(self):
        config = configuration()
        self.config.write_text(json.dumps(config), encoding='utf-8')
        checked = {'ok': True, 'status': 'valid', 'message': '账户认证正常'}
        with patch.object(admin, 'check_order_account', return_value=checked) as check, patch.object(admin.egress, 'apply_session') as route:
            status, body = self.request('/api/seckill/account-check', {'account_id': 'account'})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)['ok'])
        self.assertEqual(check.call_args.args[1]['id'], 'account')
        self.assertNotIn('item_id', json.loads(body))
        route.assert_called_once()

    def test_account_check_uses_unsaved_account_draft_without_saving_it(self):
        config = configuration()
        self.config.write_text(json.dumps(config), encoding='utf-8')
        draft = admin.public_config(config)
        draft['vps_seckill']['accounts'][0]['username'] = 'draft-user'
        checked = {'ok': True, 'status': 'valid', 'message': '账户认证正常'}
        with patch.object(admin, 'check_order_account', return_value=checked) as check, patch.object(admin.egress, 'apply_session'):
            status, body = self.request('/api/seckill/account-check', {
                'config': draft, 'account_id': 'account', 'account': draft['vps_seckill']['accounts'][0]
            })
        self.assertEqual(status, 200)
        self.assertEqual(check.call_args.args[1]['username'], 'draft-user')
        self.assertEqual(check.call_args.args[1]['password'], 'private-password')
        self.assertEqual(json.loads(self.config.read_text())['vps_seckill']['accounts'][0]['username'], 'user')
        self.assertNotIn(b'private-password', body)


# Do not discover the borrowed API fixture as a second copy of its entire suite.
del _AdminAPIBase

if __name__ == '__main__':
    unittest.main()
