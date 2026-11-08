const assert = require('node:assert/strict');
const clone = x => JSON.parse(JSON.stringify(x));
const storage = new Map(), requests = [];
let pageDefinition, appDefinition, confirm = true, rejectEntry = false;
const raw = 'e'.repeat(43);
const state = { revision:'r1', selected:'p1', scope:'my-app', profiles:{p1:{display_name:'大肥鱼'}},templateDrafts:{} };
global.wx = {
  getStorageSync:k=>storage.get(k),setStorageSync:(k,v)=>storage.set(k,clone(v)),removeStorageSync:k=>storage.delete(k),
  login:o=>o.success({code:'real-platform-code'}),
  enableAlertBeforeUnload(){},disableAlertBeforeUnload(){},pageScrollTo(){},
  showModal:o=>o.success({confirm}),
  request:o=>{
    const path=o.url.split('/ai/mini/v1')[1]; requests.push({path,method:o.method,data:clone(o.data||{})});
    let result;
    if(path==='/entry/session') {
      assert.deepEqual(o.data,{entry:raw,code:'real-platform-code'});
      if(rejectEntry)return o.success({statusCode:409,data:{error:'此微信已连接其他应用，请先核对原绑定。'}});
      result={session:'s'.repeat(43),expiresAt:Date.now()/1000+3600,wechatBound:true};
    } else if(path.endsWith('/settings')) result={state:clone(state),application:{name:'大肥鱼'}};
    else if(path.endsWith('/templates'))result={items:[]};
    else throw Error('Unexpected '+path);
    o.success({statusCode:200,data:result});
  }
};
global.Page=v=>{pageDefinition=v;};global.App=v=>{appDefinition=v;};
require('../src/app');require('../src/pages/settings/index');
const account=require('../src/lib/account');
const page=()=>Object.assign({},pageDefinition,{data:clone(pageDefinition.data),setData(patch){Object.assign(this.data,patch);}});
(async()=>{
  let p=page();appDefinition.onShow({query:{entry:raw}});await p.onLoad({entry:raw});
  assert.equal(p.data.mode,'account');assert.equal(p.data.applicationName,'大肥鱼');
  assert.equal(requests[0].path,'/entry/session');assert.equal(requests.some(r=>r.path==='/link/session'),false);
  const original=clone(p.draft);p.draft.profiles.p1.display_name='未保存名字';p.setData({dirty:true});confirm=false;
  const before=requests.length;await p.openEntry(raw);assert.equal(requests.length,before);assert.equal(p.draft.profiles.p1.display_name,'未保存名字');
  confirm=true;rejectEntry=true;await p.openEntry(raw);assert.equal(p.draft.profiles.p1.display_name,'未保存名字');assert.match(p.data.error,/已连接其他应用/);
  assert.equal(account.session(),'s'.repeat(43),'Rejected entry preserves previous session');
  rejectEntry=false;await p.openEntry(raw);assert.equal(p.data.dirty,false);assert.deepEqual(p.draft,original);
  const length=requests.length;p.setData({saving:true});await p.openEntry(raw);assert.equal(requests.length,length);p.setData({saving:false});
  // Opening from a chat while the mini program is already running uses App.onShow.
  appDefinition.onShow({query:{entry:raw}});p.onShow();await new Promise(resolve=>setImmediate(resolve));
  assert.equal(requests.filter(r=>r.path==='/entry/session').length,4);
  const after=requests.length;p.onShow();await new Promise(resolve=>setImmediate(resolve));assert.equal(requests.length,after);
  assert.equal(requests.some(r=>r.method==='PUT'),false,'Opening never auto saves');
  const saved=JSON.stringify([...storage]);assert.equal(saved.includes(raw),false);assert.equal(saved.includes('real-platform-code'),false);
  p.onUnload();
  console.log(JSON.stringify({ok:true,checks:['cold_launch','automatic_app_selection','no_paste','warm_launch','no_duplicate_launch','dirty_draft_confirmation','failed_entry_preserves_session','save_in_progress','manual_save','no_entry_in_storage']}));
})().catch(e=>{console.error(e);process.exitCode=1;});
