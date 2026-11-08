'use strict';
const panels=[...document.querySelectorAll('[data-panel]')];
const steps=[...document.querySelectorAll('[data-step]')];
function setStep(step){
  panels.forEach(panel=>{panel.hidden=Number(panel.dataset.panel)!==step;});
  steps.forEach(item=>{const active=Number(item.dataset.step)===step;item.classList.toggle('active',active);item.setAttribute('aria-current',active?'step':'false');});
  window.scrollTo({top:0,behavior:'smooth'});
}
document.querySelectorAll('[data-next]').forEach(button=>button.addEventListener('click',()=>setStep(Number(button.dataset.next))));
document.querySelectorAll('[data-back]').forEach(button=>button.addEventListener('click',()=>setStep(Number(button.dataset.back))));
document.querySelector('[data-finish]').addEventListener('click',()=>setStep(4));
document.querySelector('[data-reset]').addEventListener('click',()=>{document.querySelectorAll('input').forEach(input=>{input.value='';});document.querySelector('#demo-avatar-preview').replaceChildren(document.createTextNode('AI'));setStep(1);});
document.querySelector('#demo-avatar').addEventListener('change',event=>{const file=event.target.files?.[0];if(!file||!file.type.startsWith('image/'))return;const url=URL.createObjectURL(file),image=new Image();image.onload=()=>{const preview=document.querySelector('#demo-avatar-preview');preview.replaceChildren(image);URL.revokeObjectURL(url);};image.src=url;});
setStep(1);
