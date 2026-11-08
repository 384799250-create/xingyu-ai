const assert = require('node:assert/strict');
const clone = x => JSON.parse(JSON.stringify(x));
const storage = new Map(), requests = [];
let definition, scenario = 'existing', pending = false;
const session = { session: 's'.repeat(43), expiresAt: Date.now()/1000+3600, wechatBound: true };
const state = { revision: 'r1', selected: 'p1', scope: 'my-app', profiles: { p1: { display_name: '我的伙伴' } }, templateDrafts: {} };
global.wx = {
  getStorageSync: k => storage.get(k), setStorageSync: (k,v) => storage.set(k,clone(v)), removeStorageSync: k => storage.delete(k),
  login: o => o.success({ code: 'wechat-code' }),
  disableAlertBeforeUnload() {}, enableAlertBeforeUnload() {}, pageScrollTo() {},
  request: o => {
    const path = o.url.split('/ai/mini/v1')[1]; requests.push({ path, method:o.method, data:clone(o.data || {}) });
    let result;
    if (path === '/status') result = { wechatReady:true, phoneReady:scenario !== 'disabled' };
    else if (path === '/wechat/session') result = { needsBinding:true, ticket:'t'.repeat(43), hasEnrollment:pending };
    else if (path === '/phone/resolve') {
      assert.deepEqual(o.data,{ ticket:'t'.repeat(43), code:'phone-grant' });
      result = scenario === 'existing' ? session : { needsApplication:true, proof:'proof-memory-only' };
    } else if (path === '/phone/apply') {
      assert.deepEqual(o.data,{ticket:'t'.repeat(43), proof:'proof-memory-only', consent:true}); pending = true;
      if (scenario === 'unknown') return o.fail({errMsg:'network disconnected'});
      result = {pending:true,state:'waiting_pool'};
    } else if (path === '/phone/progress') {
      assert.deepEqual(o.data,{ticket:'t'.repeat(43)});
      result = scenario === 'unknown' ? {error:'没有开通记录，请重新授权。'} : Object.assign({},session,{joinQr:'data:image/png;base64,c3ludGhldGlj'});
      if (scenario === 'unknown') return o.success({statusCode:409,data:result});
    } else if (path.endsWith('/settings')) result = {state:clone(state),application:{name:'大肥鱼'}};
    else if (path.endsWith('/templates')) result = {items:[]};
    else if (path.endsWith('/join')) result = {joinQr:'data:image/png;base64,c3ludGhldGlj'};
    else throw Error('Unexpected: '+path);
    o.success({statusCode:200,data:result});
  }
};
global.Page = value => { definition = value; };
const account = require('../src/lib/account'); require('../src/pages/settings/index');
const page = () => Object.assign({},definition,{data:clone(definition.data),setData(patch){Object.assign(this.data,patch);}});
const grant = p => p.connectPhone({detail:{code:'phone-grant'}});
const count = path => requests.filter(r=>r.path===path).length;
(async () => {
  let p=page();await p.onLoad();assert.equal(p.data.phoneReady,true);assert.equal(p.data.bindingPending,true);
  await p.connectPhone({detail:{errMsg:'denied'}});assert.equal(count('/phone/resolve'),0);
  await grant(p);assert.equal(p.data.mode,'account');assert.equal(p.data.applicationName,'大肥鱼');
  assert.equal(count('/phone/apply'),0);assert.equal(p.data.dirty,false);
  account.forget();storage.clear();scenario='new';p=page();await p.onLoad();await grant(p);
  assert.equal(p.data.phoneNeedsApp,true);assert.equal(count('/phone/apply'),0,'Grant does not allocate');
  await p.applyPhone();assert.equal(count('/phone/apply'),1);assert.equal(p.data.phonePending,true);
  await p.checkPhoneProgress();assert.equal(p.data.mode,'account');assert.ok(p.data.joinQr);
  p.setData({joinQr:''});await p.loadJoinQr();assert.ok(p.data.joinQr,'Returning users can reread the QR');
  account.forget();storage.clear();pending=false;scenario='unknown';p=page();await p.onLoad();await grant(p);await p.applyPhone();
  assert.equal(p.data.phonePending,true);assert.match(p.data.error,/结果尚未确认/);
  const attempts=count('/phone/apply');await p.checkPhoneProgress();assert.equal(count('/phone/apply'),attempts);
  await p.retryPhoneAuthorization();assert.equal(p.data.phonePending,false);assert.equal(p.data.bindingPending,true);
  await grant(p);await p.applyPhone();assert.equal(count('/phone/apply'),attempts+1,'Reauthorization allows explicit recovery');
  p.onUnload();account.forget();storage.clear();pending=false;scenario='disabled';p=page();await p.onLoad();
  assert.equal(p.data.phoneReady,false);assert.equal(p.data.bindingPending,true);p.onUnload();
  const saved=JSON.stringify([...storage]);assert.equal(saved.includes('phone-grant'),false);assert.equal(saved.includes('proof-memory-only'),false);
  assert.equal(requests.filter(r=>r.method==='PUT').length,0,'Onboarding never saves persona settings');
  console.log(JSON.stringify({ok:true,checks:['existing_app','declined_grant','explicit_allocation','readonly_progress','join_qr_recovery','unknown_result','reauthorization','gate_disabled','manual_save_preserved']}));
})().catch(e=>{console.error(e);process.exitCode=1;});
