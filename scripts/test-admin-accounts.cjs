// Run with: node --test scripts/test-admin-accounts.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '..', 'admin.js'), 'utf8');
const copy = value => JSON.parse(JSON.stringify(value));

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
  const saved = { vps_seckill: { accounts: [first, second] }, products: [] };
  const draft = copy(saved);
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
