const model = require('../../lib/persona');
const demo = require('../../lib/demo');
const account = require('../../lib/account');
const config = require('../../config');
const entryIntent = require('../../lib/entry-intent');
Page({
  data: { mode: 'entry', view: 'home', loading: false, saving: false, dirty: false, error: '', savedTitle: '', choices: [], templates: [], selectedIndex: 0, groups: [], openGroup: '', syncApplication: true, templatePending: false, savedNotice: false, canConnect: config.integrationReady, username: '', password: '', applicationName: '', templateNotice: '', templateError: '', savedMessage: '', needsLogin: false, reauthPending: false },
  async onLoad(options) {
    this.baseline = null; this.draft = null; this._sequence = 0;
    this.setData({ loginMethod: 'link', settingsLink: '', wechatReady: false, bindingPending: false, wechatBound: account.bound(), rememberWechat: true, manualConnection: false, phoneReady: false, phoneNeedsApp: false, phonePending: false, phoneMessage: '', otherMethods: false, joinQr: '', joinNotice: '' });
    if (!config.integrationReady) return;
    const entry = entryIntent.take(options && options.entry);
    if (entry) { await this.openEntry(entry); return; }
    if (account.session()) { await this.loadAccount(); if (this.data.mode === 'account') return; }
    await this.checkConnection();
  },
  onShow() { const entry = entryIntent.take(); if (entry) this.openEntry(entry); },
  async openEntry(entry) {
    if (this.data.loading || this.data.saving) {
      this.setData({ error: '当前操作完成后，请重新点击聊天里的设置入口。' }); return;
    }
    if (this.data.dirty) {
      const confirmed = await new Promise(resolve => wx.showModal({ title: '打开本次设置入口？',
        content: '当前还有未保存的修改，继续打开会丢弃这些修改。', confirmText: '打开设置',
        success: result => resolve(result.confirm), fail: () => resolve(false) }));
      if (!confirmed) return;
    }
    clearTimeout(this._phoneTimer);
    const sequence = ++this._sequence;
    this.setData({ loading: true, error: '' });
    try {
      await account.connectEntry(entry);
      if (sequence !== this._sequence) return;
      this.baseline = null; this.draft = null; this._phoneProof = '';
      this.setData({ loading: false, dirty: false, mode: 'entry', view: 'home', phonePending: false,
        phoneNeedsApp: false, joinQr: '', joinNotice: '', reauthPending: false });
      if (wx.disableAlertBeforeUnload) wx.disableAlertBeforeUnload();
      await this.loadAccount();
    } catch (e) { if (sequence === this._sequence) this.setData({ loading: false, error: e.message }); }
  },
  async checkConnection() {
    const sequence = this._sequence;
    try {
      const status = await account.status();
      if (sequence !== this._sequence || this.data.mode !== 'entry') return;
      this.setData({ wechatReady: !!status.wechatReady, phoneReady: !!status.phoneReady, manualConnection: !account.autoAllowed() });
      if (status.wechatReady && account.autoAllowed()) await this.connectWeChat();
    } catch (_) { /* The original settings-link route remains available. */ }
  },
  async connectWeChat() {
    if (this.data.loading || this.data.saving) return;
    const sequence = ++this._sequence;
    this.setData({ loading: true, error: '' });
    try {
      const connected = await account.wechat();
      if (sequence !== this._sequence) return;
      this.setData({ loading: false, bindingPending: !connected, phoneNeedsApp: false, phonePending: !connected && account.hasEnrollment() });
      if (connected) await this.loadAccount();
      else if (account.hasEnrollment() && this.data.phoneReady) { this.setData({ phonePending: true }); await this.checkPhoneProgress(); }
    } catch (e) { if (sequence === this._sequence) this.setData({ loading: false, error: e.message }); }
  },
  chooseLoginMethod(e) { if (!this.data.loading) this.setData({ loginMethod: e.currentTarget.dataset.method, error: '' }); },
  linkField(e) { this.setData({ settingsLink: e.detail.value }); },
  async connectLink() {
    if (this.data.loading || this.data.saving) return;
    if (!this.data.settingsLink.trim()) { this.setData({ error: '请先粘贴原应用发来的完整设置链接。' }); return; }
    const sequence = ++this._sequence;
    this.setData({ loading: true, error: '' });
    try {
      await account.connectLink(this.data.settingsLink);
      if (sequence !== this._sequence) return;
      this.setData({ loading: false, settingsLink: '', bindingPending: false });
      await this.loadAccount();
    } catch (e) { if (sequence === this._sequence) this.setData({ loading: false, error: e.message }); }
  },
  onUnload() { this._sequence++; clearTimeout(this._phoneTimer); },
  toggleOtherMethods() { if (!this.data.loading) this.setData({ otherMethods: !this.data.otherMethods }); },
  async retryPhoneAuthorization() {
    if (this.data.loading || this.data.saving) return;
    clearTimeout(this._phoneTimer); this._phoneProof = ''; this._phonePolls = 0;
    // Refresh the short-lived login ticket, retaining the server's enrollment.
    const sequence = ++this._sequence;
    this.setData({ loading: true, error: '' });
    try {
      const connected = await account.wechat();
      if (sequence !== this._sequence) return;
      this.setData({ loading: false, bindingPending: !connected, phonePending: false, phoneNeedsApp: false, phoneMessage: '' });
      if (connected) await this.loadAccount();
    } catch (e) { if (sequence === this._sequence) this.setData({ loading: false, error: e.message }); }
  },
  async connectPhone(e) {
    if (this.data.loading || this.data.saving) return;
    if (!e.detail.code) { this.setData({ error: '手机号授权未完成，可以重新授权或选择其他连接方式。' }); return; }
    await this.phoneOperation(() => account.phoneResolve(e.detail.code));
  },
  async applyPhone() {
    if (!this._phoneProof || this.data.loading) return;
    this.setData({ phonePending: true, phoneNeedsApp: false });
    await this.phoneOperation(() => account.phoneApply(this._phoneProof));
  },
  async checkPhoneProgress() {
    if (this.data.loading || this.data.saving || this.data.mode !== 'entry') return;
    await this.phoneOperation(() => account.phoneProgress());
  },
  async loadJoinQr() {
    if (this.data.loading || this.data.saving) return;
    const sequence = ++this._sequence;
    this.setData({ loading: true, error: '' });
    try {
      const result = await account.join();
      if (sequence === this._sequence) this.setData({ joinQr: result.joinQr || '', joinNotice: result.joinNotice || '' });
    } catch (e) { if (sequence === this._sequence) this.setData({ error: e.message }); }
    finally { if (sequence === this._sequence) this.setData({ loading: false }); }
  },
  async phoneOperation(operation) {
    if (this.data.loading || this.data.saving) return;
    clearTimeout(this._phoneTimer);
    const sequence = ++this._sequence;
    this.setData({ loading: true, error: '' });
    try {
      const result = await operation();
      if (sequence !== this._sequence) return;
      this.setData({ loading: false });
      if (result.session) {
        this._phoneProof = ''; this._phonePolls = 0;
        this.setData({ phonePending: false, phoneNeedsApp: false, joinQr: result.joinQr || '', joinNotice: result.joinNotice || '' });
        await this.loadAccount();
      } else if (result.needsApplication) {
        this._phoneProof = result.proof;
        this.setData({ phoneNeedsApp: true, phonePending: false, phoneMessage: '' });
      } else if (result.pending) {
        this._phoneProof = '';
        this.setData({ phonePending: true, phoneNeedsApp: false, phoneMessage: result.message || '申请已收到，正在准备你的应用。' });
        if (!['waiting_pool', 'needs_inspection'].includes(result.state) && (this._phonePolls || 0) < 12) {
          this._phonePolls = (this._phonePolls || 0) + 1;
          this._phoneTimer = setTimeout(() => { if (sequence === this._sequence) this.checkPhoneProgress(); }, 3000);
        }
      }
    } catch (e) { if (sequence === this._sequence) this.setData({ loading: false, error: e.message }); }
  },
  loginField(e) { if (['username', 'password'].includes(e.currentTarget.dataset.key)) this.setData({ [e.currentTarget.dataset.key]: e.detail.value }); },
  toggleRememberWechat(e) { if (!this.data.loading) this.setData({ rememberWechat: !!e.detail.value }); },
  async login() {
    if (this.data.loading || this.data.saving) return;
    if (!this.data.username.trim() || !this.data.password) { this.setData({ error: '请输入原星屿账号和密码。' }); return; }
    this.setData({ loading: true, error: '' });
    const sequence = ++this._sequence;
    try {
      await account.login(this.data.username, this.data.password, this.data.rememberWechat !== false);
      if (sequence !== this._sequence) return;
      this.setData({ password: '', loading: false });
      await this.loadAccount();
    } catch (e) { if (sequence === this._sequence) this.setData({ password: '', error: e.message, loading: false }); }
  },
  async loadAccount() {
    if (this.data.loading || this.data.saving) return;
    const sequence = ++this._sequence;
    this.setData({ loading: true, error: '' });
    try {
      const loaded = await account.load();
      if (sequence !== this._sequence) return;
      const keepDraft = this.data.reauthPending && this.baseline && this.baseline.scope === loaded.state.scope && model.changed(this.baseline, this.draft);
      if (!keepDraft) { this.baseline = model.clone(loaded.state); this.draft = model.clone(loaded.state); }
      this.setData({ mode: 'account', view: keepDraft ? 'persona' : 'home', templates: loaded.templates,
        applicationName: loaded.application.name, templateError: loaded.templateError, savedNotice: false,
        savedMessage: '', needsLogin: false, reauthPending: false, wechatBound: account.bound(), bindingPending: false,
        templateNotice: keepDraft ? '未保存的修改已保留，核对后再保存。' : '' });
      this.renderDraft();
    } catch (e) {
      if (sequence === this._sequence) { if (e.status === 401) account.forget(); this.setData({ error: e.message, needsLogin: e.status === 401 }); }
    } finally { if (sequence === this._sequence) this.setData({ loading: false }); }
  },
  reLogin() { this.setData({ mode: 'entry', reauthPending: true, manualConnection: true, error: '', password: '' }); },
  async leaveAccount() {
    if (this.data.loading || this.data.saving) return;
    const leave = async () => {
      try { await account.logout(); } catch (_) { /* local session is cleared even if network is unavailable */ }
      this._sequence++; clearTimeout(this._phoneTimer); this.baseline = null; this.draft = null; this._phoneProof = '';
      if (wx.disableAlertBeforeUnload) wx.disableAlertBeforeUnload();
      this.setData({ mode: 'entry', view: 'home', dirty: false, error: '', password: '', username: '', settingsLink: '', bindingPending: false, wechatBound: false, manualConnection: true, rememberWechat: true, reauthPending: false, needsLogin: false, phonePending: false, phoneNeedsApp: false, joinQr: '', joinNotice: '' });
    };
    if (this.data.dirty) wx.showModal({ title: '退出当前账号？', content: '还有未保存的修改，退出后会丢失。', confirmText: '退出账号', success: r => { if (r.confirm) leave(); } });
    else await leave();
  },
  disconnectWeChat() {
    if (this.data.loading || this.data.saving) return;
    wx.showModal({ title: '解除微信连接？', content: '之后需重新验证应用归属。应用和已保存的设置会保留。' + (this.data.dirty ? '未保存的修改将丢失。' : ''), confirmText: '解除连接', success: async result => {
      if (!result.confirm) return;
      this.setData({ loading: true, error: '' });
      try {
        await account.unbind(); this.baseline = null; this.draft = null; this._sequence++;
        if (wx.disableAlertBeforeUnload) wx.disableAlertBeforeUnload();
        this.setData({ mode: 'entry', view: 'home', dirty: false, wechatBound: false, bindingPending: false, manualConnection: true, rememberWechat: true, reauthPending: false, needsLogin: false, username: '', password: '', settingsLink: '' });
      } catch (e) { this.setData({ error: e.message }); }
      finally { this.setData({ loading: false }); }
    } });
  },
  async startDemo() {
    if (this.data.loading || this.data.saving) return;
    const sequence = ++this._sequence;
    this.setData({ loading: true, error: '' });
    try {
      const loaded = await demo.load();
      if (sequence !== this._sequence) return;
      model.validate(loaded.state);
      this.baseline = model.clone(loaded.state); this.draft = model.clone(loaded.state);
      this.setData({ mode: 'demo', templates: loaded.templates, savedNotice: false, templateNotice: '', templateError: '', reauthPending: false, needsLogin: false }); this.renderDraft();
    } catch (e) { this.setData({ error: '本地示例读取失败，请重试。' }); }
    finally { this.setData({ loading: false }); }
  },
  renderDraft() {
    const choices = model.profileList(this.draft); const profile = this.draft.profiles[this.draft.selected];
    this.setData({ choices, selectedIndex: choices.findIndex(p => p.id === this.draft.selected),
      savedTitle: this.baseline.profiles[this.baseline.selected].display_name,
      dirty: model.changed(this.baseline, this.draft),
      groups: ['basic', 'advanced'].map(id => ({ id, title: id === 'basic' ? '修改名字和性格' : '完整人格设定', fields: model.fields.filter(f => f.group === id).map(f => Object.assign({}, f, { value: profile[f.key] || '' })) })),
      templatePending: this.data.mode === 'account' ? !!(this.draft.templateDrafts || {})[this.draft.selected] : !!this.draft.templateDraft && this.draft.templateDraft.persona === this.draft.selected,
      syncApplication: this.data.mode === 'account' ? !(this.draft.templateDrafts || {})[this.draft.selected] || this.draft.templateDrafts[this.draft.selected].syncApplication : !this.draft.templateDraft || this.draft.templateDraft.syncApplication
    });
    this.updateLeaveAlert();
  },
  updateLeaveAlert() {
    if (this.data.dirty && wx.enableAlertBeforeUnload) wx.enableAlertBeforeUnload({ message: '有未保存的修改，离开后将丢失。' });
    else if (wx.disableAlertBeforeUnload) wx.disableAlertBeforeUnload();
  },
  openPersona() { this.setData({ view: 'persona', error: '' }); wx.pageScrollTo({ scrollTop: 0, duration: 0 }); },
  goHome() { if (this.data.saving || this.data.loading) return; this.setData({ view: 'home', error: '' }); wx.pageScrollTo({ scrollTop: 0, duration: 0 }); },
  toggleGroup(e) { const id = e.currentTarget.dataset.id; this.setData({ openGroup: this.data.openGroup === id ? '' : id }); },
  selectPersona(e) {
    if (this.data.saving || this.data.loading) return;
    const choice = this.data.choices[Number(e.detail.value)]; if (!choice) return;
    this.draft.selected = choice.id; this.setData({ savedNotice: false }); this.renderDraft();
  },
  async useTemplate(e) {
    if (this.data.saving || this.data.loading) return;
    const template = this.data.templates.find(t => t.id === e.currentTarget.dataset.id); if (!template) return;
    try {
      if (this.data.mode === 'account') {
        this.setData({ loading: true, error: '' });
        const sequence = this._sequence;
        const prepared = await account.prepare(template);
        if (sequence !== this._sequence) return;
        this.draft = account.addTemplate(this.draft, prepared);
        this.setData({ templateNotice: '已新建「' + prepared.profile.display_name + '」草稿' + (prepared.hasVoice ? '，语音已跟随模板' : '') + (prepared.hasReference ? '，参考图已带入' : '') + '。点击“保存设置”后生效。' + (prepared.notice || '') });
      } else this.draft = model.addTemplate(this.draft, template, () => 'local-' + Date.now() + '-' + (++this._sequence));
      this.setData({ error: '', savedNotice: false, openGroup: 'basic' }); this.renderDraft();
    } catch (e) { this.setData({ error: e.message, needsLogin: e.status === 401 }); }
    finally { this.setData({ loading: false }); }
  },
  edit(e) {
    if (this.data.saving || this.data.loading) return;
    try { this.draft = model.editField(this.draft, e.currentTarget.dataset.key, e.detail.value); this.setData({ error: '', savedNotice: false }); this.renderDraft(); }
    catch (e) { this.setData({ error: e.message }); }
  },
  toggleSync(e) {
    if (this.data.saving || this.data.loading) return;
    if (this.data.mode === 'account') {
      const source = (this.draft.templateDrafts || {})[this.draft.selected];
      if (!source) return;
      source.syncApplication = e.detail.value;
    } else { if (!this.draft.templateDraft) return; this.draft.templateDraft.syncApplication = e.detail.value; }
    this.setData({ savedNotice: false }); this.renderDraft();
  },
  async save() {
    if (this.data.saving || this.data.loading || !this.data.dirty || this.data.needsLogin) return;
    try { model.validate(this.draft); } catch (e) { this.setData({ error: e.message, openGroup: 'basic' }); return; }
    this.setData({ saving: true, error: '' });
    try {
      const remoteResult = this.data.mode === 'account' ? await account.save(this.baseline, model.clone(this.draft)) : null;
      const saved = remoteResult ? remoteResult.state : await demo.save(model.clone(this.draft), this.baseline.revision);
      this.baseline = model.clone(saved); this.draft = model.clone(saved);
      this.setData({ savedNotice: true, templateNotice: '', savedMessage: remoteResult ? remoteResult.notice : '' }); this.renderDraft();
    } catch (e) { this.setData({ error: e.message || '保存失败，修改仍保留，请重试。', needsLogin: e.status === 401 }); }
    finally { this.setData({ saving: false }); }
  },
  resetDemo() {
    if (this.data.saving || this.data.loading) return;
    const remote = this.data.mode === 'account';
    wx.showModal({ title: remote ? '读取最新设置' : '重新读取示例', content: this.data.dirty ? '放弃当前未保存的修改，读取上次已保存的内容？' : remote ? '读取网页或其他设备最后保存的设置？' : '读取本机上次手动保存的示例？', confirmText: '重新读取', success: result => { if (result.confirm) remote ? this.loadAccount() : this.startDemo(); } });
  },
  showHelp() { wx.showModal({ title: '如何开始聊天', content: this.data.mode === 'demo' ? '当前是本地示例，保存只保存在这台设备，不会改变微信聊天人格。' : '向自己的 AI 发送 /设置。小程序直达入口开放后，点开即可连接对应设置；目前仍可使用原设置链接或星屿账号连接。选好人格后点击“保存设置”，再回微信聊天。更多设置可发送 /网页设置。', showCancel: false }); }
});
