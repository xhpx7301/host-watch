// Run with: node --test scripts/test-admin-accounts.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '..', 'web', 'admin.js'), 'utf8');
const copy = value => JSON.parse(JSON.stringify(value));

test('Narwhal system select defaults to Alpine while preserving explicit and unavailable choices', () => {
  const systems = [{ id: 'debian', name: 'Debian (Podman)' }, { id: 'alpine', name: 'Alpine (Podman)' }];
  const context = load({ Option: function(name, id) { this.text = name; this.value = id; } }, ['narwhalSystemOptions']);
  for (const [requested, expected] of [['', 'alpine'], ['Debian (Podman)', 'debian'], ['debian', 'debian'], ['old-os', 'old-os']]) {
    const select = { replaceChildren(...options) { this.options = options; }, add(option) { this.options.push(option); } };
    assert.equal(context.narwhalSystemOptions(select, systems, requested, 'alpine'), expected);
    assert.equal(select.value, expected);
    assert.equal(select.disabled, false);
    if (requested === 'old-os') assert.equal(select.options.at(-1).disabled, true);
  }
});

test('Narwhal system loading shares requests and saves defaults, failures retain saved choice for retry', async () => {
  const systems = [{ id: 'debian', name: 'Debian' }, { id: 'alpine', name: 'Alpine' }];
  let calls = 0, saves = 0, fail = false;
  const context = load({
    config: { egress: {} }, narwhalSystemRequests: new Map(),
    Option: function(name, id) { this.text = name; this.value = id; },
    requestJSON: async () => { calls++; if (fail) throw Error('Network'); return { systems, default_system: 'alpine' }; },
    updateDraftState() {}, scheduleAutoSave() { saves++; },
  }, ['narwhalSystemOptions', 'loadNarwhalSystems']);
  const makeSelect = () => ({ isConnected: true, replaceChildren(...options) { this.options = options; }, add(option) { this.options.push(option); } });
  const first = {}, second = { operating_system: 'debian' }, product = { machine_id: 'node', plan_id: 'plan' };
  await context.loadNarwhalSystems(makeSelect(), {}, {}, first, product);
  await context.loadNarwhalSystems(makeSelect(), {}, {}, second, product);
  assert.equal(first.operating_system, 'alpine'); assert.equal(second.operating_system, 'debian');
  assert.equal(calls, 1); assert.equal(saves, 1);
  fail = true;
  const select = makeSelect(), note = {}, retry = {};
  await context.loadNarwhalSystems(select, note, retry, second, product, true);
  assert.equal(second.operating_system, 'debian'); assert.equal(select.disabled, true); assert.equal(retry.disabled, false);
  fail = false;
  await context.loadNarwhalSystems(select, note, retry, second, product, true);
  assert.equal(select.value, 'debian'); assert.equal(calls, 3);
  const detached = makeSelect(); detached.isConnected = false;
  const removed = {};
  await context.loadNarwhalSystems(detached, {}, {}, removed, product);
  assert.equal(removed.operating_system, undefined);
});

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

const machineId = '17141ff1-5623-4455-a7a5-91a0146f090e';
const planId = 'fe7edd6f-a84c-43fc-a41a-6221b33410ff';
const narwhalTarget = `https://dash.fuckip.me/deploy?plan_id=${planId}&machine_id=${machineId}`;

test('catalog seckill selection survives scoped auto-save and reload with merchant systems intact', async () => {
  const saved = { products: [{ id: 'existing', name: 'Saved', watch_enabled: false }],
    vps_seckill: { accounts: [{ id: 'account', provider: 'narwhal', name: 'Saved account' }], items: [] } };
  const draft = copy(saved);
  Object.assign(draft.products[0], { name: 'Unsaved edit', watch_enabled: true });
  draft.products.push({ id: 'unrelated', watch_enabled: true });
  draft.vps_seckill.accounts[0].name = 'Unsaved account';
  let submitted, systemReads = 0;
  const context = load({
    config: draft, savedConfig: JSON.stringify(saved), actionRunning: false, structuredClone, URL, autoSaveScopeNames: { seckill: '抢购' },
    crypto: { randomUUID: () => 'catalog-product' }, merchantNames: { narwhal: '独角鲸云' },
    document: { querySelector: () => ({ value: '' }) },
    collectConfig: () => copy(context.config),
    renderProducts() {}, renderVpsSeckill() {}, updateDraftState() {}, setMessage() {}, navigatePage() {},
    scheduleAutoSave: scope => assert.equal(scope, 'seckill'), narwhalSystemRequests: new Map(),
    Option: function(name, id) { this.text = name; this.value = id; },
    requestJSON: async (url, options) => {
      const payload = JSON.parse(options.body);
      if (url === '/api/config') {
        submitted = payload;
        assert.equal(options.headers['X-HostWatch-Auto-Save'], '1');
        return { config: copy(payload), service: { ok: true } };
      }
      assert.equal(url, '/api/seckill/systems'); systemReads++;
      const item = payload.config.vps_seckill.items.find(row => row.id === payload.item_id);
      const product = payload.config.products.find(row => row.id === item.product_id);
      assert.equal(product.adapter, 'narwhal'); assert.equal(product.plan_id, planId);
      return { systems: [{ id: 'alpine', name: 'Alpine' }], default_system: 'alpine' };
    },
  }, ['cloneConfig', 'mergeConfigScope', 'savedConfigSnapshot', 'seckillItems', 'seckillAccounts',
    'seckillProviderKey', 'seckillCoupon', 'seckillAddDefaults', 'addCatalogToVpsSeckill', 'saveConfig',
    'narwhalSystemOptions', 'loadNarwhalSystems']);
  const row = { key: 'narwhal-plan', name: 'Test plan', purchase_url: narwhalTarget,
    watch: { adapter: 'narwhal', provider: '独角鲸云', name: 'Test plan', machine_id: machineId, plan_id: planId } };
  context.addCatalogToVpsSeckill([{ row }]);
  // Existing products referenced by a new item must keep their saved values.
  context.config.vps_seckill.items.push({ id: 'second', product_id: 'existing' });
  // Even a simultaneous monitoring draft on the new product belongs to that card.
  context.config.products.at(-1).watch_enabled = true;
  await context.saveConfig('seckill', true);
  assert.deepEqual(submitted.products.map(row => row.id), ['existing', 'catalog-product']);
  assert.deepEqual(submitted.products[0], saved.products[0]);
  assert.equal(submitted.products[1].watch_enabled, false);
  assert.equal(submitted.products[1].notify_enabled, false);
  assert.equal(submitted.vps_seckill.accounts[0].name, 'Saved account');
  assert.equal(context.config.products[0].name, 'Unsaved edit');
  assert.equal(context.config.products.at(-1).watch_enabled, true);
  // Reload from the actual auto-save baseline, rather than the in-memory draft.
  context.config = JSON.parse(context.savedConfig);
  const item = context.config.vps_seckill.items[0];
  const product = context.config.products.find(row => row.id === item.product_id);
  assert.equal(context.seckillProviderKey(product.adapter), 'narwhal');
  assert.equal(item.payment_method, 'balance'); assert.equal(item.narwhal_auto_renew, true);
  const select = { isConnected: true, replaceChildren() {}, add() {} };
  await context.loadNarwhalSystems(select, {}, {}, item, product);
  assert.equal(select.value, 'alpine'); assert.equal(systemReads, 1);
});

test('Narwhal recovery recognizes only official deployment URLs with unique valid plan and node IDs', () => {
  const context = load({ URL }, ['narwhalProductFromTarget']);
  const item = { product_id: 'lost', label: 'Original', target_url: narwhalTarget };
  assert.equal(context.narwhalProductFromTarget(item).machine_id, machineId);
  for (const target_url of [
    narwhalTarget.replace('https:', 'http:'), narwhalTarget.replace('dash.fuckip.me', 'example.com'),
    narwhalTarget.replace('/deploy?', '/other?'), narwhalTarget.replace('dash.', 'user@dash.'),
    narwhalTarget.replace('dash.fuckip.me', 'dash.fuckip.me:8443'), `${narwhalTarget}#fragment`,
    `${narwhalTarget}&plan_id=${planId}`, narwhalTarget.replace(machineId, 'invalid'),
    `https://dash.fuckip.me/deploy?plan_id=${planId}`, '',
  ]) assert.equal(context.narwhalProductFromTarget({ ...item, target_url }), null, target_url);
});

test('recovering a missing Narwhal product verifies metadata before preserving all existing item settings', async () => {
  for (const outcome of ['success', 'failure', 'removed', 'changed', 'limit']) {
    const item = { id: 'item', product_id: 'lost', label: 'Original', target_url: narwhalTarget,
      account_id: 'account', egress_node_id: 'node', coupon_code: 'CODE', operating_system: 'debian',
      quantity: 1, narwhal_auto_renew: false, enabled: false };
    const original = copy(item), scopes = [], messages = [];
    const config = { products: [], vps_seckill: { items: [item], accounts: [], narwhal_coupons: { [machineId]: 'NODE' } } };
    if (outcome === 'limit') config.products = Array.from({ length: 100 }, (_, id) => ({ id: String(id) }));
    let finish, requests = 0;
    const button = { disabled: false, isConnected: true };
    const context = load({
      config, URL, actionRunning: false, collectConfig: () => copy(config),
      requestJSON: (url, options) => {
        requests++; assert.equal(url, '/api/seckill/systems');
        const payload = JSON.parse(options.body);
        assert.equal(payload.config.products.at(-1).plan_id, planId);
        assert.equal(config.products.length, 0, 'Recovery must wait for server verification');
        return new Promise((resolve, reject) => { finish = () => outcome === 'failure' ? reject(Error('Unavailable')) : resolve({}); });
      },
      renderProducts() {}, renderVpsSeckill() {}, updateDraftState() {},
      scheduleAutoSave: scope => scopes.push(scope), setMessage: (message, kind) => messages.push({ message, kind }),
    }, ['narwhalProductFromTarget', 'restoreNarwhalSeckillProduct', 'seckillItems']);
    const pending = context.restoreNarwhalSeckillProduct(item, button);
    if (outcome === 'removed') config.vps_seckill.items = [];
    if (outcome === 'changed') item.target_url += '&changed=1';
    if (finish) finish();
    await pending;
    assert.equal(button.disabled, false);
    assert.equal(requests, outcome === 'limit' ? 0 : 1);
    if (outcome === 'success') {
      assert.equal(config.products[0].id, item.product_id);
      assert.equal(config.products[0].watch_enabled, false);
      assert.deepEqual(item, original); assert.equal(config.vps_seckill.narwhal_coupons[machineId], 'NODE');
      assert.deepEqual(scopes, ['seckill']);
      const saved = context.config;
      const merger = load({}, ['cloneConfig', 'mergeConfigScope']);
      assert.equal(merger.mergeConfigScope({ products: [] }, saved, 'seckill').products[0].id, 'lost');
    } else {
      assert.equal(config.products.length, outcome === 'limit' ? 100 : 0);
      assert.deepEqual(scopes, []);
      if (['failure', 'limit'].includes(outcome)) assert.equal(messages.at(-1).kind, 'error');
    }
  }
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
