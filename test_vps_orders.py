"""Order-only seckill regressions: no live merchant requests or payments."""
import copy
import json
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
        self.assertIn('未配置待付款下单接口', submission['message'])
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
        self.assertEqual(result, {"order_id": "202610170001", "quantity": 1})
        with self.assertRaises(ValueError):
            admin.akile_order_result(response({"status_code": 0, "data": {"payment_url": "https://pay.example"}}))


class AccountVerificationTests(unittest.TestCase):
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
        account.update(provider='dmit', login_url='https://www.dmit.io/login')
        blocked = Mock(status_code=403, headers={'cf-mitigated': 'challenge', 'CF-Ray': 'ray-123'}, url='https://www.dmit.io/login')
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
        page = Mock(status_code=200, url='https://bwh81.net/clientarea.php', content=b'<form action="dologin.php"><input type="hidden" name="token" value="csrf"><input type="hidden" name="loginorigin" value="clientarea"><input name="username"><input name="password"></form>')
        redirected = Mock(status_code=302, headers={'Location': '/clientarea.php'})
        logged_in = Mock(status_code=200, url='https://bwh81.net/clientarea.php', content=b'<a href="logout.php">Logout</a>')
        session = Mock()
        session.get.side_effect = [page, logged_in]
        session.post.return_value = redirected
        result = admin.check_order_account(session, account)
        self.assertEqual(result['status'], 'valid')
        self.assertEqual(session.post.call_args.kwargs['data'], {'token': 'csrf', 'loginorigin': 'clientarea', 'username': 'user', 'password': 'private-password'})

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

    def test_dmit_form_login_uses_builtin_adapter(self):
        account = configuration()['vps_seckill']['accounts'][0]
        account.update(provider='dmit', login_url='https://www.dmit.io/login')
        page = Mock(status_code=200, url='https://www.dmit.io/login', content=b'<form action="/login" method="post"><input type="hidden" name="token" value="csrf"><input name="username"><input name="password"></form>')
        redirected = Mock(status_code=302, headers={'Location': '/clientarea.php'})
        logged_in = Mock(status_code=200, url='https://www.dmit.io/clientarea.php', content=b'<a href="logout.php">Logout</a>')
        session = Mock()
        session.get.side_effect = [page, logged_in]
        session.post.return_value = redirected
        result = admin.check_order_account(session, account)
        self.assertEqual(result['status'], 'valid')
        self.assertEqual(session.post.call_args.args[0], 'https://www.dmit.io/login')

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
