"""Opt-in daily member greetings; extends the existing personal settings service."""
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
import re
import time

import aiohttp
from aiohttp import web
import kf_app as kf
from reply_pipeline import OrderedDeliveryStopped
import member_app as member

LOG = logging.getLogger('wecom-greetings')
GREETING_VERSION = 1
CHINA = timezone(timedelta(hours=8))
DEFAULT_TEXT = '嗨，今天过得怎么样？记得给自己留一点休息的时间。'
PANEL_HTML = '<!doctype html>\n<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#166356"><meta name="referrer" content="no-referrer"><link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns=\'http://www.w3.org/2000/svg\' viewBox=\'0 0 32 32\'%3E%3Crect width=\'32\' height=\'32\' rx=\'8\' fill=\'%23166356\'/%3E%3Cpath d=\'M7 8h18v12H14l-5 5v-5H7z\' fill=\'none\' stroke=\'white\' stroke-width=\'1.5\'/%3E%3C/svg%3E"><title>小小艺 · 我的设置</title>\n<style>\n:root{color-scheme:light;--ink:#193e37;--muted:#596e67;--accent:#166356;--tint:#edf5f1;--line:#d6e1da;--bg:#f3f6f2;--error:#a33434;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);line-height:1.65}button,input,textarea{font:inherit}button{cursor:pointer}button:disabled{cursor:default;opacity:.55}button:focus-visible{outline:3px solid #a8c6b0;outline-offset:3px}::selection{background:#cae4d4;color:#173e34}[hidden]{display:none!important}.shell{max-width:1080px;padding:30px 32px 38px;margin:auto}.topbar{display:flex;align-items:center;gap:12px;padding-bottom:22px;border-bottom:1px solid var(--line)}.mark{width:42px;height:42px;display:grid;place-items:center;border-radius:12px;background:var(--accent);color:#fff}.mark svg{width:26px;height:26px}.brand{font-size:18px;font-weight:700}.sub{font-size:12px;color:var(--muted)}.private{margin-left:auto;font-size:13px;color:var(--muted)}.heading{padding:32px 0 26px}.heading h1{font-size:30px;line-height:1.3;letter-spacing:-.025em;margin:0 0 10px;font-weight:650}.heading p{font-size:14px;color:var(--muted);margin:0;max-width:65ch}.layout{display:grid;grid-template-columns:minmax(0,1fr) 270px;gap:32px;align-items:start}.editor{background:#fff;border-radius:14px;border:1px solid var(--line);padding:26px}fieldset{margin:0;padding:0;border:0;min-width:0}legend{font-size:16px;font-weight:650;padding:0}.helper{font-size:13px;color:var(--muted);margin:4px 0 18px}.choices{display:grid;grid-template-columns:1fr 1fr;gap:12px}.choice{position:relative;display:block;cursor:pointer}.choice input{position:absolute;opacity:0;width:1px;height:1px;left:18px;top:20px}.choice-body{display:block;padding:16px 17px;border:1px solid var(--line);border-radius:12px;height:100%;transition:background .16s,border-color .16s}.choice input:checked+.choice-body{background:var(--tint);border-color:var(--accent)}.choice input:focus-visible+.choice-body{outline:3px solid #a8c6b0;outline-offset:2px}.choice-name{display:flex;align-items:center;justify-content:space-between;gap:8px;font-size:15px;font-weight:650}.dot{width:16px;height:16px;border:1.5px solid #8b9f96;border-radius:50%;flex-shrink:0;display:grid;place-items:center}.choice input:checked+.choice-body .dot{border-color:var(--accent)}.choice input:checked+.choice-body .dot:after{content:"";height:8px;width:8px;border-radius:50%;background:var(--accent)}.desc{display:block;margin-top:5px;font-size:12px;color:var(--muted)}.custom-choice{grid-column:1/-1}.custom-choice .choice-body{padding-top:14px;padding-bottom:14px}.description{border-top:1px solid var(--line);margin-top:24px;padding-top:21px}.field-top{display:flex;justify-content:space-between;gap:12px;font-size:14px;font-weight:650}.count{color:var(--muted);font-size:12px;font-weight:400;font-variant-numeric:tabular-nums}textarea{display:block;width:100%;min-height:168px;resize:vertical;border:1px solid #b7cbbf;border-radius:10px;padding:12px 14px;line-height:1.8;background:#fff;color:var(--ink);font-size:14px;margin-top:10px;outline:none;caret-color:var(--accent)}textarea:focus{border-color:var(--accent);outline:2px solid #cbe0d2;outline-offset:1px}textarea::placeholder{color:#64796f}textarea:disabled{background:#f5f7f5;color:#53695d}.field-help{color:var(--muted);font-size:12px;margin:8px 0 0}.actions{border-top:1px solid var(--line);padding-top:20px;margin-top:24px;display:flex;align-items:center;flex-wrap:wrap;gap:14px}.primary{padding:11px 23px;border:1px solid var(--accent);background:var(--accent);color:#fff;border-radius:9px;font-size:14px;font-weight:650;min-height:46px}.secondary{border:1px solid #bdcdc4;background:#fff;color:var(--ink);padding:10px 16px;border-radius:9px;font-size:13px;min-height:44px}.text-button{border:0;padding:10px 0;color:var(--muted);background:transparent;font-size:13px;text-decoration:underline;text-underline-offset:4px;min-height:44px}.feedback{font-size:13px;margin:14px 0 0;min-height:22px;color:var(--accent)}.feedback.error{color:var(--error)}.sidebar{padding-top:5px}.sidebar h2{font-size:14px;margin:0 0 13px;font-weight:650}.saved-persona{font-size:23px;letter-spacing:-.025em;margin:0 0 4px;font-weight:650}.saved-note{font-size:12px;color:var(--muted);margin:0}.example{margin-top:25px;padding-top:22px;border-top:1px solid var(--line)}.example-label{font-size:12px;color:var(--muted);margin:0 0 10px}.bubble{background:#dceade;padding:14px 16px;border-radius:3px 14px 14px;font-size:14px;line-height:1.85;margin:0}.fine{font-size:11px;color:var(--muted);margin:8px 0 0}.details{font-size:12px;color:var(--muted);margin-top:24px;padding-top:21px;border-top:1px solid var(--line)}.details p{margin:0 0 13px}.details strong{color:var(--ink);font-weight:600}.session-tools{display:flex;justify-content:space-between;gap:12px;margin-top:17px}.footer{color:var(--muted);font-size:12px;display:flex;justify-content:space-between;gap:20px;margin-top:26px}.gate{max-width:570px;padding:35px 30px;background:#fff;border:1px solid var(--line);border-radius:14px;margin:30px 0 70px}.gate h2{font-size:21px;margin:0 0 12px}.gate p{color:var(--muted);font-size:14px;margin:0 0 15px}.command{font-size:18px;background:var(--tint);border-radius:9px;padding:12px 16px;color:var(--accent);display:inline-block;margin:0 0 17px}.gate .buttons{display:flex;gap:15px;align-items:center}.loading{color:var(--muted);padding:30px 0}.dirty{color:var(--muted);font-size:12px}.spinner{display:inline-block;width:14px;height:14px;border:2px solid #c1d4c7;border-top-color:var(--accent);border-radius:50%;vertical-align:-2px;margin-right:8px;animation:spin .8s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}@media(hover:hover){.choice-body:hover{border-color:#6d9a80}.primary:hover{background:#114f44}.text-button:hover{color:var(--accent)}}@media(max-width:760px){.shell{padding:20px 18px 30px}.heading{padding:26px 0 22px}.heading h1{font-size:26px}.layout{grid-template-columns:1fr;gap:23px}.editor{padding:21px 18px}.sidebar{padding:0 3px}.saved-wrap{display:flex;align-items:baseline;justify-content:space-between;gap:14px}.saved-wrap h2{margin:0}.saved-persona{font-size:19px;margin:0}.example{margin-top:17px;padding-top:17px}.details{display:grid;grid-template-columns:1fr 1fr;gap:15px;margin-top:18px;padding-top:18px}.details p{margin:0}.footer{flex-direction:column;gap:4px;margin-top:23px}.private{font-size:12px}.gate{padding:28px 23px;margin-top:15px}.choice-body{padding:14px 12px}.choice-name{font-size:14px}.heading p{font-size:13px}.session-tools{max-width:320px}}@media(max-width:360px){.shell{padding-left:12px;padding-right:12px}.editor{padding:18px 13px}.choices{gap:9px}.choice-name{font-size:13px}.private{font-size:11px}.actions{gap:10px}.primary{padding-left:18px;padding-right:18px}}@media(prefers-reduced-motion:reduce){*{transition:none!important}.spinner{animation:none}}\n\n.greetings{border-top:1px solid var(--line);margin-top:24px;padding-top:21px}.greetings h2{font-size:16px;font-weight:650;margin:0 0 6px}.greet-toggle{display:flex;align-items:center;gap:10px;min-height:44px;font-size:14px;font-weight:600;cursor:pointer}.greet-toggle input{width:19px;height:19px;accent-color:var(--accent);margin:0;flex-shrink:0}.greet-toggle input:focus-visible{outline:3px solid #a8c6b0;outline-offset:3px}.greet-time-row{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin:12px 0 17px;font-size:14px}.greet-time-row input{min-height:44px;max-width:100%;padding:7px 12px;border:1px solid #b7cbbf;border-radius:9px;background:#fff;color:var(--ink);min-width:130px}.greet-time-row input:focus-visible{outline:2px solid #cbe0d2;outline-offset:1px;border-color:var(--accent)}.greet-time-row input:disabled{background:#f5f7f5;color:#53695d}.greet-time-row span{font-size:12px;color:var(--muted)}#greet-text{min-height:108px}.greet-saved{font-size:12px;color:var(--muted);margin:14px 0 0;padding-left:11px;border-left:1px solid var(--line);overflow-wrap:anywhere}\n</style></head><body>\n<!-- daily-greeting-v1 --><!-- THESIS: 微信成员选择自己的相处方式并保存。 OWN-WORLD: 明亮青绿设置页，实线控件和聊天气泡。 STORY: 选人物，编辑设定，保存并回微信聊天。 FIRST VIEWPORT: 左编辑器右已保存状态，手机按任务排列。 FORM: operate personal settings; seed 0c2c733e. FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, and DESIGN.md -->\n<main class="shell">\n<header class="topbar"><div class="mark" aria-hidden="true"><svg viewBox="0 0 32 32" fill="none"><path d="M6 7h20v13H15l-6 5v-5H6V7Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M12 13h8M12 16h5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg></div><div><div class="brand">小小艺</div><div class="sub">我的专属设置</div></div><span class="private">仅修改你自己的设置</span></header>\n<div class="heading"><h1>让小艺更懂你的表达习惯</h1><p>选择一种相处方式，或者写下你喜欢的称呼、性格和语气。也可以安排每天的一句问候。保存后，回到微信继续聊。</p></div>\n<div id="loading" class="loading" role="status"><span class="spinner" aria-hidden="true"></span>正在读取你的设置…</div>\n<section id="gate" class="gate" hidden><h2 id="gate-title">从微信打开你的专属设置</h2><p id="gate-text">在“小小艺 AI 客服”的企业应用聊天里发送这条指令，再点回复中的链接。</p><code class="command">我要设置</code><div class="buttons"><button id="copy" class="primary" type="button">复制指令</button><button id="retry" class="text-button" type="button">重新连接</button></div><p id="gate-feedback" role="status"></p></section>\n<div class="layout" id="workspace" hidden>\n<form class="editor" id="form"><fieldset id="fields"><legend>选择人物</legend><p class="helper">每个人物分别保留聊天记录，可以随时切换回来。</p><div class="choices" id="choices"></div><div class="description"><div class="field-top"><label for="custom">专属人物设定</label><span class="count" id="count">0 / 4000</span></div><textarea id="custom" aria-describedby="custom-help" placeholder="例如：你叫小艺，语气温柔但不说教，称呼我为小林。回答先给结论，再给两个具体建议；不确定时先问我。"></textarea><p class="field-help" id="custom-help">选择“自定义小艺”后生效。这是完整人物描述，不会自动叠加内置模板。</p></div><section class="greetings" aria-labelledby="greet-title"><h2 id="greet-title">每日主动问候</h2><p class="helper" id="greet-help">每天向你发送一次下方的固定文字，不调用模型生成。默认关闭，保存后生效。</p><label class="greet-toggle"><input id="greet-enabled" type="checkbox" aria-describedby="greet-help"><span>开启每日问候</span></label><div class="greet-time-row"><label for="greet-time">发送时间</label><input id="greet-time" type="time" value="09:00" required aria-describedby="greet-zone"><span id="greet-zone">北京时间（UTC+8）</span></div><div class="field-top"><label for="greet-text">问候文字</label><span class="count" id="greet-count">0 / 300</span></div><textarea id="greet-text" aria-describedby="greet-text-help" placeholder="写一句你想每天收到的话，例如：早上好，今天也记得好好吃饭。"></textarea><p class="field-help" id="greet-text-help">只发给你本人。微信发送 /关闭问候 可停用；/问候状态 可查看。停机错过超过 10 分钟会跳过，不集中补发。</p><p class="greet-saved" id="greet-status" role="status">已保存：每日问候关闭。</p></section></fieldset><div class="actions"><button class="primary" id="save" type="submit" disabled>保存设置</button><button class="text-button" id="default" type="button">选回默认人物</button><span class="dirty" id="dirty" hidden>有未保存的修改</span></div><p class="feedback" id="feedback" role="status" aria-live="polite"></p><button class="secondary" id="reload" type="button" hidden>重新读取最新设置</button></form>\n<aside class="sidebar"><div class="saved-wrap"><h2>微信中正在使用</h2><p class="saved-persona" id="current">—</p></div><p class="saved-note" id="saved-note">已与当前成员的配置连接</p><section class="example"><p class="example-label" id="example-label">语气示例</p><blockquote class="bubble" id="example"></blockquote><p class="fine">固定文案示意，不是模型的实际回复。</p></section><div class="details"><p><strong>下一条新消息生效</strong><br>微信里的 /切换、/设定 也会同步到这里。修改人物描述会开启独立的上下文。</p><p><strong>文字聊天与定时问候</strong><br>语音、生图和表情包需要后续接入。应用名称和头像由管理员设置。</p></div><div class="session-tools"><button class="text-button" id="refresh" type="button">重新读取设置</button><button class="text-button" id="logout" type="button">退出此浏览器</button></div></aside>\n</div><footer class="footer"><span>小小艺 · 个人设置</span><span>专属链接请勿转发；共用设备使用后可退出。</span></footer></main>\n<script>\n\'use strict\';\nconst $=s=>document.querySelector(s);\nconst personas={default:{name:\'默认小艺\',description:\'自然友好，日常聊天和通用问答。\',example:\'你好，今天想聊点什么？我会尽量说得清楚、好懂。\'},gentle:{name:\'温柔小艺\',description:\'耐心倾听，温和回应情绪。\',example:\'可以慢慢说，我在听。我们先照顾好你的感受，再一起想下一步。\'},rational:{name:\'理性小艺\',description:\'分析问题，给出清晰建议。\',example:\'我们先确认目标和限制，再比较几个选择，挑出现在能执行的方案。\'},encourage:{name:\'鼓励小艺\',description:\'陪你拆解目标，迈出下一步。\',example:\'先做最小的一步就好。你想从哪件十分钟能完成的小事开始？\'},custom:{name:\'自定义小艺\',description:\'用你的描述，定义自己的相处方式。\',example:\'你的专属设定会用于下一次回复。保存后回微信发消息，看看小艺的变化。\'}};\nlet state=null,busy=false;\nconst token=new URLSearchParams(location.hash.slice(1)).get(\'token\');\nif(token)history.replaceState(null,\'\',location.pathname);\nclass ApiError extends Error{constructor(message,status){super(message);this.status=status}}\nasync function api(path,options={}){const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),15000);try{const response=await fetch(\'/ai/enterprises/ww0000000000000002/1000020/xiaoyi/api/\'+path,{credentials:\'same-origin\',cache:\'no-store\',...options,signal:controller.signal,headers:{\'Content-Type\':\'application/json\',...(options.headers||{})}});const data=await response.json().catch(()=>({}));if(!response.ok)throw new ApiError(data.error||\'服务暂时不可用，请稍后重试。\',response.status);return data}catch(e){if(e.name===\'AbortError\')throw new Error(\'连接超时，请检查网络后重试。\');if(e instanceof TypeError)throw new Error(\'网络连接失败，请检查网络后重试。\');throw e}finally{clearTimeout(timer)}}\nfunction formValue(){return{selected:document.querySelector(\'input[name="persona"]:checked\')?.value||\'default\',custom_prompt:$(\'#custom\').value.trim(),greeting:{enabled:$(\'#greet-enabled\').checked,time:$(\'#greet-time\').value,text:$(\'#greet-text\').value.trim()}}}\nfunction changed(){if(!state)return false;const d=formValue(),g=state.greeting;return d.selected!==state.selected||d.custom_prompt!==state.custom_prompt||[\'enabled\',\'time\',\'text\'].some(k=>d.greeting[k]!==g[k])}\nfunction feedback(text,error=false){$(\'#feedback\').textContent=text;$(\'#feedback\').className=\'feedback\'+(error?\' error\':\'\')}\nfunction update(){const draft=formValue(),p=personas[draft.selected]||personas.default;$(\'#custom\').disabled=draft.selected!==\'custom\'||busy;$(\'#count\').textContent=Array.from($(\'#custom\').value).length+\' / 4000\';$(\'#greet-time\').disabled=busy||!draft.greeting.enabled;$(\'#greet-text\').disabled=busy||!draft.greeting.enabled;$(\'#greet-count\').textContent=Array.from($(\'#greet-text\').value).length+\' / 300\';$(\'#example-label\').textContent=\'所选人物预览 · 保存后生效（\'+p.name+\'）\';$(\'#example\').textContent=p.example;$(\'#dirty\').hidden=!changed();$(\'#save\').disabled=busy||!changed();$(\'#save\').textContent=busy?\'正在保存…\':\'保存设置\'}\nfunction apply(data){state=data;document.querySelector(\'input[value="\'+(personas[data.selected]?data.selected:\'default\')+\'"]\').checked=true;$(\'#custom\').value=data.custom_prompt||\'\';const g=data.greeting;$(\'#greet-enabled\').checked=g.enabled;$(\'#greet-time\').value=g.time;$(\'#greet-text\').value=g.text;const due=g.next_due?new Intl.DateTimeFormat(\'zh-CN\',{timeZone:\'Asia/Shanghai\',month:\'numeric\',day:\'numeric\',hour:\'2-digit\',minute:\'2-digit\',hourCycle:\'h23\'}).format(new Date(g.next_due*1000)):null;$(\'#greet-status\').textContent=g.enabled?\'已保存：每天 \'+g.time+\'（北京时间）。\'+(due?\'下一次计划：\'+due+\'。\':\'\'):\'已保存：每日问候关闭。\';$(\'#current\').textContent=data.name;$(\'#saved-note\').textContent=\'已与当前成员的配置连接\';$(\'#reload\').hidden=true;update()}\nfunction gate(title,message){$(\'#loading\').hidden=true;$(\'#workspace\').hidden=true;$(\'#gate\').hidden=false;$(\'#gate-title\').textContent=title;$(\'#gate-text\').textContent=message}\nasync function boot(exchangeToken){$(\'#gate\').hidden=true;$(\'#workspace\').hidden=true;$(\'#loading\').hidden=false;try{if(exchangeToken)await api(\'session\',{method:\'POST\',body:JSON.stringify({token:exchangeToken})});apply(await api(\'settings\'));$(\'#loading\').hidden=true;$(\'#workspace\').hidden=false;feedback(\'\')}catch(e){gate(e.status===401?\'请重新获取专属链接\':\'暂时没能打开设置\',e.message)}}\nasync function reload(){if(changed()&&!confirm(\'重新读取会放弃尚未保存的修改，继续吗？\'))return;try{apply(await api(\'settings\'));feedback(\'已读取最新设置。\')}catch(e){feedback(e.message,true);if(e.status===401)gate(\'登录已过期\',\'请回微信发送 我要设置，获取新的专属链接。\')}}\nasync function save(event){event.preventDefault();if(busy||!state||!changed())return;const draft=formValue();if(Array.from(draft.custom_prompt).length>4000){feedback(\'专属人物设定不能超过 4000 字。\',true);return}if(draft.selected===\'custom\'&&!draft.custom_prompt){feedback(\'请先填写专属人物设定。\',true);$(\'#custom\').focus();return}let restored=false;if(!draft.greeting.enabled){if(Array.from(draft.greeting.text).length>300){draft.greeting.text=state.greeting.text;restored=true}if(!/^(?:[01][0-9]|2[0-3]):[0-5][0-9]$/.test(draft.greeting.time)){draft.greeting.time=state.greeting.time;restored=true}}if(Array.from(draft.greeting.text).length>300){feedback(\'问候文字不能超过 300 字。\',true);$(\'#greet-text\').focus();return}if(draft.greeting.enabled&&!draft.greeting.text){feedback(\'开启问候前，请填写问候文字。\',true);$(\'#greet-text\').focus();return}if(!/^(?:[01][0-9]|2[0-3]):[0-5][0-9]$/.test(draft.greeting.time)){feedback(\'请选择有效的问候时间。\',true);$(\'#greet-time\').focus();return}busy=true;$(\'#fields\').disabled=true;$(\'#default\').disabled=true;update();feedback(\'正在同步到微信…\');try{const data=await api(\'settings\',{method:\'PUT\',body:JSON.stringify({...draft,revision:state.revision})});apply(data);feedback(restored?\'已保存，已关闭问候。无效的时间或超长文字已恢复为上次保存的内容。\':\'已保存。人物设定用于下一条新消息；问候按已保存的时间安排。\');$(\'#saved-note\').textContent=\'刚刚保存 · 人物设定用于新消息\'}catch(e){feedback(e.message,true);if(e.status===409)$(\'#reload\').hidden=false;if(e.status===401)gate(\'登录已过期\',\'请回微信发送 我要设置，获取新的专属链接。\')}finally{busy=false;$(\'#fields\').disabled=false;$(\'#default\').disabled=false;update()}}\nconst box=$(\'#choices\');for(const [key,p]of Object.entries(personas)){const label=document.createElement(\'label\');label.className=\'choice\'+(key===\'custom\'?\' custom-choice\':\'\');label.innerHTML=\'<input type="radio" name="persona" value="\'+key+\'"><span class="choice-body"><span class="choice-name">\'+p.name+\'<span class="dot" aria-hidden="true"></span></span><span class="desc">\'+p.description+\'</span></span>\';box.append(label)}\nfor(const id of [\'greet-enabled\',\'greet-time\',\'greet-text\'])$(\'#\'+id).addEventListener(\'input\',()=>{feedback(\'\');update()});box.addEventListener(\'change\',()=>{feedback(\'\');update()});$(\'#custom\').addEventListener(\'input\',()=>{feedback(\'\');update()});$(\'#form\').addEventListener(\'submit\',save);$(\'#default\').addEventListener(\'click\',()=>{document.querySelector(\'input[value="default"]\').checked=true;feedback(\'已选择默认小艺，点击保存后生效。自定义描述会保留。\');update()});$(\'#refresh\').addEventListener(\'click\',reload);$(\'#reload\').addEventListener(\'click\',reload);$(\'#retry\').addEventListener(\'click\',()=>boot(null));$(\'#logout\').addEventListener(\'click\',async()=>{if(changed()&&!confirm(\'退出会放弃尚未保存的修改，继续吗？\'))return;try{await api(\'logout\',{method:\'POST\',body:\'{}\'});state=null;gate(\'已退出此浏览器\',\'下次使用时，请在微信发送 我要设置，获取专属链接。\')}catch(e){feedback(e.message,true)}});$(\'#copy\').addEventListener(\'click\',async()=>{try{await navigator.clipboard.writeText(\'我要设置\');$(\'#gate-feedback\').textContent=\'已复制，回到微信粘贴发送。\'}catch{$(\'#gate-feedback\').textContent=\'请长按上方 我要设置 复制，或手动输入。\'}});window.addEventListener(\'beforeunload\',e=>{if(changed()){e.preventDefault();e.returnValue=\'\'}});boot(token);\n</script></body></html>\n'


def next_occurrence(clock, now):
    hour, minute = map(int, clock.split(':'))
    current = datetime.fromtimestamp(now, CHINA)
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target.timestamp() <= now:
        target += timedelta(days=1)
    return target.timestamp()


class GreetingService(member.MemberService):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.store.db.executescript('''
          CREATE TABLE IF NOT EXISTS member_greeting_prefs (
            agent INTEGER NOT NULL, user TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 0, clock TEXT NOT NULL DEFAULT '09:00',
            content TEXT NOT NULL, next_due REAL, updated REAL NOT NULL,
            PRIMARY KEY(agent,user)
          );
          CREATE TABLE IF NOT EXISTS member_greeting_outbox (
            id INTEGER PRIMARY KEY, agent INTEGER NOT NULL, user TEXT NOT NULL,
            day TEXT NOT NULL, scheduled REAL NOT NULL, content TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', attempt INTEGER NOT NULL DEFAULT 0,
            due REAL NOT NULL DEFAULT 0, sent_at REAL,
            UNIQUE(agent,user,day)
          );
        ''')
        with self.store.db:
            self.store.db.execute("UPDATE member_greeting_outbox SET status='pending' WHERE status='working'")
            self.store.db.execute("DELETE FROM member_greeting_outbox WHERE scheduled<? AND status IN ('done','failed','cancelled','expired')",
                                  (time.time() - 30 * 86400,))

    def greeting(self, row):
        saved = self.store.db.execute('SELECT enabled,clock,content,next_due FROM member_greeting_prefs WHERE agent=? AND user=?',
                                      (row['agent'], row['user'])).fetchone()
        return {'enabled': bool(saved['enabled']) if saved else False,
                'time': saved['clock'] if saved else '09:00',
                'text': saved['content'] if saved else DEFAULT_TEXT,
                'timezone': 'Asia/Shanghai',
                'next_due': saved['next_due'] if saved else None}

    @staticmethod
    def validate_greeting(value):
        if not isinstance(value, dict) or not isinstance(value.get('enabled'), bool):
            raise ValueError('问候开关格式不正确')
        clock, content = value.get('time'), value.get('text')
        if not isinstance(clock, str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', clock):
            raise ValueError('问候时间请使用 00:00 至 23:59 的格式')
        if not isinstance(content, str) or len(content) > 300:
            raise ValueError('问候文字最多 300 字')
        if value['enabled'] and not content.strip():
            raise ValueError('开启问候前，请填写问候文字')
        return {'enabled': value['enabled'], 'time': clock, 'text': content.strip()}

    def set_greeting(self, row, value, now=None):
        value = self.validate_greeting(value)
        current = self.greeting(row)
        # Persona-only saves must not postpone a greeting or discard a pending retry.
        if all(value[name] == current[name] for name in ('enabled', 'time', 'text')):
            return
        now = time.time() if now is None else now
        due = next_occurrence(value['time'], now) if value['enabled'] else None
        if due is not None:
            day = datetime.fromtimestamp(due, CHINA).strftime('%Y-%m-%d')
            used = self.store.db.execute('''SELECT 1 FROM member_greeting_outbox
                WHERE agent=? AND user=? AND day=? AND status IN ('working','done','failed','expired')''',
                (row['agent'], row['user'], day)).fetchone()
            if used:
                due = next_occurrence(value['time'], due)
        self.store.db.execute('''INSERT INTO member_greeting_prefs(agent,user,enabled,clock,content,next_due,updated)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(agent,user) DO UPDATE SET
            enabled=excluded.enabled,clock=excluded.clock,content=excluded.content,
            next_due=excluded.next_due,updated=excluded.updated''',
            (row['agent'], row['user'], int(value['enabled']), value['time'], value['text'], due, now))
        self.store.db.execute("UPDATE member_greeting_outbox SET status='cancelled' WHERE agent=? AND user=? AND status='pending'",
                              (row['agent'], row['user']))

    def panel_payload(self, row):
        result = super().panel_payload(row)
        result['greeting'] = self.greeting(row)
        # Scheduling advances next_due without changing settings; exclude it from revision.
        preferences = {key: result['greeting'][key] for key in ('enabled', 'time', 'text')}
        result['revision'] = hashlib.sha256(json.dumps([result['revision'], preferences], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        result['greeting_version'] = GREETING_VERSION
        return result

    async def panel_settings(self, request):
        row = self.panel_user(request)
        if row is None:
            return web.json_response({'error': '请从微信里的专属链接进入'}, status=401)
        if request.method == 'GET':
            return web.json_response(self.panel_payload(row))
        if not self.panel_write_allowed(request):
            return web.json_response({'error': '请从小艺设置页操作'}, status=403)
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError('设置格式不正确')
            if data.get('revision') != self.panel_payload(row)['revision']:
                return web.json_response({'error': '设置已在其他页面或微信中更新，请重新读取后再保存'}, status=409)
            selected, custom = data.get('selected', 'default'), data.get('custom_prompt', '')
            if selected not in member.PERSONAS and selected != 'custom':
                raise ValueError('人物模板无效')
            if not isinstance(custom, str) or len(custom) > 4000:
                raise ValueError('专属设定最多 4000 字')
            if selected == 'custom' and not custom.strip():
                raise ValueError('请填写专属设定，或选择内置人物')
            value = self.validate_greeting(data['greeting']) if 'greeting' in data else None
            with self.store.db:
                self.set_persona(row, selected, custom.strip())
                if value is not None:
                    self.set_greeting(row, value)
            return web.json_response(self.panel_payload(row))
        except (TypeError, json.JSONDecodeError):
            return web.json_response({'error': '设置格式不正确'}, status=400)
        except ValueError as exc:
            return web.json_response({'error': str(exc) or '设置格式不正确'}, status=400)

    def persona_command(self, row, text):
        if text in ('/关闭问候', '/停止问候'):
            saved = self.greeting(row)
            self.set_greeting(row, dict(saved, enabled=False))
            return '已关闭你的每日问候。你仍可正常聊天；如需重新开启，请发送 我要设置，在网页中打开每日问候。'
        if text == '/问候状态':
            saved = self.greeting(row)
            if not saved['enabled']:
                return '你的每日问候目前关闭。发送 我要设置，可在网页选择时间和填写问候文字后开启。'
            return '你的每日问候已开启：每天 ' + saved['time'] + '（北京时间）。\n问候文字：' + saved['text'] + '\n发送 /关闭问候 可停用。'
        return super().persona_command(row, text)

    async def health(self, request):
        response = await super().health(request)
        data = json.loads(response.text)
        data['greeting_version'] = GREETING_VERSION
        return web.json_response(data)

    def enqueue_greetings(self, now=None):
        now = time.time() if now is None else now
        rows = self.store.db.execute('''SELECT * FROM member_greeting_prefs
            WHERE agent=? AND enabled=1 AND next_due<=? ORDER BY next_due LIMIT 100''', (self.agent_id, now)).fetchall()
        with self.store.db:
            for row in rows:
                scheduled = row['next_due']
                # Skip missed times after a long outage; no overnight message backlog.
                if 0 <= now - scheduled < 600:
                    day = datetime.fromtimestamp(scheduled, CHINA).strftime('%Y-%m-%d')
                    self.store.db.execute('''INSERT INTO member_greeting_outbox(agent,user,day,scheduled,content,due)
                        VALUES(?,?,?,?,?,?) ON CONFLICT(agent,user,day) DO UPDATE SET
                        scheduled=excluded.scheduled,content=excluded.content,due=excluded.due,
                        status='pending',attempt=0 WHERE member_greeting_outbox.status='cancelled' ''',
                        (row['agent'], row['user'], day, scheduled, row['content'], now))
                self.store.db.execute('UPDATE member_greeting_prefs SET next_due=? WHERE agent=? AND user=?',
                                      (next_occurrence(row['clock'], now), row['agent'], row['user']))

    async def greeting_reply(self, job):
        session = self.session(job)
        try:
            # Settings may cancel a job between dispatch and this task starting.
            job = self.store.db.execute('SELECT * FROM member_greeting_outbox WHERE id=?', (job['id'],)).fetchone()
            if job is None or job['status'] != 'pending':
                return
            now = time.time()
            saved = self.greeting(job)
            clock = datetime.fromtimestamp(job['scheduled'], CHINA).strftime('%H:%M')
            cancelled = not saved['enabled'] or saved['text'] != job['content'] or saved['time'] != clock
            if cancelled or now - job['scheduled'] >= 600:
                with self.store.db:
                    self.store.db.execute("UPDATE member_greeting_outbox SET status=? WHERE id=?",
                        ('cancelled' if cancelled else 'expired', job['id']))
                return
            with self.store.db:
                self.store.db.execute("UPDATE member_greeting_outbox SET status='working' WHERE id=? AND status='pending'", (job['id'],))
            if hasattr(self,'send_text'):
                await self.send_text(job,job['content'])
            else:
                sent = await self.api.call('message/send', {
                    'touser': job['user'], 'agentid': job['agent'], 'msgtype': 'text',
                    'text': {'content': job['content']}, 'safe': 0,
                    'enable_duplicate_check': 1, 'duplicate_check_interval': 1800,
                })
                if any(sent.get(key) for key in ('invaliduser', 'invalidparty', 'invalidtag', 'unlicenseduser')):
                    raise kf.APIError(81013)
            with self.store.db:
                self.store.db.execute("UPDATE member_greeting_outbox SET status='done',sent_at=? WHERE id=?", (time.time(), job['id']))
            LOG.info('每日问候已提交。')
        except OrderedDeliveryStopped:
            with self.store.db:
                self.store.db.execute("UPDATE member_greeting_outbox SET status='cancelled' WHERE id=? AND status='working'", (job['id'],))
        except Exception as exc:
            self.log_failure('每日问候', exc)
            attempt = job['attempt'] + 1
            saved = self.greeting(job)
            permanent = isinstance(exc, kf.APIError) and exc.code not in (-1, 45009, 45033)
            clock = datetime.fromtimestamp(job['scheduled'], CHINA).strftime('%H:%M')
            cancelled = not saved['enabled'] or saved['text'] != job['content'] or saved['time'] != clock
            status = 'cancelled' if cancelled else ('failed' if permanent or attempt >= 3 else 'pending')
            with self.store.db:
                self.store.db.execute('UPDATE member_greeting_outbox SET status=?,attempt=?,due=? WHERE id=?',
                                      (status, attempt, time.time() + 30 * attempt, job['id']))
        finally:
            self.active.discard(session)

    def dispatch_greetings(self):
        if not self.enabled or not self.api:
            return
        now = time.time()
        self.enqueue_greetings(now)
        with self.store.db:
            self.store.db.execute("UPDATE member_greeting_outbox SET status='expired' WHERE status='pending' AND scheduled<=?", (now - 600,))
        rows = self.store.db.execute('''SELECT g.* FROM member_greeting_outbox g
            WHERE g.agent=? AND g.status='pending' AND g.due<=? AND NOT EXISTS (
                SELECT 1 FROM member_inbox i WHERE i.agent=g.agent AND i.user=g.user
                AND i.status IN ('pending','working')) ORDER BY g.scheduled,g.id LIMIT 100''', (self.agent_id, now)).fetchall()
        for row in rows:
            session = self.session(row)
            if session in self.active:
                continue
            if len(self.active) >= 2:
                break
            self.active.add(session)
            task = asyncio.create_task(self.greeting_reply(row))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    async def greeting_processor(self):
        while True:
            self.dispatch_greetings()
            await asyncio.sleep(1)

    async def collector(self):
        await asyncio.gather(super().collector(), self.greeting_processor())


def create_app(service):
    member.PANEL_HTML = PANEL_HTML
    return member.create_member_app(service)


async def main_app():
    crypto = kf.Crypto(kf.required('CALLBACK_TOKEN'), kf.required('CALLBACK_AES_KEY'), kf.required('WECOM_CORP_ID'))
    store = kf.Store(os.environ.get('DATABASE_FILE', '/data/kf.sqlite3'))
    http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30, connect=10))
    try:
        enabled = os.environ.get('ENABLE_AI', 'false').lower() == 'true'
        api = kf.WeCom(http, crypto.corp_id, kf.required('WECOM_APP_SECRET')) if enabled else None
        model = kf.Model(http) if enabled else None
        service = GreetingService(crypto, store, api, model, enabled)
        if enabled:
            service.member_model = kf.Model(http)
            service.member_model.prompt = os.environ.get('MEMBER_SYSTEM_PROMPT', member.MEMBER_BASE_PROMPT)
        application = create_app(service)
    except BaseException:
        await http.close()
        store.db.close()
        raise
    async def cleanup(application):
        await http.close()
        store.db.close()
    application.on_cleanup.append(cleanup)
    return application


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        web.run_app(main_app(), host='0.0.0.0', port=8088, access_log=None, print=None)
    except ValueError:
        LOG.error('问候服务配置无效，请核对现有企业应用配置。')
        raise SystemExit(1)
