const scopeParam=new URLSearchParams(location.search).get('enterprise');
const enterprise=['all','legacy','ww0000000000000002','ww0000000000000003'].includes(scopeParam)?scopeParam:'ww0000000000000002';

// Reuse each enterprise's existing authenticated screen and scoped controls.
const enterprisePanelViews=new Set(['apps','onboarding','payments','business','starcoin','usage','shadow','prices','defaults','jobs','audit','wallet']);
const embeddedEnterprise=window.parent!==window&&enterprise!=='all'&&new URLSearchParams(location.search).get('embedded')==='enterprise';
let enterprisePanelView='',enterpriseResizePending=false;
function enterpriseFrames(){return [...document.querySelectorAll('#scope-choices iframe[data-enterprise]')];}
function enterpriseFrameStates(){return enterpriseFrames().map(frame=>{try{return frame.contentWindow.enterprisePanelState?.()||{};}catch{return {};}});}
function hasUnsavedChanges(){return configDirty||enterpriseFrameStates().some(state=>state.dirty);}
function enterpriseOperationsBusy(){return enterpriseFrameStates().some(state=>state.busy);}
function clearEnterprisePanels(){if(embeddedEnterprise)return;enterprisePanelView='';$('scope-choices').replaceChildren();}
function enterprisePanelResize(){
 if(!embeddedEnterprise||enterpriseResizePending)return;enterpriseResizePending=true;
 requestAnimationFrame(()=>{enterpriseResizePending=false;
  const target=!$('shell').hidden?$('main'):!$('gate').hidden?$('gate'):$('loading');
  const dialogBottom=Math.max(0,...[...document.querySelectorAll('dialog[open]')].map(dialog=>dialog.getBoundingClientRect().bottom));
  const height=Math.max(180,Math.ceil(Math.max(target.getBoundingClientRect().bottom,dialogBottom)+window.scrollY+20));
  window.parent.postMessage({type:'xingyu-enterprise-height',enterprise,height},location.origin);
 });
}
function enterpriseDialogPosition(dialog){
 if(!embeddedEnterprise)return;
 const frame=window.frameElement,rect=frame.getBoundingClientRect();
 const top=Math.max(16,32-rect.top),height=Math.max(160,window.parent.innerHeight-80);
 dialog.style.setProperty('--enterprise-dialog-top',top+'px');dialog.style.setProperty('--enterprise-dialog-height',height+'px');
}
function initEnterprisePanels(){
 if(embeddedEnterprise){
  document.documentElement.dataset.enterpriseEmbedded='true';
  const name=location.hash.slice(1);if(enterprisePanelViews.has(name))$('view-'+name)?.querySelector('h1')?.classList.add('embedded-page-title');
  window.enterprisePanelState=()=>({dirty:configDirty,busy:priceSaving||templateSaving||walletBusy||!!document.querySelector('dialog[open]')});
  window.discardEnterpriseChanges=()=>{configDirty=false;};
  for(const dialog of document.querySelectorAll('dialog')){const show=dialog.showModal.bind(dialog);dialog.showModal=()=>{enterpriseDialogPosition(dialog);show();};}
  new ResizeObserver(enterprisePanelResize).observe($('main'));
  new MutationObserver(enterprisePanelResize).observe(document.body,{subtree:true,childList:true,attributes:true,attributeFilter:['hidden','open']});
  window.addEventListener('resize',enterprisePanelResize);enterprisePanelResize();
 }else{
  window.addEventListener('message',event=>{
   if(event.origin!==location.origin)return;const frame=enterpriseFrames().find(frame=>frame.contentWindow===event.source);if(!frame||event.data?.enterprise!==frame.dataset.enterprise)return;
   if(event.data.type==='xingyu-enterprise-height'&&Number.isFinite(event.data.height)&&event.data.height>=180&&event.data.height<=100000){frame.style.height=Math.ceil(event.data.height)+'px';frame.setAttribute('aria-busy','false');}
   if(event.data.type==='xingyu-enterprise-changed')invalidateDashboard();
  });
 }
}
function showEnterprisePanels(name){
 view('scope');saveRoute(name);$('scope-title').textContent=navTitle(name)+' · 全部企业';$('scope-description').textContent='各企业资料直接展示，可分别筛选、查看详情和管理。';$('current-view-label').textContent=navTitle(name);
 document.querySelectorAll('nav [data-view]').forEach(button=>{if(button.dataset.view===name)button.setAttribute('aria-current','page');});
 syncAdminNavigation(name);
 if(enterprisePanelView===name&&enterpriseFrames().length===3)return;
 enterprisePanelView=name;const box=$('scope-choices');box.replaceChildren();
 for(const key of ['ww0000000000000002','ww0000000000000003','legacy']){
  const section=el('section',undefined,'enterprise-panel'),heading=el('h2',enterpriseLabel(key));heading.id='enterprise-panel-'+key;section.setAttribute('aria-labelledby',heading.id);
  const frame=el('iframe');frame.dataset.enterprise=key;frame.title=enterpriseLabel(key)+' · '+navTitle(name);frame.setAttribute('aria-busy','true');frame.loading='eager';frame.src=scopeLink(name,key,{embedded:'enterprise'});
  section.append(heading,frame);box.append(section);
 }
}


// admin-defaults-refresh-20261103.1
let globalDefaults=null,defaultsPending=null,defaultsLoading=false,defaultsVoiceLoading=false,defaultsSequence=0;
const defaultsWritable=()=>identity?.role==='superadmin';
function defaultsLabel(field,value){
  if(field.type==='boolean')return value?'开启':'关闭';
  return field.choices?.find(v=>v.value===value)?.label??String(value);
}
function defaultsValue(field){const input=$('default-'+field.key);return field.type==='boolean'?input.value==='true':field.type==='number'?(input.value===''?NaN:Number(input.value)):input.value;}
function defaultsChanges(){
  const options={};if(!globalDefaults)return options;
  for(const field of globalDefaults.schema){const input=$('default-'+field.key);if(!input)continue;const value=defaultsValue(field);if(value!==globalDefaults.values[field.key])options[field.key]=value;}
  return options;
}
function defaultsScope(){
  const sync=$('defaults-sync').checked;
  $('defaults-existing').disabled=!sync||!defaultsWritable();
  if(!sync)$('defaults-existing').checked=false;
  $('defaults-scope-note').textContent=!sync?'仅更新当前企业的默认模板，供之后新分配的应用继承。':$('defaults-existing').checked?'同步到当前企业现有应用，并覆盖已有用户的对应选项。用户之后仍可自行调整。':'同步到当前企业现有应用；已有用户保存的个人选择会保留。';
}
function defaultsSelection(){
  const changes=defaultsChanges(),count=Object.keys(changes).length;
  $('defaults-selected').textContent=count?'有 '+count+' 项待保存':'暂无待保存的修改';
  $('defaults-save').disabled=!count||!defaultsWritable()||defaultsLoading||defaultsVoiceLoading;
  $('defaults-reset').disabled=(!count&&!$('defaults-sync').checked)||defaultsLoading||!defaultsWritable();
  for(const field of globalDefaults?.schema||[]){
    const row=$('default-row-'+field.key);if(!row)continue;
    const changed=Object.hasOwn(changes,field.key);row.dataset.changed=String(changed);
    $('default-state-'+field.key).textContent=changed?'待保存 · 原值：'+defaultsLabel(field,globalDefaults.values[field.key]):Object.hasOwn(globalDefaults.options,field.key)?'已设为默认':'系统初始值';
  }
  for(const details of $('defaults-groups').querySelectorAll('details')){
    const changed=details.querySelectorAll('[data-changed="true"]').length;
    details.querySelector('summary span').textContent=changed?'高级设置 · '+changed+' 项待保存':'高级设置';
  }
  configDirty=count>0||$('defaults-sync').checked;
}
function defaultsVoiceStatus(data){
  const missing=data.voice_unavailable||[],stale=data.voice_stale||[];
  $('defaults-voices').textContent=missing.length?'有 '+missing.length+' 个应用的音色暂无法核验，可沿用各应用音色，或重试目录。':stale.length?'有 '+stale.length+' 个应用暂时使用最近读取的音色目录，保存具体音色前会重新核验。':'音色目录已就绪，停止中的应用也可读取。统一音色列表只展示当前企业全部应用均支持的声音。';
  $('defaults-voices').dataset.warning=String(!!(missing.length||stale.length));
}
function defaultsFillChoices(input,field,value){
  input.replaceChildren();
  const choices=field.type==='boolean'?[{value:'true',label:'开启'},{value:'false',label:'关闭'}]:field.choices;
  for(const item of choices){const option=el('option',item.label);option.value=String(item.value);input.append(option);}
  if(![...input.options].some(option=>option.value===String(value))){const option=el('option',String(value)+'（待核验）');option.value=String(value);input.append(option);}
  input.value=String(value);
}
async function loadDefaults(){
  const sequence=++defaultsSequence;
  defaultsLoading=true;globalDefaults=null;$('defaults-form').hidden=true;$('defaults-error').hidden=true;$('defaults-status').textContent='正在读取当前企业的默认模板…';$('defaults-refresh').disabled=true;
  try{
    const data=await request('global-defaults');if(sequence!==defaultsSequence)return;globalDefaults=data;
    $('defaults-status').textContent=data.revision?'保存版本 '+data.revision+' · '+date(data.updated)+' · '+data.actor+' 保存':'尚未保存默认模板，以下展示系统初始值。';
    if(!defaultsWritable())$('defaults-status').textContent+=' · 当前为只读权限';
    $('defaults-enterprise').textContent=enterpriseLabel(enterprise);
    $('defaults-agent-count').textContent=data.agents.length+' 个应用';
    const voiceStatus=$('defaults-voices'),voiceRefresh=$('defaults-voice-refresh');
    const parent=$('defaults-groups');parent.replaceChildren();let group=null,section=null,advanced=null;
    for(const field of data.schema){
      if(field.group!==group){
        group=field.group;section=el('section',undefined,'section defaults-group');section.append(el('h2',group),el('p',data.group_help?.[group]||''));parent.append(section);advanced=null;
        if(group==='语音与图片'){const voice=el('div',undefined,'defaults-voice-status');voice.append(voiceStatus,voiceRefresh);section.append(voice);}
      }
      let container=section;
      if(field.advanced){if(!advanced){advanced=el('details',undefined,'defaults-advanced');const summary=el('summary');summary.append(el('span','高级设置'));advanced.append(summary);section.append(advanced);}container=advanced;}
      const row=el('div',undefined,'defaults-row');row.id='default-row-'+field.key;
      const body=el('div'),label=el('label',field.label);label.htmlFor='default-'+field.key;body.append(label);
      if(field.help){const help=el('small',field.help,'defaults-help');help.id='default-help-'+field.key;body.append(help);}
      const state=el('small',undefined,'defaults-field-state');state.id='default-state-'+field.key;body.append(state);
      const input=el(field.type==='number'?'input':'select');input.id='default-'+field.key;input.dataset.defaultValue=field.key;
      if(field.type==='number'){input.type='number';input.min=field.min;input.max=field.max;input.step=field.step;input.value=data.values[field.key];}
      else defaultsFillChoices(input,field,data.values[field.key]);
      input.disabled=!defaultsWritable();input.required=field.key!=='voice';input.setAttribute('aria-describedby',(field.help?'default-help-'+field.key+' ':'')+state.id);
      input.oninput=input.onchange=defaultsSelection;
      row.append(body,input);if(container===section&&advanced)section.insertBefore(row,advanced);else container.append(row);
    }
    $('defaults-fields').disabled=!defaultsWritable();$('defaults-sync').disabled=!defaultsWritable();$('defaults-sync').checked=false;$('defaults-existing').checked=false;
    $('defaults-targets').textContent='当前企业共 '+data.agents.length+' 个应用（含待分配和停止中的应用）。同步默认值不会启动应用或开启 AI。';
    defaultsVoiceStatus(data);$('defaults-form').hidden=false;defaultsScope();
  }catch(error){if(sequence===defaultsSequence){$('defaults-status').textContent='默认模板读取失败';$('defaults-error').textContent=error.message+' 请点击“重新读取”。';$('defaults-error').hidden=false;}}
  finally{if(sequence===defaultsSequence){defaultsLoading=false;$('defaults-refresh').disabled=false;if(globalDefaults)defaultsSelection();}}
}
async function refreshDefaultsVoices(){
  if(!globalDefaults||defaultsVoiceLoading)return;const owner=globalDefaults,sequence=defaultsSequence;defaultsVoiceLoading=true;const button=$('defaults-voice-refresh');button.disabled=true;button.textContent='正在读取音色…';defaultsSelection();
  try{
    const data=await request('global-defaults?refresh_voices=1'),field=data.schema.find(field=>field.key==='voice'),input=$('default-voice'),value=input.value;
    if(sequence!==defaultsSequence||globalDefaults!==owner)return;
    globalDefaults.schema=globalDefaults.schema.map(item=>item.key==='voice'?field:item);defaultsFillChoices(input,field,value);defaultsVoiceStatus(data);
    if(data.revision!==globalDefaults.revision)$('defaults-voices').textContent+=' 默认模板已被更新，保存前请重新读取并核对；本页未保存内容仍保留。';
  }catch(error){if(sequence===defaultsSequence&&globalDefaults===owner)$('defaults-voices').textContent='音色重试失败：'+error.message+'。本页未保存的修改已保留。';}
  finally{defaultsVoiceLoading=false;button.disabled=false;button.textContent='重新读取音色';if(globalDefaults)defaultsSelection();}
}
function initDefaults(){
  $('defaults-refresh').onclick=()=>{if(hasUnsavedChanges()&&!confirm('放弃尚未保存的设置并重新读取？'))return;loadDefaults();};
  $('defaults-voice-refresh').onclick=refreshDefaultsVoices;
  $('defaults-reset').onclick=()=>{for(const field of globalDefaults.schema){const input=$('default-'+field.key);if(field.type==='number')input.value=globalDefaults.values[field.key];else defaultsFillChoices(input,field,globalDefaults.values[field.key]);}$('defaults-sync').checked=false;$('defaults-existing').checked=false;defaultsScope();defaultsSelection();};
  for(const id of ['defaults-sync','defaults-existing'])$(id).onchange=()=>{defaultsScope();defaultsSelection();};
  $('defaults-form').onsubmit=event=>{
    event.preventDefault();if(!defaultsWritable()||defaultsVoiceLoading||!$('defaults-form').reportValidity())return;const options=defaultsChanges();if(!Object.keys(options).length)return;
    defaultsPending={options,revision:globalDefaults.revision,sync_apps:$('defaults-sync').checked,apply_existing:$('defaults-existing').checked,request_id:crypto.randomUUID().replaceAll('-',''),confirm:true};
    const box=$('defaults-review-summary');box.replaceChildren();box.append(el('p','当前企业：'+enterpriseLabel(enterprise),'defaults-review-scope'));
    for(const field of globalDefaults.schema)if(Object.hasOwn(options,field.key)){
      const row=el('div',undefined,'defaults-diff');row.append(el('strong',field.label),el('span',defaultsLabel(field,globalDefaults.values[field.key])+' → '+defaultsLabel(field,options[field.key])));box.append(row);
    }
    box.append(el('p','之后新分配的应用：继承当前企业更新后的默认模板。'));
    box.append(el('p',defaultsPending.sync_apps?'现有应用：同步这 '+Object.keys(options).length+' 项到当前企业 '+globalDefaults.agents.length+' 个应用。':'现有应用：本次不做同步。'));
    box.append(el('p',defaultsPending.apply_existing?'已有用户：覆盖上述选项，用户之后仍可自行调整。':'已有用户：保留已保存的个人选择。'));
    $('defaults-review-error').textContent='';$('defaults-review-save').disabled=false;$('defaults-review').showModal();
  };
  for(const id of ['defaults-review-close','defaults-review-cancel'])$(id).onclick=()=>$('defaults-review').close();
  $('defaults-review-save').onclick=async()=>{
    if(!defaultsPending)return;const button=$('defaults-review-save');button.disabled=true;button.textContent='正在提交…';
    try{const result=await request('global-defaults',defaultsPending);defaultsPending=null;configDirty=false;$('defaults-review').close();note('默认设置已进入维护队列，任务号 '+result.job+'。请查看模板及各应用的执行结果。');view('jobs');await loadJobs();}
    catch(error){$('defaults-review-error').textContent=error.message;}
    finally{button.disabled=false;button.textContent='确认保存';}
  };
}


let walletList=[],walletCurrent='',walletEntryPage=1,walletEventPage=1,walletSequence=0,walletReverseEntry=null,walletBusy=false,walletPending={},walletDetailResult=null;
const walletStates={waiting:'待核对',queued:'待扣费',insufficient:'模拟余额不足',paid:'已模拟扣费',excluded:'不扣费',reversed:'已冲正'};
const walletTypes={credit:'模拟充值',debit:'模拟扣费',reversal:'冲正'};
function walletKey(){return 'xiaoyi-sim-wallet-pending:'+enterprise+':'+identity.username;}
function walletRemember(){sessionStorage.setItem(walletKey(),JSON.stringify(walletPending));}
function walletIsSuper(){return false;}
function walletControls(){
  for(const [id,permitted]of [['wallet-create-submit',walletIsSuper()],['wallet-credit-submit',walletIsSuper()],['wallet-reverse-submit',walletIsSuper()],['wallet-sync',writeAllowed()]])$(id).disabled=walletBusy||!permitted;
  for(const [op,ids]of Object.entries({create:['wallet-create-agent','wallet-create-user'],credit:['wallet-credit-amount','wallet-credit-note'],reverse:['wallet-reverse-note','wallet-reverse-confirm']}))for(const id of ids)$(id).disabled=walletBusy||!walletIsSuper()||!!walletPending[op];
  for(const [op,id,label]of [['create','wallet-create-submit','开通模拟账户'],['credit','wallet-credit-submit','确认模拟充值'],['reverse','wallet-reverse-submit','确认冲正']])$(id).textContent=walletPending[op]?'重试原操作':label;
  $('wallet-refresh').disabled=walletBusy;
  document.querySelectorAll('.wallet-dynamic-action').forEach(b=>b.disabled=walletBusy);
  if(walletDetailResult){const r=walletDetailResult;$('wallet-entry-prev').disabled=walletBusy||r.page<=1;$('wallet-entry-next').disabled=walletBusy||r.page*r.page_size>=r.entry_count;$('wallet-event-prev').disabled=walletBusy||r.event_page<=1;$('wallet-event-next').disabled=walletBusy||r.event_page*r.page_size>=r.event_count;}

}
function initWallet(){
  try{walletPending=JSON.parse(sessionStorage.getItem(walletKey())||'{}');}catch{walletPending={};}
  $('wallet-refresh').onclick=()=>loadWallets().catch(e=>note(e.message,true));
  $('wallet-create-form').onsubmit=e=>{e.preventDefault();walletMutate('create',{agent:Number($('wallet-create-agent').value),user:$('wallet-create-user').value.trim()});};
  $('wallet-credit-form').onsubmit=e=>{e.preventDefault();walletMutate('credit',{wallet:walletCurrent,amount:$('wallet-credit-amount').value.trim(),note:$('wallet-credit-note').value.trim()});};
  $('wallet-reverse-form').onsubmit=e=>{e.preventDefault();walletMutate('reverse',{wallet:walletCurrent,entry:walletReverseEntry,note:$('wallet-reverse-note').value.trim()});};
  $('wallet-reverse-cancel').onclick=()=>{if(walletPending.reverse){$('wallet-message').textContent='上次冲正结果尚未确认，请重试原操作核对结果。';return;}$('wallet-reverse-form').hidden=true;};
  $('wallet-sync').onclick=async()=>{
    walletBusy=true;walletControls();$('wallet-message').textContent='正在同步账户开通后的用量…';
    try{const r=await request('wallet/settle',{wallet:walletCurrent});$('wallet-message').textContent='已读取 '+r.read+' 条更新，模拟扣费 '+r.charged+' 笔 / '+shadowMoney(r.amount)+'；余额不足 '+r.insufficient+' 笔。'+(r.unavailable.length?'应用 '+r.unavailable.join('、')+' 读取失败，请稍后重试。':'')+(r.more?'仍有用量待处理，请再次同步。':'');await loadWallets();}
    catch(e){$('wallet-message').textContent=e.message+' 可再次同步，已扣请求不会重复扣费。';}
    finally{walletBusy=false;walletControls();}
  };
  for(const [id,page,delta]of [['wallet-entry-prev','entry',-1],['wallet-entry-next','entry',1],['wallet-event-prev','event',-1],['wallet-event-next','event',1]])$(id).onclick=()=>{if(page==='entry')walletEntryPage+=delta;else walletEventPage+=delta;loadWalletDetail().catch(e=>note(e.message,true));};
  walletControls();
}
async function walletMutate(operation,data){
  if(walletBusy)return;
  if(walletPending[operation]&&operation!=='create'&&walletPending[operation].wallet!==walletCurrent){$('wallet-message').textContent='另一账户有未确认操作，请先选择该账户重试原操作。';return;}
  if(!walletPending[operation]){walletPending[operation]={...data,request_id:crypto.randomUUID().replaceAll('-','')};walletRemember();}
  const payload=walletPending[operation];walletBusy=true;walletControls();$('wallet-message').textContent='正在提交模拟操作…';
  try{
    const r=await request('wallet/'+operation,payload);delete walletPending[operation];walletRemember();
    if(operation==='create'){walletCurrent=r.wallet;$('wallet-message').textContent=r.existing?'此成员已有模拟账户，已打开。':'模拟账户已开通，初始余额为 0。';}
    else{$('wallet-message').textContent=(operation==='credit'?'模拟充值已记录':'冲正已记录')+'，当前模拟余额 '+shadowMoney(r.balance)+'。';if(operation==='reverse')$('wallet-reverse-form').hidden=true;}
    walletEntryPage=1;walletEventPage=1;await loadWallets();
  }catch(e){
    if(e.status>=400&&e.status<500){delete walletPending[operation];walletRemember();$('wallet-message').textContent=e.message;}
    else $('wallet-message').textContent=e.message+' 原操作编号 '+payload.request_id+' 已保留。请先刷新流水核对，再点“重试原操作”；不会生成第二笔。';
  }finally{walletBusy=false;walletControls();}
}
async function loadWallets(){
  $('wallet-refresh').disabled=true;
  try{
    const result=await request('wallets');walletList=result.items;
    const selected=$('wallet-create-agent').value;$('wallet-create-agent').replaceChildren();
    for(const a of apps){const o=el('option',a.agent+' · '+a.name);o.value=a.agent;$('wallet-create-agent').append(o);}if(selected)$('wallet-create-agent').value=selected;
    if(walletPending.create){$('wallet-create-agent').value=walletPending.create.agent;$('wallet-create-user').value=walletPending.create.user;}
    if(!walletCurrent)walletCurrent=walletPending.credit?.wallet||walletPending.reverse?.wallet||walletList[0]?.id||'';
    $('wallet-list-note').textContent='共 '+result.count+' 个模拟账户'+(result.count>result.limit?'；当前显示最近 '+result.limit+' 个':'')+'。金额单位：人民币元。';
    const body=$('wallet-rows');body.replaceChildren();
    for(const item of walletList){const tr=el('tr');tr.className=item.id===walletCurrent?'wallet-current':'';const who=el('td',item.user);who.append(el('small',item.corp));const button=el('button',item.id===walletCurrent?'当前账户':'查看','secondary wallet-dynamic-action');button.disabled=walletBusy;button.onclick=()=>{walletCurrent=item.id;walletEntryPage=1;walletEventPage=1;$('wallet-reverse-form').hidden=true;loadWallets().catch(e=>note(e.message,true));};const td=el('td');td.append(button);tr.append(who,el('td',shadowMoney(item.balance),'number'),el('td','待核对 '+(item.counts.waiting||0)+' / 余额不足 '+(item.counts.insufficient||0)),el('td',date(item.created)),td);body.append(tr);}
    if(!walletList.length){const tr=el('tr'),td=el('td','尚无历史模拟账户。当前余额与消费请查看“星币账户”。','empty');td.colSpan=5;tr.append(td);body.append(tr);}
    if(walletCurrent)await loadWalletDetail();else $('wallet-detail').hidden=true;
  }finally{walletControls();}
}
async function loadWalletDetail(){
  const seq=++walletSequence,ident=walletCurrent;if(!ident)return;
  const r=await request('wallet-detail?'+new URLSearchParams({wallet:ident,page:walletEntryPage,event_page:walletEventPage}));if(seq!==walletSequence||walletCurrent!==ident)return;
  walletDetailResult=r;$('wallet-detail').hidden=false;$('wallet-detail-title').textContent=r.wallet.user+' · 模拟账户';$('wallet-selected').textContent='企业 '+r.wallet.corp+' · 从 '+date(r.wallet.created)+' 起记录';$('wallet-balance').textContent='模拟余额 '+shadowMoney(r.wallet.balance)+(r.wallet.reserved?' · 预留 '+shadowMoney(r.wallet.reserved):'');
  $('wallet-integrity').textContent=r.wallet.consistent?'账本核对通过：余额等于全部充值、扣费及冲正流水的合计。':'账本核对异常，请停止模拟操作并联系维护人员。';
  if(walletPending.credit?.wallet===ident){$('wallet-credit-amount').value=walletPending.credit.amount;$('wallet-credit-note').value=walletPending.credit.note;}
  const body=$('wallet-entry-rows');body.replaceChildren();
  for(const item of r.entries){const tr=el('tr'),when=el('td',date(item.created));when.append(el('small','#'+item.id));const related=el('td',item.agent?String(item.agent):'—');if(item.request_id)related.append(el('small',item.request_id));if(item.reversal)related.append(el('small','冲正 #'+item.reversal));const noteCell=el('td',item.note);noteCell.append(el('small',item.actor));const actionCell=el('td');if(item.kind!=='reversal'&&!item.reversed_by&&walletIsSuper()){const button=el('button','冲正','secondary wallet-dynamic-action');button.disabled=walletBusy;button.onclick=()=>{walletReverseEntry=item.id;$('wallet-reverse-selected').textContent='#'+item.id+' · '+walletTypes[item.kind]+' · '+shadowMoney(item.amount);$('wallet-reverse-confirm').checked=false;$('wallet-reverse-note').value='';$('wallet-reverse-form').hidden=false;$('wallet-reverse-note').focus();};actionCell.append(button);}else actionCell.textContent=item.reversed_by?'已由 #'+item.reversed_by+' 冲正':'—';tr.append(when,el('td',walletTypes[item.kind]),el('td',(Number(item.amount)>0?'+':'')+shadowMoney(item.amount),'number'),el('td',shadowMoney(item.balance),'number'),related,noteCell,actionCell);body.append(tr);}
  walletEmpty(body,r.entries.length,7,'尚无流水。模拟充值后会在这里留存记录。');
  $('wallet-entry-page').textContent='第 '+r.page+' 页 · '+r.entry_count+' 笔流水';$('wallet-entry-prev').disabled=walletBusy||r.page<=1;$('wallet-entry-next').disabled=walletBusy||r.page*r.page_size>=r.entry_count;
  const events=$('wallet-event-rows');events.replaceChildren();for(const item of r.events){const tr=el('tr'),when=el('td',date(item.created));when.append(el('small',item.request_id));const s=item.snapshot,resource=el('td',shadowKinds[item.kind]+' / '+s.model);resource.append(el('small',item.kind==='token'?'输入 '+tokenNumber(s.input_tokens)+' / 输出 '+tokenNumber(s.output_tokens):item.kind==='image'?tokenNumber(s.image_count)+' 张':tokenNumber(s.generated_chars)+' 字符'));const state=el('td',(item.review?'需复核 · ':'')+(walletStates[item.state]||item.state));state.append(el('small',shadowState[item.reason]||({amount_out_of_range:'金额超出模拟上限'}[item.reason]||item.reason)));tr.append(when,el('td',String(item.agent)),resource,el('td',shadowMoney(item.amount),'number'),state,el('td',item.revision?'v'+item.revision:'待定价'));events.append(tr);}
  walletEmpty(events,r.events.length,6,'尚无用量。新的聊天与主动消息会自动记录，也可点击“同步用量并模拟扣费”核对。');
  $('wallet-event-page').textContent='第 '+r.event_page+' 页 · '+r.event_count+' 笔用量';$('wallet-event-prev').disabled=walletBusy||r.event_page<=1;$('wallet-event-next').disabled=walletBusy||r.event_page*r.page_size>=r.event_count;
  if(walletPending.reverse?.wallet===ident){walletReverseEntry=walletPending.reverse.entry;$('wallet-reverse-selected').textContent='重试原冲正操作 #'+walletReverseEntry;$('wallet-reverse-note').value=walletPending.reverse.note;$('wallet-reverse-confirm').checked=true;$('wallet-reverse-form').hidden=false;}
  walletControls();
}
function walletEmpty(body,count,columns,text){if(count)return;const tr=el('tr'),td=el('td',text,'empty');td.colSpan=columns;tr.append(td);body.append(tr);}

// Reference costs only. No wallet, payment or debit API is used here.
let shadowPage=1,shadowSequence=0,priceConfig=null,shadowCatalog=[],priceSequence=0,priceSaving=false;
const shadowKinds={token:'聊天 Token',image:'生图',tts:'语音'};
const shadowState={source_unavailable:'原始用量暂不可核对',awaiting_delivery:'等待回复发出',internal_excluded:'内部处理免费',not_delivered_excluded:'未发出不扣费',estimated:'已试算',unpriced:'待定价',pending_or_unknown:'结果待确认',provider_cost_unknown:'成本待核对',usage_missing:'用量缺失',calendar_unconfirmed:'分时日历待核对',cache_usage_missing:'缓存用量缺失',failed_excluded:'失败不计拟收费',retry_or_preview_excluded:'试听 / 重试不计拟收费'};
function shadowMoney(value){if(value===null||value===undefined)return '—';const n=Number(value);return n===0?'¥0':Math.abs(n)<.000001?'¥'+n.toFixed(9):'¥'+n.toFixed(6);}
function initShadow(){
  $('shadow-start').value=$('usage-start').value;$('shadow-end').value=$('usage-end').value;
  $('shadow-form').onsubmit=e=>{e.preventDefault();loadShadow(true).catch(e=>note(e.message,true));};
  $('shadow-prev').onclick=()=>{shadowPage--;loadShadow().catch(e=>note(e.message,true));};
  $('shadow-next').onclick=()=>{shadowPage++;loadShadow().catch(e=>note(e.message,true));};
  $('price-kind').onchange=updatePriceFields;
  $('price-observed').onchange=()=>{
    const raw=$('price-observed').value;if(!raw)return;
    const [kind,provider,model]=JSON.parse(raw);
    $('price-kind').value=kind;$('price-provider').value=provider;$('price-model').value=model;
    const rule=priceConfig?.items.find(r=>r.kind===kind&&r.provider===provider&&r.model===model);
    for(const side of ['cost','sale'])for(const field of ['input','output','cached','unit'])$('price-'+side+'-'+field).value=rule?.rates[side][field]??'';
    $('price-schedule').checked=rule?.rates.schedule==='deepseek_cn_2026';$('price-enabled').checked=rule?rule.enabled:true;updatePriceFields();
  };
  $('price-form').onsubmit=e=>{e.preventDefault();saveShadowPrice();};
  $('price-form').addEventListener('input',()=>{configDirty=true;});$('price-form').addEventListener('change',()=>{configDirty=true;});
  $('prices-refresh').onclick=()=>{if(priceSaving)return;if(hasUnsavedChanges()&&!confirm('放弃尚未保存的价格修改并重新读取？'))return;loadPrices();};
  $('price-fields').disabled=identity.role!=='superadmin';$('price-permission').hidden=identity.role==='superadmin';
  updatePriceFields();
}
function updatePriceFields(){
  const token=$('price-kind').value==='token';$('price-schedule').disabled=!token;$('price-token-fields').hidden=!token;$('price-unit-fields').hidden=token;
  for(const side of ['cost','sale'])for(const field of ['input','output','cached','unit'])$('price-'+side+'-'+field).disabled=field==='unit'?token:!token;
  $('price-unit-help').textContent=(token?'元 / 百万 Token。':$('price-kind').value==='image'?'元 / 成功生成图片；同一模型统一规格价格。':'元 / 千字；按成功合成字数。')+'留空表示未定价，填 0 表示明确免费。';
}
function shadowRateText(r,side){const p=r.rates[side];return r.kind==='token'?'输入 '+(p.input??'待定价')+' / 输出 '+(p.output??'待定价')+' / 缓存 '+(p.cached??'同输入')+(r.rates.schedule?'（空闲价，高峰 ×2）':''):p.unit??'待定价';}
function renderShadowPrices(){
  $('price-revision').textContent='当前价表版本 v'+priceConfig.revision+' · 每次保存新增版本；新请求采用新售价，已锁价消费不重算。';
  const body=$('price-rows');body.replaceChildren();
  for(const r of priceConfig.items){const tr=el('tr');tr.append(el('td',shadowKinds[r.kind]+' / '+r.provider+' / '+r.model),el('td',shadowRateText(r,'cost')),el('td',shadowRateText(r,'sale')),el('td','v'+r.id+' / '+(r.enabled?'启用':'停用')));body.append(tr);}
  if(!priceConfig.items.length){const tr=el('tr'),td=el('td','尚未设置价格。用量会保留，金额显示待定价。','empty');td.colSpan=4;tr.append(td);body.append(tr);}
  const history=$('price-history');history.replaceChildren();
  for(const r of priceConfig.history){const item=el('div');item.append(el('p','v'+r.id+' · '+shadowKinds[r.kind]+' · '+r.provider+' / '+r.model),el('small',date(r.created)+' · '+r.actor+' · '+(r.enabled?'启用':'停用')+' · 成本 '+shadowRateText(r,'cost')+' · 售价 '+shadowRateText(r,'sale')));history.append(item);}
  if(!priceConfig.history.length)history.append(el('p','尚无价格变更。','footnote'));
  const selected=$('price-observed').value,choices=new Map();
  for(const r of [...priceConfig.items,...shadowCatalog]){const key=JSON.stringify([r.kind,r.provider,r.model]);choices.set(key,r);}
  $('price-observed').replaceChildren();const first=el('option','手动填写规则');first.value='';$('price-observed').append(first);
  for(const [key,r]of choices){const opt=el('option',shadowKinds[r.kind]+' · '+r.provider+' · '+(r.model||'未记录模型'));opt.value=key;$('price-observed').append(opt);}
  $('price-observed').value=selected;
}
async function loadPrices(){
  const sequence=++priceSequence;$('prices-status').textContent='正在读取计费价表…';$('prices-status').className='report-status';$('prices-content').hidden=true;$('prices-refresh').disabled=true;$('price-save').disabled=true;priceConfig=null;
  try{const data=await request('shadow-prices');if(sequence!==priceSequence)return;priceConfig=data;renderShadowPrices();$('price-form').reset();updatePriceFields();$('price-fields').disabled=identity.role!=='superadmin';$('price-save').disabled=identity.role!=='superadmin';$('price-message').textContent='';$('prices-content').hidden=false;$('prices-status').textContent='';configDirty=false;}
  catch(error){if(sequence!==priceSequence)return;$('prices-status').textContent=error.message+' 请点击“重新读取”。';$('prices-status').className='report-status error';}
  finally{if(sequence===priceSequence)$('prices-refresh').disabled=false;}
}
async function saveShadowPrice(){
  if(identity.role!=='superadmin'||priceSaving)return;priceSaving=true;const button=$('price-save');button.disabled=true;$('prices-refresh').disabled=true;$('price-fields').disabled=true;$('price-message').textContent='正在保存价格版本…';
  try{
    if(!priceConfig)throw new Error('请先重新查询价表。');
    const kind=$('price-kind').value,rates={cost:{},sale:{}};
    for(const side of ['cost','sale'])for(const field of ['input','output','cached','unit'])rates[side][field]=((field==='unit')===(kind!=='token'))?($('price-'+side+'-'+field).value.trim()||null):null;
    if(kind==='token'&&$('price-schedule').checked)rates.schedule='deepseek_cn_2026';
    const result=await request('shadow-prices',{kind,provider:$('price-provider').value.trim(),model:$('price-model').value.trim(),rates,enabled:$('price-enabled').checked,revision:priceConfig.revision});
    configDirty=false;await loadPrices();$('price-message').textContent='已保存 v'+result.version+'，新请求采用新价表，已锁价记录不变。';
  }catch(e){$('price-message').textContent=e.message+' 草稿已保留；如结果未确认，请重新读取并核对版本后再操作。';}
  finally{priceSaving=false;$('prices-refresh').disabled=false;button.disabled=identity.role!=='superadmin'||!priceConfig;$('price-fields').disabled=identity.role!=='superadmin'||!priceConfig;}
}
async function loadShadow(reset=false){
  if(reset)shadowPage=1;const sequence=++shadowSequence;
  const agent=$('shadow-agent'),selected=agent.value;agent.replaceChildren();const all=el('option','全部应用');all.value='';agent.append(all);
  for(const a of apps){const opt=el('option',a.agent+' · '+a.name);opt.value=a.agent;agent.append(opt);}agent.value=selected;
  $('shadow-query').disabled=true;$('shadow-prev').disabled=true;$('shadow-next').disabled=true;$('shadow-error').hidden=true;
  $('shadow-summary').textContent='正在核对用量与价表…';$('shadow-quality').textContent='';$('shadow-rows').replaceChildren();$('shadow-empty').hidden=true;
  try{
    const q=new URLSearchParams({start:$('shadow-start').value,end:$('shadow-end').value,agent:agent.value,user:$('shadow-user').value.trim(),kind:$('shadow-kind').value,group:$('shadow-group').value,page:shadowPage});
    const result=await request('shadow-report?'+q);if(sequence!==shadowSequence)return;
    shadowCatalog=result.catalog;const total=result.summary;
    $('shadow-summary').textContent='参考成本 '+shadowMoney(total.cost)+' · 拟收费 '+shadowMoney(total.sale)+' · 可核对差额 '+shadowMoney(total.margin);
    const notes=[total.requests+' 次接口请求',total.cost_unknown+' 次成本待核对',total.sale_unknown+' 次拟收费待核对',total.both_known+' 次可计算差额',total.excluded_sale+' 次拟收费排除','价表 v'+result.revision,'试算于 '+date(result.calculated_at)];
    if(total.unattributed)notes.push(total.unattributed+' 次尚未归属成员');if(result.unavailable.length)notes.push('读取失败应用：'+result.unavailable.join('、'));if(result.missing_ledgers.length)notes.push('尚无账本：'+result.missing_ledgers.join('、'));
    $('shadow-quality').textContent=notes.join('；')+'。金额为已知部分合计，缺失部分未计入。';
    const detailed=result.group==='request',head=$('shadow-head');
    const columns=detailed?['时间 / 请求编号','应用 / 成员','类型 / 模型 / 用途','参考成本','拟收费','参考差额','核对状态 / 价格版本']:['应用','统计维度','请求数','参考成本','拟收费','可核对差额','成本 / 拟收费待核对'];
    head.replaceChildren(...columns.map(t=>el('th',t)));
    for(const item of result.items){const row=el('tr');
      if(detailed){const id=el('td',date(item.created));id.append(el('small',item.id));const resource=el('td',shadowKinds[item.kind]+' / '+item.provider+' / '+item.model+' / '+(purposeNames[item.purpose]||item.purpose));resource.append(el('small',item.kind==='token'?'输入 '+tokenNumber(item.input_tokens)+' / 输出 '+tokenNumber(item.output_tokens)+' / 缓存 '+tokenNumber(item.cached_tokens)+(item.price_multiplier===2?' · 高峰 ×2':''):item.kind==='image'?tokenNumber(item.image_count)+' 张':tokenNumber(item.generated_chars)+' 字符'));row.append(id,el('td',item.agent+' / '+(item.user||'未归属')),resource,el('td',shadowMoney(item.cost),'number'),el('td',shadowMoney(item.sale),'number'),el('td',shadowMoney(item.margin),'number'),el('td',(shadowState[item.cost_state]||item.cost_state)+' / '+(shadowState[item.sale_state]||item.sale_state)+' / '+(item.version?'v'+item.version:'待定价')));}
      else row.append(el('td',String(item.agent)),el('td',result.group==='app'?'应用全部成员':item.dimension||'未归属'),el('td',tokenNumber(item.requests),'number'),el('td',shadowMoney(item.cost),'number'),el('td',shadowMoney(item.sale),'number'),el('td',shadowMoney(item.margin),'number'),el('td',item.cost_unknown+' / '+item.sale_unknown,'number'));
      $('shadow-rows').append(row);
    }
    $('shadow-empty').hidden=!!result.items.length;$('shadow-empty').textContent='所选范围暂无用量，调整日期或应用后再查询。';$('shadow-table-wrap').hidden=!result.items.length;
    $('shadow-page').textContent='第 '+result.page+' 页 · 共 '+result.count+' '+(detailed?'笔':'组');$('shadow-prev').disabled=result.page<=1;$('shadow-next').disabled=result.page*result.page_size>=result.count;
  }catch(e){if(sequence!==shadowSequence)return;$('shadow-summary').textContent='成本分析未完成';$('shadow-error').textContent=e.message;$('shadow-error').hidden=false;}
  finally{if(sequence===shadowSequence)$('shadow-query').disabled=false;}
}

'use strict';
let usagePage=1,usageSequence=0;
const purposeNames={chat:'普通聊天',proactive:'主动联系',greeting:'每日问候',repair:'回复修复',organization:'回复整理',reminder:'提醒文案',voice_planning:'语音规划',persona_card:'人物卡解析',sticker_analysis:'表情识别',sticker_selection:'表情选择',image_planning:'生图规划',customer_service:'客服对话',unattributed:'尚未归属'};
const tokenNumber=value=>value===null||value===undefined?'—':new Intl.NumberFormat('zh-CN').format(value);
function initUsage(){
  const today=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
  if(!$('usage-start').value)$('usage-start').value=new Date(Date.parse(today+'T00:00:00Z')-29*86400000).toISOString().slice(0,10);
  if(!$('usage-end').value)$('usage-end').value=today;
  $('usage-resource').onchange=()=>loadUsage(true);
  $('usage-form').onsubmit=event=>{event.preventDefault();loadUsage(true);};
  $('usage-prev').onclick=()=>{usagePage--;loadUsage();};
  $('usage-next').onclick=()=>{usagePage++;loadUsage();};
}
async function loadUsage(reset=false){
  if(reset)usagePage=1;
  const sequence=++usageSequence,select=$('usage-agent'),selectedAgent=select.value;
  select.replaceChildren();const all=el('option','全部应用');all.value='';select.append(all);
  for(const app of apps){const option=el('option',app.agent+' · '+app.name);option.value=app.agent;select.append(option);}select.value=selectedAgent;
  const query=new URLSearchParams({start:$('usage-start').value,end:$('usage-end').value,agent:select.value,user:$('usage-user').value.trim(),group:$('usage-group').value,page:String(usagePage),resource:$('usage-resource').value});
  $('usage-query').disabled=true;$('usage-prev').disabled=true;$('usage-next').disabled=true;
  $('usage-error').hidden=true;$('usage-summary').textContent='正在读取用量…';$('usage-quality').textContent='';$('usage-page').textContent='';
  const body=$('usage-rows');body.replaceChildren();$('usage-message').textContent='正在汇总所选类型的用量…';$('usage-message').hidden=false;$('usage-table-wrap').hidden=true;
  try{
    const result=await request('token-usage?'+query);
    if(sequence!==usageSequence)return;
    const totals=result.summary,type=result.resource||'token';
    const columns=type==='token'?[['requests','模型请求'],['input_tokens','输入 token'],['output_tokens','输出 token'],['total_tokens','已知总量'],['cached_tokens','缓存命中'],['missing_requests','用量不完整']]:type==='image'?[['requests','接口调用'],['successful_requests','成功响应'],['image_count','返回图片数'],['failed_requests','失败'],['unknown_requests','结果未确认'],['retry_requests','重试调用']]:[['requests','合成请求'],['submitted_chars','提交字数'],['generated_chars','成功合成字数'],['preview_chars','其中试听字数'],['failed_requests','失败'],['unknown_requests','结果未确认'],['retry_requests','重试调用']];
    const dimension={app:'用户账号',user:'企业微信成员 ID',day:'日期（北京时间）',model:type==='tts'?'模型或音色':'模型',purpose:'调用用途'}[result.group];
    $('usage-head').replaceChildren(el('th','应用'),el('th',dimension),...columns.map(([,label])=>el('th',label)));
    const period=result.start+' 至 '+result.end+' · ';
    $('usage-summary').textContent=period+(type==='token'?tokenNumber(totals.requests)+' 次模型请求 · 已知总量 '+tokenNumber(totals.total_tokens)+' token':type==='image'?tokenNumber(totals.requests)+' 次生图调用 · '+tokenNumber(totals.successful_requests)+' 次成功响应 · 返回 '+tokenNumber(totals.image_count)+' 张图片':tokenNumber(totals.requests)+' 次合成请求 · 提交 '+tokenNumber(totals.submitted_chars)+' 字 · 成功合成 '+tokenNumber(totals.generated_chars)+' 字（含试听 '+tokenNumber(totals.preview_chars)+' 字）');
    const quality=[];
    if(type==='token'&&totals.missing_requests)quality.push(tokenNumber(totals.missing_requests)+' 次用量不完整，实际总量可能更高');
    if(totals.failed_requests)quality.push(tokenNumber(totals.failed_requests)+(type==='token'?' 次失败、取消或中断的请求也保留记录':' 次接口失败，已保留请求记录'));
    if(totals.unknown_requests)quality.push(tokenNumber(totals.unknown_requests)+' 次结果未确认，服务商可能已产生消耗');
    if(totals.retry_requests)quality.push(tokenNumber(totals.retry_requests)+' 次重试已包含在总调用次数中');
    if(totals.pending_requests)quality.push(tokenNumber(totals.pending_requests)+' 次正在请求，完成后刷新可查看');
    if(result.unavailable.length)quality.push('应用 '+result.unavailable.join('、')+' 暂无法读取，请稍后重试');
    if(result.unattributed_requests)quality.push(tokenNumber(result.unattributed_requests)+' 次尚未归属具体成员');
    $('usage-quality').textContent=quality.join('；');
    $('usage-method').textContent=type==='token'?'一条回复可能包含多次模型请求。主动联系、识图、人物卡解析、回复修复等均计入；缓存和推理 token 是输入或输出中的子项。总量仅累计服务商已返回的用量，“—”表示未提供。':type==='image'?'按实际发出的生图请求计次；成功响应和返回图片数不代表已送达微信。失败、超时和重试保留记录；重试是总次数的子集。仅返回异步任务的请求记为结果未确认。':'按实际提交语音接口的文字计数，包含标点、空格与换行，每个 Unicode 字符计 1 字；服务商计费规则可能不同。长语音分段、音色回退和重试分别计入，重试是总次数的子集。成功合成包含试听，可按调用用途区分；缓存播放及已缓存消息的重发不增加合成字数。';
    body.replaceChildren();
    for(const item of result.items){const row=el('tr');row.append(el('td',String(item.agent)),el('td',result.group==='app'?(item.account_name||'未关联网站账号'):result.group==='purpose'?(type==='token'?(purposeNames[item.dimension]||item.dimension):({preview:'音色试听',chat:'聊天语音',generation:'图片生成'}[item.dimension]||item.dimension)):item.dimension||'未归属成员'));
      for(const [key] of columns)row.append(el('td',tokenNumber(item[key]),'number'));body.append(row);}
    $('usage-message').hidden=!!result.items.length;$('usage-table-wrap').hidden=!result.items.length;
    if(!result.items.length)$('usage-message').textContent=result.ledger_apps.length?'所选范围暂无此类请求记录。可调整用量类型或扩大日期范围后查询。':'当前应用尚未启用此类用量账本或还没有记录。后续实际接口请求会显示在这里。';
    $('usage-page').textContent='第 '+result.page+' 页 · 共 '+tokenNumber(result.count)+' 组';
    $('usage-prev').disabled=result.page<=1;$('usage-next').disabled=result.page*result.page_size>=result.count;
    $('usage-since').textContent=(result.first_recorded?'首次记录：'+date(result.first_recorded)+'。':'从功能启用后开始记录。')+'此类功能启用之前的历史用量无法准确补算，恢复聊天备份不会回退累计用量。';
  }catch(error){if(sequence!==usageSequence)return;body.replaceChildren();$('usage-message').hidden=true;$('usage-summary').textContent='用量查询未完成';$('usage-error').textContent=error.message;$('usage-error').hidden=false;}
  finally{if(sequence===usageSequence)$('usage-query').disabled=false;}
}
const $=id=>document.getElementById(id);
const labels={task_attention:'核对维护提醒',delete_prepare:'准备删除应用',delete_finalize:'人工确认后移除应用',global_defaults:'保存全局默认设置',reclaim:'回收至应用池',start:'启动应用',stop:'停止应用',restart:'重启应用',backup:'创建备份',restore:'恢复备份',release:'更新版本',configure:'保存配置'};
const roleNames={superadmin:'超级管理员',operator:'运维管理员',viewer:'只读管理员',member:'普通成员'};
const statusNames={pending:'等待执行',running:'执行中',done:'已完成',failed:'部分或全部失败',interrupted:'操作中断，需核对'};
let optionSchema=[];
const optionField=key=>optionSchema.find(field=>field.key===key);
let csrf='',identity=null,apps=[],selected=new Set(),currentAgent=null,currentSettings=null,pending=null,poll=0,refreshing=false,configDirty=false,releaseVersion='';
const writeAllowed=()=>identity&&identity.role!=='viewer';
function el(tag,text,cls){const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(cls)node.className=cls;return node;}
function date(value){return value?new Date(value*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'—';}
function note(text,error=false){$('notice').textContent=text;$('notice').className='notice'+(error?' error':'');$('notice').hidden=!text;}
async function request(path,data,portal=false){
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),32000);
  try{const options={credentials:'same-origin',signal:controller.signal,headers:{}};
    let writeToken=csrf;
    if(data!==undefined&&identity&&!(portal&&path==='login')){
      const sessionResponse=await fetch('/ai/api/me',{credentials:'same-origin',cache:'no-store',signal:controller.signal});
      const session=await sessionResponse.json();
      if(!sessionResponse.ok)throw new Error(session.error||'无法更新页面验证，请稍后重试。');
      if(!session.authenticated){dashboardCache.clear();$('shell').hidden=true;$('gate').hidden=false;const failure=new Error('登录已失效，请重新登录。');failure.status=401;throw failure;}
      if(session.username!==identity.username)throw new Error('登录账号已变更，请刷新后台后再操作。');
      if(!session.csrf)throw new Error('无法更新页面验证，请刷新后台后重试。');
      writeToken=csrf=session.csrf;
    }
    if(data!==undefined){options.method='POST';options.headers={'Content-Type':'application/json','X-Requested-With':'xiaoyi-portal','X-CSRF-Token':writeToken};options.body=JSON.stringify(data);}
    if(!portal)options.headers['X-Xingyu-Enterprise']=enterprise;
    const response=await fetch((portal?'/ai/api/':'/ai/admin/api/')+path,options),value=await response.json();
    if(!response.ok){if(response.status===401){dashboardCache.clear();$('shell').hidden=true;$('gate').hidden=false;}const failure=new Error(value.error||'操作未完成，请刷新后重试。');failure.status=response.status;throw failure;}if(data!==undefined&&!portal&&!['templates/analyze-reference','templates/parse-card','templates/voice-preview'].includes(path)){invalidateDashboard();if(embeddedEnterprise)window.parent.postMessage({type:'xingyu-enterprise-changed',enterprise},location.origin);}return value;
  }catch(error){if(error.name==='AbortError')throw new Error(path==='templates/voice-preview'?'试听尚未准备好，请稍后重试。':path==='templates/parse-card'?'识别超时，表单和原文已保留，可稍后重试。':path.startsWith('operations/')?'资料读取超时，请重新读取。':path.startsWith('wallet')?'模拟操作结果尚未确认，请刷新流水核对。':path.startsWith('token-usage')?'用量读取超时，请重新查询。':'请求超时。操作可能已提交，请先查看维护任务。');if(error instanceof TypeError)throw new Error('连接中断，请检查网络后重试。');throw error;}finally{clearTimeout(timer);}
}
async function action(button,fn){button.disabled=true;try{await fn();}catch(error){note(error.message,true);}finally{button.disabled=!writeAllowed()&&button.classList.contains('write');}}
// Organize existing screens by the administrator's task; routes remain stable.
const adminSections=[{"key": "overview", "label": "运营总览", "views": ["dashboard", "business"]}, {"key": "users", "label": "用户与应用", "views": ["accounts", "apps", "onboarding"]}, {"key": "billing", "label": "账务与用量", "views": ["starcoin", "payments", "usage", "shadow", "prices"]}, {"key": "content", "label": "人格与内容", "views": ["templates", "public-stickers", "defaults"]}, {"key": "system", "label": "系统管理", "views": ["jobs", "audit"]}];
const sectionLastView={};
function syncAdminNavigation(name){
 const current=({customer:'accounts',detail:'apps',pilot:'apps',profile:'starcoin'}[name]||name);
 const section=adminSections.find(item=>item.views.includes(current));
 document.querySelectorAll('#primary-navigation [data-section]').forEach(button=>{
  if(button.dataset.section===section?.key)button.setAttribute('aria-current','location');else button.removeAttribute('aria-current');
 });
 $('section-navigation').hidden=!section;
 document.querySelectorAll('#section-navigation [data-nav-section]').forEach(group=>group.hidden=group.dataset.navSection!==section?.key);
 document.querySelectorAll('#section-navigation [data-view]').forEach(button=>{
  if(button.dataset.view===current)button.setAttribute('aria-current','page');else button.removeAttribute('aria-current');
 });
 if(section){sectionLastView[section.key]=current;$('section-navigation').setAttribute('aria-label',section.label+'内页面');}
 if(!embeddedEnterprise)requestAnimationFrame(()=>document.querySelector('#section-navigation [aria-current="page"]')?.scrollIntoView({block:'nearest',inline:'nearest'}));
}
function bindSectionNavigation(){
 document.querySelectorAll('#primary-navigation [data-section]').forEach(button=>button.onclick=()=>{
  const section=adminSections.find(item=>item.key===button.dataset.section);
  const name=sectionLastView[section.key]||section.views[0];
  const target=document.querySelector('#section-navigation [data-view="'+name+'"]');
  if(target&&!target.hidden)target.click();
 });
}

function view(name){if(document.body.dataset.adminView==='public-stickers'&&name!=='public-stickers')poolLeave();if(name!=='scope')clearEnterprisePanels();if(document.body.dataset.adminView==='prices'&&name!=='prices')priceSequence++;if(name==='wallet')$('nav-history').open=true;if(document.body.dataset.adminView==='templates'&&name!=='templates'&&!$('template-editor').hidden)templateOpen(templateRecord);document.body.dataset.adminView=name;if(name!=='scope')saveRoute(name);closeMenu();$('enterprise-scope').textContent=['accounts','customer','templates','public-stickers'].includes(name)?'网站账号与权限 · 跨企业视图':enterprise==='all'?'全部企业 · 汇总视图':enterpriseLabel()+' · 当前企业资料';$('current-view-label').textContent=navTitle(name);document.querySelectorAll('main>section').forEach(section=>section.hidden=section.id!=='view-'+name);document.querySelectorAll('nav button').forEach(button=>{if(button.dataset.view===name)button.setAttribute('aria-current','page');else button.removeAttribute('aria-current');});syncAdminNavigation(name);if(!embeddedEnterprise)$('main').scrollIntoView({block:'start'});else if($('loading').hidden)window.frameElement.scrollIntoView({block:'start'});requestAnimationFrame(()=>{const heading=$('view-'+name)?.querySelector('h1');if(heading&&(!embeddedEnterprise||$('loading').hidden)){heading.tabIndex=-1;heading.focus({preventScroll:true});}});}
function bindViews(){document.querySelectorAll('nav button[data-view]').forEach(button=>button.onclick=()=>action(button,async()=>{if(poolBusy||priceSaving||enterpriseOperationsBusy()){note('请先完成或取消正在进行的操作。');return;}if(hasUnsavedChanges()&&!confirm('放弃尚未保存的设置？'))return;configDirty=false;clearTimeout(poll);if(requireEnterprise(button.dataset.view))return;view(button.dataset.view);if(reportLoaders[button.dataset.view])await reportLoaders[button.dataset.view](true);if(button.dataset.view==='templates')await loadTemplates();if(button.dataset.view==='public-stickers')await poolRead();if(button.dataset.view==='apps')await loadApps();if(button.dataset.view==='defaults')await loadDefaults();if(button.dataset.view==='usage')await loadUsage(true);if(button.dataset.view==='shadow')await loadShadow(true);if(button.dataset.view==='prices')await loadPrices();if(button.dataset.view==='starcoin')await loadStarcoins();if(button.dataset.view==='wallet')await loadWallets();if(button.dataset.view==='accounts')await loadAccounts();if(button.dataset.view==='jobs')await loadJobs();if(button.dataset.view==='audit')await loadAudit();}));}
function filtered(){const pool=$('pool-filter').value;const term=$('search').value.trim().toLowerCase(),filter=$('status-filter').value;return apps.filter(a=>(pool==='all'||pool==='independent'&&a.independent||pool==='assigned'&&a.pool_state==='assigned'||pool==='free'&&a.pool_state==='free'||pool==='attention'&&['failed','provisioning','disabled'].includes(a.pool_state))&&(!term||[a.agent,a.name,a.owner,a.owner_display,a.owner_account].join(' ').toLowerCase().includes(term))&&(filter==='all'||filter==='running'&&a.running||filter==='stopped'&&!a.running||filter==='error'&&(a.unavailable||a.error_count||a.running&&!a.healthy)));}
function stateLabel(a){if(a.lifecycle?.state==='reclaiming')return '资料回收中';if(['paused','expired'].includes(a.lifecycle?.state))return a.lifecycle.state==='paused'?'运行中 · AI 已暂停':'资料待回收';return a.unavailable?'读取异常':!a.running?'已停止':!a.healthy?'启动检查中':a.ai_enabled?'运行中 · AI 开启':'运行中 · 仅验证';}
function renderApps(){
  saveRoute('apps',true);
  const body=$('app-rows');body.replaceChildren();const shown=filtered();
  if(!shown.length){const row=el('tr'),cell=el('td','没有符合条件的应用。可清空筛选后重试。','empty');cell.colSpan=7;row.append(cell);body.append(row);}
  for(const a of shown){const row=el('tr'),check=el('input');check.type='checkbox';check.disabled=!!a.independent;check.checked=selected.has(a.agent);check.setAttribute('aria-label','选择应用 '+a.agent);check.onchange=()=>{check.checked?selected.add(a.agent):selected.delete(a.agent);selection();};const checkbox=el('td');checkbox.append(check);row.append(checkbox);
    let cell=el('td');cell.append(el('strong',a.name||'未命名应用'),el('small',String(a.agent),'number'));row.append(cell);
    cell=el('td');cell.append(el('span',stateLabel(a),'status '+(a.unavailable?'error':a.running&&a.healthy?'ok':a.running?'warn':'')));row.append(cell);
    row.append(el('td',a.owner_display||(a.owner?'已绑定成员':'')||(a.pool_state==='shared'?'共享应用':({free:'待分配',failed:'开通失败',disabled:'已停用',provisioning:'开通中'}[a.pool_state]||'未绑定'))));
    cell=el('td');cell.append(el('span',a.model||'未配置模型'),el('small',(a.source_version||a.version||'版本未知')+(a.source_hash?' · '+a.source_hash:'')));row.append(cell);
    cell=el('td');cell.append(el('span','待处理 '+((a.queue?.pending||0)+(a.queue?.working||0))),el('small','失败 '+(a.error_count||0)));row.append(cell);
    cell=el('td');const open=el('button',a.independent?'查看试点':'管理','secondary');open.onclick=()=>action(open,()=>a.independent?openPilot(a):openDetail(a.agent));cell.append(open);row.append(cell);body.append(row);
  }selection();
}
function selection(){const visible=filtered().filter(a=>!a.independent),count=selected.size;$('selection').textContent=count?'已选择 '+count+' 个应用':'未选择应用';document.querySelectorAll('.batch-op,#batch-config').forEach(button=>button.disabled=!count||!writeAllowed());$('select-all').checked=!!visible.length&&visible.every(a=>selected.has(a.agent));$('select-all').indeterminate=visible.some(a=>selected.has(a.agent))&&!$('select-all').checked;}
async function loadApps(){if(refreshing)return;refreshing=true;$('refresh').disabled=true;try{const result=await request('operations/inventory');apps=result.items;releaseVersion=result.release;selected=new Set([...selected].filter(a=>apps.some(item=>item.agent===a&&!item.independent)));const running=apps.filter(a=>a.running).length;$('summary').textContent=apps.length+' 个应用，'+running+' 个运行中，'+(apps.length-running)+' 个已停止';$('updated').textContent='最近读取 '+date(result.timestamp);renderApps();}finally{refreshing=false;$('refresh').disabled=false;}}
function renderOptions(container,values,batch=false,schema=optionSchema){
 const parent=$(container);parent.replaceChildren();let group='',section=null,advanced=null;
 for(const field of schema){
  if(batch&&field.key==='voice')continue;
  if(field.group!==group){group=field.group;section=el('section',undefined,'app-options-group');section.append(el('h3',group));parent.append(section);advanced=null;}
  let target=section;if(field.advanced){if(!advanced){advanced=el('details',undefined,'app-options-advanced');advanced.append(el('summary','高级设置'));section.append(advanced);}target=advanced;}
  const row=el('div',undefined,batch?'batch-field':'app-option'),label=el('label',field.label),id=container+'-'+field.key;
  const input=el(field.type==='number'||field.type==='boolean'&&!batch?'input':'select');input.id=id;input.dataset.option=field.key;input.dataset.optionType=field.type;label.htmlFor=id;
  if(field.type==='boolean'&&!batch){input.type='checkbox';input.checked=values[field.key]??field.default;label.className='toggle';label.prepend(input);}
  else if(field.type==='number'){input.type='number';input.min=field.min;input.max=field.max;input.step=field.step;input.value=values[field.key]??field.default;input.required=true;}
  else defaultsFillChoices(input,field,values[field.key]??field.default);
  if(batch){const include=el('input');include.type='checkbox';include.dataset.include=field.key;include.setAttribute('aria-label','修改'+field.label);input.disabled=true;include.onchange=()=>input.disabled=!include.checked;row.append(include,label,input);}
  else{row.append(label);if(field.type!=='boolean')row.append(input);input.disabled=!writeAllowed();}
  if(field.help){const help=el('small',field.help);help.id=id+'-help';input.setAttribute('aria-describedby',help.id);row.append(help);}
  if(target===section&&advanced)section.insertBefore(row,advanced);else target.append(row);
 }
 if(!batch){const quiet=$(container+'-quiet_enabled');const update=()=>{for(const key of ['quiet_start','quiet_end']){const input=$(container+'-'+key);if(input)input.disabled=!writeAllowed()||!quiet.checked;}};if(quiet){quiet.addEventListener('change',update);update();}}
}
function readOptions(container,batch=false){const values={};for(const input of $(container).querySelectorAll('[data-option]')){const key=input.dataset.option;if(batch&&!$(container).querySelector('[data-include="'+key+'"]').checked)continue;values[key]=input.dataset.optionType==='boolean'?(input.type==='checkbox'?input.checked:input.value==='true'):input.dataset.optionType==='number'?Number(input.value):input.value;}return values;}

function renderOwnership(value){
  const area=$('detail-ownership');area.replaceChildren();
  const fields=[['归属成员',value.owner_display||(value.owner?'已绑定成员':'未绑定')],['网站账号',value.owner_account||(value.owner?'未关联网站账号':'—')],['企业微信 UserID',value.owner||'—']];
  for(const [label,text] of fields){const row=el('div');row.append(el('dt',label),el('dd',text));area.append(row);}
}
async function openDetail(agent){if(hasUnsavedChanges()&&!confirm('放弃尚未保存的设置？'))return;const result=await request('apps/'+agent);currentAgent=agent;currentSettings=result;configDirty=false;view('detail');const app=apps.find(a=>a.agent===agent);$('detail-title').textContent=app?.name||'应用设置';$('detail-subtitle').textContent='应用 '+agent+' · '+(app?stateLabel(app):'');renderOwnership(result.ownership||{});$('model').value=result.model;$('base-url').value=result.base_url;$('api-key').value='';$('ai-enabled').checked=result.ai_enabled;$('key-status').textContent=result.api_key_configured?'已配置密钥，留空即可保留。':'当前未配置模型密钥。';$('apply-existing').checked=false;optionSchema=result.schema||identity.options_schema;renderOptions('detail-options',result.options,false,optionSchema);if(result.voice_unavailable)$('detail-options').append(el('p','音色目录暂不可读，当前音色已保留。可重新读取后再选择音色。','footnote'));
  $('config-form').querySelectorAll('input,select').forEach(input=>{if(!writeAllowed())input.disabled=true;input.oninput=input.onchange=()=>configDirty=true;});$('config-form').querySelectorAll('button').forEach(button=>button.disabled=!writeAllowed());document.querySelectorAll('.detail-op').forEach(button=>button.disabled=!writeAllowed());
  renderAppDeletion(result.deletion);
  $('reclaim-button').disabled=!writeAllowed()||!result.reclaim?.allowed;$('reclaim-status').textContent=result.reclaim?.reason||'暂时无法核对回收条件，请重新读取。';
  $('events').replaceChildren();if(!result.events.length)$('events').append(el('div','暂无处理记录。','empty'));for(const event of result.events){const row=el('div');row.append(el('span',({done:'已完成',failed:'失败',error:'发送异常',pending:'等待处理'}[event.status]||event.status)+' · '+event.count+' 次'),el('small','最近 '+date(event.latest)));$('events').append(row);}
  $('backups').replaceChildren();if(!result.backups.length)$('backups').append(el('div','尚无后台备份。首次配置修改和版本更新会自动创建，也可手动备份。','empty'));for(const backup of result.backups){const row=el('div'),info=el('div');info.append(el('span',date(backup.created)+' · '+backup.reason),el('small',backup.id));const restore=el('button','恢复此备份','secondary write');restore.disabled=!writeAllowed();restore.onclick=()=>confirmOperation('restore',[agent],{backup:backup.id});row.append(info,restore);$('backups').append(row);}
}
function confirmOperation(operation,agents,data={}){if(!writeAllowed())return;if(configDirty&&operation!=='configure'&&!confirm('继续维护操作会离开设置页，是否放弃未保存的修改？'))return;pending={operation,agents:[...agents],data,request_id:crypto.randomUUID().replaceAll('-',''),confirm:true};$('protected-confirm-label').hidden=operation!=='reclaim'||!currentSettings?.reclaim?.requires_superadmin;$('protected-confirm').checked=false;$('confirm-title').textContent=labels[operation];$('confirm-message').textContent='';$('confirm-run').disabled=false;$('confirm-run').textContent='确认执行';$('restore-confirm').value='';$('confirm-restore-label').hidden=!['restore','reclaim'].includes(operation);$('confirm-id-label').textContent=operation==='reclaim'?'请输入应用 ID，确认清理并解除绑定':'请输入应用 ID，确认覆盖数据库';$('confirm-run').classList.toggle('danger',operation==='reclaim');const descriptions={reclaim:'回收会停止当前用户的使用，清理该应用的聊天、设置、媒体资料及待发送任务，并解除所选应用的绑定。完成后应用关闭 AI 回复，返回待分配。',stop:'停止后，对应应用将暂时无法接收和回复聊天。',restart:'重启期间应用会短暂断开，已有设置与聊天保留。',start:'仅启动服务，不会自动开启 AI 或绕过应用开通。',backup:'在服务器创建一致性数据库和代码、配置备份。',release:'更新到服务器已验证的版本。保留已有用户设置及原来的启停状态。',restore:'恢复会覆盖该备份之后的聊天、配置与代码；执行前会再创建一份恢复前快照。',configure:'保存前会自动备份。修改模型配置会短暂重建对应服务。'};$('confirm-description').textContent=descriptions[operation];$('confirm-summary').replaceChildren(el('p','当前企业：'+enterpriseLabel()),el('p','目标应用：'+agents.join('、')));
  if(operation==='reclaim'){
    const owner=currentSettings.ownership||{};
    $('confirm-summary').append(el('p','当前归属：'+(owner.owner_display||'已绑定成员')),el('p','网站账号：'+(owner.owner_account||'未关联')),el('p','企业微信 UserID：'+(owner.owner||'—')),el('p','执行前会单独保存回收备份；备份不提供给下一位用户，也不能在这里直接恢复到已重新分配的应用。'),el('p',currentSettings.reclaim?.preserve_member?'本次保留企业成员身份和其他应用的数据、网站绑定及访问权限；仅移除所选应用的部门归属。自动回收保护不变。':'自动开通的普通用户如已无其他应用数据，可能被移出企业；仍有其他应用时保留成员及其他应用绑定。'));
    $('confirm-run').textContent='确认回收';$('confirm-run').disabled=true;
  }
  if(operation==='release')$('confirm-summary').append(el('p','服务器已验证版本：'+(releaseVersion||currentSettings?.version||'请重新读取应用状态')));
  if(operation==='restore')$('confirm-summary').append(el('p','备份：'+data.backup));
  if(operation==='configure'){for(const [k,v]of Object.entries(data.options||{}))$('confirm-summary').append(el('p',(optionField(k)?.label||k)+'：'+(optionField(k)?defaultsLabel(optionField(k),v):v)));for(const [k,title]of [['model','模型名称'],['base_url','服务地址'],['ai_enabled','AI 回复']])if(k in data)$('confirm-summary').append(el('p',title+'：'+(typeof data[k]==='boolean'?(data[k]?'开启':'关闭'):data[k])));if(data.api_key)$('confirm-summary').append(el('p','模型密钥：替换（不显示内容）'));$('confirm-summary').append(el('p',data.apply_existing?'覆盖已有用户的本次改动规则。':'仅更新默认规则；已有用户保存的选项不覆盖。'));}
  $('confirm-dialog').showModal();}
// Account closure is separate from app maintenance: one member may own several apps.
let closingPreview=null,closingPayload=null,closingBusy=false,accountDeleteEpoch=0;
const closureLabels={pending:'等待注销',running:'正在注销',incomplete:'注销未完成',done:'已注销'};
async function loadClosures(){
  const box=$('account-closures');box.replaceChildren();
  if(identity.role!=='superadmin'){$('account-closure-section').hidden=true;return [];}
  $('account-closure-section').hidden=false;
  try{
    const data=await request('account-deletions');
    if(!data.items.length)box.append(el('p','暂无注销记录。','empty'));
    for(const item of data.items){
      const row=el('article',undefined,'job'),head=el('div',undefined,'job-head');
      head.append(el('strong',item.username),el('span',closureLabels[item.status]||item.status,'status '+(item.status==='done'?'ok':item.status==='incomplete'?'error':'warn')));
      row.append(head,el('p',date(item.updated)+' · '+(item.result||'已受理。注销期间无法登录；处理结果会保存在这里。')));
      if(item.status==='incomplete'){
        const button=el('button','继续注销','danger');button.onclick=()=>action(button,async()=>{
          if(!confirm('继续注销 '+item.username+'？将从保存的进度继续清理，已回收的应用不会重复操作。'))return;
          await request('account-deletions/retry',{closure:item.id,confirm:true});note('已提交继续注销。请刷新查看处理结果。');await loadAccounts();
        });row.append(button);
      }
      box.append(row);
    }
    return data.items;
  }catch(e){box.append(el('p',e.message+' 可点击“刷新成员”重试。','notice error'));throw e;}
}
async function openAccountDeletion(account){
  const epoch=++accountDeleteEpoch;
  closingPreview=null;closingPayload=null;$('account-delete-error').textContent='';
  $('account-delete-name').textContent=account.username;$('account-delete-summary').replaceChildren();
  $('account-delete-entry').hidden=true;$('account-delete-run').hidden=true;
  $('account-delete-state').textContent='正在核对应用归属和企业微信成员…';$('account-delete-input').value='';
  $('account-delete-dialog').showModal();
  try{
    const p=await request('account-deletions/preview',{account:account.id});
    if(!$('account-delete-dialog').open||epoch!==accountDeleteEpoch)return;
    if(!p.allowed){$('account-delete-state').textContent=p.reason;return;}
    closingPreview=p;$('account-delete-state').textContent='此操作无法撤销。请核对后输入完整账号确认。';
    const box=$('account-delete-summary');
    for(const a of p.apps)box.append(el('p','回收应用：'+(a.name||'应用')+'（'+a.agent+'） · 企业 '+a.corp_id));
    if(!p.apps.length)box.append(el('p','该账号没有绑定应用。'));
    for(const m of p.members||[])box.append(el('p','移出成员：'+(m.name||'未设置姓名')+' · UserID '+m.user_id+' · 企业 '+m.corp_id));
    if(!p.member_count)box.append(el('p','企业微信：没有需要移出的关联成员。'));
    box.append(el('p','清理网站登录、设置链接、聊天、记忆、参考图和个人设置；星币账户同步注销并移出正常账户列表，余额不清零、不自动退款，历史订单和流水保留供查账。'));
    $('account-delete-entry').hidden=false;$('account-delete-run').hidden=false;$('account-delete-run').disabled=true;
    $('account-delete-input').focus();
  }catch(e){if(epoch===accountDeleteEpoch)$('account-delete-error').textContent=e.message+' 请关闭后重新读取。';}
}
function initAccountDeletion(){
  const dialog=$('account-delete-dialog'),close=()=>{if(!closingBusy){accountDeleteEpoch++;dialog.close();closingPreview=null;closingPayload=null;}};
  $('account-delete-close').onclick=close;$('account-delete-cancel').onclick=close;
  dialog.addEventListener('cancel',event=>{if(closingBusy)event.preventDefault();});
  $('account-delete-input').oninput=()=>{
    $('account-delete-run').disabled=closingBusy||!closingPreview||$('account-delete-input').value!==closingPreview.username;
  };
  $('account-delete-run').onclick=async()=>{
    if(!closingPreview||closingBusy||$('account-delete-input').value!==closingPreview.username)return;
    closingBusy=true;$('account-delete-run').disabled=true;$('account-delete-run').textContent='正在提交…';
    $('account-delete-close').disabled=true;$('account-delete-cancel').disabled=true;$('account-delete-error').textContent='';
    closingPayload=closingPayload||{account:closingPreview.account,binding:closingPreview.binding,typed_username:closingPreview.username,confirm:true,request_id:crypto.randomUUID()};
    try{
      await request('account-deletions/submit',closingPayload);dialog.close();closingPreview=null;closingPayload=null;
      note('注销已受理。账号已冻结登录，处理进度见下方“账号注销记录”。');await loadAccounts();
    }catch(e){$('account-delete-error').textContent=e.message+' 提交结果不明时可先关闭窗口、刷新注销记录核对；重复提交使用同一请求编号。';}
    finally{closingBusy=false;$('account-delete-close').disabled=false;$('account-delete-cancel').disabled=false;$('account-delete-run').textContent='确认注销账号';$('account-delete-run').disabled=!closingPreview||$('account-delete-input').value!==closingPreview.username;}
  };
}

function accountBindingText(account){
  if(account.applications?.length)return account.applications.map(app=>app.enterprise_label+' · '+(app.name?app.name+'（'+app.agent+'）':app.agent)+(app.status==='manual_dedicated'?'（人工专属）':'（已分配）')).join('；');
  if(account.binding_status)return ({unallocated:'未分配应用',waiting_pool:'等待应用池分配',opening:'正在开通',needs_inspection:'开通待检查',failed:'开通未完成',reclaimed:'原应用已回收'})[account.binding_status]||'归属待检查';
  return account.agent?String(account.agent):account.waiting?'等待分配':'未绑定';
}

async function loadAccounts(reset=false){
  const result=await listRequest('accounts',reset);if(!result)return;
  const closures=identity.role==='superadmin'?await loadClosures():[];if(result.sequence!==listSequence.accounts)return;$('account-rows').replaceChildren();
  for(const account of result.items){
    const closure=closures.find(v=>v.account===account.id&&v.status!=='done');
    const row=el('tr');row.append(el('td',account.username),el('td',accountBindingText(account)),el('td',roleNames[account.role]||account.role),el('td',date(account.created),'number'));
    const td=el('td');
    if(identity.role==='superadmin'&&!closure){
      const select=el('select');select.setAttribute('aria-label',account.username+' 的后台权限');
      for(const [value,label]of Object.entries(roleNames)){const option=el('option',label);option.value=value;select.append(option);}
      select.value=account.role;const save=el('button','保存权限','secondary');save.classList.add('role-save');
      save.onclick=()=>action(save,async()=>{if(!confirm('将 '+account.username+' 的后台权限设为“'+roleNames[select.value]+'”？'))return;await request('roles',{username:account.username,role:select.value});note('权限已更新。');await loadAccounts();});td.append(select,save);
    }else td.textContent=closure?'注销期间不可调整':'仅超级管理员可调整';
    row.append(td);const manage=el('td');
    if(closure)manage.append(el('span',closureLabels[closure.status]||'注销中','status warn'));
    else if(identity.role==='superadmin'&&account.role==='member'){
      const remove=el('button','注销账号','danger');remove.onclick=()=>openAccountDeletion(account);manage.append(remove);
    }else manage.textContent=account.role!=='member'?'管理员受保护':'仅超级管理员可注销';
    const detail=el('button','查看详情','secondary');detail.onclick=()=>openCustomer(account.id);manage.prepend(detail);row.append(manage);for(const [i,cell]of [...row.children].entries())cell.dataset.label=['网站账号','应用归属','后台权限','注册时间','权限调整','账号管理'][i];$('account-rows').append(row);
  }
  if(!result.items.length){const row=el('tr'),td=el('td','没有符合条件的成员，可调整筛选后查询。','empty');td.colSpan=6;row.append(td);$('account-rows').append(row);}pageControls('accounts',result);saveRoute('accounts',true);
}

async function loadJobs(reset=false){clearTimeout(poll);await loadAppDeletions();const result=await listRequest('jobs',reset);if(!result)return;pageControls('jobs',result);saveRoute('jobs',true);$('job-rows').replaceChildren();if(!result.items.length)$('job-rows').append(el('p','尚无维护任务。可从全部应用选择操作。','empty'));for(const job of result.items){const item=el('article',undefined,'job'),head=el('div',undefined,'job-head');head.append(el('strong',(labels[job.operation]||job.operation)+' · '+(job.agents.join('、')||'全局模板')),el('span',statusNames[job.status]||job.status,'status '+(job.status==='done'?'ok':job.status==='failed'?'error':'warn')));item.append(head,el('p',job.actor+' · '+date(job.created)));const list=el('ul');for(const result of Array.isArray(job.result)?job.result:[]){let text=(result.agent===0?'全局模板':result.agent)+' · '+(result.ok?'成功':'失败：'+result.error);if(result.result?.deletion_phase)text+=' · '+result.result.message;if(result.result?.template_revision)text+=' · 模板版本 '+result.result.template_revision+' · 新分配应用自动继承';if(result.result?.backup)text+=' · 备份 '+result.result.backup;if(result.result?.reclaim_backup)text+=' · 已回到待分配 · 回收前备份 '+result.result.reclaim_backup+'（隔离保存）';if(result.result?.updated_users!==undefined)text+=' · 同步 '+result.result.updated_users+' 位用户';list.append(el('li',text,result.ok?'':'error'));}if(!Array.isArray(job.result)&&job.result?.message)item.append(el('p',job.result.message));item.append(list);if(['failed','interrupted'].includes(job.status)){if(job.acknowledgement)item.append(el('p','已核对 · '+job.acknowledgement.actor+' · '+date(job.acknowledgement.updated)),el('p',job.acknowledgement.note));if(writeAllowed())item.append(button(job.acknowledgement?'恢复待核对':'填写核对结果',()=>openTaskAcknowledgement(job)));}$('job-rows').append(item);}if(result.items.some(j=>['pending','running'].includes(j.status))&&!$('view-jobs').hidden&&!document.hidden)poll=setTimeout(()=>loadJobs().catch(e=>note(e.message,true)),3500);}
async function loadAudit(reset=false){const result=await listRequest('audit',reset);if(!result)return;pageControls('audit',result);saveRoute('audit',true);$('audit-rows').replaceChildren();for(const item of result.items){const row=el('tr');for(const value of [date(item.created),item.actor,labels[item.operation]||item.operation,item.agents.join('、')||(item.operation==='global_defaults'?'全局模板':'后台权限'),item.result])row.append(el('td',value));$('audit-rows').append(row);}if(!result.items.length){const row=el('tr'),td=el('td','暂无后台操作记录。','empty');td.colSpan=5;row.append(td);$('audit-rows').append(row);}}
async function boot(){try{const me=await request('me',undefined,true);if(!me.authenticated){$('gate').hidden=false;return;}csrf=me.csrf;identity=await request('identity');optionSchema=identity.options_schema;initUsage();initShadow();initWallet();initDefaults();initAccountDeletion();initAppDeletion();$('enterprise-select').value=enterprise;$('identity').textContent=identity.username+' · '+roleNames[identity.role];$('shell').hidden=false;$('gate').hidden=true;document.querySelectorAll('.write').forEach(button=>button.disabled=!writeAllowed());initTemplates();initPublicStickers();initOperations();initListQueries();initRouting();await restoreRoute(true);}catch(error){$('gate').hidden=false;$('gate-error').textContent=error.message;}finally{$('loading').hidden=true;}}
// Keep each operator's location in the URL; never store secrets or form drafts.
const routeDefaults={};
let routeRestoring=false,routeIndex=Number(history.state?.xingyuIndex)||0,routeURL=location.href,routeUndo=false;
const routeFields={apps:['search','status-filter','pool-filter'],onboarding:['onboarding-q','onboarding-state'],payments:['payments-start','payments-end','payments-provider','payments-state','payments-q','payments-user'],business:['business-start','business-end'],starcoin:['starcoin-q','starcoin-state'],accounts:['accounts-q','accounts-role','accounts-enterprise'],jobs:['jobs-q','jobs-agent','jobs-status','jobs-start','jobs-end'],audit:['audit-q','audit-agent','audit-start','audit-end'],usage:['usage-start','usage-end','usage-resource','usage-agent','usage-user','usage-group'],shadow:['shadow-start','shadow-end','shadow-kind','shadow-agent','shadow-user']};
const routeNames=new Set(['dashboard','apps','detail','onboarding','accounts','starcoin','payments','business','usage','prices','shadow','templates','public-stickers','defaults','jobs','audit','wallet','profile','customer']);
function saveRoute(name,replace=false){
 if(routeRestoring||!routeNames.has(name)||(replace&&document.body.dataset.adminView!==name))return;
 const url=new URL(location.href);for(const key of [...url.searchParams.keys()])if(key.startsWith('f_')||['agent','account','wallet','user','page'].includes(key))url.searchParams.delete(key);
 url.hash=name;
 if(name==='detail')url.searchParams.set('agent',currentAgent);
 if(name==='customer')url.searchParams.set('account',customerAccount);
 if(name==='profile'){if(profileWallet)url.searchParams.set('wallet',profileWallet);else if(profileUser)url.searchParams.set('user',profileUser);}
 for(const id of routeFields[name]||[]){const input=$(id);if(input?.value)url.searchParams.set('f_'+id,input.value);}
 const page=name==='usage'?usagePage:name==='shadow'?shadowPage:reportPages[name];if(page>1)url.searchParams.set('page',page);
 if(url.href===location.href){routeURL=location.href;return;}
 const method=replace||embeddedEnterprise?'replaceState':'pushState';if(method==='pushState')routeIndex++;
 history[method]({xingyuIndex:routeIndex},'',url);routeURL=url.href;
}
async function restoreRoute(initial=false){
 if(routeRestoring)return;routeRestoring=true;
 try{
  const params=new URLSearchParams(location.search),name=routeNames.has(location.hash.slice(1))?location.hash.slice(1):'dashboard';
  if(['usage','shadow','wallet'].includes(name)&&enterprise!=='all')await loadApps();
  for(const id of routeFields[name]||[]){const input=$(id),value=params.get('f_'+id)??routeDefaults[id]??'';if(!input)continue;if(['usage-agent','shadow-agent'].includes(id)&&value&&apps.some(a=>String(a.agent)===value)&&![...input.options].some(o=>o.value===value)){const option=el('option',value);option.value=value;input.append(option);}input.value=value;}
  const page=Math.max(1,Math.min(100000,Number(params.get('page'))||1));if(Object.hasOwn(reportPages,name))reportPages[name]=page;if(name==='usage')usagePage=page;if(name==='shadow')shadowPage=page;
  if(name==='detail'){
   if(requireEnterprise('apps'))return;await loadApps();const agent=Number(params.get('agent'));if(!apps.some(app=>app.agent===agent&&!app.independent))throw new Error('当前企业没有这个应用，请返回应用列表核对。');await openDetail(agent);
  }else if(name==='customer'&&Number(params.get('account'))>0)await openCustomer(Number(params.get('account')));
  else if(name==='profile'&&(params.get('wallet')||params.get('user')))await openProfile(params.get('wallet')||'',params.get('user')||'',false);
  else if(name==='templates'){view(name);await loadTemplates();}
  else if(name==='public-stickers'){view(name);await poolRead();}
  else if(name==='defaults'){if(!requireEnterprise(name)){view(name);await loadDefaults();}}
  else if(['customer','profile'].includes(name))throw new Error('此链接缺少用户或账户信息，请从成员列表重新打开。');
  else await go(name,false);
 }catch(error){view('apps');note(error.message,true);}
 finally{routeRestoring=false;routeURL=location.href;if(initial)history.replaceState({xingyuIndex:routeIndex},'',routeURL);}
}
function initRouting(){
 if(window.xingyuRoutingInitialized)return;window.xingyuRoutingInitialized=true;
 for(const [name,ids]of Object.entries(routeFields))for(const id of ids){const input=$(id);if(input){routeDefaults[id]=input.value;for(const event of ['input','change'])input.addEventListener(event,()=>saveRoute(name,true));}}
 window.addEventListener('popstate',async event=>{
  if(routeUndo){routeUndo=false;return;}if(!routeNames.has(location.hash.slice(1)))return;
  const next=Number(event.state?.xingyuIndex)||0;
  if(routeRestoring||priceSaving||enterpriseOperationsBusy()||hasUnsavedChanges()&&!confirm('放弃尚未保存的设置？')){
   if(next!==routeIndex){routeUndo=true;history.go(routeIndex-next);}else history.replaceState({xingyuIndex:routeIndex},'',routeURL);return;
  }
  configDirty=false;routeIndex=next;clearTimeout(poll);await restoreRoute();
 });
}


const listSequence={accounts:0,jobs:0,audit:0};
let taskAckPending=null,taskAckBusy=false;
async function listRequest(name,reset=false){
 const sequence=++listSequence[name];if(reset)reportPages[name]=1;const params={page:reportPages[name]};
 for(const id of routeFields[name]||[]){const key=id.slice(name.length+1);params[key]=$(id).value.trim();}
 const message=$(name+'-query-status');message.textContent='正在读取…';
 try{const result=await request(name+'?'+new URLSearchParams(params));if(sequence!==listSequence[name])return null;result.sequence=sequence;message.textContent='已读取 '+result.count+' 条符合条件的记录 · '+date(Date.now()/1000);return result;}
 catch(error){if(sequence!==listSequence[name])return null;message.textContent=error.message+' 请重新查询。';throw error;}
}
function openTaskAcknowledgement(job){
 taskAckPending=job;$('task-ack-summary').textContent=enterpriseLabel()+' · '+(labels[job.operation]||job.operation)+' · '+job.id;
 $('task-ack-title').textContent=job.acknowledgement?'恢复待核对提醒':'核对维护任务';$('task-ack-submit').textContent=job.acknowledgement?'恢复待核对':'标记已核对';$('task-ack-note').value='';$('task-ack-error').textContent='';$('task-ack-dialog').showModal();$('task-ack-note').focus();
}
function initListQueries(){
 for(const name of ['accounts','jobs','audit']){const load={accounts:loadAccounts,jobs:loadJobs,audit:loadAudit}[name];$(name+'-query-form').onsubmit=event=>{event.preventDefault();action(event.submitter||$(name+'-query-form').querySelector('button[type=submit]'),()=>load(true));};for(const [direction,change]of [['prev',-1],['next',1]])$(name+'-'+direction).onclick=()=>action($(name+'-'+direction),async()=>{reportPages[name]+=change;await load();});}
 $('task-ack-cancel').onclick=()=>{if(!taskAckBusy)$('task-ack-dialog').close();};$('task-ack-dialog').addEventListener('cancel',event=>{if(taskAckBusy)event.preventDefault();});
 $('task-ack-form').onsubmit=async event=>{event.preventDefault();if(!taskAckPending||taskAckBusy)return;taskAckBusy=true;$('task-ack-submit').disabled=true;$('task-ack-cancel').disabled=true;
  try{await request('task-attention',{job:taskAckPending.id,acknowledged:!taskAckPending.acknowledgement,note:$('task-ack-note').value.trim(),expected_updated:taskAckPending.updated});$('task-ack-dialog').close();taskAckPending=null;await loadJobs();}
  catch(error){$('task-ack-error').textContent=error.message+' 备注已保留。';}
  finally{taskAckBusy=false;$('task-ack-submit').disabled=false;$('task-ack-cancel').disabled=false;}
 };
}

// operations-console-v1
const reportLoaded={},reportEpoch={},reportPages={onboarding:1,payments:1,starcoin:1,profile:1,accounts:1,jobs:1,audit:1};
let profileWallet='',profileUser='',customerAccount=0;
const providerNames={wechat:'微信支付',alipay:'支付宝'};
const openingNames={queued:'排队中',provisioning:'正在开通',verifying:'正在校验',awaiting_scan:'已开通',ready:'已开通',needs_inspection:'需要检查',waiting_pool:'等待分配',failed:'开通未完成',cancelled:'已取消',reclaimed:'已回收'};
const stageNames={queued:'等待执行',reserving:'预留应用',creating_member:'创建成员',updating_profile:'更新应用资料',activating:'启动应用',scan_qr:'应用已分配',ready:'完成开通',confirmed:'账号已确认',stopped:'已停止，待检查',interrupted:'执行中断',released:'已回收',waiting_pool:'等待可用应用'};
const paymentNames={prepared:'待支付',awaiting_payment:'待支付',pending:'待支付',paid:'已支付',closed:'已关闭',expired:'已过期'};
const issueNames={missing_credit:'已支付，缺少到账记录',unexpected_credit:'订单状态与到账记录不一致',mismatch:'报价、归属或流水待核对'};
const reportLoaders={dashboard:()=>loadDashboard(),onboarding:loadOnboarding,payments:loadPayments,business:loadBusiness};
const enterpriseLabel=(key=enterprise)=>key==='all'?'总览 · 全部企业':key==='legacy'?'原企业 · 现有用户':key==='ww0000000000000003'?'星屿 02 · 新用户':'星屿 01 · 新用户';
const navTitle=name=>document.querySelector('nav [data-view="'+name+'"]')?.textContent||({profile:'账户账务',customer:'用户详情',detail:'应用设置',pilot:'独立试点'}[name]||'运营概览');
const coin=value=>value==null?'待核对':new Intl.NumberFormat('zh-CN',{maximumFractionDigits:6}).format(Number(value));
const yuan=fen=>new Intl.NumberFormat('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2}).format(Number(fen||0)/100);
function closeMenu(){document.body.dataset.menuOpen='false';$('menu-toggle').setAttribute('aria-expanded','false');}
function emptyLines(box,text){if(!box.children.length)box.append(el('p',text,'empty'));}
function status(text,tone=''){return el('span',text,'status '+tone);}
function line(title,description,button){const row=el('div',undefined,'task-line'),body=el('div');body.append(el('strong',title));if(description)body.append(el('p',description));row.append(body);if(button)row.append(button);return row;}
function button(text,callback,cls='secondary'){const b=el('button',text,cls);b.type='button';b.onclick=()=>action(b,callback);return b;}
function summaryItem(box,title,value,note,unit=''){const item=el('div'),amount=el('dd',String(value));item.append(el('dt',title));if(unit)amount.append(el('span',unit,'unit'));item.append(amount);if(note)item.append(el('small',note));box.append(item);}
function recordValue(box,title,value,detail){const item=el('div'),dd=el('dd',String(value));item.append(el('dt',title),dd);if(detail)dd.append(el('small',detail));box.append(item);}
function safeTableEmpty(id,count,cols,text){if(!count){const row=el('tr'),cell=el('td',text,'empty');cell.colSpan=cols;row.append(cell);$(id).append(row);}}
function pageControls(name,data){reportPages[name]=data.page;$(name+'-page').textContent='第 '+data.page+' 页 · 共 '+data.count+(name==='starcoin'?' 个账户':name==='accounts'?' 位成员':' 笔记录');$(name+'-prev').disabled=data.page<=1;$(name+'-next').disabled=data.page*data.page_size>=data.count;}
async function readReport(name,endpoint,query,render){
  const epoch=reportEpoch[name]=(reportEpoch[name]||0)+1,box=$(name+'-content'),message=$(name+'-status');
  const key=JSON.stringify([enterprise,identity.username,identity.role,query]),prior=reportLoaded[name],keep=prior?.key===key&&!box.hidden;box.hidden=!keep;box.setAttribute('aria-busy','true');message.className='report-status';message.textContent=keep?'正在更新… 当前显示 '+date(prior.time)+' 的数据。':'正在读取'+navTitle(name)+'…';
  const controls=[...$('view-'+name).querySelectorAll('button[id$="-refresh"],button[type="submit"],button[id$="-prev"],button[id$="-next"]')];controls.forEach(b=>b.disabled=true);
  try{const data=await request('operations/'+endpoint+'?'+new URLSearchParams(query));if(epoch!==reportEpoch[name])return;render(data);reportLoaded[name]={key,time:Date.now()/1000};box.hidden=false;message.textContent='更新于 '+date(reportLoaded[name].time);saveRoute(name,true);}
  catch(e){if(epoch!==reportEpoch[name])return;message.className='report-status error';const retained=keep&&![401,403].includes(e.status);box.hidden=!retained;if(!retained)delete reportLoaded[name];message.textContent=e.message+(retained?' 当前仍显示 '+date(prior.time)+' 的数据，请重新读取。':' 请点击重新读取或调整条件后查询。');}
  finally{if(epoch===reportEpoch[name]){box.setAttribute('aria-busy','false');controls.filter(b=>!b.id.endsWith('-prev')&&!b.id.endsWith('-next')).forEach(b=>b.disabled=false);}}
}
async function go(name,reset=false){if(poolBusy||priceSaving||enterpriseOperationsBusy()){note('请先完成或取消正在进行的操作。');return;}if(hasUnsavedChanges()&&!confirm('放弃尚未保存的设置？'))return;configDirty=false;clearTimeout(poll);if(requireEnterprise(name))return;view(name);const fn=reportLoaders[name]||({apps:loadApps,accounts:loadAccounts,starcoin:loadStarcoins,jobs:loadJobs,audit:loadAudit,shadow:()=>loadShadow(true),prices:loadPrices,wallet:loadWallets,usage:()=>loadUsage(true)}[name]);if(fn)await fn(reset);}
function scopeLink(name,key,values={}){const url=new URL(location.href);url.search='';url.searchParams.set('enterprise',key);for(const [k,v] of Object.entries(values))url.searchParams.set(k,v);url.hash=name;return url.href;}
function channelLines(id,channels){const box=$(id);box.replaceChildren();for(const c of channels){const item=el('div',undefined,'channel-line'),head=el('div',undefined,'channel-head');const open=c.readable&&c.enabled&&c.public_enabled;head.append(el('strong',providerNames[c.provider]||c.provider),status(!c.readable?'配置未读到':open?'充值已开放':'充值未开放',!c.readable?'warn':open?'ok':''));item.append(head,el('p','服务：'+({active:'运行中',inactive:'未运行',unavailable:'暂不可读',unknown:'待核对'}[c.service]||'待核对')));item.append(el('small',c.amounts_fen?.length?'已配置档位：'+c.amounts_fen.map(n=>yuan(n)).join(' / ')+' 元':'尚未读取到开放档位'));box.append(item);}emptyLines(box,'通道状态暂不可读，请重新读取。');}
function requireEnterprise(name){if(name==='wallet')$('nav-history').open=true;
  if(enterprise!=='all'||!enterprisePanelViews.has(name))return false;
  showEnterprisePanels(name);return true;
}
function combinedDashboard(result){
  const available=result.enterprises.filter(item=>item.available),datasets=available.map(item=>item.data);
  const sumMap=key=>datasets.reduce((out,data)=>{for(const [k,v] of Object.entries(data[key]))out[k]=(out[k]||0)+v;return out;},{});
  return {inventory:{items:available.flatMap(item=>item.data.inventory.items.map(app=>({...app,enterprise:item.key}))),release:''},onboarding_counts:sumMap('onboarding_counts'),billing_attention:sumMap('billing_attention'),payment_attention:datasets.reduce((n,d)=>n+d.payment_attention,0),wallet_count:datasets.reduce((n,d)=>n+d.wallet_count,0),maintenance_attention:datasets.flatMap(d=>d.maintenance_attention),maintenance_attention_count:datasets.reduce((n,d)=>n+(d.maintenance_attention_count??d.maintenance_attention.length),0),media_attention:{open_count:datasets.reduce((n,d)=>n+(d.media_attention?.open_count||0),0),readable:datasets.every(d=>d.media_attention?.readable!==false),items:available.flatMap(e=>(e.data.media_attention?.items||[]).map(a=>({...a,enterprise:e.key})))},recent_applications:available.flatMap(item=>item.data.recent_applications.map(job=>({...job,enterprise:item.key}))).sort((a,b)=>b.created-a.created).slice(0,8),channels:(available.find(item=>item.key==='legacy')||available[0]).data.channels,timestamp:result.timestamp};
}
function enterpriseBreakdown(result){
  $('enterprise-breakdown').hidden=false;const box=$('enterprise-breakdown-rows');box.replaceChildren();
  for(const item of result.enterprises){
    const row=el('div',undefined,'enterprise-summary'),head=el('div',undefined,'section-heading'),link=el('a','查看企业');
    link.href=scopeLink('dashboard',item.key);head.append(el('h3',enterpriseLabel(item.key)),link);row.append(head);
    if(!item.available){row.append(el('p','资料读取失败 · 此企业未计入汇总，请刷新重试。','error'));box.append(row);continue;}
    const data=item.data,items=data.inventory.items,grid=el('dl',undefined,'enterprise-numbers');
    for(const [label,value] of [['应用总数',items.length],['运行正常',items.filter(a=>a.running&&a.healthy).length],['待分配',items.filter(a=>!a.independent&&a.pool_state==='free').length],['开通申请',Object.values(data.onboarding_counts).reduce((n,v)=>n+v,0)],['星币账户',data.wallet_count]])recordValue(grid,label,value);
    row.append(grid);box.append(row);
  }
}
// Only this tab retains overview data; billing and member detail pages stay live.
const dashboardCache=new Map(),dashboardPending=new Map();
const dashboardKey=()=>JSON.stringify([enterprise,identity?.username,identity?.role]);
function invalidateDashboard(){for(const value of dashboardCache.values())value.receivedAt=0;}
function dashboardStatus(text,tone=''){
  $('dashboard-status').textContent=text;$('dashboard-status').className='report-status'+(tone?' '+tone:'');
}
async function loadDashboard(force=false){
  const key=dashboardKey(),cached=dashboardCache.get(key),visible=()=>dashboardKey()===key&&document.body.dataset.adminView==='dashboard';
  const show=()=>{if(!visible())return;if(cached){renderDashboard(cached.data);$('dashboard-content').hidden=false;}else $('dashboard-content').hidden=true;};
  show();
  if(dashboardPending.has(key)){
    if(visible())dashboardStatus(cached?'正在更新… 当前显示 '+date(cached.data.timestamp)+' 的数据。':'正在读取运营概览…');
    return cached?undefined:dashboardPending.get(key);
  }
  if(cached&&!force&&Date.now()-cached.receivedAt<(cached.data.partial?5000:30000)){
    if(visible())dashboardStatus('更新于 '+date(cached.data.timestamp));return;
  }
  if(visible()){
    dashboardStatus(cached?'正在更新… 当前显示 '+date(cached.data.timestamp)+' 的数据。':'正在读取运营概览…');
    $('dashboard-refresh').disabled=true;$('dashboard-status').setAttribute('aria-busy','true');
  }
  const task=(async()=>{
    try{
      const data=await request('operations/dashboard');
      if(dashboardKey()!==key)return;
      dashboardCache.set(key,{data,receivedAt:Date.now()});
      if(visible()){renderDashboard(data);$('dashboard-content').hidden=false;dashboardStatus('更新于 '+date(data.timestamp));}
    }catch(error){
      if([401,403,409].includes(error.status)){
        dashboardCache.clear();if(visible()){$('dashboard-content').hidden=true;dashboardStatus(error.message,'error');}
      }else if(visible()){
        if(cached){$('dashboard-content').hidden=false;dashboardStatus('更新未完成，当前显示 '+date(cached.data.timestamp)+' 的数据。'+error.message+' 点击“刷新概览”重试。','stale');}
        else{$('dashboard-content').hidden=true;dashboardStatus(error.message+' 点击“刷新概览”重试。','error');}
      }
    }finally{
      dashboardPending.delete(key);
      if(dashboardKey()===key){$('dashboard-refresh').disabled=false;$('dashboard-status').setAttribute('aria-busy','false');}
    }
  })();
  dashboardPending.set(key,task);return cached?undefined:task;
}

function renderDashboard(result){
  const isAll=enterprise==='all',partial=isAll&&result.partial,data=isAll?combinedDashboard(result):result;
  $('dashboard-title').textContent=isAll?'全部企业总览':'运营概览';$('attention-scope').textContent=isAll?(partial?'按已读取企业汇总':'按全部企业汇总'):'按当前企业汇总';
  $('enterprise-breakdown').hidden=!isAll;if(isAll)enterpriseBreakdown(result);
  $('overview-warning').hidden=!partial;if(partial)$('overview-warning').textContent='部分企业资料未读到。以下仅汇总已读取企业，未读到的资料不计为零；请刷新重试。';
  apps=data.inventory.items;releaseVersion=data.inventory.release;
  const healthy=apps.filter(a=>a.running&&a.healthy).length,free=apps.filter(a=>!a.independent&&a.pool_state==='free').length,issues=apps.filter(a=>a.unavailable||a.running&&!a.healthy||a.error_count).length;
  $('health-indicator').textContent=partial?'部分企业未读到':issues?'有运行记录需要关注':'运行状态已读取';$('health-indicator').className='status '+(issues||partial?'warn':'ok');
  $('health-title').textContent=healthy+' / '+apps.length+' 个应用运行正常'+(partial?'（已读取企业）':'');$('health-description').textContent='应用池待分配 '+free+' 个 · 独立试点 '+apps.filter(a=>a.independent).length+' 个 · 星币账户 '+data.wallet_count+' 个'+(isAll?'（按企业分别计数）':'');
  $('updated').textContent='最近读取 '+date(data.timestamp);channelLines('dashboard-channels',data.channels);
  const list=$('attention-list');list.replaceChildren();
  const opening=Object.entries(data.onboarding_counts).filter(([k])=>['needs_inspection','failed','waiting_pool'].includes(k)).reduce((n,[,v])=>n+v,0);
  for(const [title,count,description,target]of [['开通申请',opening,'等待分配、开通失败或需要人工检查','onboarding'],['到账核对',data.payment_attention,'支付订单与到账流水需要核对','payments'],['扣费记录',Object.values(data.billing_attention).reduce((n,v)=>n+v,0),'用量待定价或账户余额不足','starcoin'],['应用运行',issues,'应用读取异常、健康检查或失败处理记录','apps'],['维护任务',data.maintenance_attention_count??data.maintenance_attention.length,'近期失败或中断任务，需核对执行结果','jobs']]){
    list.append(line(title+' · '+count+(count?' 项待查看':' 项'),count?description:'当前没有此类待处理事项',button('查看',()=>go(target))));
  }
  const media=data.media_attention;
  const group=el('details'),summary=el('summary');
  summary.textContent='聊天媒体异常 · '+(media?.readable===false?'暂不可读':String(media?.open_count||0)+' 项待查看');
  group.append(summary);group.open=Boolean(media?.open_count||media?.readable===false);
  const reasons={intent_missing:'正文承诺媒体但缺少有效操作',intent_conflict:'媒体决定与正文矛盾',intent_repair_unavailable:'媒体决定校正未完成',capability_unavailable:'本轮媒体能力不可用',fallback_send_unconfirmed:'收尾文字发送结果待确认',selection_unavailable:'已选素材当前不可用',channel_text_preference:'按纯文字偏好取消表情',missing_decision:'缺少发送决定',missing_selection:'未选择素材',invalid_selection:'素材编号无效',repair_unavailable:'操作校正未完成',no_suitable_sticker:'没有合适表情',character_declined:'人物决定不发送',upload_timeout:'上传超时',upload_failed:'上传失败',send_unconfirmed:'发送结果待确认',generation_unconfirmed:'媒体生成未确认，未自动重试',fulfilled_later:'后续请求已成功补发',delivered:'已确认送达',superseded:'新消息已取消旧媒体'};
  if(media?.readable===false)group.append(el('p','异常记录暂不可读，请刷新概览重试。','issue-text'));
  for(const item of media?.items||[]){
    const record=el('div',undefined,'task-line'),body=el('div');
    body.append(el('strong',(isAll?enterpriseLabel(item.enterprise)+' · ':'')+'应用 '+item.agent+' · '+({sticker:'表情包',voice:'语音',image:'图片'}[item.kind]||'媒体')+' · '+({open:'待处理',resolved:'已解决',cancelled:'已取消'}[item.state]||'待核对')));
    body.append(el('p',(reasons[item.reason]||'媒体处理异常')+' · '+({decision:'回复决定阶段',selection:'选择阶段',upload:'上传阶段',send_media:'发送阶段',send:'发送确认',generation:'合成或生成阶段'}[item.stage]||'媒体处理')+' · '+(item.text_delivered?'已有文字送达':'未记录文字送达')));
    const details=el('details'),title=el('summary','定位信息');details.append(title,el('p','消息 '+item.msgid+' · 用户 '+item.user_hash+' · '+date(item.updated)+' · 状态变化 '+item.occurrences+' 次','record-id'));body.append(details);record.append(body);group.append(record);
  }
  if(!media?.items?.length&&media?.readable!==false)group.append(el('p','当前没有聊天媒体异常记录。','empty'));
  list.append(group);
  const recent=$('recent-onboarding');recent.replaceChildren();for(const job of data.recent_applications)recent.append(line(job.username,(isAll?enterpriseLabel(job.enterprise)+' · ':'')+openingNames[job.state]+' · '+(job.agent?'应用 '+job.agent:'尚未分配应用'),button('用户详情',()=>openCustomer(job.account))));emptyLines(recent,(isAll?'已读取企业':'当前企业')+'尚无开通申请；新申请会显示在这里。');
}
async function loadOnboarding(reset=false){if(reset)reportPages.onboarding=1;return readReport('onboarding','onboarding',{q:$('onboarding-q').value.trim(),state:$('onboarding-state').value,page:reportPages.onboarding},data=>{
  $('onboarding-summary').textContent='共 '+data.count+' 笔符合条件的申请 · 已在使用 '+(data.progress_counts?.in_use||0)+' · 待首次聊天 '+(data.progress_counts?.pending_first_chat||0)+' · 使用情况待核对 '+(data.progress_counts?.usage_unknown||0)+' · 需要检查 '+(data.counts.needs_inspection||0)+' · 等待分配 '+(data.counts.waiting_pool||0);
  const box=$('onboarding-rows');box.replaceChildren();for(const job of data.items){const item=el('article',undefined,'application-line'),head=el('div',undefined,'record-head');head.append(el('strong',job.username),status(job.progress_label||openingNames[job.state]||job.state,['needs_inspection','failed'].includes(job.state)||job.progress_state==='assignment_changed'?'warn':['in_use','pending_first_chat'].includes(job.progress_state)||job.state==='ready'?'ok':''));const grid=el('dl',undefined,'record-grid');recordValue(grid,'应用',job.agent||'尚未分配',job.display_name||'');if(job.usage_state){recordValue(grid,'使用情况',job.usage_label,job.last_inbound?'最近收到消息：'+date(job.last_inbound):'');}else recordValue(grid,'当前步骤',stageNames[job.stage]||job.stage);recordValue(grid,'提交时间',date(job.created));recordValue(grid,'最近进展',date(job.updated));item.append(head,grid);if(job.issue)item.append(el('p',job.issue,'issue-text'));item.append(el('p',job.next_step),button('查看用户详情',()=>openCustomer(job.account)));box.append(item);}emptyLines(box,'没有符合条件的申请，试试其他状态或清空搜索。');pageControls('onboarding',data);
});}
async function loadPayments(reset=false){if(reset)reportPages.payments=1;const query={page:reportPages.payments};for(const key of ['start','end','provider','state','q','user'])query[key]=$('payments-'+key).value.trim();return readReport('payments','payments',query,data=>{
  channelLines('payment-channels',data.channels);$('payments-summary').textContent=data.totals.orders+' 笔订单 · 已支付金额 ¥'+yuan(data.totals.paid_fen)+' · 到账待核对 '+data.totals.issues+' 笔'+(data.orphan_entries?' · 无关联订单的到账记录 '+data.orphan_entries+' 笔':'');
  $('payments-method').textContent='按订单创建日期筛选（北京时间）。此处核对订单、支付入账和星币流水；本次未向支付提供方查询。读取时间：'+date(data.checked_at)+'。';
  const box=$('payments-rows');box.replaceChildren();for(const order of data.items){const item=el('article',undefined,'order-line'),head=el('div',undefined,'record-head'),who=el('div');who.append(el('strong',providerNames[order.provider]||order.provider),el('small',order.user));head.append(who,status(paymentNames[order.display_state]||order.display_state,order.state==='paid'?'ok':''));item.append(head,el('p',order.id,'record-id'));
    const grid=el('dl',undefined,'record-grid');recordValue(grid,order.state==='paid'?'实付金额':'订单金额','¥'+yuan(order.fen),order.state==='paid'?'订单已支付':'尚未确认收款');recordValue(grid,'本金星币',coin(order.base_starcoins));recordValue(grid,'赠送星币',coin(order.bonus_starcoins));recordValue(grid,'到账星币',order.received_coins==null?'尚未入账':coin(order.received_coins),order.received_at?date(order.received_at):'');item.append(grid);
    const footer=el('div',undefined,'record-footer');footer.append(status(order.issue?issueNames[order.issue]:order.reconciled?'本地到账核对通过':'尚未形成到账记录',order.issue?'warn':order.reconciled?'ok':''));if(order.wallet)footer.append(button('账户流水',()=>openProfile(order.wallet)));else footer.append(button('成员账务',()=>openProfile('',order.user)));item.append(footer);
    const detail=el('details',undefined,'order-details');detail.append(el('summary','订单核对信息'),el('p','创建：'+date(order.created)+' · 到期：'+date(order.expires)),el('p','报价版本：'+order.pricing_version+' · 付款方式：'+order.checkout_mode),el('p','支付入账编号：'+(order.payment_entry??'—')+' · 星币流水编号：'+(order.entry??'—')),el('p','订单本金及赠送按下单时锁定报价显示，历史订单不套用新赠送规则。'));item.append(detail);box.append(item);
  }emptyLines(box,'此范围没有订单，调整日期、渠道或成员后再查询。');pageControls('payments',data);
});}
async function loadBusiness(){return readReport('business','business',{start:$('business-start').value,end:$('business-end').value},data=>{
  $('business-period').textContent=data.start+' 至 '+data.end+' · 北京时间';const box=$('business-summary');box.replaceChildren();
  const paid=data.channels.reduce((n,c)=>n+c.paid_fen,0),bonus=data.channels.reduce((n,c)=>n+c.bonus_coins,0),cost=data.cost_estimate;
  summaryItem(box,'已入账收款','¥'+yuan(paid),'支付入账所关联的订单金额');summaryItem(box,'充值赠送',coin(bonus),'按每笔订单原始报价','星币');summaryItem(box,'实际消费',coin(data.ledger.consumed),'按扣费流水统计','星币');summaryItem(box,'首次赠送',coin(data.ledger.opening_gifts),'非现金收入','星币');summaryItem(box,'消费成员',data.ledger.paying_users,'期间发生扣费的账户','人');summaryItem(box,'供应商参考成本',cost.readable?(cost.cost==null?(cost.requests?'用量成本待核对':'暂无可计价用量'):'¥'+new Intl.NumberFormat('zh-CN',{maximumFractionDigits:6}).format(Number(cost.cost))):'暂不可读','按当前成本价复算，非结算账单');
  const channels=$('business-channel-rows');channels.replaceChildren();for(const c of data.channels){const row=el('tr');for(const v of [providerNames[c.provider],c.orders,yuan(c.paid_fen),coin(c.principal_coins),coin(c.bonus_coins),coin(c.credited)])row.append(el('td',String(v)));channels.append(row);}safeTableEmpty('business-channel-rows',data.channels.length,6,'所选期间尚无充值入账。');
  const daily=$('business-daily-rows');daily.replaceChildren();for(const d of data.daily){const row=el('tr');for(const v of [d.day,d.orders,yuan(d.paid_fen),coin(d.credited)])row.append(el('td',String(v)));daily.append(row);}safeTableEmpty('business-daily-rows',data.daily.length,4,'所选期间尚无每日收款记录。');
  $('business-cost-note').textContent=cost.readable?'参考成本仅合计可计价的已知用量；'+cost.cost_unknown+' 笔成本待核对'+(cost.unavailable.length?'，读取失败应用 '+cost.unavailable.join('、'):'')+(cost.missing_ledgers?.length?'，尚无用量账本应用 '+cost.missing_ledgers.join('、'):'')+'。':'参考成本读取未完成，请重新读取；未将未知成本计为零。';
});}
async function loadStarcoins(reset=false){if(reset)reportPages.starcoin=1;return readReport('starcoin','wallets',{q:$('starcoin-q').value.trim(),state:$('starcoin-state').value,page:reportPages.starcoin},data=>{
  const box=$('starcoin-rows');box.replaceChildren();for(const w of data.items){const row=el('tr');const who=el('td',undefined,'wallet-person');who.append(el('strong',w.display_name||w.closed_username||w.user));if(w.account_status&&w.account_status!=='active')who.append(el('small',(w.account_status==='closing'?'注销中':'已注销')+' · 仅供查账'));if(w.member_name&&w.website_username&&w.member_name!==w.website_username)who.append(el('small','账号：'+w.website_username));else if(w.website_binding==='pending_confirmation')who.append(el('small','开通账号'));if(w.display_name&&w.display_name!==w.user)who.append(el('small','成员 ID：'+w.user));row.append(who,el('td',coin(w.available),'number'),el('td',coin(w.balance),'number'),el('td',coin(w.reserved),'number'));const cell=el('td');cell.append(button('查看账务',()=>openProfile(w.id)));row.append(cell);box.append(row);}safeTableEmpty('starcoin-rows',data.items.length,5,'尚无匹配账户，请核对查询条件和账户状态。');pageControls('starcoin',data);
});}
async function openProfile(wallet='',user='',reset=true){if(requireEnterprise('starcoin'))return;profileWallet=wallet;profileUser=user;if(reset){reportPages.profile=1;resetProfileRecords();}view('profile');return loadProfile();}
async function loadProfile(){const query={page:reportPages.profile,view:'records',recharge_page:profileRechargePage};if(profileWallet)query.wallet=profileWallet;else query.user=profileUser;return readReport('profile','wallet',query,data=>{
  const w=data.wallet;profileWallet=w.id;profileUser=w.user;$('profile-title').textContent=(w.display_name||w.user)+' · 账务';$('profile-subtitle').textContent=enterpriseLabel()+' · 成员 ID：'+w.user+' · '+data.order_count+' 笔充值订单'+(w.account_status&&w.account_status!=='active'?' · '+(w.account_status==='closing'?'注销中':'已注销')+'（仅供查账；余额不清零、不自动退款）':'');
  const box=$('profile-summary');box.replaceChildren();for(const [title,value,note]of [['可用星币',w.available,w.account_status&&w.account_status!=='active'?'账户已停用，余额留存供查账':'余额扣除当前预留'],['账户余额',w.balance,'包含赠送和充值到账'],['生成预留',w.reserved,'仅计未过期请求'],['累计充值到账',w.recharged,'包含订单赠送'],['累计赠送',w.gifted,'包含首次赠送和每日赠送，不属于充值'],['累计消费扣费',w.consumed,'按扣费流水合计']])summaryItem(box,title,coin(value),note,'星币');
  $('profile-integrity').textContent=w.consistent?'账本核对通过：余额与全部流水合计一致。':'余额与流水合计不一致，请先联系维护人员核对。';$('profile-integrity').className='footnote'+(w.consistent?'':' error');
  renderProfileRecords(data);
});}
async function showOrder(id){$('payments-q').value=id;$('payments-user').value='';$('payments-state').value='';$('payments-provider').value='';$('payments-start').value='2026-01-01';view('payments');return loadPayments(true);}
async function openCustomer(id){customerAccount=id;view('customer');return loadCustomer();}
async function loadCustomer(){return readReport('customer','customer',{account:customerAccount},data=>{
  const a=data.account;$('customer-title').textContent=a.username;$('customer-subtitle').textContent=(roleNames[a.role]||a.role)+' · 注册于 '+date(a.created)+' · 跨企业视图';const appsBox=$('customer-apps');appsBox.replaceChildren();
  for(const app of data.applications){const link=el('a','查看该企业应用');link.href=scopeLink('detail',app.enterprise,{agent:app.agent});appsBox.append(line((app.name||'应用')+' · '+app.agent,app.enterprise_label+' · '+(app.status==='manual_dedicated'?'人工专属':'已分配'),link));}emptyLines(appsBox,'此网站账号目前没有关联应用。');
  const wallets=$('customer-wallets');wallets.replaceChildren();for(const w of data.wallets){const link=el('a','查看余额、消费与充值');link.href=scopeLink('profile',w.enterprise,{wallet:w.id});wallets.append(line(w.user+' · 可用 '+coin(w.available)+' 星币',enterpriseLabel(w.enterprise)+' · 余额 '+coin(w.balance)+' · 预留 '+coin(w.reserved),link));}emptyLines(wallets,'暂无可展示的星币账户。');
  const jobs=$('customer-onboarding');jobs.replaceChildren();for(const j of data.onboarding)jobs.append(line(enterpriseLabel(j.enterprise)+' · '+(openingNames[j.state]||j.state),(j.agent?'应用 '+j.agent+' · ':'')+(stageNames[j.stage]||j.stage)+' · '+date(j.updated)));emptyLines(jobs,'此账号没有开通申请记录。');
});}
function openPilot(app){view('pilot');const box=$('pilot-info');box.replaceChildren();for(const [title,value]of [['运行状态',stateLabel(app)],['企业范围',enterpriseLabel()],['模型',app.model||'未配置'],['源代码版本',app.source_version||'未声明版本'],['源代码校验',app.source_hash||'暂不可读'],['分配方式','独立试点，不进入应用池']])summaryItem(box,title,value);}
function setReportDates(prefix){const today=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());if(!$(prefix+'-end').value)$(prefix+'-end').value=today;if(!$(prefix+'-start').value)$(prefix+'-start').value=new Date(Date.parse(today+'T00:00:00Z')-29*86400000).toISOString().slice(0,10);}
function initOperations(){
  initProfileRecords();
  for(const prefix of ['payments','business'])setReportDates(prefix);
  $('menu-toggle').onclick=()=>{const open=document.body.dataset.menuOpen!=='true';document.body.dataset.menuOpen=String(open);$('menu-toggle').setAttribute('aria-expanded',String(open));};
  $('pool-filter').onchange=renderApps;
  for(const [name,load]of Object.entries({...reportLoaders,starcoin:loadStarcoins,profile:loadProfile,customer:loadCustomer})){$(name+'-refresh').onclick=()=>load();const form=$(name+'-form');if(form)form.onsubmit=e=>{e.preventDefault();load(true);};}
  $('dashboard-refresh').onclick=()=>loadDashboard(true);
  for(const [name,load]of [['onboarding',loadOnboarding],['payments',loadPayments],['starcoin',loadStarcoins],['profile',loadProfile]])for(const [dir,delta]of [['prev',-1],['next',1]])$(name+'-'+dir).onclick=()=>{reportPages[name]+=delta;load();};
  document.querySelectorAll('[data-go]').forEach(b=>b.onclick=()=>action(b,()=>go(b.dataset.go)));
  $('profile-back').onclick=()=>go('starcoin');$('customer-back').onclick=()=>go('accounts');$('pilot-back').onclick=()=>go('apps');
  $('profile-payments').onclick=()=>{$('payments-user').value=profileUser;$('payments-q').value='';$('payments-state').value='';$('payments-provider').value='';view('payments');loadPayments(true);};
  $('profile-usage').onclick=()=>{$('usage-user').value=profileUser;$('usage-agent').value='';view('usage');loadUsage(true);};
}
async function legacyInitialReport(){const name=location.hash.slice(1),params=new URLSearchParams(location.search);if(name==='profile'&&params.get('wallet'))await openProfile(params.get('wallet'));else if(name==='customer'&&params.get('account'))await openCustomer(params.get('account'));else if(['dashboard','apps','onboarding','payments','business','starcoin','accounts','usage','shadow','prices','jobs','audit','wallet'].includes(name)&&name!=='dashboard')await go(name);}

// official-persona-templates-v1
let templateRecord=null,templateAvatar='',templateCatalogue=null,templateSaving=false;
const templateWritable=()=>identity?.role==='superadmin';
function templateContent(){return {name:$('template-name').value.trim(),intro:$('template-intro').value.trim(),prompt:templateProfilePrompt(),avatar:templateAvatar,voice:templateVoiceContent(),reference:templateReferenceContent()};}
function templateDirty(){const current=templateContent(),saved=templateRecord?.draft||{name:'',intro:'',prompt:'',avatar:''};return !$('template-editor').hidden&&(templateCardBusy||!!$('template-card-text').value.trim()||templateReferenceAnalyzing||templateReferenceLoading||templateCropLoading||!!templateCrop?.pending||['name','intro','prompt','avatar','voice','reference'].some(key=>templateFieldSignature(current[key])!==templateFieldSignature(saved[key])));}
function templateControls(){
  const writable=templateWritable()&&!templateSaving;
  for(const node of $('template-editor').querySelectorAll('input,textarea,select'))node.disabled=!writable;
  for(const key of ['new','save','avatar-clear'])$('template-'+key).disabled=!writable;
  for(const key of ['publish','withdraw','default'])$('template-'+key).disabled=!writable||!templateRecord||templateDirty();
  $('template-withdraw').hidden=!templateRecord?.published||!!templateRecord?.withdrawn;
  $('template-default').hidden=!templateRecord?.published||!!templateRecord?.withdrawn;
  templateVoiceControls();templateCropControls();templateReferenceControls();templateCardControls();$('template-draft-state').textContent=templateSaving?'正在保存…':templateCardBusy?'正在识别角色卡…':$('template-card-text').value.trim()?'请先确认填入或清空导入':templateReferenceAnalyzing?'正在识别参考图…':templateCropLoading||templateCrop?.pending?'请先确认或取消裁剪':templateDirty()?'有未保存的修改':'草稿已保存';
  $('template-default').textContent=templateCatalogue?.default===templateRecord?.id?'取消新用户默认':'设为新用户默认';
}
function templateOpen(value){
  templateCardReset();templateCropDispose();$('template-avatar-status').textContent='';templateRecord=value;const data=value?.draft||{name:'',intro:'',prompt:'',avatar:''};templateAvatar=data.avatar;templateVoiceOpen(data.voice);templateReferenceOpen(data.reference);
  for(const k of ['name','intro'])$('template-'+k).value=data[k];templateProfileOpen(data.prompt);
  $('template-avatar-file').value='';$('template-avatar-recrop').hidden=!templateAvatar;$('template-avatar-preview').hidden=!templateAvatar;if(templateAvatar)$('template-avatar-preview').src=templateAvatar;
  $('template-editor-title').textContent=value?data.name:'新建模板';$('template-version').textContent=value?value.status+(value.published?' · 当前公开 v'+value.published:'')+(value.has_changes?' · 有未发布修改':''):'先保存草稿，再预览和发布';
  $('template-editor').hidden=false;$('template-preview').hidden=true;$('template-feedback').textContent='';configDirty=false;templateControls();
}
async function loadTemplates(quiet=false){
  $('enterprise-scope').textContent='官方人格模板 · 跨企业共享';$('templates-status').textContent='正在读取模板库…';
  try{
    const data=await request('templates');templateCatalogue=data;templateVoiceChoices=data.voice_choices||[];$('templates-list').replaceChildren();$('template-voice-list').replaceChildren();for(const v of templateVoiceChoices){const o=el('option',v.name);o.value=v.id;$('template-voice-list').append(o);}
    renderTemplateVoiceChoices();for(const value of data.items){
      const row=el('div',undefined,'template-item'),open=el('button','编辑与预览','secondary');open.type='button';row.append(el('h3',value.name),el('p',value.intro||'尚未填写介绍'),el('small',value.status+(value.published?' · v'+value.published:'')+(value.has_changes?' · 有未发布修改':'')+(data.default===value.id?' · 新用户默认':'')));
      open.onclick=()=>action(open,async()=>{if(templateDirty()&&!confirm('放弃当前尚未保存的模板修改？'))return;const editor=$('template-editor'),wasHidden=editor.hidden;editor.hidden=true;try{templateOpen(await request('templates/detail?id='+encodeURIComponent(value.id)));$('template-editor-title').tabIndex=-1;$('template-editor-title').focus();}catch(error){editor.hidden=wasHidden;throw error;}});row.append(open);$('templates-list').append(row);
    }
    $('templates-status').textContent=data.items.length?'共 '+data.items.length+' 份模板'+(templateWritable()?'':' · 当前权限仅可查看'):'还没有官方模板，先创建第一位聊天伙伴。';
    $('templates-default-note').textContent=data.default?'新用户首次使用时会采用指定模板，已有用户的选择保持不变。':'尚未指定新用户默认人格，首次使用沿用原有默认人格。';templateControls();
  }catch(error){$('templates-status').textContent=error.message+' 可点击“重新读取”重试。';}
}
async function templateWrite(operation){
  if(templateSaving||!templateWritable()||templateCardBusy||templateReferenceLoading||templateReferenceAnalyzing)return;
  if(templateCropLoading||templateCrop?.pending){$('template-avatar-status').textContent='请先确认或取消头像裁剪，再保存模板。';return;}
  if(operation==='save'&&$('template-voice-enabled').checked&&(!$('template-voice-id').value.trim()||!$('template-voice-name').value.trim())){$('template-voice-custom').open=true;note('请先选择音色，或填写自定义音色的编号和名称。',true);$('template-voice-choice').focus();return;}
  if(operation==='save'&&(!$('template-editor').reportValidity()||!templateProfileValidate()))return;
  if(operation==='publish'&&!confirm('发布为新的公开版本？所有应用将可选择，已有用户不会自动更新。'))return;
  if(operation==='withdraw'&&!confirm('下架后不再供新用户选择，已经使用的副本仍保留。继续吗？'))return;
  templateSaving=true;templateControls();$('template-feedback').textContent='正在处理…';
  try{
    const payload={id:templateRecord?.id,revision:templateRecord?.revision};if(operation==='save')payload.content=templateContent();
    const value=await request('templates/'+operation,payload);templateOpen(value);await loadTemplates(true);$('template-feedback').textContent={save:'草稿已保存。',publish:'新版本已发布，用户可在“人格设定 → 官方人格”选择。',withdraw:'模板已下架。',default:'新用户默认设置已更新。'}[operation];configDirty=false;
  }catch(error){$('template-feedback').textContent=error.message+' 如连接中断，请重新读取并核对版本后操作。';}
  finally{templateSaving=false;templateControls();}
}
// persona-template-voice-v1
function templateFieldSignature(v){return JSON.stringify(v&&typeof v==='object'?Object.keys(v).sort().map(k=>[k,v[k]]):v??null);}
function templateVoiceContent(){
 if(!$('template-voice-enabled').checked)return null;
 const rate=Number($('template-voice-rate').value);
 return {provider:$('template-voice-provider').value,voice:$('template-voice-id').value.trim(),name:$('template-voice-name').value.trim(),voice_speed:Number($('template-voice-speed').value),voice_rate:rate,voice_language:$('template-voice-language').value,voice_subtitles:$('template-voice-subtitles').checked,voice_auto_emotion:$('template-voice-emotion').checked,ai_voice:rate>0};
}
function templateVoiceOpen(v){
 templateVoicePreviewReset();
 $('template-voice-enabled').checked=!!v;$('template-voice-fields').hidden=!v;
 $('template-voice-provider').value=v?.provider||'breeze';$('template-voice-id').value=v?.voice||'';$('template-voice-name').value=v?.name||'';$('template-voice-speed').value=v?.voice_speed??1;$('template-voice-rate').value=v?.voice_rate??0;$('template-voice-language').value=v?.voice_language||'zh';$('template-voice-emotion').checked=v?.voice_auto_emotion??true;$('template-voice-subtitles').checked=v?.voice_subtitles??false;renderTemplateVoiceChoices();
}
function templateVoiceControls(){
 const enabled=$('template-voice-enabled').checked;$('template-voice-fields').hidden=!enabled;
 for(const field of $('template-voice-fields').querySelectorAll('input,select'))field.disabled=!enabled||!templateWritable()||templateSaving;
 for(const id of ['id','name'])$('template-voice-'+id).required=false;$('template-voice-speed').required=enabled;templateVoicePreviewControls();
}
function initTemplateVoice(){
 for(const v of templateVoiceChoices){const o=el('option',v.name);o.value=v.id;$('template-voice-list').append(o);}
 $('template-voice-id').addEventListener('change',()=>{const v=templateVoiceChoices.find(v=>v.id===$('template-voice-id').value);if(v&&$('template-voice-provider').value==='breeze')$('template-voice-name').value=v.name;configDirty=templateDirty();templateControls();});
}

// Official template audition: preview only; all template changes still require manual save.
let templateVoicePreviewBusy=false,templateVoicePreviewEpoch=0,templateVoicePreviewURL='',templateVoicePreviewSelection='';
function templateVoicePreviewParams(){return {provider:$('template-voice-provider').value,voice:$('template-voice-id').value.trim(),speed:Number($('template-voice-speed').value),language:$('template-voice-language').value};}
function templateVoicePreviewSignature(){return JSON.stringify([$('template-voice-enabled').checked,templateRecord?.id||'',templateVoicePreviewParams()]);}
function templateVoicePreviewReset(){
 templateVoicePreviewEpoch++;const audio=$('template-voice-audio');audio.pause();audio.removeAttribute('src');audio.load();audio.hidden=true;
 if(templateVoicePreviewURL)URL.revokeObjectURL(templateVoicePreviewURL);templateVoicePreviewURL='';
 $('template-voice-preview-status').textContent='';$('template-voice-preview-status').classList.remove('error');templateVoicePreviewSelection=templateVoicePreviewSignature();
}
function templateVoicePreviewControls(){
 if(templateVoicePreviewSelection!==templateVoicePreviewSignature())templateVoicePreviewReset();
 const p=templateVoicePreviewParams(),enabled=$('template-voice-enabled').checked;
 $('template-voice-preview').disabled=templateVoicePreviewBusy||!enabled||!templateWritable()||templateSaving||!p.voice||!$('template-voice-speed').value||!Number.isFinite(p.speed)||p.speed<.5||p.speed>2;
 $('template-voice-preview').textContent=templateVoicePreviewBusy?'正在准备试听…':templateVoicePreviewURL?'重新试听':'试听音色';
 $('template-voice-preview').setAttribute('aria-busy',String(templateVoicePreviewBusy));
}
async function previewTemplateVoice(){
 if($('template-voice-preview').disabled)return;
 const audio=$('template-voice-audio'),status=$('template-voice-preview-status');
 if(templateVoicePreviewURL){audio.currentTime=0;try{await audio.play();}catch{status.textContent='请点击播放器播放试听。';}return;}
 const signature=templateVoicePreviewSignature(),epoch=++templateVoicePreviewEpoch;
 templateVoicePreviewBusy=true;status.classList.remove('error');status.textContent='正在准备当前音色，请稍候…';templateVoicePreviewControls();
 try{
  const result=await request('templates/voice-preview',templateVoicePreviewParams());
  if(epoch!==templateVoicePreviewEpoch||signature!==templateVoicePreviewSignature()||!templateWritable()||document.body.dataset.adminView!=='templates')return;
  if(result.mime!=='audio/mpeg'||typeof result.audio!=='string'||!result.audio.length||result.audio.length>1400000)throw new Error('试听音频无效，请重试。');
  const bytes=Uint8Array.from(atob(result.audio),c=>c.charCodeAt(0));templateVoicePreviewURL=URL.createObjectURL(new Blob([bytes],{type:result.mime}));
  audio.src=templateVoicePreviewURL;audio.hidden=false;status.textContent=(result.cached?'已载入已有试听':'试听已准备好')+' · '+result.text;
  try{await audio.play();}catch{if(epoch===templateVoicePreviewEpoch)status.textContent+=' · 点击播放器播放';}
 }catch(error){if(epoch===templateVoicePreviewEpoch&&signature===templateVoicePreviewSignature()){status.textContent=error.message;status.classList.add('error');}}
 finally{templateVoicePreviewBusy=false;templateVoicePreviewControls();}
}
function initTemplateVoicePreview(){
 $('template-voice-preview').onclick=()=>void previewTemplateVoice();
 $('template-voice-audio').addEventListener('error',()=>{if(templateVoicePreviewURL){templateVoicePreviewReset();$('template-voice-preview-status').textContent='此音频暂时无法播放，请重新试听。';$('template-voice-preview-status').classList.add('error');templateVoicePreviewControls();}});
 for(const type of ['input','change'])$('template-editor').addEventListener(type,templateVoicePreviewControls);
 window.addEventListener('pagehide',templateVoicePreviewReset);
 document.addEventListener('visibilitychange',()=>{if(document.hidden)$('template-voice-audio').pause();});
}

let templateVoiceChoices=[];
let templatesInitialized=false;
function initTemplates(){
 if(templatesInitialized)return;
 initTemplateProfile();initTemplateVoice();initTemplateVoicePicker();initTemplateVoicePreview();initTemplateCrop();initTemplateReference();initTemplateReferenceAnalysis();initTemplateCard();
  $('templates-refresh').onclick=async()=>{if(templateDirty()&&!confirm('重新读取会放弃尚未保存的模板修改，继续吗？'))return;const id=templateRecord?.id;await loadTemplates();if(id)templateOpen(await request('templates/detail?id='+encodeURIComponent(id)));};
  $('template-new').onclick=()=>{if(templateDirty()&&!confirm('放弃尚未保存的修改？'))return;templateOpen(null);$('template-name').focus();};
  $('template-editor').onsubmit=e=>{e.preventDefault();void templateWrite('save');};
  for(const key of ['publish','withdraw','default'])$('template-'+key).onclick=()=>templateWrite(key);
  $('template-editor').oninput=()=>{configDirty=templateDirty();templateControls();};
  $('template-preview-button').onclick=()=>{const v=templateContent();$('template-preview-title').textContent=v.name||'未命名模板';$('template-preview-intro').textContent=v.intro+(v.voice?' · 语音：'+v.voice.name+' / '+v.voice.voice_speed+'倍速':' · 不包含语音设置')+(v.reference?' · 包含参考图':' · 不包含参考图');$('template-preview-prompt').textContent=v.prompt;$('template-preview').hidden=false;};
 templatesInitialized=true;

}

// official-template-form-v2
const templateProfileFields=[
 ['name','人格自称','人格自称',40,'例如：鲸鱼娘、鱼鱼、本鲸'],
 ['nickname','称呼用户','称呼用户',40,'例如：小林'],
 ['relationship','关系设定','关系设定',80,'例如：一起讨论事情的朋友'],
 ['personality','性格描述','性格',1600,'TA 的性格特点，遇到问题时通常如何回应…'],
 ['background','人物背景','背景',1200,'年龄、职业、虚构经历和人物来历'],
 ['mood','情绪基线','情绪基线',120,'例如：平静、温暖，有自己的判断'],
 ['style','表达方式','表达风格',500,'例如：简短自然，偶尔冷幽默'],
 ['catchphrases','常用语','常用语',300,'每行一句，可选'],
 ['avoid','避免使用的词句','避免使用的词句',300,'每行一个，可选'],
 ['do','希望 TA 做什么','应当如何互动',800,'例如：先听完再建议；不确定就问我'],
 ['dont','不希望 TA 做什么','不要如何互动',800,'例如：不要反复说教，不要编造经历'],
 ['anchor','核心特质','核心特质',500,'用一两句话概括 TA 最重要的特质'],
 ['worldbook','世界书设定','世界书（必须遵守的世界设定）',6000,'时代背景、世界规则、组织关系、专有名词和回答边界'],
 ['prompt','补充设定 / 模板原文','补充设定',60000,'其他完整设定；不能准确拆分的旧模板会保留在这里']
];
const templateProfileLabels=new Map([['人格名称',['display_name',40]],...templateProfileFields.map(([key,,label,limit])=>[label,[key,limit]])]);
function parseTemplateProfile(raw){
 const matches=[...raw.matchAll(/^(人格名称|人格自称|称呼用户|关系设定|性格|背景|情绪基线|表达风格|常用语|避免使用的词句|应当如何互动|不要如何互动|核心特质|世界书（必须遵守的世界设定）|补充设定)[：:]/gm)];
 const result={};if(!matches.length||raw.slice(0,matches[0].index).trim())return {prompt:raw};
 for(let i=0;i<matches.length;i++){
  const match=matches[i],[key,limit]=templateProfileLabels.get(match[1]),value=raw.slice(match.index+match[0].length,matches[i+1]?.index).trim();
  if(key in result||Array.from(value).length>limit)return {prompt:raw};result[key]=value;
 }
 return result;
}
function serializeTemplateProfile(profile){
 const parts=[];for(const [key,,label] of templateProfileFields){const value=(profile[key]||'').trim();if(value)parts.push(label+'：'+value);}
 return parts.join('\n');
}
let templateProfileOriginal='',templateProfileInitial='';
function templateProfileValue(){return Object.fromEntries(templateProfileFields.map(([key])=>[key,$('template-profile-'+key).value]));}
function templateProfilePrompt(){const profile=templateProfileValue();return JSON.stringify(profile)===templateProfileInitial?templateProfileOriginal:serializeTemplateProfile(profile);}
function templateProfileOpen(raw){
 const profile=parseTemplateProfile(raw||'');for(const [key] of templateProfileFields)$('template-profile-'+key).value=profile[key]||'';
 templateProfileOriginal=raw||'';templateProfileInitial=JSON.stringify(templateProfileValue());
 $('template-legacy-note').hidden=!raw||!profile.prompt||Object.keys(profile).some(key=>key!=='prompt');
}
function initTemplateProfile(){
 const box=$('template-profile-fields');
 if(box.childElementCount)return;
 for(const [key,title,,limit,placeholder] of templateProfileFields){
  const field=el('div',undefined,'template-field'+(['personality','background','anchor','worldbook','prompt'].includes(key)?' full':'')),id='template-profile-'+key,label=el('label',title),input=el(['name','nickname','relationship','mood','style'].includes(key)?'input':'textarea');
  label.htmlFor=id;input.id=id;input.maxLength=limit;input.placeholder=placeholder;if(input.tagName==='TEXTAREA')input.rows=key==='worldbook'||key==='prompt'?6:3;
  field.append(label,input);box.append(field);
 }
}
function templateProfileValidate(){
 const prompt=templateProfilePrompt();if(!prompt.trim()){note('请至少填写一项人格设定。',true);$('template-profile-personality').focus();return false;}
 if(Array.from(prompt).length>60000){note('人格各项合计超过 6 万字，请缩短后保存。',true);return false;}
 return true;
}

// Crop is a local draft. Only confirming changes the template avatar; saving remains explicit.
let templateCrop=null,templateCropDrag=null,templateCropEpoch=0,templateCropLoading=false;
const templateCropAllowed=()=>templateWritable()&&!templateSaving;
function templateCropDispose(){
 templateCropEpoch++;templateCropLoading=false;templateCropDrag=null;if(templateCrop)templateCrop.image.close();templateCrop=null;
 $('template-crop-editor').hidden=true;$('template-avatar-recrop').hidden=true;$('template-avatar-file').value='';
}
function templateCropGeometry(){const c=templateCrop,scale=Math.max(720/c.width,720/c.height)*c.zoom;return {scale,width:c.width*scale,height:c.height*scale,maxX:Math.max(0,c.width*scale-720),maxY:Math.max(0,c.height*scale-720)};}
function drawTemplateCrop(){
 if(!templateCrop)return;const c=templateCrop,g=templateCropGeometry(),canvas=$('template-crop-canvas'),ctx=canvas.getContext('2d');
 ctx.fillStyle='#fff';ctx.fillRect(0,0,720,720);ctx.drawImage(c.image,-g.maxX*c.x,-g.maxY*c.y,g.width,g.height);
 ctx.save();ctx.strokeStyle='rgba(255,255,255,.9)';ctx.lineWidth=2;ctx.setLineDash([9,9]);ctx.beginPath();ctx.arc(360,360,357,0,Math.PI*2);ctx.stroke();ctx.setLineDash([]);ctx.strokeStyle='rgba(255,255,255,.6)';for(const n of [240,480]){ctx.beginPath();ctx.moveTo(n,0);ctx.lineTo(n,720);ctx.moveTo(0,n);ctx.lineTo(720,n);ctx.stroke();}ctx.restore();
 $('template-crop-zoom').value=c.zoom;$('template-crop-zoom-value').textContent=c.zoom.toFixed(2)+' 倍';$('template-crop-zoom').setAttribute('aria-valuetext',c.zoom.toFixed(2)+' 倍');
 for(const axis of ['x','y']){$('template-crop-'+axis).value=Math.round(c[axis]*100);$('template-crop-'+axis).disabled=!templateCropAllowed()||g[axis==='x'?'maxX':'maxY']<.01;}
}
async function openTemplateCrop(file){
 if(!templateCropAllowed()||!file)return;const epoch=++templateCropEpoch;templateCropLoading=true;configDirty=templateDirty();templateControls();
 try{
  if(!file.size||file.size>15*1024*1024||!['image/png','image/jpeg','image/webp'].includes(file.type))throw new Error('请选择 15 MB 以内的 PNG、JPEG 或 WebP 图片。');
  const bitmap=await createImageBitmap(file);
  if(epoch!==templateCropEpoch||!templateCropAllowed()){bitmap.close();return;}
  if(!bitmap.width||!bitmap.height||bitmap.width*bitmap.height>64000000){bitmap.close();throw new Error('图片尺寸过大，请缩小到 6400 万像素以内。');}
  if(!$('template-crop-canvas').getContext('2d')){bitmap.close();throw new Error('此浏览器暂不支持裁剪，请换用系统浏览器。');}
  if(templateCrop)templateCrop.image.close();templateCrop={image:bitmap,width:bitmap.width,height:bitmap.height,zoom:1,x:.5,y:.5,pending:true};
  $('template-crop-editor').hidden=false;$('template-avatar-recrop').hidden=true;drawTemplateCrop();
  $('template-avatar-status').textContent='拖动、缩放后确认裁剪。当前头像仍保留，尚未保存。';
 }catch(error){if(epoch===templateCropEpoch)$('template-avatar-status').textContent=error.message;}
 finally{if(epoch===templateCropEpoch){templateCropLoading=false;configDirty=templateDirty();templateControls();$('template-avatar-file').value='';}}
}
function templateCropControls(){
 const locked=!templateCropAllowed();for(const node of $('template-crop-editor').querySelectorAll('input,button'))node.disabled=locked;
 $('template-crop-canvas').tabIndex=locked?-1:0;
 $('template-avatar-recrop').disabled=locked;$('template-avatar-clear').disabled=locked;
 $('template-save').disabled=locked||templateCropLoading||!!templateCrop?.pending;
 if(templateCrop)drawTemplateCrop();
}
function initTemplateCrop(){
 $('template-avatar-file').onchange=e=>openTemplateCrop(e.target.files[0]);
 $('template-avatar-clear').onclick=()=>{if(!templateCropAllowed())return;templateCropDispose();templateAvatar='';$('template-avatar-preview').hidden=true;$('template-avatar-status').textContent='已清除头像草稿，保存后生效。';configDirty=templateDirty();templateControls();};
 $('template-crop-zoom').oninput=()=>{if(!templateCropAllowed()||!templateCrop)return;templateCrop.zoom=Number($('template-crop-zoom').value);templateCropDrag=null;drawTemplateCrop();};
 for(const axis of ['x','y'])$('template-crop-'+axis).oninput=()=>{if(!templateCropAllowed()||!templateCrop)return;templateCrop[axis]=Number($('template-crop-'+axis).value)/100;drawTemplateCrop();};
 $('template-crop-reset').onclick=()=>{if(!templateCropAllowed()||!templateCrop)return;templateCrop.zoom=1;templateCrop.x=templateCrop.y=.5;drawTemplateCrop();};
 $('template-crop-cancel').onclick=()=>{if(!templateCropAllowed())return;templateCropDispose();$('template-avatar-recrop').hidden=!templateAvatar;$('template-avatar-status').textContent='已取消裁剪，保留原头像草稿。';configDirty=templateDirty();templateControls();};
 $('template-avatar-recrop').onclick=async()=>{if(!templateCropAllowed())return;if(!templateCrop){if(!templateAvatar)return;try{const response=await fetch(templateAvatar),blob=await response.blob();await openTemplateCrop(new File([blob],'avatar',{type:blob.type}));}catch(error){$('template-avatar-status').textContent='原头像未能载入，请重新选择图片。';}return;}templateCrop.pending=true;$('template-crop-editor').hidden=false;$('template-avatar-recrop').hidden=true;configDirty=templateDirty();templateControls();};
 $('template-crop-confirm').onclick=()=>{
  if(!templateCropAllowed()||!templateCrop)return;
  try{const c=templateCrop,g=templateCropGeometry(),side=720/g.scale,canvas=document.createElement('canvas');canvas.width=canvas.height=256;const ctx=canvas.getContext('2d');ctx.fillStyle='#fff';ctx.fillRect(0,0,256,256);ctx.drawImage(c.image,g.maxX*c.x/g.scale,g.maxY*c.y/g.scale,side,side,0,0,256,256);
   const data=canvas.toDataURL('image/jpeg',.88);if(data.length>173000)throw new Error('裁剪图片仍过大，请重新选择图片。');
   templateAvatar=data;c.pending=false;$('template-avatar-preview').src=data;$('template-avatar-preview').hidden=false;$('template-crop-editor').hidden=true;$('template-avatar-recrop').hidden=false;$('template-avatar-status').textContent='裁剪已确认。点击“保存草稿”后保存，发布后供用户选择。';configDirty=templateDirty();templateControls();
  }catch(error){$('template-avatar-status').textContent=error.message;}
 };
 const canvas=$('template-crop-canvas');
 canvas.onpointerdown=e=>{if(!templateCropAllowed()||!templateCrop||templateCropDrag||e.button>0)return;const g=templateCropGeometry();templateCropDrag={id:e.pointerId,startX:e.clientX,startY:e.clientY,x:templateCrop.x,y:templateCrop.y,maxX:g.maxX,maxY:g.maxY};canvas.setPointerCapture(e.pointerId);canvas.focus({preventScroll:true});e.preventDefault();};
 canvas.onpointermove=e=>{const d=templateCropDrag;if(!templateCropAllowed()||!templateCrop||!d||d.id!==e.pointerId)return;const b=canvas.getBoundingClientRect();for(const [axis,pos,start,max] of [['x',e.clientX,'startX','maxX'],['y',e.clientY,'startY','maxY']])if(d[max]>.01)templateCrop[axis]=Math.max(0,Math.min(1,d[axis]-(pos-d[start])*720/b.width/d[max]));drawTemplateCrop();e.preventDefault();};
 for(const name of ['pointerup','pointercancel','lostpointercapture'])canvas.addEventListener(name,e=>{if(templateCropDrag?.id===e.pointerId)templateCropDrag=null;});
 canvas.onkeydown=e=>{if(!templateCropAllowed()||!templateCrop)return;const axis={ArrowLeft:['x',.02],ArrowRight:['x',-.02],ArrowUp:['y',.02],ArrowDown:['y',-.02]}[e.key];if(axis){templateCrop[axis[0]]=Math.max(0,Math.min(1,templateCrop[axis[0]]+axis[1]));drawTemplateCrop();e.preventDefault();}};
}
function renderTemplateVoiceChoices(){
 const select=$('template-voice-choice'),provider=$('template-voice-provider').value,current=$('template-voice-id').value;select.replaceChildren();
 select.append(new Option('请选择音色',''));
 if(provider==='breeze')for(const item of templateVoiceChoices)select.append(new Option(item.name,item.id));
 const known=provider==='breeze'&&templateVoiceChoices.some(v=>v.id===current);
 if(current&&!known)select.append(new Option(($('template-voice-name').value||current)+'（自定义）',current));
 select.append(new Option('填写其他音色…','__custom__'));select.value=current||'';
 $('template-voice-custom').open=!!current&&!known;
}
function initTemplateVoicePicker(){
 $('template-voice-choice').onchange=()=>{
  const id=$('template-voice-choice').value;if(id==='__custom__'){$('template-voice-id').value='';$('template-voice-name').value='';$('template-voice-custom').open=true;$('template-voice-id').focus();configDirty=templateDirty();templateControls();return;}
  $('template-voice-id').value=id;const item=templateVoiceChoices.find(v=>v.id===id);$('template-voice-name').value=item?.name||'';configDirty=templateDirty();templateControls();
 };
 $('template-voice-provider').addEventListener('change',()=>{$('template-voice-id').value='';$('template-voice-name').value='';renderTemplateVoiceChoices();$('template-voice-custom').open=$('template-voice-provider').value!=='breeze';configDirty=templateDirty();templateControls();});
 $('template-voice-id').addEventListener('input',()=>{const item=templateVoiceChoices.find(v=>v.id===$('template-voice-id').value);if($('template-voice-provider').value==='breeze'&&item)$('template-voice-name').value=item.name;renderTemplateVoiceChoices();});
}

initEnterprisePanels();
bindSectionNavigation();bindViews();$('login-form').onsubmit=event=>{event.preventDefault();action(event.submitter,async()=>{try{await request('login',{username:$('username').value,password:$('password').value},true);$('password').value='';$('gate-error').textContent='';await boot();}catch(error){$('gate-error').textContent=error.message;}});};$('logout').onclick=()=>action($('logout'),async()=>{await request('logout',{},true);clearTimeout(poll);location.reload();});$('refresh').onclick=()=>action($('refresh'),loadApps);$('search').oninput=renderApps;$('status-filter').onchange=renderApps;$('select-all').onchange=()=>{for(const a of filtered().filter(a=>!a.independent))$('select-all').checked?selected.add(a.agent):selected.delete(a.agent);renderApps();};$('back').onclick=()=>{if(hasUnsavedChanges()&&!confirm('放弃尚未保存的设置？'))return;configDirty=false;view('apps');loadApps().catch(e=>note(e.message,true));};$('detail-refresh').onclick=()=>action($('detail-refresh'),()=>openDetail(currentAgent));
document.querySelectorAll('.batch-op').forEach(button=>button.onclick=()=>confirmOperation(button.dataset.operation,[...selected]));document.querySelectorAll('.detail-op').forEach(button=>button.onclick=()=>confirmOperation(button.dataset.operation,[currentAgent]));$('batch-config').onclick=()=>{renderOptions('batch-options',{},true,identity.options_schema);$('batch-existing').checked=false;$('batch-dialog').showModal();};$('batch-form').onsubmit=event=>{event.preventDefault();const options=readOptions('batch-options',true);if(!Object.keys(options).length)return;$('batch-dialog').close();confirmOperation('configure',[...selected],{options,apply_existing:$('batch-existing').checked});};$('config-form').onsubmit=event=>{event.preventDefault();const values=readOptions('detail-options'),options={};for(const [key,value]of Object.entries(values))if(value!==(currentSettings.options[key]??optionField(key)?.default))options[key]=value;const data={options,apply_existing:$('apply-existing').checked};if($('model').value!==currentSettings.model)data.model=$('model').value;if($('base-url').value!==currentSettings.base_url)data.base_url=$('base-url').value;if($('api-key').value)data.api_key=$('api-key').value;if($('ai-enabled').checked!==currentSettings.ai_enabled)data.ai_enabled=$('ai-enabled').checked;confirmOperation('configure',[currentAgent],data);};$('reclaim-button').onclick=()=>{if(currentSettings?.reclaim?.allowed&&writeAllowed())confirmOperation('reclaim',[currentAgent],{binding:currentSettings.reclaim.binding,typed_agent:'',protected_confirm:false});};const updateReclaimConfirm=()=>{if(pending?.operation==='reclaim')$('confirm-run').disabled=$('restore-confirm').value!==String(pending.agents[0])||(!$('protected-confirm-label').hidden&&!$('protected-confirm').checked);};$('restore-confirm').oninput=updateReclaimConfirm;$('protected-confirm').onchange=updateReclaimConfirm;$('confirm-cancel').onclick=()=>$('confirm-dialog').close();$('confirm-run').onclick=async()=>{if(!pending)return;if(['restore','reclaim'].includes(pending.operation)&&$('restore-confirm').value!==String(pending.agents[0])){$('confirm-message').textContent='请输入正确的应用 ID。';return;}const button=$('confirm-run');button.disabled=true;button.textContent='正在提交…';try{if(pending.operation==='reclaim'){if(!$('protected-confirm-label').hidden&&!$('protected-confirm').checked)throw new Error('请勾选受保护应用的额外回收确认。');pending.data.typed_agent=$('restore-confirm').value;pending.data.protected_confirm=!$('protected-confirm-label').hidden&&$('protected-confirm').checked;}const result=await request('jobs',pending);pending=null;configDirty=false;$('api-key').value='';$('confirm-dialog').close();note('操作已进入维护队列。任务编号 '+result.job);view('jobs');await loadJobs();}catch(error){$('confirm-message').textContent=error.message;}finally{button.disabled=false;button.textContent='确认执行';}};$('accounts-refresh').onclick=()=>action($('accounts-refresh'),loadAccounts);$('jobs-refresh').onclick=()=>action($('jobs-refresh'),loadJobs);$('audit-refresh').onclick=()=>action($('audit-refresh'),loadAudit);window.addEventListener('beforeunload',event=>{if(hasUnsavedChanges()){event.preventDefault();event.returnValue='';}});document.addEventListener('visibilitychange',()=>{if(document.hidden)clearTimeout(poll);else if(!$('view-jobs').hidden)loadJobs().catch(e=>note(e.message,true));});boot();

$('enterprise-select').addEventListener('change',event=>{
 if(poolBusy||priceSaving||enterpriseOperationsBusy()){event.target.value=enterprise;note('请先完成或取消正在进行的操作。');return;}
 if(hasUnsavedChanges()&&!confirm('切换企业将放弃尚未保存的设置，继续吗？')){event.target.value=enterprise;return;}
 if(document.querySelector('dialog[open]')){event.target.value=enterprise;note('请先完成或取消当前确认，再切换企业。',true);return;}
 const target=new URL(location.href);target.searchParams.set('enterprise',event.target.value);target.hash='';configDirty=false;for(const frame of enterpriseFrames())frame.contentWindow.discardEnterpriseChanges?.();location.assign(target.href);
});


// template-reference-v1
let templateReferenceImage='',templateReferenceLoading=false,templateReferenceEpoch=0;
function templateReferenceContent(){return templateReferenceImage?{image:templateReferenceImage,appearance:$('template-reference-appearance').value.trim(),style:$('template-reference-style').value.trim()}:null;}
function templateReferenceControls(){
 const locked=!templateWritable()||templateSaving||templateReferenceLoading;
 $('template-reference-file').disabled=locked;$('template-reference-clear').disabled=locked||!templateReferenceImage;
 $('template-reference-fields').hidden=!templateReferenceImage;
 for(const key of ['appearance','style']){const field=$('template-reference-'+key);field.disabled=locked||!templateReferenceImage;field.required=!!templateReferenceImage;}
 $('template-reference-analyze').disabled=locked||!templateReferenceImage||templateReferenceAnalyzing;
 $('template-reference-analyze').textContent=templateReferenceAnalyzing?'正在识别…':$('template-reference-appearance').value||$('template-reference-style').value?'重新识别外貌与画风':'自动识别外貌与画风';
 $('template-save').disabled=$('template-save').disabled||templateReferenceLoading||templateReferenceAnalyzing;
 $('template-reference-heading').setAttribute('aria-busy',String(templateReferenceLoading||templateReferenceAnalyzing));
}
function templateReferenceOpen(value){
 ++templateReferenceEpoch;templateReferenceLoading=false;templateReferenceAnalyzing=false;templateReferenceEdits={appearance:0,style:0};templateReferenceImage=value?.image||'';
 $('template-reference-file').value='';for(const key of ['appearance','style'])$('template-reference-'+key).value=value?.[key]||'';
 const img=$('template-reference-preview');img.hidden=!templateReferenceImage;if(templateReferenceImage)img.src=templateReferenceImage;else img.removeAttribute('src');
 $('template-reference-status').textContent='';templateReferenceControls();
}
function initTemplateReference(){
 $('template-reference-clear').onclick=()=>{templateReferenceOpen(null);configDirty=templateDirty();templateControls();};
 $('template-reference-file').onchange=async()=>{
  const file=$('template-reference-file').files[0];if(!file)return;
  const epoch=++templateReferenceEpoch;templateReferenceAnalyzing=false;templateReferenceLoading=true;configDirty=templateDirty();templateControls();$('template-reference-status').textContent='正在准备预览…';
  try{
   if(!['image/png','image/jpeg','image/webp'].includes(file.type)||file.size>15*1024*1024)throw new Error('请选择 15 MB 以内的 PNG、JPEG 或 WebP 图片。');
   const bitmap=await createImageBitmap(file);let image;
   try{
    if(bitmap.width*bitmap.height>40000000)throw new Error('图片尺寸过大，请缩小后重新选择。');
    const canvas=document.createElement('canvas'),scale=Math.min(1,1280/Math.max(bitmap.width,bitmap.height));canvas.width=Math.max(1,Math.round(bitmap.width*scale));canvas.height=Math.max(1,Math.round(bitmap.height*scale));
    const ctx=canvas.getContext('2d');ctx.fillStyle='#fff';ctx.fillRect(0,0,canvas.width,canvas.height);ctx.drawImage(bitmap,0,0,canvas.width,canvas.height);
    for(const quality of [.88,.76,.64,.5]){image=canvas.toDataURL('image/jpeg',quality);if(image.length<=400023)break;}
    if(image.length>400023)throw new Error('压缩后仍超过 300 KB，请换一张较小的图片。');
   }finally{bitmap.close();}
   if(epoch!==templateReferenceEpoch)return;
   templateReferenceImage=image;$('template-reference-preview').src=image;$('template-reference-preview').hidden=false;
   $('template-reference-status').textContent='参考图已加入草稿，准备自动识别外貌与画风…';
  }catch(error){if(epoch===templateReferenceEpoch)$('template-reference-status').textContent=error.message||'无法读取图片，请重新选择。';}
  finally{if(epoch===templateReferenceEpoch){templateReferenceLoading=false;configDirty=templateDirty();templateControls();if(templateReferenceImage&&$('template-reference-status').textContent.includes('准备自动识别'))void templateReferenceAnalyze();}}
 };
}

// template-reference-auto-v1
let templateReferenceAnalyzing=false,templateReferenceEdits={appearance:0,style:0};
async function templateReferenceAnalyze(){
 if(!templateWritable()||templateSaving||templateReferenceLoading||templateReferenceAnalyzing||!templateReferenceImage)return;
 const epoch=templateReferenceEpoch,image=templateReferenceImage,edits={...templateReferenceEdits};
 templateReferenceAnalyzing=true;templateControls();$('template-reference-status').textContent='正在识别人物外貌与画风，你可以继续手动编辑…';
 try{
  const result=await request('templates/analyze-reference',{image});
  if(epoch!==templateReferenceEpoch||image!==templateReferenceImage)return;
  if(!result||['appearance','style'].some(key=>typeof result[key]!=='string'||!result[key].trim()||result[key].length>600))throw new Error('识别结果不完整，可手动填写或重新识别。');
  let preserved=false;
  for(const key of ['appearance','style']){if(templateReferenceEdits[key]===edits[key])$('template-reference-'+key).value=result[key];else preserved=true;}
  $('template-reference-status').textContent=preserved?'识别完成，识别期间手动修改的字段已保留。核对后点击“保存草稿”。':'外貌与画风已自动填写，可继续修改。核对后点击“保存草稿”。';
 }catch(error){if(epoch===templateReferenceEpoch&&image===templateReferenceImage)$('template-reference-status').textContent=(error.message||'自动识别暂未成功。')+' 图片和填写内容已保留，也可以手动填写。';}
 finally{if(epoch===templateReferenceEpoch){templateReferenceAnalyzing=false;configDirty=templateDirty();templateControls();}}
}
function initTemplateReferenceAnalysis(){
 $('template-reference-analyze').onclick=()=>void templateReferenceAnalyze();
 for(const key of ['appearance','style'])$('template-reference-'+key).addEventListener('input',()=>{templateReferenceEdits[key]++;});
}

// admin-binding-copy-20261101.1

// admin-session-refresh-20261102.1

// admin-app-delete-20261103.1 -- self-built WeCom deletion remains a human action.
let appDeletionPending=null,appDeletionBusy=false;
const appDeletionLabels={preparing:'待完成停用',awaiting_wecom:'待企业微信删除',finalizing:'待完成星屿移除',done:'已移除 · 企业微信人工确认'};
function renderAppDeletion(value){
  $('app-delete-area').hidden=identity?.role!=='superadmin';
  $('app-delete-status').textContent=value?.reason||'暂时无法核对删除条件，请重新读取。';
  $('app-delete-button').disabled=identity?.role!=='superadmin'||!value?.allowed;
  $('app-delete-button').textContent=value?.phase&&value.phase!=='new'?'继续删除':'开始删除应用';
}
function openAppDeletion(agent,value,name){
  if(identity?.role!=='superadmin'||appDeletionBusy)return;
  if(hasUnsavedChanges()&&!confirm('放弃尚未保存的设置并继续删除确认？'))return;
  const finalize=['awaiting_wecom','finalizing'].includes(value.phase);
  appDeletionPending={operation:finalize?'delete_finalize':'delete_prepare',agents:[agent],data:{binding:value.binding,typed_agent:''},request_id:crypto.randomUUID().replaceAll('-',''),confirm:true};
  if(finalize)appDeletionPending.data.wecom_confirmed=false;
  $('app-delete-title').textContent=finalize?'确认企业微信已删除':'开始删除应用';
  $('app-delete-description').textContent=finalize?'请先在企业微信管理后台删除下面的应用，再勾选人工确认。提交后将移除星屿中的应用容器和应用池记录。':'此步会停止服务并退出自动分配，保留删除进度。之后需到企业微信管理后台删除对应应用。';
  const box=$('app-delete-summary');box.replaceChildren(el('p','当前企业：'+enterpriseLabel()),el('p','应用：'+(name||'未命名应用')+' · AgentId '+agent),el('p','企业 ID：'+(enterprise==='legacy'?'ww0000000000000001':enterprise)));
  const link=el('a','打开企业微信管理后台 →');link.href='https://work.weixin.qq.com/wework_admin/frame#apps';link.target='_blank';link.rel='noopener noreferrer';box.append(link,el('p','进入「应用管理 → 自建应用」，核对企业 ID 和 AgentId 后删除。'));
  box.append(el('p','网站账号、余额、充值及账务记录保留。星屿应用文件会转入管理员私有归档。'));
  $('app-delete-official-label').hidden=!finalize;$('app-delete-official-confirm').checked=false;$('app-delete-id').value='';$('app-delete-error').textContent='';
  $('app-delete-submit').textContent=finalize?'确认并从星屿移除':'停止并退出分配';$('app-delete-submit').disabled=true;
  $('app-delete-dialog').showModal();$('app-delete-id').focus();
}
async function loadAppDeletions(){
  const section=$('app-deletion-records');section.hidden=identity?.role!=='superadmin';if(section.hidden)return;
  const box=$('app-deletion-rows');box.replaceChildren();
  try{
    const data=await request('application-deletions');
    if(!data.items.length){box.append(el('p','暂无应用删除记录。可从「全部应用 → 管理 → 运行维护」发起。','empty'));return;}
    for(const item of data.items){
      const row=el('article',undefined,'job'),head=el('div',undefined,'job-head');
      head.append(el('strong',item.name+' · '+item.agent),el('span',appDeletionLabels[item.phase]||'需要检查','status '+(item.phase==='done'?'ok':'warn')));row.append(head,el('p','企业 ID：'+item.corp),el('p',date(item.updated)+' · 发起人：'+item.actor));
      if(item.phase==='done')row.append(el('p','企业微信删除由 '+item.confirmed_by+' 于 '+date(item.confirmed_at)+' 人工确认；星屿应用已移除，账务记录保留。'));
      else{row.append(el('p',item.phase==='preparing'?'停止服务尚未完成。应用已退出分配，请继续处理。':item.phase==='finalizing'?'已保留人工确认和移除进度，请继续完成。':'应用已停止并退出分配。到企业微信删除对应应用后，回到这里完成确认。'));const button=el('button',item.phase==='preparing'?'继续停止应用':item.phase==='finalizing'?'继续从星屿移除':'企业微信已删除，继续确认','danger');button.onclick=()=>openAppDeletion(item.agent,item,item.name);row.append(button);}
      box.append(row);
    }
  }catch(error){box.append(el('p','删除进度暂时无法读取：'+error.message,'error'));}
}
function initAppDeletion(){
  $('app-delete-button').onclick=()=>{if(currentSettings?.deletion?.allowed)openAppDeletion(currentAgent,currentSettings.deletion,$('detail-title').textContent);};
  const update=()=>{$('app-delete-submit').disabled=appDeletionBusy||!appDeletionPending||$('app-delete-id').value!==String(appDeletionPending.agents[0])||(!$('app-delete-official-label').hidden&&!$('app-delete-official-confirm').checked);};
  $('app-delete-id').oninput=update;$('app-delete-official-confirm').onchange=update;
  $('app-delete-cancel').onclick=()=>{if(!appDeletionBusy)$('app-delete-dialog').close();};
  $('app-delete-dialog').addEventListener('cancel',event=>{if(appDeletionBusy)event.preventDefault();});
  $('app-delete-submit').onclick=async()=>{
    if(appDeletionBusy||!appDeletionPending)return;update();if($('app-delete-submit').disabled)return;
    const payload=appDeletionPending;payload.data.typed_agent=$('app-delete-id').value;
    if(payload.operation==='delete_finalize')payload.data.wecom_confirmed=$('app-delete-official-confirm').checked;
    appDeletionBusy=true;update();$('app-delete-error').textContent='';
    try{const result=await request('jobs',payload);appDeletionPending=null;configDirty=false;$('app-delete-dialog').close();note('已提交维护任务。请查看应用删除进度与任务结果。编号 '+result.job);view('jobs');await loadJobs();}
    catch(error){$('app-delete-error').textContent=error.message+' 进度已保留，可刷新维护任务查看。';}
    finally{appDeletionBusy=false;update();}
  };
}


// admin-template-card-20261103.1 -- recognition is a preview, never a save.
let templateCardBusy=false,templateCardEpoch=0,templateCardResult=null,templateCardSnapshot='';
function templateCardReset(){
 templateCardEpoch++;templateCardBusy=false;templateCardResult=null;templateCardSnapshot='';
 $('template-card-file').value='';$('template-card-text').value='';$('template-card-status').textContent='';
 $('template-card-preview').hidden=true;$('template-card-import').open=false;$('template-card-original').checked=true;
}
function templateCardCurrentSignature(){return JSON.stringify(templateContent());}
function templateCardCandidate(){
 if(!templateCardResult)return null;
 const profile={...templateCardResult.profile};
 const raw=$('template-card-original').checked?(templateCardResult.original??$('template-card-text').value).trim():(profile.prompt||'');
 profile.prompt=raw?'角色卡补充资料：\n'+raw.split('\n').map(line=>'  '+line).join('\n'):'';
 return profile;
}
function templateCardControls(){
 const locked=!templateWritable()||templateSaving;
 for(const id of ['file','text','original'])$('template-card-'+id).disabled=locked||templateCardBusy;
 $('template-card-parse').disabled=locked||templateCardBusy||!$('template-card-text').value.trim();
 $('template-card-parse').textContent=templateCardBusy?'正在识别…':'识别角色卡';
 $('template-card-clear').disabled=locked;
 const profile=templateCardCandidate(),length=profile?Array.from(serializeTemplateProfile(profile)).length:0;
 $('template-card-apply').disabled=locked||templateCardBusy||!profile||!length||length>60000;
 $('template-card-length').textContent=profile?'填入后人格共 '+length.toLocaleString()+' 字 / 60,000 字'+(length>60000?'。超过总长度限制，请缩短原文后重新识别，或取消保留原文。':'。'):'';
 if(templateCardBusy||$('template-card-text').value.trim()){
  $('template-save').disabled=true;
 }
}
function decodeTemplateCard(buffer){
 const bytes=new Uint8Array(buffer);let text;
 try{
  if(bytes[0]===0xff&&bytes[1]===0xfe)text=new TextDecoder('utf-16le',{fatal:true}).decode(bytes);
  else if(bytes[0]===0xfe&&bytes[1]===0xff)text=new TextDecoder('utf-16be',{fatal:true}).decode(bytes);
  else{try{text=new TextDecoder('utf-8',{fatal:true}).decode(bytes);}catch{text=new TextDecoder('gb18030',{fatal:true}).decode(bytes);}}
 }catch{throw new Error('无法读取文字编码，请另存为 UTF-8 后重试。');}
 if(/[\x00-\x08\x0b\x0c\x0e-\x1f]/.test(text))throw new Error('文件包含非文本内容，请导出为文本角色卡。');
 return text.replace(/^\ufeff/,'');
}
function templateCardCRC(bytes){
 let crc=0xffffffff;
 for(const byte of bytes){crc^=byte;for(let n=0;n<8;n++)crc=(crc>>>1)^((crc&1)?0xedb88320:0);}
 return (crc^0xffffffff)>>>0;
}
function readTemplatePNGCard(buffer){
 const bytes=new Uint8Array(buffer),view=new DataView(buffer),signature=[137,80,78,71,13,10,26,10];
 if(!signature.every((v,i)=>bytes[i]===v))throw new Error('PNG 文件格式无效。');
 let offset=8,ended=false,found=null,unsupported=false;
 const ascii=new TextDecoder('utf-8',{fatal:true});
 while(offset+12<=bytes.length){
  const size=view.getUint32(offset),end=offset+12+size;
  if(end>bytes.length)throw new Error('PNG 文件不完整，请重新导出角色卡。');
  const type=String.fromCharCode(...bytes.slice(offset+4,offset+8)),data=bytes.subarray(offset+8,offset+8+size);
  if(['tEXt','iTXt','zTXt'].includes(type)){
   const zero=data.indexOf(0);
   if(zero>0&&zero<80){
    const key=ascii.decode(data.subarray(0,zero));
    if(['chara','ccv3'].includes(key)){
     if(size>400000)throw new Error('PNG 内嵌角色资料过大，请导出为精简 JSON。');
     if(templateCardCRC(bytes.subarray(offset+4,offset+8+size))!==view.getUint32(offset+8+size))throw new Error('PNG 内嵌资料校验失败，请重新导出。');
     let start=zero+1;
     if(type==='zTXt'||type==='iTXt'&&data[start]!==0){unsupported=true;offset=end;continue;}
     if(type==='iTXt'){
      start+=2;let next=data.indexOf(0,start);if(next<0)throw new Error('PNG 内嵌资料格式无效。');
      start=next+1;next=data.indexOf(0,start);if(next<0)throw new Error('PNG 内嵌资料格式无效。');start=next+1;
     }
     try{
      const encoded=ascii.decode(data.subarray(start)).trim();
      const text=encoded.startsWith('{')?encoded:ascii.decode(Uint8Array.from(atob(encoded),c=>c.charCodeAt(0)));
      const value=JSON.parse(text);if(!value||Array.isArray(value)||typeof value!=='object')throw new Error();
      if(!found||key==='ccv3')found={key,text};
     }catch{throw new Error('PNG 中的角色卡数据无效，请改用导出的 JSON 文件。');}
    }
   }
  }
  offset=end;if(type==='IEND'){ended=true;break;}
 }
 if(!ended)throw new Error('PNG 文件不完整，请重新导出角色卡。');
 if(!found)throw new Error(unsupported?'此 PNG 使用压缩角色数据，请从原软件导出 JSON 后导入。':'这张 PNG 没有内嵌角色卡资料；普通头像图片请使用下方头像上传。');
 return found.text;
}
async function readTemplateCardFile(file){
 if(!templateWritable()||templateSaving||templateCardBusy||!file)return;
 const epoch=++templateCardEpoch;templateCardBusy=true;templateControls();
 $('template-card-status').textContent='正在读取角色卡…';
 try{
  const png=/\.png$/i.test(file.name);
  if(!/\.(txt|json|ya?ml|png)$/i.test(file.name))throw new Error('请选择 TXT、JSON、YAML 或内嵌角色数据的 PNG。');
  if(!file.size||file.size>(png?15:2)*1024*1024)throw new Error(png?'PNG 请控制在 15 MB 以内。':'文本文件请控制在 2 MB 以内。');
  const buffer=await file.arrayBuffer();if(epoch!==templateCardEpoch)return;
  const text=png?readTemplatePNGCard(buffer):decodeTemplateCard(buffer);
  if(!text.trim()||Array.from(text).length>60000)throw new Error('角色卡需包含 1～60,000 字，请精简后重试。');
  $('template-card-text').value=text;templateCardResult=null;$('template-card-preview').hidden=true;
  $('template-card-status').textContent='已读取“'+file.name+'”，共 '+Array.from(text).length.toLocaleString()+' 字。点击识别后核对结果。';
 }catch(error){if(epoch===templateCardEpoch)$('template-card-status').textContent=error.message+' 已有表单与导入文本保留。';}
 finally{if(epoch===templateCardEpoch){templateCardBusy=false;$('template-card-file').value='';configDirty=templateDirty();templateControls();}}
}
async function recognizeTemplateCard(){
 if(!templateWritable()||templateSaving||templateCardBusy)return;
 const text=$('template-card-text').value.trim();if(!text)return;
 if(Array.from(text).length>60000){$('template-card-status').textContent='角色卡超过 6 万字，请缩短后再识别。';return;}
 const epoch=++templateCardEpoch;templateCardBusy=true;templateCardResult=null;
 templateCardSnapshot=templateCardCurrentSignature();$('template-card-preview').hidden=true;templateControls();
 $('template-card-status').textContent='正在整理人物资料，完成后先预览，不会自动写入表单…';
 try{
  const result=await request('templates/parse-card',{text});
  if(epoch!==templateCardEpoch)return;
  if(!result.profile||typeof result.profile!=='object')throw new Error('识别结果不完整，请重试。');
  templateCardResult=result;
  const box=$('template-card-result');box.replaceChildren();
  const fields=[['display_name','人格名称'],['intro','一句介绍'],...templateProfileFields.map(([key,title])=>[key,title])];
  for(const [key,title] of fields){const value=result.profile[key];if(typeof value!=='string'||!value.trim())continue;const row=el('div');row.append(el('dt',title),el('dd',value));box.append(row);}
  if(!box.childElementCount)box.append(el('p','未拆出明确分项，可保留原文后继续手动整理。'));
  const titles=new Map(fields);
  $('template-card-result-note').textContent=result.mode+' · '+(result.notice||'')+
   (result.truncated?.length?' 以下字段超过表单上限，预览已缩短：'+result.truncated.map(key=>titles.get(key)||key).join('、')+'。保留原文可供核对。':'')+
   (result.source_excerpted?' 长卡已分段节选供模型识别；完整原文仍保留。':'');
  $('template-card-preview').hidden=false;$('template-card-status').textContent='识别完成，请核对后确认填入。';$('template-card-result-heading').focus();
 }catch(error){if(epoch===templateCardEpoch)$('template-card-status').textContent=(error.message||'识别失败。')+' 表单和原文已保留，可重试。';}
 finally{if(epoch===templateCardEpoch){templateCardBusy=false;configDirty=templateDirty();templateControls();}}
}
function applyTemplateCard(){
 if(!templateWritable()||templateSaving||templateCardBusy||!templateCardResult||$('template-card-apply').disabled)return;
 const profile=templateCardCandidate();
 const changed=templateCardCurrentSignature()!==templateCardSnapshot;
 if((changed||templateProfilePrompt().trim())&&!confirm(changed?'识别期间表单已修改。仍要用这份角色卡替换人格分项吗？':'确认替换当前人格分项？请在填入后核对，并手动保存草稿。'))return;
 for(const [key] of templateProfileFields)$('template-profile-'+key).value=profile[key]||'';
 for(const [field,key] of [['name','display_name'],['intro','intro']])if(profile[key])$('template-'+field).value=profile[key];
 templateCardReset();$('template-legacy-note').hidden=true;$('template-preview').hidden=true;
 $('template-feedback').textContent='角色卡已填入表单，请核对各项后点击“保存草稿”。发布仍需另行确认。';
 configDirty=templateDirty();templateControls();$('template-profile-heading').tabIndex=-1;$('template-profile-heading').focus();
}
function initTemplateCard(){
 $('template-card-file').onchange=e=>void readTemplateCardFile(e.target.files[0]);
 $('template-card-text').addEventListener('input',()=>{
  templateCardEpoch++;templateCardResult=null;$('template-card-preview').hidden=true;
  $('template-card-status').textContent='文本已修改，请点击“识别角色卡”重新识别。';
 });
 $('template-card-parse').onclick=()=>void recognizeTemplateCard();
 $('template-card-clear').onclick=()=>{templateCardReset();$('template-card-import').open=true;configDirty=templateDirty();templateControls();};
 $('template-card-original').onchange=templateControls;
 $('template-card-apply').onclick=applyTemplateCard;
}

// public-stickers-v1: central administration, explicit upload/save, bounded pages.
let poolPage=1,poolPages=1,poolRecord=null,poolImage='',poolBusy=false,poolReadSequence=0,poolFileSequence=0,poolRequestId='';
const poolWritable=()=>identity?.role==='superadmin';
function poolId(){return crypto.randomUUID().replaceAll('-','');}
function poolOpen(item=null){
 if(!item){$('pool-files').click();return;}
 poolFileSequence++;poolRecord=item;poolImage='';poolRequestId=poolId();$('pool-editor').hidden=false;$('pool-editor-title').textContent=item?'编辑公共表情':'上传新表情';$('pool-save').textContent=item?'保存说明':'上传并上架';$('pool-file').value='';$('pool-file').disabled=!!item;
 for(const key of ['label','mood','description','text','scenes','avoid']){const value=key==='label'||key==='mood'?item?.[key]:item?.profile?.[key];$('pool-'+key).value=Array.isArray(value)?value.join('\n'):value||'';}
 $('pool-preview').hidden=!item;if(item)$('pool-preview').src=item.thumbnail;else $('pool-preview').removeAttribute('src');$('pool-editor-status').textContent='';poolEditorDirty=false;poolDirty();$('pool-label').focus();
}
function poolClose(){poolFileSequence++;$('pool-editor').hidden=true;poolRecord=null;poolImage='';poolRequestId='';poolEditorDirty=false;poolDirty();}
async function poolMutate(operation,payload){
 if(!poolWritable()||poolBusy)return;poolBusy=true;poolBatchUpdate();$('pool-save').disabled=true;$('pool-cancel').disabled=true;$('pool-new').disabled=true;for(const b of document.querySelectorAll('#pool-items button'))b.disabled=true;for(const input of $('pool-editor').querySelectorAll('input,select,textarea'))input.disabled=true;
 try{await request('public-stickers/'+operation,payload);if(operation==='save')poolClose();await poolRead();if(poolRecord?.id===payload.id&&operation!=='save')poolClose();$('pool-status').textContent={save:payload.id?'表情说明已保存。':'公共表情已上传并上架。',toggle:payload.enabled?'公共表情已上架。':'公共表情已下架。',delete:'公共表情已删除。'}[operation];}
 catch(error){$('pool-editor-status').textContent=error.message+' 表单已保留；若结果未确认，请先刷新列表核对。';$('pool-status').textContent=error.message;}
 finally{poolBusy=false;poolBatchUpdate();for(const input of $('pool-editor').querySelectorAll('input,select,textarea'))input.disabled=!poolWritable();$('pool-file').disabled=!!poolRecord||!poolWritable();$('pool-save').disabled=!poolWritable();$('pool-cancel').disabled=false;$('pool-new').disabled=!poolWritable();for(const b of document.querySelectorAll('#pool-items button'))b.disabled=!poolWritable();}
}

// Shared pool upload follows the personal sticker queue; publishing stays explicit.
const poolMoods=['开心','亲昵撒娇','想念','安慰鼓励','难过委屈','生气无语','疑问惊讶','害羞','打招呼','告别','感谢','抱歉','赞同','拒绝','搞怪','嘲讽'];
const poolBatch={items:[],count:0,limit:500,preparing:false,recognizing:0,running:false,pause:false,epoch:0,failures:0,unavailable:false};
let poolEditorDirty=false;
const poolPending=()=>poolBatch.items.filter(item=>item.status!=='done');
const poolValid=item=>item.ready&&poolMoods.includes(item.mood)&&item.label.trim()&&item.description.trim();
const poolSize=size=>size<1024*1024?Math.ceil(size/1024)+' KB':(size/1024/1024).toFixed(1)+' MB';
function poolDirty(){if(document.body.dataset.adminView==='public-stickers')configDirty=poolEditorDirty||poolPending().length>0||poolRecognitionDirty();}
function poolCategory(select,current=''){select.replaceChildren();const option=el('option','待确认 / 自动识别');option.value='';select.append(option);for(const mood of poolMoods){const option=el('option',mood);option.value=mood;select.append(option);}select.value=current;}
function poolBatchUpdate(){
 const batch=poolBatch,pending=poolPending(),ready=pending.filter(poolValid),recognizing=batch.recognizing>0||($('pool-auto-analyze').checked&&!batch.unavailable&&pending.some(item=>item.ready&&item.analysis==='waiting'));
 $('pool-queue-summary').textContent=batch.items.length?'本批 '+batch.items.length+' 张 · 已上传 '+(batch.items.length-pending.length)+' 张 · 待处理 '+pending.length+' 张':'还没有待上传图片';
 $('pool-capacity').textContent='已上传 '+batch.count+' / '+batch.limit+' 张';
 $('pool-upload').textContent=batch.running?'正在上传…':batch.preparing?'正在处理图片…':recognizing?'正在识别图片…':ready.length?'上传并上架 '+ready.length+' 张图片':'上传并上架';
 $('pool-upload').disabled=poolBusy||batch.preparing||recognizing||!ready.length;
 for(const id of ['pool-pick','pool-files','pool-new','pool-prefix','pool-batch-mood','pool-auto-analyze'])$(id).disabled=poolBusy||!poolWritable();
 $('pool-clear').disabled=poolBusy||!batch.items.length;$('pool-apply-mood').disabled=poolBusy||!pending.length;
 $('pool-reanalyze').disabled=poolBusy||recognizing||!pending.some(item=>item.ready&&item.analysis==='error');
 $('pool-pause').hidden=!batch.running;$('pool-pause').disabled=batch.pause;$('pool-pause').textContent=batch.pause?'本张完成后暂停…':'暂停后续上传';
 $('pool-drop').setAttribute('aria-disabled',String(poolBusy));$('pool-queue').setAttribute('aria-busy',String(batch.running||batch.preparing));
 $('pool-progress').hidden=!batch.items.length;$('pool-progress').max=Math.max(1,batch.items.length);$('pool-progress').value=batch.items.length-pending.length;
 for(const item of batch.items){
  if(!item.node)continue;item.node.dataset.status=item.status;
  item.statusNode.textContent=item.status==='done'?'已上传并上架':item.status==='uploading'?'正在上传…':item.status==='preparing'?'正在检查、压缩或转换…':item.status==='failed'?item.error:item.ready?poolSize(item.ready.size)+(item.ready!==item.file?' · 已处理':''):'等待处理';
  item.statusNode.classList.toggle('pool-status-error',item.status==='failed');
  item.analysisNode.textContent=item.status==='done'?'':item.analysis==='working'?'正在识别分类和画面含义…':item.analysis==='error'?item.analysisError:item.analysis==='done'?(item.mood?'识别完成，可核对后上架。':'分类待确认，请手动选择后上架。'):$('pool-auto-analyze').checked?'等待识别…':'自动识别已关闭，请手动填写。';
  for(const input of item.node.querySelectorAll('input,select,textarea,button'))input.disabled=poolBusy||item.status==='done';
  item.remove.disabled=poolBusy;item.remove.textContent=item.status==='done'?'移出列表':'移除';item.retry.hidden=!!item.ready&&item.analysis!=='error'||item.status==='done';item.retry.textContent=item.ready?'重试识别':'重试这张图片';
 }
 poolDirty();
}
function poolQueueRender(){
 const box=$('pool-queue');box.replaceChildren();
 for(const item of poolBatch.items){
  const row=el('article',undefined,'pool-pending'),image=el('img'),body=el('div',undefined,'pool-pending-body'),fields=el('div',undefined,'pool-pending-fields');image.src=item.url;image.alt=item.file.name;image.width=112;image.height=112;image.loading='lazy';
  const controls={};
  function field(key,label,tag='input',maximum=40){const wrap=el('label',label),input=el(tag);input.value=item[key];if(tag!=='select')input.maxLength=maximum;else poolCategory(input,item[key]);input.setAttribute('aria-label',item.file.name+'的'+label);input.oninput=()=>{item[key]=input.value;item.edited.add(key);poolBatchUpdate();};wrap.append(input);controls[key]=input;return wrap;}
  fields.append(field('label','图片名称'),field('mood','情绪分类','select'));body.append(fields,field('description','画面含义与语气','textarea',300));
  const details=el('details'),summary=el('summary','图中文字和适用场景（选填）');details.append(summary,field('text','图中可见文字','input',200),field('scenes','适用场景，每行一项','textarea',480),field('avoid','不适用场景，每行一项','textarea',480));body.append(details);
  const status=el('p','', 'help'),analysis=el('p','','help'),buttons=el('div',undefined,'pool-queue-controls');status.setAttribute('role','status');analysis.setAttribute('role','status');
  const remove=el('button','移除','quiet'),retry=el('button','重试这张图片','quiet');remove.type=retry.type='button';
  remove.onclick=()=>{if(poolBusy)return;item.controller?.abort();URL.revokeObjectURL(item.url);poolBatch.items=poolBatch.items.filter(v=>v!==item);poolQueueRender();};
  retry.onclick=()=>{if(poolBusy)return;poolBatch.failures=0;poolBatch.unavailable=false;if(!item.ready){item.status='queued';item.error='';void poolPrepareQueue();}else{item.analysis='waiting';item.analysisError='';$('pool-auto-analyze').checked=true;poolAnalyzeQueue();}poolBatchUpdate();};
  buttons.append(retry,remove);body.append(status,analysis,buttons);row.append(poolImageButton(image,()=>poolViewerOpen(poolBatch.items.map(v=>({label:v.label,src:v.url,mood:v.mood})),poolBatch.items.indexOf(item))),body);box.append(row);Object.assign(item,{node:row,controls,statusNode:status,analysisNode:analysis,remove,retry});
 }
 poolBatchUpdate();
}
function poolClearQueue(){poolBatch.epoch++;for(const item of poolBatch.items){item.controller?.abort();URL.revokeObjectURL(item.url);}poolBatch.items=[];poolBatch.failures=0;poolBatch.unavailable=false;poolQueueRender();}
function poolLeave(){poolClearQueue();poolClose();poolRecognitionClear();poolLibrary.selected.clear();if($('pool-viewer').open)$('pool-viewer').close();}
function poolAddFiles(files){
 if(poolBusy||!poolWritable())return;const accepted=[],skipped=[],seen=new Set(poolBatch.items.map(item=>item.signature));let total=poolBatch.items.reduce((sum,item)=>sum+item.file.size,0);
 for(const file of Array.from(files||[])){
  const signature=[file.name,file.size,file.lastModified].join(':');if(seen.has(signature))continue;
  if(!/\.(png|jpe?g|gif|webp)$/i.test(file.name)||!['','image/png','image/jpeg','image/gif','image/webp'].includes(file.type)){skipped.push(file.name+'：格式不支持');continue;}
  if(!file.size||file.size>20*1024*1024){skipped.push(file.name+'：原图需在20 MB以内');continue;}
  if(poolBatch.items.length+accepted.length>=poolBatch.limit){skipped.push('本批最多'+poolBatch.limit+'张');break;}
  if(total+file.size>200*1024*1024){skipped.push('本批原图合计最多200 MB，请分批添加');break;}
  total+=file.size;seen.add(signature);accepted.push({file,signature});
 }
 const prefix=$('pool-prefix').value.trim(),mood=$('pool-batch-mood').value;
 for(const [index,value]of accepted.entries()){const item={...value,label:(prefix?(accepted.length===1?prefix:prefix+' '+(index+1)):value.file.name.replace(/\.[^.]+$/,'')).slice(0,40),mood,description:'',text:'',scenes:'',avoid:'',edited:new Set(mood?['mood']:[]),uploadId:poolId(),status:'queued',error:'',analysis:'waiting',analysisError:'',ready:null,url:URL.createObjectURL(value.file)};poolBatch.items.push(item);}
 poolQueueRender();$('pool-batch-status').textContent=skipped.length?'已添加 '+accepted.length+' 张。'+skipped.slice(0,3).join('；'):accepted.length?'已添加 '+accepted.length+' 张；核对后点击上传，暂未写入公共池。':'这些图片已在列表中。';void poolPrepareQueue();
}
async function poolDataUrl(file){return await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=()=>reject(new Error('图片读取失败，请重新选择。'));reader.readAsDataURL(file);});}
async function poolPrepRequest(path,body,controller=new AbortController()){
 const timer=setTimeout(()=>controller.abort(),27000);
 try{
  const sessionResponse=await fetch('/ai/api/me',{credentials:'same-origin',cache:'no-store',signal:controller.signal}),session=await sessionResponse.json();
  if(!sessionResponse.ok||!session.authenticated||session.username!==identity?.username||!session.csrf)throw new Error('登录状态已变化，请刷新后台后重试。');csrf=session.csrf;
  const binary=body instanceof File,response=await fetch('/ai/admin/api/public-stickers/'+path,{method:'POST',credentials:'same-origin',signal:controller.signal,headers:{'Content-Type':binary?'image/webp':'application/json','X-Requested-With':'xiaoyi-portal','X-CSRF-Token':csrf,'X-Xingyu-Enterprise':enterprise},body:binary?body:JSON.stringify(body)}),data=await response.json();
  if(!response.ok)throw new Error(data.error||'图片处理未完成，可重试或手动填写。');return data;
 }catch(error){if(error.name==='AbortError')throw new Error('图片处理已中止或超时，已填写内容保留，可重试。');throw error;}finally{clearTimeout(timer);}
}
async function poolPrepareQueue(){
 if(poolBatch.preparing)return;poolBatch.preparing=true;
 try{while(true){const item=poolBatch.items.find(v=>v.status==='queued'&&!v.ready);if(!item)break;const epoch=poolBatch.epoch;item.status='preparing';poolBatchUpdate();
  try{const ready=await poolPrepareFile(item.file);if(epoch!==poolBatch.epoch||!poolBatch.items.includes(item))continue;item.ready=ready;item.status='ready';item.error='';poolAnalyzeQueue();}
  catch(error){if(poolBatch.items.includes(item)){item.status='failed';item.error=error.message;}}
  poolBatchUpdate();
 }}finally{poolBatch.preparing=false;poolBatchUpdate();poolAnalyzeQueue();}
}
async function poolRecognitionPreview(file){
 const url=URL.createObjectURL(file),image=new Image();
 try{await new Promise((resolve,reject)=>{image.onload=resolve;image.onerror=()=>reject(new Error('图片无法生成识别预览，可手动填写。'));image.src=url;});const canvas=document.createElement('canvas'),ctx=canvas.getContext('2d');if(!ctx)throw new Error('浏览器无法识别预览。');
  try{for(const edge of [512,384,256]){const scale=Math.min(1,edge/Math.max(image.naturalWidth,image.naturalHeight));canvas.width=Math.max(1,Math.round(image.naturalWidth*scale));canvas.height=Math.max(1,Math.round(image.naturalHeight*scale));ctx.fillStyle='#fff';ctx.fillRect(0,0,canvas.width,canvas.height);ctx.drawImage(image,0,0,canvas.width,canvas.height);const blob=await poolCanvasBlob(canvas,'image/jpeg',.8);if(blob.size<=190*1024)return await poolDataUrl(blob);}throw new Error('识别预览过大，可手动填写。');}finally{canvas.width=canvas.height=1;}
 }finally{image.src='';URL.revokeObjectURL(url);}
}
function poolAnalyzeQueue(){
 if(poolBusy||poolBatch.recognizing||poolBatch.unavailable||!$('pool-auto-analyze').checked)return;
 const item=poolPending().find(v=>v.ready&&v.analysis==='waiting');if(!item)return;
 const epoch=poolBatch.epoch,controller=new AbortController();item.controller=controller;item.analysis='working';poolBatch.recognizing++;poolBatchUpdate();
 (async()=>{try{const preview=await poolRecognitionPreview(item.ready);if(controller.signal.aborted||epoch!==poolBatch.epoch||!poolBatch.items.includes(item))return;const data=await poolPrepRequest('analyze',{image:preview},controller);
  if(epoch!==poolBatch.epoch||!poolBatch.items.includes(item)||!$('pool-auto-analyze').checked||controller.signal.aborted)return;
  const result={mood:data.mood,...data.content};for(const key of ['mood','description','text','scenes','avoid'])if(!item.edited.has(key)&&typeof result[key]==='string'){item[key]=result[key];item.controls[key].value=result[key];}item.analysis='done';poolBatch.failures=0;
 }catch(error){if(epoch===poolBatch.epoch&&poolBatch.items.includes(item)){item.analysis='error';item.analysisError=error.message;if(++poolBatch.failures>=3)poolBatch.unavailable=true;}}
 finally{if(item.analysis==='working')item.analysis='waiting';item.controller=null;poolBatch.recognizing--;if(poolBatch.unavailable)for(const other of poolPending().filter(v=>v.analysis==='waiting')){other.analysis='error';other.analysisError='自动识别暂不可用，可手动填写，或点击重试。';}poolBatchUpdate();poolAnalyzeQueue();}})();
}
async function poolPublishQueue(){
 if(poolBusy)return;const ready=poolPending().filter(poolValid);if(!ready.length)return;
 poolBusy=poolBatch.running=true;poolBatch.pause=false;poolBatchUpdate();let done=0;
 try{for(const item of ready){if(poolBatch.pause)break;if(poolBatch.count>=poolBatch.limit){$('pool-batch-status').textContent='公共池已满，请先删除不需要的表情。';break;}item.status='uploading';poolBatchUpdate();
  try{const content={};for(const key of ['label','mood','description','text','scenes','avoid'])content[key]=item[key];const result=await request('public-stickers/save',{id:'',revision:0,content,image:await poolDataUrl(item.ready),request_id:item.uploadId});item.status='done';item.error='';if(!result.duplicate)poolBatch.count++;done++;}
  catch(error){item.status='failed';item.error=error.message+' 可用此条目重试；结果未确认时先核对已上传列表。';poolBatch.pause=true;}
  poolBatchUpdate();
 }}finally{poolBusy=poolBatch.running=false;await poolRead();poolBatchUpdate();$('pool-batch-status').textContent='本次已上传 '+done+' 张。'+(poolPending().length?'其余图片保留在列表中，可补充说明、重试或继续上传。':'全部完成。');}
}
function initPublicStickers(){
 initPoolLibrary();
 document.querySelector('[data-view="public-stickers"]').hidden=!poolWritable();poolCategory($('pool-batch-mood'));
 $('pool-new').onclick=$('pool-pick').onclick=()=>{$('pool-upload-disclosure').open=true;$('pool-files').click();};$('pool-files').onchange=e=>{poolAddFiles(e.target.files);e.target.value='';};
 $('pool-refresh').onclick=()=>void poolRead();$('pool-prev').onclick=()=>void poolRead(poolPage-1);$('pool-next').onclick=()=>void poolRead(poolPage+1);
 $('pool-clear').onclick=()=>{if(!poolBusy){poolClearQueue();$('pool-batch-status').textContent='已清空待上传列表；已上架的表情仍保留。';}};
 $('pool-apply-mood').onclick=()=>{const mood=$('pool-batch-mood').value;if(!mood){$('pool-batch-status').textContent='请先选择一个固定分类。';return;}for(const item of poolPending()){item.mood=mood;item.edited.add('mood');item.controls.mood.value=mood;}poolBatchUpdate();};
 $('pool-auto-analyze').onchange=()=>{if(!$('pool-auto-analyze').checked)for(const item of poolPending())item.controller?.abort();else{poolBatch.unavailable=false;poolBatch.failures=0;}poolBatchUpdate();poolAnalyzeQueue();};
 $('pool-reanalyze').onclick=()=>{poolBatch.unavailable=false;poolBatch.failures=0;$('pool-auto-analyze').checked=true;for(const item of poolPending())if(item.ready&&item.analysis==='error'){item.analysis='waiting';item.analysisError='';}poolAnalyzeQueue();};
 $('pool-upload').onclick=()=>void poolPublishQueue();$('pool-pause').onclick=()=>{poolBatch.pause=true;poolBatchUpdate();};
 let depth=0;const drop=$('pool-drop');drop.ondragenter=e=>{e.preventDefault();depth++;if(!poolBusy)drop.classList.add('is-over');};drop.ondragover=e=>{e.preventDefault();if(e.dataTransfer)e.dataTransfer.dropEffect=poolBusy?'none':'copy';};drop.ondragleave=e=>{e.preventDefault();if(--depth<=0){depth=0;drop.classList.remove('is-over');}};drop.ondrop=e=>{e.preventDefault();depth=0;drop.classList.remove('is-over');poolAddFiles(e.dataTransfer?.files);};
 for(const event of ['dragover','drop'])window.addEventListener(event,e=>{if(Array.from(e.dataTransfer?.types||[]).includes('Files'))e.preventDefault();});
 $('pool-cancel').onclick=()=>{if(poolEditorDirty&&!confirm('放弃尚未保存的表情说明？'))return;poolClose();};$('pool-editor').oninput=()=>{poolEditorDirty=true;poolDirty();};
 $('pool-editor').onsubmit=e=>{e.preventDefault();if(!poolRecord||poolBusy)return;const content={};for(const key of ['label','mood','description','text','scenes','avoid'])content[key]=$('pool-'+key).value;void poolMutate('save',{id:poolRecord.id,revision:poolRecord.revision,content,image:'',request_id:poolRequestId});};poolBatchUpdate();
}

function poolCanvasBlob(canvas,type,quality){return new Promise((resolve,reject)=>canvas.toBlob(value=>value?resolve(value):reject(new Error('图片处理失败')),type,quality));}
async function poolPrepareFile(file){
  const signature=new Uint8Array(await file.slice(0,24).arrayBuffer());
  const webp=signature.length>=20&&String.fromCharCode(...signature.slice(0,4))==='RIFF'&&String.fromCharCode(...signature.slice(8,12))==='WEBP';
  const animatedWebp=webp&&String.fromCharCode(...signature.slice(12,16))==='VP8X'&&!!(signature[20]&2);
  if(animatedWebp){
    if(file.size>800*1024)throw new Error('动态 WebP 原图需小于 800 KB，请压缩后重试；不会自动转成静态图。');
    const data=await poolPrepRequest('convert',file);
    if(data.mime!=='image/gif'||!data.animated||typeof data.image!=='string')throw new Error('动态 WebP 转换结果无效，请重试；不会自动转成静态图。');
    const body=Uint8Array.from(atob(data.image),c=>c.charCodeAt(0));
    if(body.length>512*1024||!/^GIF8[79]a$/.test(String.fromCharCode(...body.slice(0,6))))throw new Error('WebP 转换后的动画无效，请重试。');
    return new File([body],file.name.replace(/\.[^.]+$/,'.gif'),{type:'image/gif'});
  }
  const png=webp||(signature.length>=24&&[137,80,78,71,13,10,26,10].every((n,i)=>signature[i]===n));
  const jpeg=signature[0]===255&&signature[1]===216&&signature[2]===255;
  const gif=String.fromCharCode(...signature.slice(0,6));
  if(gif==='GIF87a'||gif==='GIF89a'){if(file.size>512*1024)throw new Error('GIF 超过 512 KB，请先压缩动画后重试；不会自动转成静态图。');return file;}
  if(!png&&!jpeg)throw new Error('图片内容无法识别，请选择有效的 PNG、JPEG、GIF 或 WebP。');
  const url=URL.createObjectURL(file),image=new Image();
  try{
    await new Promise((resolve,reject)=>{image.onload=resolve;image.onerror=()=>reject(new Error('图片无法解码，请换一张图片。'));image.src=url;});
    const width=image.naturalWidth,height=image.naturalHeight;
    if(!width||!height||width*height>64000000)throw new Error('图片像素过大，请先缩小到 6400 万像素以内。');
    if(!webp&&file.size<=512*1024&&Math.max(width,height)<=4096&&width*height<=8000000)return file;
    const canvas=document.createElement('canvas'),ctx=canvas.getContext('2d');if(!ctx)throw new Error('浏览器无法压缩图片，请在系统浏览器打开后重试。');
    try{for(const edge of [2048,1536,1024,768,512,384,256]){
      const scale=Math.min(1,edge/Math.max(width,height));canvas.width=Math.max(1,Math.round(width*scale));canvas.height=Math.max(1,Math.round(height*scale));ctx.drawImage(image,0,0,canvas.width,canvas.height);
      for(const quality of png?[undefined]:[.88,.72,.56]){
        const blob=await poolCanvasBlob(canvas,png?'image/png':'image/jpeg',quality);
        if(blob.size<=480*1024)return new File([blob],file.name.replace(/\.[^.]+$/,png?'.png':'.jpg'),{type:blob.type});
      }
    }throw new Error('自动压缩后仍超过 512 KB，请缩小图片再重试。');}finally{canvas.width=canvas.height=1;}
  }finally{image.src='';URL.revokeObjectURL(url);}
}

const poolViewer={items:[],index:0,returnFocus:null};
function poolViewerRender(){
 const item=poolViewer.items[poolViewer.index];if(!item)return;
 $('pool-viewer-title').textContent=item.label||'查看表情';$('pool-viewer-position').textContent=(poolViewer.index+1)+' / '+poolViewer.items.length;
 $('pool-viewer-description').textContent=[item.mood,item.profile?.description].filter(Boolean).join(' · ');
 $('pool-viewer-error').hidden=true;$('pool-viewer-image').alt=item.label||'表情图片';$('pool-viewer-image').src=item.src||item.thumbnail;$('pool-viewer-original').href=item.src||item.thumbnail;
 $('pool-viewer-prev').disabled=poolViewer.index===0;$('pool-viewer-next').disabled=poolViewer.index>=poolViewer.items.length-1;$('pool-viewer-zoom').value='1';$('pool-viewer-image').style.transform='';
}
function poolViewerOpen(items,index=0){
 if(!items.length)return;poolViewer.items=items;poolViewer.index=Math.max(0,Math.min(index,items.length-1));poolViewer.returnFocus=document.activeElement;poolViewerRender();if(!$('pool-viewer').open)$('pool-viewer').showModal();
}
function poolImageButton(image,open){const button=el('button',undefined,'pool-image-button');button.type='button';button.setAttribute('aria-label','放大查看 '+image.alt);button.append(image);button.onclick=open;return button;}
async function poolViewSaved(item){
 try{const data=await request('public-stickers/selection'+poolFilterQuery());const index=data.items.findIndex(v=>v.id===item.id);if(index<0)throw Error('图片已不在当前筛选结果中，请刷新。');poolViewerOpen(data.items,index);}catch(error){$('pool-status').textContent=error.message;}
}
function initPoolViewer(){
 const dialog=$('pool-viewer');$('pool-viewer-close').onclick=()=>dialog.close();dialog.onclick=e=>{if(e.target===dialog){const r=dialog.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)dialog.close();}};
 dialog.onclose=()=>{$('pool-viewer-image').removeAttribute('src');poolViewer.items=[];if(poolViewer.returnFocus?.isConnected)poolViewer.returnFocus.focus({preventScroll:true});};
 const step=amount=>{const next=poolViewer.index+amount;if(next>=0&&next<poolViewer.items.length){poolViewer.index=next;poolViewerRender();}};
 $('pool-viewer-prev').onclick=()=>step(-1);$('pool-viewer-next').onclick=()=>step(1);dialog.onkeydown=e=>{if(e.target.matches('input'))return;if(e.key==='ArrowLeft'||e.key==='ArrowRight'){e.preventDefault();step(e.key==='ArrowLeft'?-1:1);}};
 $('pool-viewer-image').onerror=()=>{$('pool-viewer-error').hidden=false;};$('pool-viewer-zoom').oninput=()=>{$('pool-viewer-image').style.transform='scale('+Number($('pool-viewer-zoom').value)+')';};
}

const poolLibrary={items:[],selected:new Map(),filtered:0};
const poolAnalysisNames={pending:'尚未识别',recognized:'已识别',manual:'已有手动说明'};
function poolFilterQuery(){const query=new URLSearchParams();if($('pool-filter-mood').value)query.set('mood',$('pool-filter-mood').value);if($('pool-filter-analysis').value)query.set('understanding',$('pool-filter-analysis').value);return '?'+query;}
function poolSelectionUpdate(){
 const selected=poolLibrary.selected;$('pool-selected-count').textContent='已选择 '+selected.size+' 张';$('pool-recognize-selected').disabled=!selected.size||poolBusy||poolRecognition.running||poolRecognition.saving;
 $('pool-select-page').checked=!!poolLibrary.items.length&&poolLibrary.items.every(v=>selected.has(v.id));$('pool-select-page').indeterminate=poolLibrary.items.some(v=>selected.has(v.id))&&!$('pool-select-page').checked;
 for(const checkbox of document.querySelectorAll('#pool-items [data-pool-select]'))checkbox.checked=selected.has(checkbox.dataset.poolSelect);
}
async function poolRead(page=poolPage){
 const sequence=++poolReadSequence;poolPage=page;$('pool-status').textContent='正在读取公共表情…';$('pool-refresh').disabled=true;
 try{
  const data=await request('public-stickers'+poolFilterQuery()+'&page='+page);if(sequence!==poolReadSequence)return;
  poolPage=data.page;poolPages=data.pages;poolBatch.count=data.total;poolBatch.limit=data.limit;poolLibrary.items=data.items;poolLibrary.filtered=data.filtered_total;poolBatchUpdate();const box=$('pool-items');box.replaceChildren();
  if(!$('pool-mood').options.length){$('pool-mood').append(el('option','请选择分类'));for(const mood of data.categories){const option=el('option',mood);option.value=mood;$('pool-mood').append(option);}}
  const selectedMood=$('pool-filter-mood').value;$('pool-filter-mood').replaceChildren();const all=el('option','全部分类（'+data.total+'）');all.value='';$('pool-filter-mood').append(all);
  for(const name of data.categories){const option=el('option',name+'（'+data.counts[name]+'）');option.value=name;$('pool-filter-mood').append(option);}$('pool-filter-mood').value=selectedMood;
  for(const item of data.items){
   if(poolLibrary.selected.has(item.id))poolLibrary.selected.set(item.id,item);
   const row=el('article',undefined,'pool-item'),img=el('img'),body=el('div'),buttons=el('div',undefined,'pool-buttons');img.src=item.thumbnail;img.alt=item.label;img.width=112;img.height=112;img.loading='lazy';
   const picker=el('label',undefined,'pool-select-card'),check=el('input');check.type='checkbox';check.dataset.poolSelect=item.id;check.setAttribute('aria-label','选择 '+item.label);check.onchange=()=>{if(check.checked)poolLibrary.selected.set(item.id,item);else poolLibrary.selected.delete(item.id);poolSelectionUpdate();};picker.append(check,el('strong',item.label));
   body.append(picker,el('p',item.mood+' · '+(item.enabled?'已上架':'已下架')+' · '+poolAnalysisNames[item.analysis_status],'help'),el('p',item.profile.description));
   for(const [name,label]of [['recognize','重新识别'],['edit','编辑说明'],['toggle',item.enabled?'下架':'上架'],['delete','删除']]){
    const button=el('button',label,name==='delete'?'danger':'secondary');button.type='button';button.disabled=!poolWritable()||poolBusy;button.onclick=async()=>{
     if(poolBusy)return;if(name==='recognize'){poolRecognitionSet([item]);return;}
     if(name==='edit'){if(poolEditorDirty&&!confirm('放弃尚未保存的表情说明？'))return;poolOpen(item);return;}
     if(name==='delete'&&!confirm('确定删除“'+item.label+'”？已勾选公共池的用户也将无法再使用这张表情。'))return;
     if(name==='toggle'&&!confirm((item.enabled?'下架后所有用户将不再使用':'上架后已勾选公共池的用户可使用')+'“'+item.label+'”，确定吗？'))return;
     await poolMutate(name,{id:item.id,revision:item.revision,...(name==='toggle'?{enabled:!item.enabled}:{})});if(name==='delete'){poolLibrary.selected.delete(item.id);poolSelectionUpdate();}
    };buttons.append(button);
   }
   row.append(poolImageButton(img,()=>void poolViewSaved(item)),body,buttons);box.append(row);
  }
  $('pool-count').textContent='共 '+data.total+' / '+data.limit+' 张';$('pool-filter-count').textContent='当前筛选 '+data.filtered_total+' 张；尚未识别 '+data.understanding_counts.pending+' 张。';
  $('pool-status').textContent=data.filtered_total?'':data.total?'当前分类和状态下没有图片，可更换筛选条件。':'还没有公共表情，可以在上方批量添加。';$('pool-page').textContent=poolPage+' / '+poolPages+' 页';$('pool-prev').disabled=poolPage<=1;$('pool-next').disabled=poolPage>=poolPages;poolSelectionUpdate();
 }catch(error){if(sequence===poolReadSequence){$('pool-items').replaceChildren();$('pool-status').textContent=error.message+' 点击“刷新列表”重试。';}}
 finally{if(sequence===poolReadSequence)$('pool-refresh').disabled=false;}
}

const poolRecognition={items:[],running:false,saving:false,pause:false,epoch:0};
function poolRecognitionDirty(){return poolRecognition.items.some(v=>v.state!=='saved');}
function poolRecognitionUpdate(){
 const batch=poolRecognition,busy=batch.running||batch.saving,ready=batch.items.filter(v=>v.state==='ready');
 $('pool-recognition').hidden=!batch.items.length;$('pool-recognition-start').disabled=busy||!batch.items.some(v=>['waiting','error'].includes(v.state));$('pool-recognition-start').textContent=batch.items.some(v=>v.state==='error')?'继续或重试识别':'开始识别';
 $('pool-recognition-save').disabled=busy||!ready.length;$('pool-recognition-save').textContent=ready.length?'保存 '+ready.length+' 张识别结果':'保存识别结果';$('pool-recognition-clear').disabled=busy;
 $('pool-recognition-pause').hidden=!busy;$('pool-recognition-pause').disabled=batch.pause;$('pool-recognition-pause').textContent=batch.pause?'当前图片完成后暂停…':'暂停后续处理';
 $('pool-recognition-progress').max=Math.max(1,batch.items.length);$('pool-recognition-progress').value=batch.items.filter(v=>['ready','saved'].includes(v.state)).length;
 for(const item of batch.items){
  if(!item.node)continue;item.statusNode.textContent={waiting:'等待识别',working:'正在读取并识别…',ready:item.error||'识别完成，核对后保存',saving:'正在保存…',saved:'已保存识别结果',error:item.error,conflict:item.error}[item.state]||'';item.statusNode.classList.toggle('error',!!item.error||['error','conflict'].includes(item.state));
  for(const input of item.node.querySelectorAll('input,select,textarea'))input.disabled=!item.base||item.state==='saved'||batch.saving;
  item.moodControl.disabled=!item.base||!item.useMood.checked||item.state==='saved'||batch.saving;
 }
 poolSelectionUpdate();poolDirty();
}
function poolRecognitionClear(){poolRecognition.epoch++;poolRecognition.items=[];poolRecognition.pause=false;$('pool-recognition-items').replaceChildren();poolRecognitionUpdate();}
function poolRecognitionSet(items){
 const batch=poolRecognition;if(batch.running||batch.saving||poolBusy)return;
 if(poolRecognitionDirty()&&!confirm('放弃本批尚未保存的识别预览，改为识别新选择的图片？'))return;
 poolRecognitionClear();batch.items=items.map(v=>({id:v.id,label:v.label,thumbnail:v.thumbnail,state:'waiting',error:'',base:null,result:null,draft:null,edited:new Set(),token:'',allowReplace:v.analysis_status==='pending',applyMood:false}));
 const box=$('pool-recognition-items');
 for(const item of batch.items){
  const row=el('article',undefined,'pool-recognition-row'),img=el('img'),body=el('div'),title=el('h3',item.label),status=el('p','','help'),suggestion=el('p','','help');img.src=item.thumbnail;img.alt=item.label;img.loading='lazy';
  const replace=el('input');replace.type='checkbox';replace.checked=item.allowReplace;const replaceLabel=el('label',undefined,'toggle');replaceLabel.append(replace,el('span','用识别结果替换现有说明（已手动改动的字段保留）'));
  const useMood=el('input');useMood.type='checkbox';const moodLabel=el('label',undefined,'toggle');moodLabel.append(useMood,el('span','同时使用建议分类'));
  const mood=el('select');poolCategory(mood,'');mood.setAttribute('aria-label',item.label+'的新分类');mood.oninput=()=>{if(!item.draft)return;item.draft.mood=mood.value;item.edited.add('mood');};
  const current=el('details'),summary=el('summary','查看现有说明'),old=el('p','','help');current.append(summary,old);
  const fields=el('div',undefined,'pool-recognition-fields'),controls={};
  for(const [key,label,maximum] of [['description','画面含义',300],['text','图中可见文字',200],['scenes','适用场景，每行一项',480],['avoid','不适用场景，每行一项',480]]){
   const wrap=el('label',label),input=el('textarea');input.maxLength=maximum;input.setAttribute('aria-label',item.label+'的'+label);input.oninput=()=>{if(!item.draft)return;item.draft[key]=input.value;item.edited.add(key);};controls[key]=input;wrap.append(input);fields.append(wrap);
  }
  replace.onchange=()=>{item.allowReplace=replace.checked;poolRecognitionMerge(item);};useMood.onchange=()=>{item.applyMood=useMood.checked;poolRecognitionMerge(item);};
  body.append(title,status,suggestion,replaceLabel,moodLabel,mood,current,fields);row.append(poolImageButton(img,()=>poolViewerOpen(batch.items.map(v=>({label:v.label,thumbnail:v.thumbnail})),batch.items.indexOf(item))),body);box.append(row);
  Object.assign(item,{node:row,statusNode:status,suggestion,oldNode:old,controls,useMood,moodControl:mood,replaceControl:replace});
 }
 $('pool-recognition-status').textContent='本批 '+batch.items.length+' 张。尚未调用模型；点击“开始识别”后处理。';poolRecognitionUpdate();$('pool-recognition').scrollIntoView({block:'start'});
}
function poolContent(item){const content={label:item.label,mood:item.mood};for(const key of ['description','text','scenes','avoid']){const value=item.profile?.[key]||'';content[key]=Array.isArray(value)?value.join('\n'):value;}return content;}
function poolRecognitionMerge(item){
 if(!item.base||!item.draft)return;
 for(const key of ['description','text','scenes','avoid'])if(!item.edited.has(key)){
  const proposed=item.result?.content?.[key];item.draft[key]=typeof proposed==='string'&&(item.allowReplace||!item.base[key].trim())?proposed:item.base[key];item.controls[key].value=item.draft[key];
 }
 if(!item.edited.has('mood')){item.draft.mood=item.applyMood&&item.result?.mood?item.result.mood:item.base.mood;item.moodControl.value=item.draft.mood;}
 item.moodControl.disabled=!item.applyMood||item.state==='saved'||poolRecognition.saving;
}
async function poolSavedImage(item){
 const response=await fetch(item.thumbnail,{credentials:'same-origin',cache:'no-store'});if(!response.ok)throw Error('图片读取失败，请刷新后重试。');const blob=await response.blob();if(blob.size>512*1024)throw Error('图片超出公共池大小限制。');return blob;
}
async function poolRecognitionRun(){
 const batch=poolRecognition;if(batch.running||batch.saving||poolBusy)return;
 batch.running=true;batch.pause=false;const epoch=batch.epoch;let failures=0;poolRecognitionUpdate();
 try{for(const item of batch.items){
  if(batch.pause||epoch!==batch.epoch)break;if(!['waiting','error'].includes(item.state))continue;
  item.state='working';item.error='';poolRecognitionUpdate();
  try{
   const detail=await request('public-stickers/detail?id='+encodeURIComponent(item.id));
   if(item.snapshot&&item.snapshot!==JSON.stringify([detail.revision,detail.digest])){item.state='conflict';item.error='表情已被修改，请关闭本批、刷新后重新选择。';continue;}
   item.snapshot=JSON.stringify([detail.revision,detail.digest]);item.target=detail;item.base=poolContent(detail);if(!item.draft){item.draft={...item.base};item.allowReplace=detail.analysis_status==='pending';item.replaceControl.checked=item.allowReplace;}item.oldNode.textContent=item.base.mood+' · '+item.base.description;
   poolRecognitionMerge(item);poolRecognitionUpdate();const preview=await poolRecognitionPreview(await poolSavedImage(detail));
   const result=await poolPrepRequest('analyze',{image:preview,id:detail.id,revision:detail.revision,digest:detail.digest});if(epoch!==batch.epoch)break;
   if(typeof result.analysis_token!=='string')throw Error('识别凭据缺失，请重新识别。');item.result=result;item.token=result.analysis_token;item.state='ready';
   item.suggestion.textContent='建议分类：'+(result.mood||'待人工确认')+'。建议说明：'+(result.content.description||'未识别到可靠说明');poolRecognitionMerge(item);failures=0;
  }catch(error){item.state='error';item.error=error.message;if(++failures>=3){batch.pause=true;$('pool-recognition-status').textContent='连续3张识别失败，已暂停；结果和手动编辑已保留。';}}
  poolRecognitionUpdate();
 }}finally{batch.running=false;poolRecognitionUpdate();if(!failures)$('pool-recognition-status').textContent=batch.pause?'已暂停，其余图片可继续识别。':'本批识别完成，请核对预览后保存。';}
}
async function poolRecognitionSave(){
 const batch=poolRecognition;if(batch.running||batch.saving||poolBusy)return;
 const ready=batch.items.filter(v=>v.state==='ready');if(!ready.length)return;
 const count=ready.filter(v=>Object.keys(v.draft).some(k=>v.draft[k]!==v.base[k])).length;
 if(!confirm('确认保存 '+ready.length+' 张识别结果？其中 '+count+' 张的说明或分类将更新；图片、名称和上下架状态保留。'))return;
 batch.saving=poolBusy=true;batch.pause=false;let saved=0;poolBatchUpdate();poolRecognitionUpdate();
 try{for(const item of ready){
  if(batch.pause)break;if(!poolMoods.includes(item.draft.mood)||!item.draft.description.trim()){item.error='分类和画面含义不能为空，请补充后保存。';item.statusNode.textContent=item.error;batch.pause=true;break;}
  item.state='saving';poolRecognitionUpdate();
  try{
   try{await request('public-stickers/save',{id:item.id,revision:item.target.revision,content:item.draft,image:'',request_id:item.saveId||(item.saveId=poolId()),analysis_token:item.token});}
   catch(error){const current=await request('public-stickers/detail?id='+encodeURIComponent(item.id));if(current.digest!==item.target.digest||current.analysis_status!=='recognized'||JSON.stringify(poolContent(current))!==JSON.stringify(item.draft))throw error;}
   item.state='saved';saved++;poolLibrary.selected.delete(item.id);
  }catch(error){item.state='ready';item.error=error.message;item.statusNode.textContent=error.message;batch.pause=true;break;}
 }}finally{batch.saving=poolBusy=false;await poolRead();poolBatchUpdate();poolRecognitionUpdate();$('pool-recognition-status').textContent='已保存 '+saved+' 张。'+(batch.pause?'处理已暂停，未保存的预览保留；请核对错误后重试。':'其余结果仍保留在本页。');}
}
function initPoolLibrary(){
 initPoolViewer();$('pool-filter-mood').onchange=$('pool-filter-analysis').onchange=()=>void poolRead(1);
 $('pool-select-page').onchange=e=>{for(const item of poolLibrary.items)if(e.target.checked)poolLibrary.selected.set(item.id,item);else poolLibrary.selected.delete(item.id);poolSelectionUpdate();};
 $('pool-select-filtered').onclick=async()=>{const button=$('pool-select-filtered');button.disabled=true;try{const data=await request('public-stickers/selection'+poolFilterQuery());for(const item of data.items)poolLibrary.selected.set(item.id,item);poolSelectionUpdate();}catch(error){$('pool-status').textContent=error.message;}finally{button.disabled=false;}};
 $('pool-selection-clear').onclick=()=>{poolLibrary.selected.clear();poolSelectionUpdate();};$('pool-recognize-selected').onclick=()=>poolRecognitionSet([...poolLibrary.selected.values()]);
 $('pool-recognition-start').onclick=()=>void poolRecognitionRun();$('pool-recognition-pause').onclick=()=>{poolRecognition.pause=true;poolRecognitionUpdate();};$('pool-recognition-save').onclick=()=>void poolRecognitionSave();
 $('pool-recognition-clear').onclick=()=>{if(poolRecognitionDirty()&&!confirm('关闭后将放弃本批未保存的识别结果，已保存内容保留。'))return;poolRecognitionClear();};
 $('pool-preview').onclick=()=>{if(poolRecord)void poolViewSaved(poolRecord);};$('pool-preview').tabIndex=0;$('pool-preview').setAttribute('role','button');$('pool-preview').setAttribute('aria-label','放大查看当前表情');$('pool-preview').onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();$('pool-preview').click();}};
}

// Presentation matches the personal settings bill view. All reads use the
// administrator's scoped, read-only endpoint; no billing engine is initialized.
let profileRechargePage=1,profileLedgerPage=1,profileLedgerEpoch=0;
const profileOpenBills=new Set();
function resetProfileRecords(){
 profileRechargePage=profileLedgerPage=1;profileLedgerEpoch++;profileOpenBills.clear();
 $('profile-usage-details').open=false;$('profile-ledger').open=false;
}
const profileRecordMoney=value=>{
  if(value===null||value===undefined||!Number.isFinite(Number(value)))return '待核对';
  const amount=Number(value);
  if(amount!==0&&Math.abs(amount)<0.1)return (amount<0?'扣除不足':'不足')+' 0.1 星币';
  return new Intl.NumberFormat('zh-CN',{minimumFractionDigits:1,maximumFractionDigits:1}).format(amount)+' 星币';
};
function profileRecordRows(target,items,usage){
  const box=typeof target==='string'?document.querySelector(target):target;box.replaceChildren();
  if(!items.length){emptyLines(box,usage?'这一页暂无用量记录。新的聊天用量会自动记录。':'暂无记录。');return;}
  const states={waiting:'待核对',queued:'待扣费',insufficient:'余额不足，待核对',paid:'已扣费',excluded:'不扣费',reversed:'已冲正'};
  const kinds={token:'模型调用',image:'图片生成',tts:'语音生成',gift:'首次赠送',daily_gift:'公测每日赠送',daily_expire:'活动星币到期',credit:'充值',debit:'消费扣费',reversal:'冲正'};
  const reasons={source_unavailable:'原始用量暂不可核对',awaiting_delivery:'等待回复发出',actual_token_usage:'按实际用量计费',internal_excluded:'历史内部处理，未收费',not_delivered_excluded:'未成功发出，不扣费',unpriced:'待定价',usage_missing:'实际用量缺失',cache_usage_missing:'缓存用量缺失',calendar_unconfirmed:'分时日历待核对',pending_or_unknown:'结果待确认',failed_excluded:'请求失败，不扣费'};
  for(const item of items){
    const row=el('div','','balance-line'),main=el('div','','balance-line-main');
    const purposes={rolling_summary:'对话摘要整理',sticker_analysis:'表情包内容识别',organization:'消息分段整理',repair:'回复修复',chat:'对话回复',proactive:'主动消息',greeting:'定时问候',memory:'记忆整理',persona_card:'角色资料解析',reminder:'提醒解析',voice_planning:'语音规划',voice_preview:'语音试听',customer_service:'客服辅助'};
    const purposeLabel=item.kind==='image'?'图片生成':item.kind==='tts'?'语音生成':(purposes[item.purpose]||'其他内容处理')+(item.is_retry?'（重试）':'');
    main.append(el('strong',usage?purposeLabel:kinds[item.kind]||'流水'));
    const meta=[date(item.created),usage?'':(item.purpose?purposeLabel:''),item.agent?'应用 '+item.agent:'',item.scene==='proactive'?'主动消息':item.scene==='chat'?'聊天回复':item.scene==='greeting'?'定时问候':''].filter(Boolean);
    main.append(el('span',meta.join(' · '),'help'));
    if(usage){
      const unit=item.kind==='token'?'输入 '+(item.input_tokens??'—')+' / 输出 '+(item.output_tokens??'—')+' Token':item.kind==='image'?(item.image_count??'—')+' 张':(item.generated_chars??'—')+' 字符';
      main.append(el('span',unit,'help'),el('span',(item.review?'需复核 · ':'')+(states[item.state]||'待核对')+(reasons[item.reason]?' · '+reasons[item.reason]:''),'help'));
    }else main.append(el('span','流水后余额 '+profileRecordMoney(item.balance),'help'));
    const amount=el('span',(!usage&&Number(item.amount)>0?'+':'')+profileRecordMoney(item.amount),'balance-amount');row.append(main,amount);box.append(row);
  }
}
function profileBillTags(group){
  if(group.scene==='sticker_batch')return [['本批 '+group.item_count+' 项识别',false]];
  const tags=[];
  if(group.has_voice)tags.push(['含语音 '+group.voice_count+' 条',false]);
  if(group.has_image)tags.push(['含图片 '+group.image_count+' 张',false]);
  if(group.voice_pending)tags.push(['语音待确认',true]);
  else if(group.voice_attempts&&!group.has_voice)tags.push(['语音未生成',true]);
  if(group.image_pending)tags.push(['图片待确认',true]);
  else if(group.image_attempts&&!group.has_image)tags.push(['图片未生成',true]);
  if(!tags.length)tags.push([group.scene==='standalone'?'独立处理':'纯文字',false]);
  return tags;
}
function profileBillState(group){
  const names={processing:'结算中',waiting:'部分费用待核对',queued:'待扣费',insufficient:'余额不足，待扣费',review:'需复核',reversed:'含已冲正费用',excluded:'本轮未扣费'};
  return group.state==='paid'?(group.turn_state==='unknown'?'已计费':'已结算'):names[group.state]||'待核对';
}
function profileBillRows(groups){
  const box=$('profile-bills'),walletId=profileWallet;box.replaceChildren();
  if(!groups.length){emptyLines(box,'暂无用量记录。新的对话会按轮汇总，语音和图片也会在这里标出。');return;}
  for(const group of groups){
    const details=el('details','','turn-bill'),summary=el('summary','','bill-summary');details.dataset.group=group.id;
    const title=({chat:'本轮对话',proactive:'主动消息',greeting:'每日问候',sticker_batch:'表情包整理',standalone:'独立处理'})[group.scene]||'本轮对话';
    summary.append(el('span',title,'bill-title'),el('span',(group.unknown_count&&group.amount!==null?'已确认 ':'')+profileRecordMoney(group.amount),'bill-amount'));
    const tags=el('span','','bill-tags');
    for(const [label,warning] of profileBillTags(group)){const tag=el('span',label,'bill-tag');tag.dataset.warning=String(warning);tags.append(tag);}
    if(group.batch_inferred)tags.append(el('span','历史连续整理归并','bill-tag'));
    summary.append(tags,el('span',date(group.created)+' · 应用 '+group.agent+' · '+profileBillState(group)+' · '+group.item_count+' 项费用','bill-meta'));
    const toggle=el('span',group.scene==='sticker_batch'?'查看本批明细':'查看明细','bill-disclosure');summary.append(toggle);
    const body=el('div','','bill-detail');body.setAttribute('role','region');body.setAttribute('aria-label',title+'计费明细');
    let loading=false,loaded=false,detailSeq=0;
    async function loadDetail(page=1){
      if(loading)return;loading=true;const seq=++detailSeq;body.setAttribute('aria-busy','true');body.replaceChildren(el('p','正在读取本轮明细…','help'));
      try{
        const r=await request('operations/wallet?'+new URLSearchParams({wallet:walletId,view:'records',group:group.id,page}));
        if(seq!==detailSeq||!details.isConnected||profileWallet!==walletId)return;
        if(r.bill_view!=='turns-v1'||!Array.isArray(r.items))throw new Error('账单明细暂未就绪，请稍后重试。');
        body.replaceChildren();
        const items=el('div','','bill-items');profileRecordRows(items,r.items,true);body.append(items);
        if(r.count>r.page_size){
          const nav=el('div','','balance-pagination'),prev=el('button','明细上一页','secondary'),next=el('button','明细下一页','secondary');prev.type=next.type='button';
          prev.disabled=r.page<=1;next.disabled=r.page*r.page_size>=r.count;prev.onclick=()=>loadDetail(r.page-1);next.onclick=()=>loadDetail(r.page+1);
          nav.append(prev,el('span','明细第 '+r.page+' 页 · 共 '+r.count+' 项','help'),next);body.append(nav);
        }
        body.append(el('p',group.scene==='sticker_batch'?'各项费用已包含在本批合计中，不会重复扣费。':'各项费用已包含在本轮合计中，不会重复扣费。','help'));
        loaded=true;
      }catch(error){
        if(!details.isConnected||profileWallet!==walletId)return;
        const retry=el('button','重试读取明细','secondary');retry.type='button';retry.onclick=()=>loadDetail(page);
        body.replaceChildren(el('p',error.message,'feedback error'),retry);
      }finally{loading=false;body.setAttribute('aria-busy','false');}
    }
    details.append(summary,body);box.append(details);
    details.addEventListener('toggle',()=>{toggle.textContent=details.open?'收起明细':'查看明细';if(details.open){profileOpenBills.add(group.id);if(!loaded&&!loading)loadDetail();}else profileOpenBills.delete(group.id);});
    if(profileOpenBills.has(group.id))details.open=true;
  }
}

function renderProfileRecords(data){
 const box=$('profile-recharge-items');box.replaceChildren();
 for(const item of data.recharges){const row=el('div',undefined,'balance-line'),main=el('div',undefined,'balance-line-main');
  main.append(el('strong',item.kind==='daily_gift'?'公测每日赠送（当天有效）':item.kind==='gift'?'首次赠送（非充值）':'星币充值'),el('span',date(item.created),'help'));
  row.append(main,el('span','+'+profileRecordMoney(item.amount),'balance-amount'));box.append(row);
 }
 emptyLines(box,'暂无充值或赠送记录。');profileRechargePage=data.recharge_page;
 $('profile-recharge-page').textContent='第 '+data.recharge_page+' 页 · 共 '+data.recharge_count+' 笔';
 $('profile-recharge-prev').disabled=data.recharge_page<=1;$('profile-recharge-next').disabled=data.recharge_page*data.recharge_page_size>=data.recharge_count;
 const stats=$('profile-usage-summary');stats.replaceChildren();
 for(const days of [1,7,30]){const item=data.usage_summary?.periods?.find(x=>x.days===days),cell=el('div');
  cell.append(el('dt','最近'+days+'天'),el('dd',item?profileRecordMoney(item.amount):'—'));stats.append(cell);
 }
 $('profile-bill-count').textContent='共 '+data.bill_count+' 笔';
 $('profile-events').textContent='待核对用量 '+(data.event_counts.waiting||0)+' 项 · 余额不足 '+(data.event_counts.insufficient||0)+' 项。';
 profileBillRows(data.bill_groups);pageControls('profile',data);
 // Refreshing records also invalidates a previously opened raw ledger.
 profileLedgerEpoch++;$('profile-ledger').dataset.loaded='';
 if($('profile-ledger').open)loadProfileLedger(profileLedgerPage);
}
async function loadProfileLedger(page=profileLedgerPage){
 const epoch=++profileLedgerEpoch,walletId=profileWallet,message=$('profile-ledger-status');
 message.textContent='正在读取原始流水…';$('profile-ledger').setAttribute('aria-busy','true');
 $('profile-ledger-prev').disabled=$('profile-ledger-next').disabled=true;$('profile-ledger-retry').hidden=true;
 const body=$('profile-rows');body.replaceChildren();$('profile-ledger-page').textContent='';
 try{
  const data=await request('operations/wallet?'+new URLSearchParams({wallet:walletId,view:'ledger',page}));
  if(epoch!==profileLedgerEpoch||walletId!==profileWallet)return;profileLedgerPage=data.page;
  for(const item of data.items){const row=el('tr'),time=el('td',date(item.created));time.append(el('small','#'+item.id));
   row.append(time,el('td',({gift:'首次赠送',daily_gift:'公测每日赠送',daily_expire:'活动星币到期',credit:'充值到账',debit:'消费扣费',reversal:'冲正'}[item.kind]||item.kind)),el('td',(Number(item.amount)>0?'+':'')+coin(item.amount),'number'),el('td',coin(item.balance),'number'));
   const ref=el('td');if(item.order_id)ref.append(button(item.order_id,()=>showOrder(item.order_id),'quiet'));else if(item.request_id)ref.append(el('span',item.request_id),el('small','应用 '+item.agent+' · '+(shadowKinds[item.resource]||item.resource)));else ref.textContent='—';row.append(ref);body.append(row);
  }
  safeTableEmpty('profile-rows',data.items.length,5,'账户尚无流水。');
  $('profile-ledger-page').textContent='第 '+data.page+' 页 · 共 '+data.count+' 笔';
  $('profile-ledger-prev').disabled=data.page<=1;$('profile-ledger-next').disabled=data.page*data.page_size>=data.count;
  message.textContent='原始金额按完整精度保留。';$('profile-ledger').dataset.loaded='true';
 }catch(error){if(epoch!==profileLedgerEpoch||walletId!==profileWallet)return;message.textContent=error.message;$('profile-ledger-retry').hidden=false;}
 finally{if(epoch===profileLedgerEpoch)$('profile-ledger').setAttribute('aria-busy','false');}
}
function initProfileRecords(){
 for(const [direction,delta] of [['prev',-1],['next',1]]){
  $('profile-recharge-'+direction).onclick=()=>{profileRechargePage=Math.max(1,profileRechargePage+delta);loadProfile();};
  $('profile-ledger-'+direction).onclick=()=>loadProfileLedger(Math.max(1,profileLedgerPage+delta));
 }
 $('profile-ledger-retry').onclick=()=>loadProfileLedger();
 $('profile-ledger').addEventListener('toggle',()=>{if($('profile-ledger').open&&!$('profile-ledger').dataset.loaded)loadProfileLedger();});
}
