// Run with: node --test scripts/test-admin-accounts.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '..', 'web', 'admin.js'), 'utf8');
const copy = value => JSON.parse(JSON.stringify(value));

test('Narwhal purchase defaults select balance and monthly while other merchants retain invoice preferences', () => {
  const context = load({
    config: { egress: { routes: { narwhal: 'node-one' } } },
    seckillProviderKey: value => value,
    seckillAccounts: () => [{ id: 'narwhal-account', provider: 'narwhal' }],
  }, ['seckillCoupon', 'seckillAddDefaults']);
  const defaults = context.seckillAddDefaults({ adapter: 'narwhal' });
  assert.equal(defaults.account_id, 'narwhal-account');
  assert.equal(defaults.egress_node_id, 'node-one');
  assert.equal(defaults.operating_system, '');
  assert.equal(defaults.billing_cycle, 'monthly');
  assert.equal(defaults.payment_method, 'balance');
  assert.equal(defaults.narwhal_auto_renew, true);
  assert.equal(context.seckillAddDefaults({ adapter: 'vmiss' }).payment_method, 'alipay');
  assert.equal(context.seckillAddDefaults({ adapter: 'akile' }).payment_method, '');
});

test('Narwhal node coupons sync plans and accounts, persist blanks and never inherit merchant-wide codes', () => {
  const products = [{ id: 'a', adapter: 'narwhal', machine_id: 'node-a' },
    { id: 'b', adapter: 'narwhal', machine_id: 'node-a' },
    { id: 'c', adapter: 'narwhal', machine_id: 'node-b' }];
  const items = products.map(row => ({ product_id: row.id, coupon_code: 'OLD' }));
  items.push({ product_id: 'a', account_id: 'second', coupon_code: 'OLD' });
  const inputs = products.map(row => ({ dataset: { narwhalCoupon: row.machine_id }, value: 'OLD' }));
  const context = load({
    config: { products, vps_seckill: { items }, affiliate_coupons: { narwhal: 'GLOBAL' } },
    document: { querySelectorAll: () => inputs }, seckillItems: () => items,
    seckillProviderKey: value => value, updateDraftState() {}, scheduleAutoSave() {},
  }, ['seckillCoupon', 'updateSeckillCoupon']);
  assert.equal(context.seckillCoupon(null, products[0]), '');
  context.updateSeckillCoupon(items[0], products[0], 'NODE-A');
  assert.deepEqual(items.map(row => row.coupon_code), ['NODE-A', 'NODE-A', 'OLD', 'NODE-A']);
  assert.deepEqual(inputs.map(input => input.value), ['NODE-A', 'NODE-A', 'OLD']);
  context.updateSeckillCoupon(items[0], products[0], '');
  assert.equal(context.seckillCoupon({ coupon_code: 'OLD' }, products[1]), '');
  assert.equal(context.config.vps_seckill.narwhal_coupons['node-a'], '');
  assert.equal(context.seckillCoupon(items[2], products[2]), 'OLD');
});

test('saving seckill settings preserves node coupons and renewal choices without saving account drafts', () => {
  const saved = { vps_seckill: { accounts: [{ id: 'one', name: 'Saved' }] } };
  const draft = { vps_seckill: { accounts: [{ id: 'one', name: 'Draft' }],
    items: [{ id: 'plan', narwhal_auto_renew: false }], narwhal_coupons: { 'node-a': 'CODE' } } };
  const context = load({}, ['cloneConfig', 'mergeConfigScope']);
  const result = context.mergeConfigScope(saved, draft, 'seckill');
  assert.equal(result.vps_seckill.accounts[0].name, 'Saved');
  assert.equal(result.vps_seckill.items[0].narwhal_auto_renew, false);
  assert.equal(result.vps_seckill.narwhal_coupons['node-a'], 'CODE');
});

test('purchase results distinguish paid instances, uncertain debits and unpaid invoices', () => {
  function element(tag, value = '') {
    return { tag, textContent: value, children: [], append(...nodes) { this.children.push(...nodes); },
      replaceChildren(...nodes) { this.children = nodes; } };
  }
  for (const [result, expected, forbidden] of [
    [{ status: 'purchased', payment_status: 'paid', vm_id: 'vm-one' }, '已扣余额并创建实例', '手动支付'],
    [{ status: 'uncertain', payment_status: 'unknown' }, '结果待核对', '手动支付'],
    [{ status: 'submitted', payment_status: 'unpaid', order_id: 'order-one' }, '已创建待付款订单', '已扣余额'],
  ]) {
    const nodes = new Map();
    const context = load({
      document: { querySelectorAll: () => [], querySelector: selector => {
        if (!nodes.has(selector)) nodes.set(selector, element('div'));
        return nodes.get(selector);
      } },
      URL, text: element, actionRunning: false, seckillAccounts: () => [],
      seckillItems: () => [{ id: 'one', label: 'Plan' }], renderOverview() {},
      vpsSeckillTask: { pending: false, logs: [], items: [{ item_id: 'one', quantity: 1,
        url: 'https://dash.fuckip.me/', ...result }] },
    }, ['renderVpsSeckillTask']);
    context.renderVpsSeckillTask();
    const rendered = JSON.stringify(nodes.get('#vps-seckill-results'));
    assert.ok(rendered.includes(expected));
    assert.ok(!rendered.includes(forbidden));
  }
});

// Exercise the shipped functions with browser/network boundaries stubbed.
function load(context, names) {
  vm.createContext(context);
  for (const name of names) {
    const match = new RegExp('^(?:async )?function ' + name + '\\(', 'm').exec(source);
    assert.ok(match, name);
    const end = source.indexOf('\n}', match.index) + 2;
    vm.runInContext(source.slice(match.index, end), context);
  }
  return context;
}

test('saving one account adopts server normalization and preserves other drafts', async () => {
  const first = { id: 'one', name: 'Main', username: 'user', password: '', password_configured: false };
  const second = { id: 'two', name: 'Other', password: '', password_configured: true };
  const saved = {
    vps_seckill: { accounts: [first, second] }, products: [],
    telegram: { configured: true, bot_token_configured: true, chat_id: '100', disabled: false },
    telegram_routes: { stock: { mode: 'custom', bot_token_configured: true, chat_id: '200' } },
  };
  const draft = copy(saved);
  // collectConfig() returns input values, without server-only credential metadata.
  draft.telegram = { bot_token: '', chat_id: '101', disabled: false, clear: false };
  draft.telegram_routes.stock = { mode: 'custom', bot_token: '', chat_id: '201', clear: false };
  Object.assign(draft.vps_seckill.accounts[0], { username: ' user ', password: 'new-password' });
  draft.vps_seckill.accounts[1].name = 'Other unsaved draft';
  const response = { config: copy(saved), service: { ok: true } };
  Object.assign(response.config.vps_seckill.accounts[0], { password_configured: true });
  let submitted;
  const context = load({
    config: draft, savedConfig: JSON.stringify(saved),
    document: { querySelector: () => ({ value: '' }) },
    collectConfig: () => copy(context.config),
    seckillAccounts: () => context.config.vps_seckill.accounts,
    applyConfig: value => { context.config = copy(value); },
    requestJSON: async (url, options) => { submitted = JSON.parse(options.body); return response; },
    updateDraftState() {}, setMessage() {},
    refreshManagement() { throw new Error('Account feedback must not wait for management refresh'); },
    loadStatus() { throw new Error('Account feedback must not wait for status refresh'); },
  }, ['cloneConfig', 'mergeConfigScope', 'savedConfigSnapshot', 'comparableSeckillAccount', 'hasUnsavedSeckillAccount', 'saveConfig']);
  assert.equal(await context.saveConfig('seckill-account:one'), response);
  assert.equal(submitted.vps_seckill.accounts.find(row => row.id === 'two').name, 'Other');
  assert.equal(context.config.vps_seckill.accounts[0].username, 'user');
  assert.equal(context.config.vps_seckill.accounts[0].password, '');
  assert.equal(context.hasUnsavedSeckillAccount('one'), false);
  assert.equal(context.hasUnsavedSeckillAccount('two'), true);
  assert.equal(context.config.vps_seckill.accounts[1].name, 'Other unsaved draft');
  assert.equal(context.config.telegram.configured, true);
  assert.equal(context.config.telegram.bot_token_configured, true);
  assert.equal(context.config.telegram.chat_id, '101');
  assert.equal(context.config.telegram_routes.stock.bot_token_configured, true);
  assert.equal(context.config.telegram_routes.stock.chat_id, '201');
  assert.equal(submitted.telegram.chat_id, '100');
  assert.equal(submitted.telegram_routes.stock.chat_id, '200');
});

test('account save keeps explicit success, apply failure and request failure feedback', async () => {
  for (const [response, error, expectedKind, phrase] of [
    [{ service: { ok: true } }, null, 'success', '已保存'],
    [{ service: { ok: false, error: 'Apply failed' } }, null, 'error', 'Apply failed'],
    [null, new Error('Network failed'), 'error', '保存失败'],
  ]) {
    let thrown;
    const context = load({
      accountSaveFeedback: new Map(), hasUnsavedSeckillAccount: () => true,
      invalidateAccountCheck() {}, renderVpsSeckill() {}, setMessage() {},
      saveConfig: async () => { if (error) throw error; return response; },
      runAction: async (button, label, message, action) => { try { await action(); } catch (value) { thrown = value; } },
    }, ['saveSeckillAccount']);
    await context.saveSeckillAccount({ id: 'one', name: 'Main' }, null);
    const feedback = context.accountSaveFeedback.get('one');
    assert.equal(feedback.kind, expectedKind);
    assert.ok(feedback.message.includes(phrase));
    assert.equal(thrown, error || undefined);
  }
});

test('checking an account reports only the final outcome after saving', async () => {
  for (const outcome of ['success', 'invalid', 'check-error', 'save-error', 'apply-error', 'verification']) {
    const account = { id: 'one', name: 'Main', username: 'user', password: '', session_cookie: 'transient-cookie' };
    const saved = { vps_seckill: { accounts: [{ id: 'one', name: 'Main', username: 'user', password_configured: true }] }, products: [] };
    const messages = [], requests = [];
    let finishCheck, receivedCookie;
    const context = load({
      config: { ...copy(saved), vps_seckill: { accounts: [account] } }, savedConfig: JSON.stringify(saved),
      actionDepth: 0, actionRunning: false,
      standaloneAccountChecks: new Map(), accountHealthChecks: new Map(),
      document: { querySelector: () => ({ value: '' }) },
      collectConfig: () => copy(context.config), seckillAccounts: () => context.config.vps_seckill.accounts,
      applyConfig: value => { context.config = copy(value); },
      requestJSON: async (url, options) => {
        requests.push(url);
        if (url === '/api/config') {
          if (outcome === 'save-error') throw new Error('Save failed');
          return { config: copy(saved), service: { ok: outcome !== 'apply-error', error: 'Apply failed' } };
        }
        assert.equal(url, '/api/seckill/account-check');
        receivedCookie = JSON.parse(options.body).account.session_cookie;
        // Saving must not announce success while the actual check is pending.
        assert.equal(messages.some(message => message.kind === 'success'), false);
        return new Promise((resolve, reject) => {
          finishCheck = () => outcome === 'check-error' ? reject(new Error('Login failed'))
            : resolve({ account_id: 'one', ok: outcome === 'success', verification_required: outcome === 'verification',
                message: outcome === 'verification' ? 'Verification needed' : outcome === 'invalid' ? 'Login invalid' : 'Login succeeded' });
        });
      },
      setMessage: (message, kind) => messages.push({ message, kind }),
      updateDraftState() {}, invalidateAccountCheck() {}, renderVpsSeckill() {},
      storeAccountCheck: (target, value) => target.set(value.account_id, value),
      cancelProductDrag() {}, syncUpdateControl() {}, renderMerchantOrder() {}, renderProductList() {},
      syncTestControls() {}, updateCardSaveStates() {}, Event: class {}, window: { dispatchEvent() {} },
    }, ['cloneConfig', 'mergeConfigScope', 'savedConfigSnapshot', 'saveConfig', 'runAction', 'checkSeckillAccount']);
    const pending = context.checkSeckillAccount(account, null);
    // Flush the save request and its continuation before resolving the check.
    await new Promise(resolve => setImmediate(resolve));
    if (finishCheck) finishCheck();
    await pending;
    const terminal = messages.filter(message => ['success', 'error'].includes(message.kind));
    if (outcome === 'verification') {
      assert.equal(terminal.length, 0);
      assert.equal(messages.at(-1).message, 'Verification needed');
    } else {
      assert.equal(terminal.length, 1, outcome);
      assert.equal(terminal[0].kind, outcome === 'success' ? 'success' : 'error', outcome);
      const expected = { success: 'Login succeeded', invalid: 'Login invalid', 'save-error': 'Save failed',
        'apply-error': 'Apply failed', 'check-error': 'Login failed' }[outcome];
      assert.ok(terminal[0].message.includes(expected), outcome);
      if (outcome === 'check-error') assert.ok(terminal[0].message.includes('账户已保存'));
    }
    assert.equal(Object.hasOwn(account, 'session_cookie'), false, outcome);
    assert.equal(context.actionRunning, false, outcome);
    if (['save-error', 'apply-error'].includes(outcome)) {
      assert.deepEqual(requests, ['/api/config']);
    } else {
      assert.deepEqual(requests, ['/api/config', '/api/seckill/account-check']);
      assert.equal(receivedCookie, 'transient-cookie');
    }
  }
});

test('a delayed background response cannot replace a completed manual check', async () => {
  let finishProbe;
  const success = { account_id: 'one', status: 'valid', ok: true, checked_at: '2026-10-20T12:00:01+08:00' };
  const context = load({
    actionRunning: false, document: { hidden: false }, currentPage: 'seckill',
    accountHealthChecks: new Map(), accountCheckGenerations: new Map(),
    seckillAccounts: () => [{ id: 'one' }],
    requestJSON: () => new Promise(resolve => { finishProbe = resolve; }),
    renderVpsSeckill() {}, setTimeout() {}, ACCOUNT_HEALTH_INTERVAL_MS: 3600000,
  }, ['storeAccountCheck', 'invalidateAccountCheck', 'refreshAccountHealth']);
  const pending = context.refreshAccountHealth();
  context.invalidateAccountCheck('one');
  context.storeAccountCheck(context.accountHealthChecks, success);
  // Even a response stamped later must be discarded if its request is stale.
  finishProbe({ account_id: 'one', status: 'expired', ok: false, checked_at: '2026-10-20T12:00:02+08:00' });
  await pending;
  assert.equal(context.accountHealthChecks.get('one'), success);
  context.storeAccountCheck(context.accountHealthChecks, { ...success, status: 'unknown', ok: false });
  assert.equal(context.accountHealthChecks.get('one'), success);
});

test('overlapping actions keep background health checks paused until both finish', async () => {
  const context = load({
    actionDepth: 0, actionRunning: false, cancelProductDrag() {}, setMessage() {},
    syncUpdateControl() {}, renderMerchantOrder() {}, renderProductList() {},
    syncTestControls() {}, updateCardSaveStates() {}, Event: class {},
    window: { dispatchEvent() {} },
  }, ['runAction']);
  let finishFirst, finishSecond;
  const first = context.runAction(null, '', '', () => new Promise(resolve => { finishFirst = resolve; }));
  const second = context.runAction(null, '', '', () => new Promise(resolve => { finishSecond = resolve; }));
  assert.equal(context.actionRunning, true);
  finishFirst();
  await first;
  assert.equal(context.actionRunning, true);
  finishSecond();
  await second;
  assert.equal(context.actionRunning, false);
});
