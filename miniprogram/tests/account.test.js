'use strict';
const assert = require('node:assert/strict');
const configPath = require.resolve('../src/config');
require.cache[configPath] = { id: configPath, filename: configPath, loaded: true,
  exports: Object.assign({}, require('../src/config'), { integrationReady: true }) };
const storage = new Map(), requests = [];
let definition, saveFailure = 0, scope = 'scope-a';
const clone = value => JSON.parse(JSON.stringify(value));
const original = { revision: 'rev-a', scope, remote: true, selected: 'default',
  profiles: { default: { display_name: '默认小艺' }, p1: { display_name: '已有伙伴', nickname: '旧称呼' } },
  profileLimit: 11, templateDrafts: {} };
let server = clone(original);
global.wx = {
  getStorageSync: key => storage.get(key) || '', setStorageSync: (key, value) => storage.set(key, clone(value)), removeStorageSync: key => storage.delete(key),
  pageScrollTo() {}, enableAlertBeforeUnload() {}, disableAlertBeforeUnload() {},
  showModal() {},
  request(options) {
    const pathname = options.url.split('/v1')[1];
    requests.push({ path: pathname, method: options.method, data: clone(options.data || {}) });
    let data, code = 200;
    if (pathname === '/status') data = { wechatReady: false };
    else if (pathname === '/session') data = { session: 't'.repeat(43), expiresAt: Date.now() / 1000 + 3600 };
    else if (pathname.endsWith('/templates/prepare')) data = { profile: { display_name: '模板伙伴', personality: '耐心' },
      source: { id: 'a'.repeat(24), version: 2, digest: 'b'.repeat(64), requestId: 'c'.repeat(32), syncApplication: true }, hasVoice: true, hasReference: true };
    else if (pathname.endsWith('/templates')) data = { items: [{ id: 'a'.repeat(24), revision: 2, title: '模板伙伴' }] };
    else if (pathname.endsWith('/settings') && options.method === 'PUT') {
      if (saveFailure) { code = saveFailure; data = { error: code === 409 ? '设置已变化，草稿已保留' : '请重新登录' }; }
      else {
        for (const id of Object.keys(options.data.edits)) server.profiles[id] = Object.assign({}, server.profiles[id], options.data.edits[id]);
        server.selected = options.data.selected; server.revision = 'rev-saved'; server.templateDrafts = {};
        data = { state: clone(server), application: { name: '应用 A' }, notice: '资料更新已提交，微信显示可能有延迟。' };
      }
    } else if (pathname.endsWith('/settings')) data = { state: clone(Object.assign({}, server, { scope })), application: { name: '应用 A' } };
    else if (pathname === '/logout') data = { ok: true };
    else throw Error('Unexpected path: ' + pathname);
    options.success({ statusCode: code, data });
  }
};
global.Page = value => { definition = value; };
const account = require('../src/lib/account');
require('../src/pages/settings/index');
const e = (dataset, value) => ({ currentTarget: { dataset }, detail: { value } });

(async () => {
  const page = Object.assign({}, definition, { data: clone(definition.data) });
  page.setData = patch => Object.assign(page.data, patch); page.onLoad();
  page.loginField(e({ key: 'username' }, 'test-account')); page.loginField(e({ key: 'password' }, 'test-password'));
  await page.login();
  assert.equal(page.data.mode, 'account'); assert.equal(page.data.password, '');
  assert.equal(JSON.stringify([...storage.values()]).includes('test-password'), false);
  const writes = () => requests.filter(r => r.method === 'PUT');
  page.openPersona(); await page.useTemplate(e({ id: 'a'.repeat(24) }));
  assert.equal(page.draft.selected, 'p2'); assert.equal(page.data.syncApplication, true);
  assert.equal(writes().length, 0, 'Preparing template never saves');
  page.toggleSync(e({}, false)); page.edit(e({ key: 'nickname' }, '小岛'));
  page.goHome(); assert.equal(page.draft.profiles.p2.nickname, '小岛'); page.openPersona();
  assert.equal(writes().length, 0, 'Editing and navigation never auto-save');
  saveFailure = 409; await page.save();
  assert.equal(page.data.dirty, true); assert.equal(page.draft.profiles.p2.nickname, '小岛');
  const sent = writes().at(-1).data;
  assert.equal(sent.templateDrafts.p2.syncApplication, false);
  assert.equal(sent.edits.p1, undefined, 'Unchanged original persona is not submitted');
  assert.equal(sent.options, undefined, 'Client cannot replace unrelated options');
  saveFailure = 401; await page.save(); assert.equal(page.data.needsLogin, true);
  page.reLogin(); page.loginField(e({ key: 'password' }, 'test-password')); await page.login();
  assert.equal(page.data.mode, 'account'); assert.equal(page.data.needsLogin, false);
  assert.equal(page.draft.profiles.p2.nickname, '小岛', 'Same account reauthentication retains draft');
  saveFailure = 0; await page.save();
  assert.equal(page.data.dirty, false); assert.equal(page.data.savedNotice, true); assert.match(page.data.savedMessage, /延迟/);
  page.edit(e({ key: 'nickname' }, '未保存'));
  page.reLogin(); scope = 'scope-b'; server = clone(original); page.loginField(e({ key: 'password' }, 'other-password')); await page.login();
  assert.equal(page.draft.profiles.p2, undefined, 'A different account cannot inherit another account draft');
  assert.equal(page.data.dirty, false);
  await page.leaveAccount(); assert.equal(page.data.mode, 'entry'); assert.equal(account.session(), '');
  console.log(JSON.stringify({ ok: true, checks: ['real_login', 'password_not_stored', 'template_uses_empty_slot', 'no_auto_save', 'template_sync_opt_out', 'patch_only_changed_fields', 'conflict_keeps_draft', 'reauth_keeps_same_account_draft', 'account_isolation', 'explicit_save', 'logout'] }));
})().catch(error => { console.error(error); process.exitCode = 1; });
