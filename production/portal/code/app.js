'use strict';
const $ = id => document.getElementById(id);
let mode = 'login', csrf = '', currentUser = null;
let pollTimer = 0;
let qrFailed = false;
const views = ['loading', 'auth-view', 'space-view', 'fatal'];
function showView(id) { views.forEach(x => $(x).hidden = x !== id); }
function message(id, text) { $(id).textContent = text || ''; $(id).hidden = !text; }
async function api(path, data) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 35000);
  try {
    const options = {credentials: 'same-origin', signal: controller.signal, headers: {}};
    if (data !== undefined) {
      options.method = 'POST'; options.headers = {'Content-Type': 'application/json', 'X-Requested-With': 'xiaoyi-portal', 'X-CSRF-Token': csrf}; options.body = JSON.stringify(data);
    }
    const response = await fetch('/ai/api/' + path, options);
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '请求未完成，请稍后重试。');
    return result;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('连接超时，请检查网络后重试。');
    if (error instanceof TypeError) throw new Error('网络连接中断，请检查网络后重试。');
    throw error;
  } finally { clearTimeout(timer); }
}
async function busy(button, action) {
  const text = button.textContent;
  button.disabled = true; button.textContent = '请稍候…';
  try { await action(); } finally { button.disabled = false; button.textContent = text; }
}
function setMode(next) {
  mode = next;
  for (const name of ['login', 'register']) {
    const active = mode === name;
    $(name + '-tab').setAttribute('aria-selected', String(active));
    $(name + '-tab').tabIndex = active ? 0 : -1;
  }
  $('auth-panel').setAttribute('aria-labelledby', mode === 'register' ? 'register-tab' : 'login-tab');
  $('form-title').textContent = {login:'回到你的 AI 空间', register:'使用邮箱注册', recover:'重新设置密码'}[mode];
  $('form-help').textContent = {login:'使用邮箱登录；已有账号可继续使用原账号。', register:'验证邮箱并设置密码，下一步创建你的 AI 伙伴。', recover:'使用已验证邮箱接收验证码，或使用自己的 AI 设置链接。'}[mode];
  $('auth-submit').textContent = {login:'登录', register:'注册并继续', recover:'重置密码并登录'}[mode];
  $('password-label').textContent = mode === 'recover' ? '新密码' : '密码';
  $('password').autocomplete = mode === 'login' ? 'current-password' : 'new-password';
  $('password').value = ''; $('recover-link').value = '';
  $('password').type = 'password'; $('show-password').textContent = '显示';
  $('show-password').setAttribute('aria-label', '显示密码'); $('show-password').setAttribute('aria-pressed', 'false');
  $('username-label').textContent=mode==='register'?'邮箱':'邮箱或原账号';
  $('username').type=mode==='register'?'email':'text';
  $('username').autocomplete=mode==='register'?'email':'username';
  $('username').placeholder=mode==='register'?'name@example.com':'注册邮箱或原账号';
  $('username-help').textContent=mode==='register'?'邮箱直接作为登录账号，也用于找回密码。':'原字母、数字账号仍可登录，不区分大小写。';
  $('registration-code-field').hidden=mode!=='register';
  $('registration-code').required=mode==='register';
  $('registration-code').disabled=mode!=='register';
  updateRegistrationCodeButton();
  $('recover-field').hidden = mode !== 'recover'; $('recover-link').required = false;
  $('recover-email').required = mode === 'recover'; $('recover-code').required = mode === 'recover';
  $('legacy-recovery').open = false; updateRecoveryMethod();
  if(mode==='recover'&&$('username').value.includes('@'))$('recover-email').value=$('username').value.trim();
  $('registration-code').value='';message('registration-code-message');
  $('register-note').hidden = mode !== 'register';
  $('recover-toggle').textContent = mode === 'recover' ? '返回登录' : '忘记密码？';
  message('auth-error');
}

function updateRecoveryMethod() {
  const legacy = mode === 'recover' && $('legacy-recovery').open;
  $('recover-email').disabled = mode !== 'recover' || legacy; $('recover-code').disabled = mode !== 'recover' || legacy;
  $('recover-email').required = mode === 'recover' && !legacy;
  $('recover-code').required = mode === 'recover' && !legacy;
  $('send-recover-code').disabled = legacy;
  $('recover-link').disabled = !legacy; $('recover-link').required = legacy;
}
$('legacy-recovery').addEventListener('toggle', updateRecoveryMethod);
async function load(focus = false) {
  clearTimeout(pollTimer);
  try {
    const me = await api('me'); currentUser = me; csrf = me.csrf || '';
    $('account-bar').hidden = !me.authenticated;
    $('visitor-nav').hidden = !!me.authenticated;
    if (!me.authenticated) { showView('auth-view'); return; }
    $('account-name').textContent = me.username;
    fetch('/ai/admin/api/identity',{credentials:'same-origin'}).then(r=>r.ok?r.json():null).then(v=>{let a=document.getElementById('console-entry');if(!a){a=document.createElement('a');a.id='console-entry';a.href='/ai/admin/';a.textContent='总管理后台';document.getElementById('account-bar').append(a);}a.hidden=!v;}).catch(()=>{});
    $('security-email').value = me.email || '';
    $('email-status').textContent = me.email_verified ? `已验证：${me.email}` : (me.email ? `待验证：${me.email}` : '尚未绑定邮箱。');
    $('security-username').value = me.username;
    // 未开通账号统一在独立开通页完成资料、申请和扫码使用。
    if (me.state !== 'assigned' && location.pathname !== '/ai/register' && location.pathname !== '/ai/register/') {
      location.replace('/ai/register'); return;
    }
    const states = {
      unbound:['尚未开通','开始使用你的 AI','填写头像、昵称和微信手机号，申请你的独立 AI。'],
      waiting:['等待分配','申请已登记','目前没有空闲应用。管理员补充名额后，页面会继续显示开通和连接提示。'],
      assigned:['已开通',me.application?.name || '我的 AI','请在微信与自己的 AI 聊天；发送 我要设置，直接打开微信返回的设置页。'],
      awaiting_scan:['已开通，请扫码使用',me.application?.name || '你的 AI 已开通','扫码后在微信里聊天。需要调整设置时，向自己的 AI 发送 我要设置。'],
      unavailable:['暂不可用','请检查应用状态',me.message || '请联系管理员核对应用状态，稍后重新检查。']
    };
    const state = states[me.state] || states.unavailable;
    $('state-label').textContent = state[0]; $('state-title').textContent = state[1]; $('state-description').textContent = state[2];
    $('wait').hidden = me.state !== 'unbound';
    $('refresh').hidden = !['waiting','unavailable'].includes(me.state);
    const bound = ['assigned','awaiting_scan','unavailable'].includes(me.state);
    // AI settings are opened only from the private link returned in WeChat.
    $('personal-note').hidden = !bound; $('bound-details').hidden = me.state !== 'assigned';
    renderOnboarding(me);
    showView('space-view');
    if (focus) $('space-title').focus();
  } catch (error) {
    if (currentUser?.authenticated) { message('space-error', error.message); pollTimer = setTimeout(() => load(), 10000); }
    else { message('fatal-text', error.message); showView('fatal'); }
  }
}
function renderOnboarding(me) {
  for (const id of ['onboarding-area','onboarding-progress','retry-pool','onboarding-qr-area']) $(id).hidden = true;
  const job = me.onboarding;
  if (!job) {
    if (me.onboarding_error) message('space-error', me.onboarding_error);
    return;
  }
  const pending = ['queued','provisioning','waiting_pool','needs_inspection','verifying'].includes(job.state);
  if (job.state === 'needs_phone' && !['assigned','unavailable'].includes(me.state)) {
    $('state-title').textContent = '开通你的专属 AI';
    $('state-label').textContent = job.qr_ready && job.free > 0 && job.worker_ready !== false ? '可以申请' : '准备中';
    $('state-description').textContent = '填写自己的微信手机号，申请独立应用、人格和聊天空间。';
    $('onboarding-area').hidden = false;
    const ready = job.qr_ready && job.free > 0 && job.worker_ready !== false;
    $('onboarding-help').textContent = job.worker_ready === false ? '开通服务暂不可用，请稍后重新检查。'
      : !job.qr_ready ? '微信二维码正在准备，请稍后重新检查。'
      : job.free < 1 ? '目前空闲应用已分配完，可以先登记等待补充。'
      : '无需短信验证码。请核对绑定微信的中国大陆手机号，开通后用对应微信扫码，设置统一从微信中打开。';
    for (const id of ['onboarding-phone','onboarding-consent','onboarding-submit']) $(id).disabled = !ready;
    $('wait').hidden = ready || me.state === 'waiting'; $('refresh').hidden = ready;
  }
  if (pending) {
    $('wait').hidden = true; $('refresh').hidden = false;
    $('state-label').textContent = job.state === 'needs_inspection' ? '需要处理' : job.state === 'waiting_pool' ? '等待名额' : '开通中';
    $('state-title').textContent = job.state === 'needs_inspection' ? '开通暂停，申请已保留' : job.state === 'waiting_pool' ? '等待空闲应用' : '正在准备你的 AI';
    const notes = {
      queued:'申请已提交，正在等待分配。无需重复提交。',
      verifying:'正在核对开通申请，请稍候。',
      provisioning:'正在分配独立应用并连接微信，完成后会自动显示二维码。',
      waiting_pool:'申请期间空闲应用已分配完。你的申请已保留，补充应用后可继续。',
      needs_inspection:'开通未完成，已停止后续操作。请联系管理员核对申请；不要反复注册或重新开通。'
    };
    $('state-description').textContent = notes[job.state];
    if (['wecom_code_60102','wecom_code_60104'].includes(job.error)) $('state-description').textContent = '该手机号可能已有企业成员记录，请联系管理员核对，避免重复申请新的 AI。';
    $('onboarding-progress').hidden = false;
    $('onboarding-progress-text').textContent = {reserving:'正在选择空闲应用…',creating_member:'正在创建你的专属成员…',updating_profile:'正在设置 AI 伙伴资料…',activating:'正在启用你的 AI…',queued:'正在排队…',stopped:'申请已保留，等待管理员处理。'}[job.stage] || '可以随时刷新页面查看进度。';
    $('retry-pool').hidden = job.state !== 'waiting_pool';
  }
  if (job.state === 'awaiting_scan' && me.state === 'awaiting_scan') {
    $('onboarding-qr-area').hidden = false;
    $('qr-app-name').textContent = me.application.name;
    message('qr-error', !job.qr_ready ? '二维码已过期或尚未更新，请联系管理员更新。'
      : qrFailed ? '二维码未能加载，请重新检查状态；如果仍无法显示，请联系管理员更新。' : '');
    const qr = $('onboarding-qr');
    qr.hidden = !job.qr_ready || !qr.naturalWidth;
    if (job.qr_ready && !qr.getAttribute('src')) qr.src = '/ai/api/onboarding/qr';
    $('refresh').hidden = false;
  }
  if (['queued','provisioning','awaiting_scan','verifying'].includes(job.state) && !document.hidden) pollTimer = setTimeout(() => load(), 5000);
}
$('onboarding-form').addEventListener('submit', event => {
  event.preventDefault(); message('onboarding-error');
  busy($('onboarding-submit'), async () => {
    const phone = $('onboarding-phone').value.trim();
    if (!/^1[3-9][0-9]{9}$/.test(phone)) { message('onboarding-error', '请输入有效的中国大陆手机号。'); return; }
    try {
      await api('onboarding/start', {phone,consent:$('onboarding-consent').checked});
      $('onboarding-phone').value = ''; await load(true);
    } catch (error) { message('onboarding-error', error.message); }
  });
});
$('retry-pool').addEventListener('click', () => busy($('retry-pool'), async () => {
  try { await api('onboarding/retry', {}); await load(); } catch (error) { message('space-error', error.message); }
}));
$('onboarding-qr').addEventListener('error', () => { qrFailed = true; $('onboarding-qr').hidden = true; message('qr-error', '二维码未能加载，请重新检查状态；如果仍无法显示，请联系管理员更新。'); });
$('onboarding-qr').addEventListener('load', () => { qrFailed = false; $('onboarding-qr').hidden = false; message('qr-error'); });
document.addEventListener('visibilitychange', () => { if (document.hidden) clearTimeout(pollTimer); else if (currentUser?.authenticated) load(); });
$('login-tab').addEventListener('click', () => setMode('login'));
$('register-tab').addEventListener('click', () => setMode('register'));
document.querySelector('.tabs').addEventListener('keydown', event => {
  if (['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) {
    event.preventDefault(); setMode(event.key === 'Home' ? 'login' : event.key === 'End' ? 'register' : mode === 'login' ? 'register' : 'login'); $(mode+'-tab').focus();
  }
});
$('recover-toggle').addEventListener('click', () => setMode(mode === 'recover' ? 'login' : 'recover'));
$('show-password').addEventListener('click', () => {
  const show = $('password').type === 'password'; $('password').type = show ? 'text' : 'password';
  $('show-password').textContent = show ? '隐藏' : '显示'; $('show-password').setAttribute('aria-label', show ? '隐藏密码' : '显示密码'); $('show-password').setAttribute('aria-pressed', String(show));
});
$('auth-form').addEventListener('submit', event => {
  event.preventDefault(); message('auth-error');
  busy($('auth-submit'), async () => {
    try {
      const data = {username: $('username').value.trim(), password: $('password').value};
      const registering = mode === 'register';
      if (registering) {data.email=data.username;data.code=$('registration-code').value.trim();}
      if (mode === 'recover') {
        if ($('legacy-recovery').open) data.link = $('recover-link').value.trim();
        else { data.email = $('recover-email').value.trim(); data.code = $('recover-code').value.trim(); }
      }
      await api(mode, data); $('password').value = ''; $('recover-link').value = '';
      if (registering) location.assign('/ai/register'); else await load(true);
    } catch (error) { message('auth-error', error.message); }
  });
});
$('wait').addEventListener('click', () => busy($('wait'), async () => {
  try { await api('waiting', {}); await load(true); } catch (error) { message('space-error', error.message); }
}));
$('refresh').addEventListener('click', () => busy($('refresh'), () => { qrFailed = false; $('onboarding-qr').removeAttribute('src'); return load(); }));
$('logout').addEventListener('click', () => busy($('logout'), async () => {
  try { await api('logout', {}); csrf = ''; currentUser = null; qrFailed = false; clearTimeout(pollTimer); $('onboarding-form').reset(); $('onboarding-qr').removeAttribute('src'); $('password-form').reset(); setMode('login'); await load(); }
  catch (error) { message('space-error', error.message); }
}));
$('password-form').addEventListener('submit', event => {
  event.preventDefault(); message('password-message');
  busy(event.submitter, async () => {
    try { await api('password', {old_password:$('old-password').value, password:$('new-password').value}); $('password-form').reset(); await load(); message('password-message', '密码已更新，其他设备的网页登录已退出。'); }
    catch (error) { message('password-message', error.message); }
  });
});
let registrationSending=false,registrationCooldown=0,registrationTimer=0;
function updateRegistrationCodeButton(){
  const seconds=Math.max(0,Math.ceil((registrationCooldown-Date.now())/1000)),button=$('send-registration-code');
  button.disabled=mode!=='register'||registrationSending||seconds>0;
  button.textContent=registrationSending?'正在发送…':seconds?seconds+' 秒后重发':'发送验证码';
  clearTimeout(registrationTimer);
  if(seconds&&mode==='register')registrationTimer=setTimeout(updateRegistrationCodeButton,1000);
}
$('send-registration-code').addEventListener('click',async()=>{
  if(mode!=='register'||registrationSending||Date.now()<registrationCooldown)return;
  if(!$('username').reportValidity())return;
  registrationSending=true;updateRegistrationCodeButton();message('auth-error');message('registration-code-message');
  try{const result=await api('register/request',{email:$('username').value.trim()});registrationCooldown=Date.now()+60000;message('registration-code-message',result.message);}
  catch(error){message('auth-error',error.message);}
  finally{registrationSending=false;updateRegistrationCodeButton();}
});
$('username').addEventListener('input',()=>{$('registration-code').value='';message('registration-code-message');if(mode==='recover'&&$('username').value.includes('@'))$('recover-email').value=$('username').value.trim();});
$('send-recover-code').addEventListener('click', () => busy($('send-recover-code'), async () => {
  message('auth-error');
  try { await api('recover/request', {username:$('username').value.trim(), email:$('recover-email').value.trim()}); message('auth-error', '验证码已发送，请检查邮箱。'); }
  catch (error) { message('auth-error', error.message); }
}));
$('send-email-code').addEventListener('click', () => busy($('send-email-code'), async () => {
  message('email-message');
  try { const result = await api('email/send', {email:$('security-email').value.trim()}); message('email-message', result.message); }
  catch (error) { message('email-message', error.message); }
}));
$('email-form').addEventListener('submit', event => {
  event.preventDefault(); message('email-message');
  busy($('verify-email'), async () => {
    try { await api('email/verify', {email:$('security-email').value.trim(), code:$('security-code').value.trim()}); await load(); message('email-message', '邮箱已验证，可用于找回密码。'); }
    catch (error) { message('email-message', error.message); }
  });
});
$('retry').addEventListener('click', () => {showView('loading'); load();});
document.querySelectorAll('[data-auth-mode]').forEach(link => link.addEventListener('click', event => {
  event.preventDefault();
  setMode(link.dataset.authMode);
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  $('account').scrollIntoView({behavior: reduced ? 'auto' : 'smooth', block: 'start'});
  $('form-title').focus({preventScroll: true});
}));
setMode(new URLSearchParams(location.search).get('mode') === 'register' ? 'register' : 'login');
load();

;(()=>{const $=selector=>document.querySelector(selector);if(!$('#community-copy'))return;
// settings-official-group-20261105.1: local clipboard only, never saves settings.
$('#community-copy').onclick=async()=>{
 const button=$('#community-copy'),message=$('#community-feedback'),manual=$('#community-manual-copy');
 if(button.disabled)return;
 button.disabled=true;message.hidden=false;message.textContent='正在复制…';manual.hidden=true;
 try{
  if(!navigator.clipboard?.writeText)throw new Error('clipboard_unavailable');
  await navigator.clipboard.writeText('1097393630');
  message.textContent='群号已复制。打开 QQ，搜索群号加入。';
 }catch{
  manual.hidden=false;manual.focus({preventScroll:true});manual.select();
  message.textContent='此浏览器暂不支持一键复制，请长按下方群号复制，再打开 QQ 搜索加入。';
 }finally{button.disabled=false;}
};

})();
