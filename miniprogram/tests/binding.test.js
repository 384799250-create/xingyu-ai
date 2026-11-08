const assert = require('node:assert/strict');
const clone = x => JSON.parse(JSON.stringify(x));
const storage = new Map(), requests = [];
let definition, connected = false, wxLogins = 0;
const state = { revision: 'r1', selected: 'p1', scope: 'only-my-app', profiles: { p1: { display_name: '我的伙伴' } }, templateDrafts: {} };
global.wx = {
  getStorageSync: k => storage.get(k), setStorageSync: (k,v) => storage.set(k,clone(v)), removeStorageSync: k => storage.delete(k),
  login: options => { wxLogins++; options.success({ code: 'wx-provider-code' }); },
  disableAlertBeforeUnload() {}, enableAlertBeforeUnload() {}, pageScrollTo() {},
  showModal: options => options.success({ confirm: true }),
  request: options => {
    const path = options.url.split('/ai/mini/v1')[1];
    requests.push({ path, method: options.method, data: clone(options.data || {}) });
    let result;
    const session = { session: 's'.repeat(43), expiresAt: Date.now()/1000+3600, wechatBound: true };
    if (path === '/status') result = { wechatReady: true };
    else if (path === '/wechat/session') {
      assert.deepEqual(options.data, { code: 'wx-provider-code' });
      result = connected ? session : { needsBinding: true, ticket: 'b'.repeat(43) };
    } else if (path === '/link/session') {
      assert.equal(options.data.ticket, 'b'.repeat(43));
      connected = true; result = session;
    } else if (path.endsWith('/settings')) result = { state: clone(state), application: { name: '大肥鱼' } };
    else if (path.endsWith('/templates')) result = { items: [] };
    else if (path === '/logout') result = { ok: true };
    else if (path === '/wechat/unbind') { connected = false; result = { ok: true }; }
    else throw Error('Unexpected request: '+path);
    options.success({ statusCode: 200, data: result });
  }
};
global.Page = value => { definition = value; };
const account = require('../src/lib/account');
require('../src/pages/settings/index');
const page = () => Object.assign({}, definition, { data: clone(definition.data), setData(patch) { Object.assign(this.data,patch); } });
(async () => {
  let p = page(); await p.onLoad();
  assert.equal(p.data.bindingPending,true); assert.equal(p.data.mode,'entry'); assert.equal(wxLogins,1);
  p.linkField({ detail: { value: 'https://www.example.com/xiaoyi/#token=private-proof' } });
  await p.connectLink(); assert.equal(p.data.mode,'account'); assert.equal(p.data.wechatBound,true);
  assert.equal(p.data.settingsLink,''); assert.equal(p.data.applicationName,'大肥鱼');
  assert.equal(requests.filter(x => x.method === 'PUT').length,0,'Connecting never writes persona settings');
  const retained = JSON.stringify([...storage]);
  assert.equal(retained.includes('private-proof'),false); assert.equal(retained.includes('wx-provider-code'),false);
  assert.equal(retained.includes('b'.repeat(43)),false,'Binding ticket is kept only in memory');
  // A normal return goes straight into the same application, without a new proof.
  p = page(); await p.onLoad(); assert.equal(p.data.mode,'account'); assert.equal(wxLogins,1);
  // Expired/cleared local session can be restored from the verified WeChat binding.
  account.forget(); p = page(); await p.onLoad(); assert.equal(p.data.mode,'account'); assert.equal(wxLogins,2);
  await p.leaveAccount(); assert.equal(p.data.mode,'entry'); assert.equal(account.autoAllowed(),false);
  p = page(); await p.onLoad(); assert.equal(p.data.mode,'entry'); assert.equal(wxLogins,2,'Explicit logout suppresses automatic login');
  await p.connectWeChat(); assert.equal(p.data.mode,'account');
  p.disconnectWeChat(); await new Promise(resolve => setImmediate(resolve));
  assert.equal(p.data.mode,'entry'); assert.equal(account.session(),''); assert.equal(connected,false);
  console.log(JSON.stringify({ ok: true, checks: ['first_ownership_proof','automatic_return','wechat_session_recovery','no_settings_write_on_bind','no_proof_in_storage','logout_respected','explicit_unbind'] }));
})().catch(e => { console.error(e); process.exitCode = 1; });
