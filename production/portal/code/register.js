'use strict';
const $=id=>document.getElementById(id); let csrf='',avatarDataUrl='',cropState=null,dragState=null,pollTimer=null,currentJob=null,statusRequest=null,statusActionBusy=false;
function message(text){$('profile-error').textContent=text||'';$('profile-error').hidden=!text}
async function api(path,data){const r=await fetch('/ai/api/'+path,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Requested-With':'xiaoyi-portal','X-CSRF-Token':csrf},body:JSON.stringify(data)});const v=await r.json();if(!r.ok)throw new Error(v.error||'请求未完成，请稍后重试。');return v}
function readImage(file){return new Promise((resolve,reject)=>{if(!file||!/^image\/(jpeg|png)$/.test(file.type)){reject(new Error('请选择 JPG 或 PNG 图片。'));return}const reader=new FileReader();reader.onload=()=>resolve(String(reader.result));reader.onerror=()=>reject(new Error('图片文件无法读取，请另存为标准 JPG 或 PNG 后重试。'));reader.readAsDataURL(file)})}
function cropGeometry(){const c=cropState,scale=Math.max(720/c.width,720/c.height)*c.zoom;return {scale,width:c.width*scale,height:c.height*scale,maxX:Math.max(0,c.width*scale-720),maxY:Math.max(0,c.height*scale-720)}}
function drawCrop(){if(!cropState)return;const c=cropState,g=cropGeometry(),canvas=$('avatar-crop-canvas'),ctx=canvas.getContext('2d');if(!ctx)return;ctx.clearRect(0,0,720,720);ctx.fillStyle='#dfe9e3';ctx.fillRect(0,0,720,720);ctx.drawImage(c.image,-g.maxX*c.x,-g.maxY*c.y,g.width,g.height);ctx.save();ctx.strokeStyle='rgba(255,255,255,.95)';ctx.lineWidth=3;ctx.setLineDash([10,9]);ctx.beginPath();ctx.arc(360,360,355,0,Math.PI*2);ctx.stroke();ctx.setLineDash([]);ctx.strokeStyle='rgba(255,255,255,.45)';ctx.lineWidth=1;for(const n of [240,480]){ctx.beginPath();ctx.moveTo(n,0);ctx.lineTo(n,720);ctx.moveTo(0,n);ctx.lineTo(720,n);ctx.stroke()}ctx.restore();$('avatar-crop-zoom-value').textContent=c.zoom.toFixed(2)+' 倍'}
function clearCrop(){if(cropState?.image)cropState.image.src='';cropState=null;dragState=null;$('avatar-crop-editor').hidden=true;$('avatar-crop-zoom').value='1';$('avatar-crop-zoom-value').textContent='1.00 倍'}
async function openCrop(file){const source=await readImage(file),image=new Image();image.src=source;await new Promise((resolve,reject)=>{image.onload=resolve;image.onerror=()=>reject(new Error('头像无法读取，请另存为标准 JPG 或 PNG 后重试。'))});const width=image.naturalWidth,height=image.naturalHeight;if(!width||!height||width*height>64*1000*1000)throw new Error('图片尺寸过大，请先缩小后重试。');cropState={file,image,width,height,zoom:1,x:.5,y:.5};$('avatar-crop-editor').hidden=false;drawCrop()}
async function confirmCrop(){if(!cropState)return;const c=cropState,g=cropGeometry(),side=Math.max(1,Math.min(1024,Math.round(720/g.scale))),canvas=document.createElement('canvas');canvas.width=canvas.height=side;try{const ctx=canvas.getContext('2d',{alpha:false});if(!ctx)throw new Error('当前浏览器不支持裁切，请换浏览器重试。');ctx.fillStyle='#fff';ctx.fillRect(0,0,side,side);ctx.drawImage(c.image,g.maxX*c.x/g.scale,g.maxY*c.y/g.scale,side,side,0,0,side,side);for(const quality of [.9,.78,.66,.54,.42]){const out=canvas.toDataURL('image/jpeg',quality);if(out.length<=690000){avatarDataUrl=out;$('avatar-upload-trigger').innerHTML='<img src="'+out+'" alt="头像预览">';$('avatar-upload-trigger').setAttribute('aria-label','更换伙伴头像');clearCrop();message('头像裁切已确认，可以继续填写资料。');return}}throw new Error('头像压缩后仍然过大，请缩小裁切范围或换一张图片。')}finally{canvas.width=canvas.height=1}}

function clamp(value,min,max){return Math.min(max,Math.max(min,value))}
function resetCrop(){if(!cropState)return;cropState.zoom=1;cropState.x=.5;cropState.y=.5;$('avatar-crop-zoom').value='1';drawCrop()}
function cropPointerDown(event){if(!cropState)return;const canvas=$('avatar-crop-canvas');canvas.setPointerCapture?.(event.pointerId);dragState={pointerId:event.pointerId,startX:event.clientX,startY:event.clientY,x:cropState.x,y:cropState.y}}
function cropPointerMove(event){if(!dragState||dragState.pointerId!==event.pointerId||!cropState)return;const rect=$('avatar-crop-canvas').getBoundingClientRect(),dx=(event.clientX-dragState.startX)/Math.max(1,rect.width),dy=(event.clientY-dragState.startY)/Math.max(1,rect.height);cropState.x=clamp(dragState.x-dx,0,1);cropState.y=clamp(dragState.y-dy,0,1);drawCrop()}
function cropPointerUp(event){if(dragState?.pointerId===event.pointerId)dragState=null}

async function me(){const r=await fetch('/ai/api/me',{credentials:'same-origin',cache:'no-store'});const data=await r.json();if(!r.ok)throw new Error(data.error||'暂时无法读取开通进度，请稍后重试。');return data}
function showForm(){resetOnboardingQR(); updateWaitEstimate(null);updateWaitingOverlay('','','',false);$('profile-form').hidden=false; $('status-area').hidden=true; }
function showStatus(title,label,description,isWaiting=false){resetOnboardingQR();if(!isWaiting)updateWaitEstimate(null);updateWaitingOverlay(title,label,description,isWaiting);$('waiting-animation').hidden=!isWaiting;$('profile-form').hidden=true;$('status-area').hidden=false;$('status-title').textContent=title;$('status-label').textContent=label;$('status-description').textContent=description;$('qr-area').hidden=true;$('status-home').hidden=true}
function pending(job){return ['queued','provisioning','waiting_pool','verifying'].includes(job?.state)}
// onboarding-capacity-recovery-v1: polling reads status; only an explicit action retries.
function updateCapacityActions(){
  const waiting=currentJob?.state==='waiting_pool';
  for(const id of ['status-refresh','waiting-overlay-refresh']){
    const button=$(id);button.disabled=statusActionBusy;
    button.textContent=statusActionBusy?'正在检查…':waiting?'检查名额并继续':'重新检查状态';
  }
}
async function loadStatus(){
  if(statusRequest)return statusRequest;
  clearTimeout(pollTimer);
  statusRequest=(async()=>{
    try{
      const data=await me();
      if(!data.authenticated){location.assign('/ai/account?mode=login#account');return}
      csrf=data.csrf||csrf;currentJob=data.onboarding||null;
      updateCapacityActions();
      if(data.state==='assigned'){
        showStatus('你的 AI 已开通','已开通','请在微信中与自己的 AI 聊天；发送 我要设置，直接打开微信返回的设置页。');
        $('status-home').hidden=false;return;
      }
      const job=currentJob;
      if(!job||job.state==='needs_phone'){showForm();return}
      if(job.state==='awaiting_scan'&&data.application){
        showStatus('你的 AI 已开通','已开通，请扫码使用','请用申请时填写手机号对应的微信扫码，关注后打开下方显示的 AI。');
        $('qr-area').hidden=false;$('qr-app-name').textContent=data.application.name||'你的 AI';
        loadOnboardingQR();return;
      }
      updateWaitEstimate(job);
      const notes={
        queued:'申请已提交，正在等待分配。无需重复提交。',
        verifying:'正在核对开通申请，请稍候。',
        provisioning:'正在分配独立应用并连接微信，完成后会显示二维码。',
        waiting_pool:'你的申请已保存，无需重新注册或填写资料。补充名额后会自动继续；也可点击“检查名额并继续”。开通后回到本页扫码使用。',
        needs_inspection:'开通未完成，申请已保留。请联系管理员核对，不要重复注册。'
      };
      const title=job.state==='needs_inspection'?'开通暂停，申请已保留':job.state==='waiting_pool'?'等待开通名额':'正在准备你的 AI';
      const label=job.state==='needs_inspection'?'需要处理':job.state==='waiting_pool'?'申请已保存':'开通中';
      showStatus(title,label,notes[job.state]||'可以稍后刷新查看进度。',pending(job));
      if(pending(job))pollTimer=setTimeout(loadStatus,5000);
    }catch(error){showStatus('暂时无法读取状态','请稍后重试',error.message)}
  })();
  try{return await statusRequest}finally{statusRequest=null}
}
async function checkCapacityAndContinue(){
  if(statusActionBusy)return;
  statusActionBusy=true;clearTimeout(pollTimer);updateCapacityActions();
  $('capacity-error').hidden=true;$('waiting-capacity-error').hidden=true;
  try{
    if(statusRequest)await statusRequest;
    clearTimeout(pollTimer);
    if(currentJob?.state==='waiting_pool')await api('onboarding/retry',{});
    await loadStatus();
  }catch(error){
    for(const id of ['capacity-error','waiting-capacity-error']){
      $(id).textContent=error.message+' 申请仍保留，请稍后再试。';$(id).hidden=false;
    }
    if(pending(currentJob))pollTimer=setTimeout(loadStatus,5000);
  }finally{statusActionBusy=false;updateCapacityActions()}
}
async function init(){await loadStatus()}
$('avatar-upload-trigger').addEventListener('click',()=>$('partner-avatar').click());
$('partner-avatar').addEventListener('change',async event=>{const file=event.target.files?.[0];if(!file)return;message('');try{await openCrop(file)}catch(error){message(error.message)}finally{event.target.value=''}});
$('avatar-crop-zoom').addEventListener('input',event=>{if(cropState){cropState.zoom=Number(event.target.value);drawCrop()}});
$('avatar-crop-cancel').addEventListener('click',()=>clearCrop());
$('avatar-crop-reset').addEventListener('click',resetCrop);
$('avatar-crop-confirm').addEventListener('click',()=>confirmCrop().catch(error=>message(error.message)));
$('avatar-crop-canvas').addEventListener('pointerdown',cropPointerDown);
$('avatar-crop-canvas').addEventListener('pointermove',cropPointerMove);
$('avatar-crop-canvas').addEventListener('pointerup',cropPointerUp);
$('avatar-crop-canvas').addEventListener('pointercancel',cropPointerUp);
$('profile-form').addEventListener('submit',async e=>{e.preventDefault();message('');const b=$('submit-profile');b.disabled=true;b.textContent='正在提交…';try{if(cropState)throw new Error('请先点击“确认裁切”，再继续创建。');const phone=$('register-phone').value.trim();if(!/^1[3-9][0-9]{9}$/.test(phone))throw new Error('请输入有效的中国大陆手机号。');await api('onboarding/start',{display_name:$('partner-name').value.trim(),avatar:avatarDataUrl,phone,consent:$('register-consent').checked});await loadStatus()}catch(err){message(err.message)}finally{b.disabled=false;b.textContent='创建并继续'}})
$('status-refresh').addEventListener('click',checkCapacityAndContinue);
$('register-logout').addEventListener('click',async()=>{const b=$('register-logout');b.disabled=true;try{await api('logout',{});location.assign('/ai/account?mode=login#account')}catch(error){message(error.message);b.disabled=false}});
// User-observed normal allocation time is a reference, not a completion promise.
const REFERENCE_WAIT_SECONDS=10;
function formatWaitDuration(seconds){
  if(seconds<60)return Math.max(5,Math.ceil(seconds/5)*5)+' 秒';
  return Math.ceil(seconds/60)+' 分钟';
}
function updateWaitEstimate(job){
  const waiting=['queued','provisioning','waiting_pool','verifying'].includes(job?.state);
  $('waiting-estimate').hidden=!waiting;
  $('status-wait-estimate').hidden=!waiting;
  if(!waiting)return;
  let value='约 '+REFERENCE_WAIT_SECONDS+' 秒',help='常规开通的参考时长，实际以开通结果为准。';
  if(job.state==='waiting_pool'){
    value='等待补充名额';help='暂无法预计。名额补充后会自动继续，可稍后回来查看进度。';
  }else if(job.wait_estimate){
    const eta=job.wait_estimate;
    if(Number.isFinite(eta.server_time)&&Number.isFinite(eta.minimum_ready_at)&&Number.isFinite(eta.maximum_ready_at)&&eta.minimum_ready_at>0&&eta.maximum_ready_at>=eta.minimum_ready_at){
      const low=eta.minimum_ready_at-eta.server_time,high=eta.maximum_ready_at-eta.server_time;
      if(high<=0){value='比预计稍久';help='仍在处理中，请稍候，无需重复提交。'}
      else if(low<=0){value='即将完成';help='预计还需 '+formatWaitDuration(high)+' 左右，实际以开通结果为准。'}
      else{
        const from=formatWaitDuration(low),to=formatWaitDuration(high);
        value=from===to?'约 '+to:'约 '+from+' – '+to;
        help='剩余时间会随开通进度更新，实际以开通结果为准。';
      }
      if(eta.source==='demo')help+='（模拟估算，仅供预览）';
    }
  }
  for(const [id,text] of [['waiting-estimate-value',value],['waiting-estimate-help',help],['status-wait-estimate','预计等待时间：'+value+'。'+help]]){
    if($(id).textContent!==text)$(id).textContent=text;
  }
}
// Allocation guide shares the existing waiting dialog; polling stays independent.
const waitingOverlay=$('waiting-overlay');
let waitingOverlayKey='',waitingOverlayDismissed=false,guideIndex=0,guideOrigin=null,guideReady=false;
function showGuideStep(index,focus=false){
  guideIndex=clamp(index,0,4);
  document.querySelectorAll('.guide-step').forEach((panel,i)=>panel.hidden=i!==guideIndex);
  document.querySelectorAll('[data-guide-step]').forEach((button,i)=>button.setAttribute('aria-current',i===guideIndex?'step':'false'));
  $('guide-prev').disabled=guideIndex===0;
  $('guide-next').textContent=guideIndex===4?'我知道了':'下一步';
  const content=waitingOverlay.querySelector('.guide-content');content.scrollTop=0;
  if(focus)$('guide-title-'+(guideIndex+1)).focus({preventScroll:true});
}
function openGuide(){
  guideOrigin=document.activeElement;
  if(!waitingOverlay.open)waitingOverlay.showModal();
  syncWaitingMotion();
}
function updateWaitingOverlay(title,label,description,isWaiting){
  const hasGuide=isWaiting||currentJob?.state==='awaiting_scan';
  $('guide-reopen').hidden=!hasGuide;
  guideReady=currentJob?.state==='awaiting_scan';
  waitingOverlay.classList.toggle('guide-ready',guideReady);
  for(const [id,text] of [['waiting-overlay-title',title],['waiting-overlay-label',label],['waiting-overlay-description',description]]){
    if($(id).textContent!==text)$(id).textContent=text;
  }
  $('waiting-overlay-dismiss').textContent=guideReady?'查看我的二维码':'先看开通进度';
  if(!hasGuide){
    if(waitingOverlay.open)dismissWaitingOverlay();
    return;
  }
  const key=String(currentJob?.id??currentJob?.job_id??currentJob?.created_at??'current-application');
  if(waitingOverlayKey!==key){
    waitingOverlayKey=key;waitingOverlayDismissed=false;showGuideStep(0);
    try{waitingOverlayDismissed=sessionStorage.getItem('xingyu-allocation-guide:'+key)==='dismissed'}catch{}
  }
  // An existing QR page does not auto-open. A guide already being read stays open.
  if(isWaiting&&!waitingOverlayDismissed&&!waitingOverlay.open)openGuide();
  syncWaitingMotion();
}
function dismissWaitingOverlay(){
  waitingOverlayDismissed=true;
  try{sessionStorage.setItem('xingyu-allocation-guide:'+waitingOverlayKey,'dismissed')}catch{}
  if(waitingOverlay.open)waitingOverlay.close();
  if(guideOrigin?.isConnected&&!guideOrigin.closest('[hidden]')&&guideOrigin!==document.body)guideOrigin.focus({preventScroll:true});
  else if(!$('guide-reopen').hidden)$('guide-reopen').focus({preventScroll:true});
  else $('status-refresh').focus({preventScroll:true});
  syncWaitingMotion();
}
$('guide-close').addEventListener('click',dismissWaitingOverlay);
$('waiting-overlay-dismiss').addEventListener('click',dismissWaitingOverlay);
$('guide-reopen').addEventListener('click',()=>{showGuideStep(0);openGuide()});
waitingOverlay.addEventListener('cancel',event=>{event.preventDefault();dismissWaitingOverlay()});
$('waiting-overlay-refresh').addEventListener('click',checkCapacityAndContinue);
$('guide-prev').addEventListener('click',()=>showGuideStep(guideIndex-1,true));
$('guide-next').addEventListener('click',()=>{if(guideIndex===4)dismissWaitingOverlay();else showGuideStep(guideIndex+1,true)});
document.querySelectorAll('[data-guide-step]').forEach(button=>button.addEventListener('click',()=>showGuideStep(Number(button.dataset.guideStep),true)));

// Keep the waiting indicator quiet when the page or indicator is out of view.
let waitingInView=true;
const waitingAnimation=$('waiting-animation');
function syncWaitingMotion(){waitingAnimation.dataset.paused=String(document.hidden||!waitingInView);waitingOverlay.dataset.paused=String(document.hidden||!waitingOverlay.open)}
document.addEventListener('visibilitychange',syncWaitingMotion);
if('IntersectionObserver' in window){
  const waitingObserver=new IntersectionObserver(entries=>{
    waitingInView=entries.some(entry=>entry.isIntersecting);
    syncWaitingMotion();
  });
  waitingObserver.observe(waitingAnimation);
}
syncWaitingMotion();
// onboarding-qr-format-v1: preserve the opened AI when only its QR fails.
let qrLoadTimer=null,qrLoadId=0;
function resetOnboardingQR(){
  clearTimeout(qrLoadTimer);qrLoadId++;
  const image=$('onboarding-qr');image.onload=null;image.onerror=null;image.hidden=true;image.removeAttribute('src');
  $('qr-load-status').hidden=true;$('qr-load-error').hidden=true;$('qr-retry').hidden=true;
}
function loadOnboardingQR(){
  resetOnboardingQR();const image=$('onboarding-qr'),id=qrLoadId;
  $('qr-load-status').textContent='正在加载二维码…';$('qr-load-status').hidden=false;
  function fail(){
    if(id!==qrLoadId)return;clearTimeout(qrLoadTimer);image.onload=null;image.onerror=null;image.hidden=true;
    $('qr-load-status').hidden=true;$('qr-load-error').textContent='AI 已开通，二维码暂时未能显示。请点击“重新加载二维码”；若仍无法显示，请稍后重试或联系管理员。无需重新申请。';
    $('qr-load-error').hidden=false;$('qr-retry').hidden=false;
  }
  image.onload=()=>{if(id!==qrLoadId)return;if(!image.naturalWidth){fail();return}clearTimeout(qrLoadTimer);image.onload=null;image.onerror=null;image.hidden=false;$('qr-load-status').hidden=true;$('qr-load-error').hidden=true;$('qr-retry').hidden=true;};
  image.onerror=fail;qrLoadTimer=setTimeout(fail,20000);
  image.src='/ai/api/onboarding/qr?t='+Date.now();
}
$('qr-retry').addEventListener('click',()=>loadOnboardingQR());
window.addEventListener('pagehide',()=>clearTimeout(qrLoadTimer));
window.addEventListener('pageshow',event=>{if(event.persisted&&!$('qr-area').hidden)loadOnboardingQR();});

init();
