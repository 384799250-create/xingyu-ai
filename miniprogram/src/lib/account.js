const gateway = require('./gateway');
const model = require('./persona');
const KEY = 'xingyu-mini-account-session-v1';
let memorySession = '';
let bindingTicket = '';
let pendingEnrollment = false;
const SKIP = 'xingyu-mini-skip-auto-v1';

function session() {
  if (!memorySession) {
    const saved = wx.getStorageSync(KEY);
    if (saved && typeof saved.token === 'string' && saved.expiresAt * 1000 > Date.now()) memorySession = saved.token;
  }
  return memorySession;
}
async function login(username, password, rememberWechat = true) {
  const data = { username: username.trim(), password };
  const bind = !!bindingTicket && rememberWechat;
  if (bind) data.ticket = bindingTicket;
  const result = await gateway.request(bind ? '/wechat/bind/account' : '/session', 'POST', data, '');
  remember(result);
}
function remember(result) {
  memorySession = result.session;
  // The password is never retained; only the revocable short-lived session.
  bindingTicket = '';
  pendingEnrollment = false;
  try {
    wx.setStorageSync(KEY, { token: result.session, expiresAt: result.expiresAt, wechatBound: !!result.wechatBound });
    wx.removeStorageSync(SKIP);
  } catch (_) { /* session remains valid in memory */ }
}
function status() { return gateway.request('/status', 'GET', undefined, ''); }
function autoAllowed() { return !wx.getStorageSync(SKIP); }
function bound() { return !!(wx.getStorageSync(KEY) || {}).wechatBound; }
function wechatCode() {
  return new Promise((resolve, reject) => wx.login({
    success: result => result.code ? resolve(result.code) : reject(Error('微信登录未完成，请重试。')),
    fail: () => reject(Error('微信登录暂时不可用，请稍后重试。'))
  }));
}
async function connectEntry(entry) {
  const code = await wechatCode();
  const result = await gateway.request('/entry/session', 'POST', { entry, code }, '');
  remember(result);
}
async function wechat() {
  const code = await wechatCode();
  const result = await gateway.request('/wechat/session', 'POST', { code }, '');
  if (result.needsBinding) { bindingTicket = result.ticket; pendingEnrollment = !!result.hasEnrollment; return false; }
  remember(result); return true;
}
async function connectLink(link) {
  const data = { link: link.trim() };
  if (bindingTicket) data.ticket = bindingTicket;
  const result = await gateway.request('/link/session', 'POST', data, '');
  remember(result);
}
async function unbind() {
  await gateway.request('/wechat/unbind', 'POST', {}, session());
  forget(); wx.setStorageSync(SKIP, true);
}
async function phoneRequest(path, data) {
  if (!bindingTicket) throw Error('请先点击微信连接，再授权手机号。');
  const result = await gateway.request('/phone/' + path, 'POST', Object.assign({ ticket: bindingTicket }, data), '');
  if (result.session) { remember(result); pendingEnrollment = false; }
  else if (result.pending) pendingEnrollment = true;
  return result;
}
function phoneResolve(code) { return phoneRequest('resolve', { code }); }
function phoneApply(proof) { return phoneRequest('apply', { proof, consent: true }); }
function phoneProgress() { return phoneRequest('progress', {}); }
function hasEnrollment() { return pendingEnrollment; }
function join() { return request('/join'); }
async function request(path, method, data) {
  const token = session();
  if (!token) { const e = Error('请先登录星屿账号。'); e.status = 401; throw e; }
  return gateway.request('/applications/current' + path, method || 'GET', data, token);
}
function forget() { memorySession = ''; wx.removeStorageSync(KEY); }
async function logout() {
  try { if (session()) await gateway.request('/logout', 'POST', {}, session()); }
  finally { forget(); bindingTicket = ''; pendingEnrollment = false; wx.setStorageSync(SKIP, true); }
}
async function load() {
  const loaded = await request('/settings');
  let templates = [], templateError = '';
  try { templates = (await request('/templates')).items; }
  catch (e) { if (e.status === 401) throw e; templateError = '模板库暂时未能读取，已有的人格仍可修改和保存。'; }
  return Object.assign({}, loaded, { templates, templateError });
}
function edits(before, draft) {
  const result = {};
  for (const id of Object.keys(draft.profiles)) {
    for (const field of model.fields) {
      const previous = (before.profiles[id] || {})[field.key] || '';
      const current = draft.profiles[id][field.key] || '';
      if (previous !== current) { result[id] = result[id] || {}; result[id][field.key] = current; }
    }
  }
  return result;
}
async function save(before, draft) {
  return request('/settings', 'PUT', { revision: before.revision, selected: draft.selected,
    edits: edits(before, draft), templateDrafts: draft.templateDrafts || {} });
}
async function prepare(template) {
  return request('/templates/prepare', 'POST', { id: template.id, version: template.revision });
}
function addTemplate(state, prepared) {
  const result = model.clone(state);
  const slot = ['p1', 'p2', 'p3', 'p4', 'p5'].find(id => !result.profiles[id] || !Object.keys(result.profiles[id]).some(k => !!result.profiles[id][k]));
  if (!slot) throw Error('5 个人格位置已满，请先在原设置页整理已有的人格。');
  result.profiles[slot] = model.clone(prepared.profile);
  result.selected = slot;
  result.templateDrafts = result.templateDrafts || {};
  result.templateDrafts[slot] = model.clone(prepared.source);
  return result;
}
module.exports = { session, login, logout, forget, load, save, prepare, addTemplate, edits,
  status, autoAllowed, bound, wechat, connectLink, unbind,
  phoneResolve, phoneApply, phoneProgress, hasEnrollment, join, connectEntry };
