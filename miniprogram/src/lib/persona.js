const fields = [
  { key: 'display_name', label: '人格名称', limit: 40, group: 'basic' },
  { key: 'name', label: 'TA 的名字', limit: 40, group: 'basic' },
  { key: 'nickname', label: 'TA 怎么称呼你', limit: 40, group: 'basic' },
  { key: 'personality', label: '性格', limit: 1600, group: 'basic', multiline: true },
  { key: 'relationship', label: '你们的关系', limit: 80, group: 'advanced' },
  { key: 'background', label: '背景经历', limit: 1200, group: 'advanced', multiline: true },
  { key: 'mood', label: '日常情绪', limit: 120, group: 'advanced' },
  { key: 'style', label: '说话方式', limit: 500, group: 'advanced', multiline: true },
  { key: 'catchphrases', label: '口头禅', limit: 300, group: 'advanced', multiline: true },
  { key: 'avoid', label: '避免的表达', limit: 300, group: 'advanced', multiline: true },
  { key: 'do', label: '希望 TA 做的事', limit: 800, group: 'advanced', multiline: true },
  { key: 'dont', label: '不希望 TA 做的事', limit: 800, group: 'advanced', multiline: true },
  { key: 'anchor', label: '相处约定', limit: 500, group: 'advanced', multiline: true },
  { key: 'worldbook', label: '世界设定', limit: 6000, group: 'advanced', multiline: true },
  { key: 'prompt', label: '补充设定', limit: 60000, group: 'advanced', multiline: true }
];
const clone = value => JSON.parse(JSON.stringify(value));
function profileList(state) {
  return Object.keys(state.profiles).map(id => ({ id, title: state.profiles[id].display_name || '未命名人格' }));
}
function addTemplate(state, template, makeId) {
  const result = clone(state);
  if (Object.keys(result.profiles).length >= result.profileLimit) throw Error('人格数量已满，请先在原设置页整理已有的人格。');
  const id = makeId();
  if (!id || result.profiles[id]) throw Error('创建人格失败，请重试。');
  // A template creates an independent editable copy. Metadata is retained for later
  // server validation of voice/reference/application identity, never applied alone.
  result.profiles[id] = clone(template.profile);
  result.selected = id;
  result.templateDraft = { persona: id, templateId: template.id, revision: template.revision, syncApplication: true };
  return result;
}
function editField(state, key, value) {
  const field = fields.find(f => f.key === key);
  if (!field || typeof value !== 'string' || value.length > field.limit) throw Error('内容过长或字段无效，请检查后重试。');
  const result = clone(state);
  if (!result.profiles[result.selected]) throw Error('请先选择人格。');
  result.profiles[result.selected][key] = value;
  return result;
}
function validate(state) {
  const profile = state.profiles[state.selected];
  if (!profile) throw Error('请选择一个人格。');
  if (!(profile.display_name || '').trim()) throw Error('请填写人格名称。');
  for (const f of fields) if (typeof profile[f.key] !== 'undefined' && (typeof profile[f.key] !== 'string' || profile[f.key].length > f.limit)) throw Error('请检查“' + f.label + '”的长度。');
}
function changed(before, draft) { return JSON.stringify(before) !== JSON.stringify(draft); }
module.exports = { fields, clone, profileList, addTemplate, editField, validate, changed };
