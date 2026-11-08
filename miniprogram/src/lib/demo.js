const { clone, validate } = require('./persona');
const KEY = 'xingyu-mini-demo-v1';
const initial = {
  revision: 'demo-1', profileLimit: 6, selected: 'default',
  profiles: { default: { display_name: '示例伙伴', name: '小屿', nickname: '你', personality: '温和、耐心，愿意听你分享日常。', style: '自然简短，不堆砌说教。' } }
};
const templates = [{ id: 'sample', revision: 1, title: '温柔伙伴 · 本地示例', description: '用于预览选择与编辑流程。正式模板将在后台接入后读取。', profile: { display_name: '温柔伙伴', name: '小屿', nickname: '你', personality: '温和、耐心，尊重对方的感受。', style: '用自然的话回应，适当提问，慢慢了解对方。' } }];
function load() {
  const stored = wx.getStorageSync(KEY);
  return Promise.resolve({ state: clone(stored || initial), templates: clone(templates) });
}
function save(draft, revision) {
  validate(draft);
  const current = wx.getStorageSync(KEY) || initial;
  if (current.revision !== revision) return Promise.reject(Error('本地示例已更新。草稿仍保留，请返回后重新读取。'));
  const saved = clone(draft); saved.revision = 'demo-' + (Number(current.revision.split('-')[1]) + 1);
  wx.setStorageSync(KEY, saved);
  return Promise.resolve(saved);
}
module.exports = { load, save };
