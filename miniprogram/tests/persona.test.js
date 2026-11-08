'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
// Exercise the offline preview with integration explicitly disabled, regardless
// of whether the deployed application's public config enables real accounts.
const configPath = require.resolve('../src/config');
require.cache[configPath] = { id: configPath, filename: configPath, loaded: true,
  exports: Object.assign({}, require('../src/config'), { integrationReady: false }) };
const model = require('../src/lib/persona');
const demo = require('../src/lib/demo');
const storage = new Map(); let networkCalls = 0; let pageDefinition;
global.wx = {
  getStorageSync: key => storage.has(key) ? model.clone(storage.get(key)) : '',
  setStorageSync: (key, value) => storage.set(key, model.clone(value)),
  pageScrollTo() {}, enableAlertBeforeUnload() {}, disableAlertBeforeUnload() {},
  request() { networkCalls++; throw Error('Demo must not use network'); },
  showModal() {}
};
global.Page = definition => { pageDefinition = definition; };
require('../src/pages/settings/index');
const event = (data, value) => ({ currentTarget: { dataset: data }, detail: { value } });
function page() {
  const instance = Object.assign({}, pageDefinition, { data: model.clone(pageDefinition.data) });
  instance.setData = patch => { Object.assign(instance.data, patch); };
  instance.onLoad(); return instance;
}
async function run() {
  const loaded = await demo.load(); const original = model.clone(loaded.state);
  const draft = model.addTemplate(original, loaded.templates[0], () => 'p1');
  assert.equal(draft.selected, 'p1'); assert.equal(draft.templateDraft.syncApplication, true);
  draft.profiles.p1.personality = '修改示例性格';
  assert.notEqual(draft.profiles.p1.personality, loaded.templates[0].profile.personality);
  assert.deepEqual(original, loaded.state);
  assert.throws(() => model.addTemplate({ ...original, profileLimit: 1 }, loaded.templates[0], () => 'p2'), /数量已满/);
  assert.throws(() => model.addTemplate(original, loaded.templates[0], () => 'default'), /创建人格失败/);
  assert.throws(() => model.editField(draft, '__proto__', 'value'), /无效/);
  assert.throws(() => model.editField(draft, 'name', 'a'.repeat(41)), /过长/);
  const invalid = model.editField(draft, 'display_name', ' '); assert.throws(() => model.validate(invalid), /人格名称/);
  const a = page(); await a.startDemo(); a.openPersona();
  a.useTemplate(event({ id: 'sample' })); a.edit(event({ key: 'nickname' }, '小岛'));
  assert.equal(a.data.dirty, true); assert.equal(storage.size, 0, 'No automatic save after template or editing');
  a.toggleSync(event({}, false)); assert.equal(a.draft.templateDraft.syncApplication, false);
  a.goHome(); assert.equal(a.draft.profiles[a.draft.selected].nickname, '小岛', 'Navigation retains draft');
  await a.save(); assert.equal(storage.size, 1); assert.equal(a.data.dirty, false); assert.equal(a.data.savedNotice, true);
  const b = page(); await b.startDemo(); assert.equal(b.draft.profiles[b.draft.selected].nickname, '小岛');
  a.edit(event({ key: 'nickname' }, '新称呼')); await a.save();
  b.edit(event({ key: 'nickname' }, '旧版本编辑')); await b.save();
  assert.match(b.data.error, /已更新/); assert.equal(b.data.dirty, true); assert.equal(b.draft.profiles[b.draft.selected].nickname, '旧版本编辑');
  assert.equal(b.data.saving, false);
  const gateway = require('../src/lib/gateway');
  await assert.rejects(gateway.request('/session', 'POST', {}, ''), /尚未配置/);
  assert.equal(networkCalls, 0);
  const config = JSON.parse(fs.readFileSync(path.join(__dirname, '../project.config.json'), 'utf8'));
  assert.equal(config.appid, 'wx0000000000000000');
  const app = JSON.parse(fs.readFileSync(path.join(__dirname, '../src/app.json'), 'utf8'));
  for (const route of app.pages) for (const ext of ['js', 'json', 'wxml', 'wxss']) assert.ok(fs.existsSync(path.join(__dirname, '../src', route + '.' + ext)));
  console.log(JSON.stringify({ ok: true, scope: ['independent_template_copy', 'persona_limit', 'field_validation', 'manual_save_only', 'navigation_keeps_draft', 'sync_opt_out', 'save_persists_locally', 'revision_conflict_retains_draft', 'network_disabled_before_integration', 'project_routes'], networkCalls }));
}
run().catch(error => { console.error(error); process.exitCode = 1; });
