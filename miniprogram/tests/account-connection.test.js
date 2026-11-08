'use strict';
const assert = require('node:assert/strict');
const clone = value => JSON.parse(JSON.stringify(value));
const storage = new Map(), requests = [];
let definition, binding = null, rejectPassword = false;
const state = { revision: 'r1', scope: 'shared-test-app', selected: 'p1',
  profiles: { p1: { display_name: '测试伙伴' } }, templateDrafts: {} };
global.wx = {
  getStorageSync: key => storage.get(key), setStorageSync: (key, value) => storage.set(key, clone(value)),
  removeStorageSync: key => storage.delete(key),
  login: options => options.success({ code: 'current-wechat-code' }),
  disableAlertBeforeUnload() {}, enableAlertBeforeUnload() {}, pageScrollTo() {},
  showModal: options => options.success({ confirm: true }),
  request(options) {
    const path = options.url.split('/ai/mini/v1')[1];
    requests.push({ path, method: options.method, data: clone(options.data || {}) });
    let data, statusCode = 200;
    const session = { session: 's'.repeat(43), expiresAt: Date.now()/1000+3600 };
    if (path === '/status') data = { wechatReady: true, phoneReady: false };
    else if (path === '/wechat/session') data = { needsBinding: true, ticket: 'b'.repeat(43) };
    else if (path === '/session' || path === '/wechat/bind/account') {
      if (rejectPassword) { statusCode = 401; data = { error: '账号或密码不正确。' }; }
      else if (path === '/wechat/bind/account') {
        assert.equal(options.data.ticket, 'b'.repeat(43));
        binding = 'current-wechat'; data = Object.assign({}, session, { wechatBound: true });
      } else {
        assert.deepEqual(Object.keys(options.data).sort(), ['password', 'username']);
        data = session;
      }
    } else if (path.endsWith('/settings')) data = { state: clone(state), application: { name: '审核测试' } };
    else if (path.endsWith('/templates')) data = { items: [] };
    else if (path === '/logout') data = { ok: true };
    else throw Error('Unexpected path: ' + path);
    options.success({ statusCode, data });
  }
};
global.Page = value => { definition = value; };
const account = require('../src/lib/account');
require('../src/pages/settings/index');
function page() {
  return Object.assign({}, definition, { data: clone(definition.data),
    setData(patch) { Object.assign(this.data, patch); } });
}
function credentials(p) {
  p.loginField({ currentTarget: { dataset: { key: 'username' } }, detail: { value: 'shared-test' } });
  p.loginField({ currentTarget: { dataset: { key: 'password' } }, detail: { value: 'test-password' } });
}
(async () => {
  let p = page(); await p.onLoad(); credentials(p);
  assert.equal(p.data.bindingPending, true);
  assert.equal(p.data.rememberWechat, true, 'Existing automatic-connection default is preserved');
  binding = 'another-wechat';
  p.toggleRememberWechat({ detail: { value: false } }); await p.login();
  assert.equal(p.data.mode, 'account'); assert.equal(p.data.wechatBound, false);
  assert.equal(binding, 'another-wechat', 'Account-only sign-in never replaces an existing binding');
  assert.equal(requests.filter(r => r.path === '/wechat/bind/account').length, 0);
  assert.equal(requests.filter(r => r.method === 'PUT').length, 0, 'Signing in never saves settings');
  assert.equal(JSON.stringify([...storage]).includes('test-password'), false);
  assert.equal(p.data.password, '');
  await p.leaveAccount();
  assert.equal(p.data.manualConnection, true, 'Explicit logout keeps manual connection controls available');
  p = page(); await p.onLoad();
  assert.equal(p.data.manualConnection, true, 'Manual connection remains available after reopening');
  credentials(p); await p.login();
  assert.equal(p.data.mode, 'account'); assert.equal(binding, 'another-wechat');
  await p.leaveAccount();
  // A fresh user choosing the original remember option still follows ownership binding.
  storage.clear(); binding = null; p = page(); await p.onLoad(); credentials(p); await p.login();
  assert.equal(binding, 'current-wechat'); assert.equal(p.data.wechatBound, true);
  await p.leaveAccount();
  p = page(); await p.onLoad(); credentials(p); rejectPassword = true; await p.login();
  assert.equal(p.data.mode, 'entry'); assert.equal(account.session(), '');
  assert.equal(p.data.password, ''); assert.match(p.data.error, /账号或密码/);
  console.log(JSON.stringify({ ok: true, checks: ['optional_binding', 'preserves_existing_wechat_binding',
    'no_settings_write_on_login', 'no_password_storage', 'logout_keeps_manual_connection',
    'manual_connection_after_reopen', 'default_ownership_binding', 'invalid_password_rejected'] }));
})().catch(error => { console.error(error); process.exitCode = 1; });
