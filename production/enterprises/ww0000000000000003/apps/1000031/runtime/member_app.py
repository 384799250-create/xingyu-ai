"""Enterprise application text chat adapter; preserves existing WeChat KF service."""
import asyncio
import base64
import copy
import hashlib
import json
import logging
import os
import re
import secrets
import struct
import time
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

import aiohttp
from aiohttp import web
import kf_app as kf

LOG = logging.getLogger('wecom-member')

MEMBER_BASE_PROMPT = (
    '你是小小艺，是用户通过微信企业应用使用的 AI 助手，后端模型是 DeepSeek。'
    '用中文自然、简洁地回答，每次尽量不超过500个汉字。你当前只能文字对话，'
    '不能直接操作服务器、读取文件或联网，不要声称已经执行这些操作。'
)
PERSONAS = {
    'default': {'name': '默认小艺', 'description': '自然友好，日常聊天和通用问答。',
                'prompt': '保持自然、友好、清晰的日常交流方式，按问题需要提供帮助。'},
    'gentle': {'name': '温柔小艺', 'description': '耐心倾听，温和回应情绪。',
               'prompt': '用温柔、平和、体贴的语气交流。先听懂用户的感受，再给简短回应和可行建议。'
                         '不要一上来长篇说教，不强行积极，不使用占有欲或要求用户依赖你的表达。'},
    'rational': {'name': '理性小艺', 'description': '分析问题，给出清晰建议。',
                 'prompt': '用冷静、直接、清晰的语气交流。区分事实、推测和建议，必要时先问一个关键问题。'
                           '给出可操作的步骤和取舍，避免空泛结论，不为凑格式强行分点。'},
    'encourage': {'name': '鼓励小艺', 'description': '陪你拆解目标，迈出下一步。',
                  'prompt': '用轻松、有活力但不夸张的语气交流。根据用户的具体进展给予鼓励，'
                            '把困难拆成小步骤，优先帮助用户完成一个能立刻行动的小目标，不空喊口号。'},
}

PANEL_VERSION = 1
PANEL_LINK_REUSABLE_VERSION = 1
PANEL_HTML = '<!doctype html>\n<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#166356"><meta name="referrer" content="no-referrer"><link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns=\'http://www.w3.org/2000/svg\' viewBox=\'0 0 32 32\'%3E%3Crect width=\'32\' height=\'32\' rx=\'8\' fill=\'%23166356\'/%3E%3Cpath d=\'M7 8h18v12H14l-5 5v-5H7z\' fill=\'none\' stroke=\'white\' stroke-width=\'1.5\'/%3E%3C/svg%3E"><title>小小艺 · 我的设置</title>\n<style>\n:root{color-scheme:light;--ink:#193e37;--muted:#596e67;--accent:#166356;--tint:#edf5f1;--line:#d6e1da;--bg:#f3f6f2;--error:#a33434;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);line-height:1.65}button,input,textarea{font:inherit}button{cursor:pointer}button:disabled{cursor:default;opacity:.55}button:focus-visible{outline:3px solid #a8c6b0;outline-offset:3px}::selection{background:#cae4d4;color:#173e34}[hidden]{display:none!important}.shell{max-width:1080px;padding:30px 32px 38px;margin:auto}.topbar{display:flex;align-items:center;gap:12px;padding-bottom:22px;border-bottom:1px solid var(--line)}.mark{width:42px;height:42px;display:grid;place-items:center;border-radius:12px;background:var(--accent);color:#fff}.mark svg{width:26px;height:26px}.brand{font-size:18px;font-weight:700}.sub{font-size:12px;color:var(--muted)}.private{margin-left:auto;font-size:13px;color:var(--muted)}.heading{padding:32px 0 26px}.heading h1{font-size:30px;line-height:1.3;letter-spacing:-.025em;margin:0 0 10px;font-weight:650}.heading p{font-size:14px;color:var(--muted);margin:0;max-width:65ch}.layout{display:grid;grid-template-columns:minmax(0,1fr) 270px;gap:32px;align-items:start}.editor{background:#fff;border-radius:14px;border:1px solid var(--line);padding:26px}fieldset{margin:0;padding:0;border:0;min-width:0}legend{font-size:16px;font-weight:650;padding:0}.helper{font-size:13px;color:var(--muted);margin:4px 0 18px}.choices{display:grid;grid-template-columns:1fr 1fr;gap:12px}.choice{position:relative;display:block;cursor:pointer}.choice input{position:absolute;opacity:0;width:1px;height:1px;left:18px;top:20px}.choice-body{display:block;padding:16px 17px;border:1px solid var(--line);border-radius:12px;height:100%;transition:background .16s,border-color .16s}.choice input:checked+.choice-body{background:var(--tint);border-color:var(--accent)}.choice input:focus-visible+.choice-body{outline:3px solid #a8c6b0;outline-offset:2px}.choice-name{display:flex;align-items:center;justify-content:space-between;gap:8px;font-size:15px;font-weight:650}.dot{width:16px;height:16px;border:1.5px solid #8b9f96;border-radius:50%;flex-shrink:0;display:grid;place-items:center}.choice input:checked+.choice-body .dot{border-color:var(--accent)}.choice input:checked+.choice-body .dot:after{content:"";height:8px;width:8px;border-radius:50%;background:var(--accent)}.desc{display:block;margin-top:5px;font-size:12px;color:var(--muted)}.custom-choice{grid-column:1/-1}.custom-choice .choice-body{padding-top:14px;padding-bottom:14px}.description{border-top:1px solid var(--line);margin-top:24px;padding-top:21px}.field-top{display:flex;justify-content:space-between;gap:12px;font-size:14px;font-weight:650}.count{color:var(--muted);font-size:12px;font-weight:400;font-variant-numeric:tabular-nums}textarea{display:block;width:100%;min-height:168px;resize:vertical;border:1px solid #b7cbbf;border-radius:10px;padding:12px 14px;line-height:1.8;background:#fff;color:var(--ink);font-size:14px;margin-top:10px;outline:none;caret-color:var(--accent)}textarea:focus{border-color:var(--accent);outline:2px solid #cbe0d2;outline-offset:1px}textarea::placeholder{color:#64796f}textarea:disabled{background:#f5f7f5;color:#53695d}.field-help{color:var(--muted);font-size:12px;margin:8px 0 0}.actions{border-top:1px solid var(--line);padding-top:20px;margin-top:24px;display:flex;align-items:center;flex-wrap:wrap;gap:14px}.primary{padding:11px 23px;border:1px solid var(--accent);background:var(--accent);color:#fff;border-radius:9px;font-size:14px;font-weight:650;min-height:46px}.secondary{border:1px solid #bdcdc4;background:#fff;color:var(--ink);padding:10px 16px;border-radius:9px;font-size:13px;min-height:44px}.text-button{border:0;padding:10px 0;color:var(--muted);background:transparent;font-size:13px;text-decoration:underline;text-underline-offset:4px;min-height:44px}.feedback{font-size:13px;margin:14px 0 0;min-height:22px;color:var(--accent)}.feedback.error{color:var(--error)}.sidebar{padding-top:5px}.sidebar h2{font-size:14px;margin:0 0 13px;font-weight:650}.saved-persona{font-size:23px;letter-spacing:-.025em;margin:0 0 4px;font-weight:650}.saved-note{font-size:12px;color:var(--muted);margin:0}.example{margin-top:25px;padding-top:22px;border-top:1px solid var(--line)}.example-label{font-size:12px;color:var(--muted);margin:0 0 10px}.bubble{background:#dceade;padding:14px 16px;border-radius:3px 14px 14px;font-size:14px;line-height:1.85;margin:0}.fine{font-size:11px;color:var(--muted);margin:8px 0 0}.details{font-size:12px;color:var(--muted);margin-top:24px;padding-top:21px;border-top:1px solid var(--line)}.details p{margin:0 0 13px}.details strong{color:var(--ink);font-weight:600}.session-tools{display:flex;justify-content:space-between;gap:12px;margin-top:17px}.footer{color:var(--muted);font-size:12px;display:flex;justify-content:space-between;gap:20px;margin-top:26px}.gate{max-width:570px;padding:35px 30px;background:#fff;border:1px solid var(--line);border-radius:14px;margin:30px 0 70px}.gate h2{font-size:21px;margin:0 0 12px}.gate p{color:var(--muted);font-size:14px;margin:0 0 15px}.command{font-size:18px;background:var(--tint);border-radius:9px;padding:12px 16px;color:var(--accent);display:inline-block;margin:0 0 17px}.gate .buttons{display:flex;gap:15px;align-items:center}.loading{color:var(--muted);padding:30px 0}.dirty{color:var(--muted);font-size:12px}.spinner{display:inline-block;width:14px;height:14px;border:2px solid #c1d4c7;border-top-color:var(--accent);border-radius:50%;vertical-align:-2px;margin-right:8px;animation:spin .8s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}@media(hover:hover){.choice-body:hover{border-color:#6d9a80}.primary:hover{background:#114f44}.text-button:hover{color:var(--accent)}}@media(max-width:760px){.shell{padding:20px 18px 30px}.heading{padding:26px 0 22px}.heading h1{font-size:26px}.layout{grid-template-columns:1fr;gap:23px}.editor{padding:21px 18px}.sidebar{padding:0 3px}.saved-wrap{display:flex;align-items:baseline;justify-content:space-between;gap:14px}.saved-wrap h2{margin:0}.saved-persona{font-size:19px;margin:0}.example{margin-top:17px;padding-top:17px}.details{display:grid;grid-template-columns:1fr 1fr;gap:15px;margin-top:18px;padding-top:18px}.details p{margin:0}.footer{flex-direction:column;gap:4px;margin-top:23px}.private{font-size:12px}.gate{padding:28px 23px;margin-top:15px}.choice-body{padding:14px 12px}.choice-name{font-size:14px}.heading p{font-size:13px}.session-tools{max-width:320px}}@media(max-width:360px){.shell{padding-left:12px;padding-right:12px}.editor{padding:18px 13px}.choices{gap:9px}.choice-name{font-size:13px}.private{font-size:11px}.actions{gap:10px}.primary{padding-left:18px;padding-right:18px}}@media(prefers-reduced-motion:reduce){*{transition:none!important}.spinner{animation:none}}\n</style></head><body>\n<!-- THESIS: 微信成员选择自己的相处方式并保存。 OWN-WORLD: 明亮青绿设置页，实线控件和聊天气泡。 STORY: 选人物，编辑设定，保存并回微信聊天。 FIRST VIEWPORT: 左编辑器右已保存状态，手机按任务排列。 FORM: operate personal settings; seed 0c2c733e. FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, and DESIGN.md -->\n<main class="shell">\n<header class="topbar"><div class="mark" aria-hidden="true"><svg viewBox="0 0 32 32" fill="none"><path d="M6 7h20v13H15l-6 5v-5H6V7Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M12 13h8M12 16h5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg></div><div><div class="brand">小小艺</div><div class="sub">我的人物设置</div></div><span class="private">仅修改你自己的设置</span></header>\n<div class="heading"><h1>让小艺更懂你的表达习惯</h1><p>选择一种相处方式，或者写下你喜欢的称呼、性格和语气。保存后，回到微信继续聊。</p></div>\n<div id="loading" class="loading" role="status"><span class="spinner" aria-hidden="true"></span>正在读取你的设置…</div>\n<section id="gate" class="gate" hidden><h2 id="gate-title">从微信打开你的专属设置</h2><p id="gate-text">在“小小艺 AI 客服”的企业应用聊天里发送这条指令，再点回复中的链接。</p><code class="command">我要设置</code><div class="buttons"><button id="copy" class="primary" type="button">复制指令</button><button id="retry" class="text-button" type="button">重新连接</button></div><p id="gate-feedback" role="status"></p></section>\n<div class="layout" id="workspace" hidden>\n<form class="editor" id="form"><fieldset id="fields"><legend>选择人物</legend><p class="helper">每个人物分别保留聊天记录，可以随时切换回来。</p><div class="choices" id="choices"></div><div class="description"><div class="field-top"><label for="custom">专属人物设定</label><span class="count" id="count">0 / 4000</span></div><textarea id="custom" aria-describedby="custom-help" placeholder="例如：你叫小艺，语气温柔但不说教，称呼我为小林。回答先给结论，再给两个具体建议；不确定时先问我。"></textarea><p class="field-help" id="custom-help">选择“自定义小艺”后生效。这是完整人物描述，不会自动叠加内置模板。</p></div></fieldset><div class="actions"><button class="primary" id="save" type="submit" disabled>保存设置</button><button class="text-button" id="default" type="button">选回默认人物</button><span class="dirty" id="dirty" hidden>有未保存的修改</span></div><p class="feedback" id="feedback" role="status" aria-live="polite"></p><button class="secondary" id="reload" type="button" hidden>重新读取最新设置</button></form>\n<aside class="sidebar"><div class="saved-wrap"><h2>微信中正在使用</h2><p class="saved-persona" id="current">—</p></div><p class="saved-note" id="saved-note">已与当前成员的配置连接</p><section class="example"><p class="example-label" id="example-label">语气示例</p><blockquote class="bubble" id="example"></blockquote><p class="fine">固定文案示意，不是模型的实际回复。</p></section><div class="details"><p><strong>下一条新消息生效</strong><br>微信里的 /切换、/设定 也会同步到这里。修改人物描述会开启独立的上下文。</p><p><strong>当前支持文字对话</strong><br>语音、生图、表情包和主动问候需要后续接入。应用名称和头像由管理员设置。</p></div><div class="session-tools"><button class="text-button" id="refresh" type="button">重新读取设置</button><button class="text-button" id="logout" type="button">退出此浏览器</button></div></aside>\n</div><footer class="footer"><span>小小艺 · 个人设置</span><span>专属链接请勿转发；共用设备使用后可退出。</span></footer></main>\n<script>\n\'use strict\';\nconst $=s=>document.querySelector(s);\nconst personas={default:{name:\'默认小艺\',description:\'自然友好，日常聊天和通用问答。\',example:\'你好，今天想聊点什么？我会尽量说得清楚、好懂。\'},gentle:{name:\'温柔小艺\',description:\'耐心倾听，温和回应情绪。\',example:\'可以慢慢说，我在听。我们先照顾好你的感受，再一起想下一步。\'},rational:{name:\'理性小艺\',description:\'分析问题，给出清晰建议。\',example:\'我们先确认目标和限制，再比较几个选择，挑出现在能执行的方案。\'},encourage:{name:\'鼓励小艺\',description:\'陪你拆解目标，迈出下一步。\',example:\'先做最小的一步就好。你想从哪件十分钟能完成的小事开始？\'},custom:{name:\'自定义小艺\',description:\'用你的描述，定义自己的相处方式。\',example:\'你的专属设定会用于下一次回复。保存后回微信发消息，看看小艺的变化。\'}};\nlet state=null,busy=false;\nconst token=new URLSearchParams(location.hash.slice(1)).get(\'token\');\nif(token)history.replaceState(null,\'\',location.pathname);\nclass ApiError extends Error{constructor(message,status){super(message);this.status=status}}\nasync function api(path,options={}){const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),15000);try{const response=await fetch(\'/ai/enterprises/ww0000000000000003/1000031/xiaoyi/api/\'+path,{credentials:\'same-origin\',cache:\'no-store\',...options,signal:controller.signal,headers:{\'Content-Type\':\'application/json\',...(options.headers||{})}});const data=await response.json().catch(()=>({}));if(!response.ok)throw new ApiError(data.error||\'服务暂时不可用，请稍后重试。\',response.status);return data}catch(e){if(e.name===\'AbortError\')throw new Error(\'连接超时，请检查网络后重试。\');if(e instanceof TypeError)throw new Error(\'网络连接失败，请检查网络后重试。\');throw e}finally{clearTimeout(timer)}}\nfunction formValue(){return{selected:document.querySelector(\'input[name="persona"]:checked\')?.value||\'default\',custom_prompt:$(\'#custom\').value.trim()}}\nfunction changed(){return !!state&&(formValue().selected!==state.selected||formValue().custom_prompt!==state.custom_prompt)}\nfunction feedback(text,error=false){$(\'#feedback\').textContent=text;$(\'#feedback\').className=\'feedback\'+(error?\' error\':\'\')}\nfunction update(){const draft=formValue(),p=personas[draft.selected]||personas.default;$(\'#custom\').disabled=draft.selected!==\'custom\'||busy;$(\'#count\').textContent=Array.from($(\'#custom\').value).length+\' / 4000\';$(\'#example-label\').textContent=\'所选人物预览 · 保存后生效（\'+p.name+\'）\';$(\'#example\').textContent=p.example;$(\'#dirty\').hidden=!changed();$(\'#save\').disabled=busy||!changed();$(\'#save\').textContent=busy?\'正在保存…\':\'保存设置\'}\nfunction apply(data){state=data;document.querySelector(\'input[value="\'+(personas[data.selected]?data.selected:\'default\')+\'"]\').checked=true;$(\'#custom\').value=data.custom_prompt||\'\';$(\'#current\').textContent=data.name;$(\'#saved-note\').textContent=\'已与当前成员的配置连接\';$(\'#reload\').hidden=true;update()}\nfunction gate(title,message){$(\'#loading\').hidden=true;$(\'#workspace\').hidden=true;$(\'#gate\').hidden=false;$(\'#gate-title\').textContent=title;$(\'#gate-text\').textContent=message}\nasync function boot(exchangeToken){$(\'#gate\').hidden=true;$(\'#workspace\').hidden=true;$(\'#loading\').hidden=false;try{if(exchangeToken)await api(\'session\',{method:\'POST\',body:JSON.stringify({token:exchangeToken})});apply(await api(\'settings\'));$(\'#loading\').hidden=true;$(\'#workspace\').hidden=false;feedback(\'\')}catch(e){gate(e.status===401?\'请重新获取专属链接\':\'暂时没能打开设置\',e.message)}}\nasync function reload(){if(changed()&&!confirm(\'重新读取会放弃尚未保存的修改，继续吗？\'))return;try{apply(await api(\'settings\'));feedback(\'已读取最新设置。\')}catch(e){feedback(e.message,true);if(e.status===401)gate(\'登录已过期\',\'请回微信发送 我要设置，获取新的专属链接。\')}}\nasync function save(event){event.preventDefault();if(busy||!state||!changed())return;const draft=formValue();if(Array.from(draft.custom_prompt).length>4000){feedback(\'专属人物设定不能超过 4000 字。\',true);return}if(draft.selected===\'custom\'&&!draft.custom_prompt){feedback(\'请先填写专属人物设定。\',true);$(\'#custom\').focus();return}busy=true;$(\'#fields\').disabled=true;$(\'#default\').disabled=true;update();feedback(\'正在同步到微信…\');try{const data=await api(\'settings\',{method:\'PUT\',body:JSON.stringify({...draft,revision:state.revision})});apply(data);feedback(\'已保存。回微信发送新消息，即可使用新设定。\');$(\'#saved-note\').textContent=\'刚刚保存 · 下一条新消息生效\'}catch(e){feedback(e.message,true);if(e.status===409)$(\'#reload\').hidden=false;if(e.status===401)gate(\'登录已过期\',\'请回微信发送 我要设置，获取新的专属链接。\')}finally{busy=false;$(\'#fields\').disabled=false;$(\'#default\').disabled=false;update()}}\nconst box=$(\'#choices\');for(const [key,p]of Object.entries(personas)){const label=document.createElement(\'label\');label.className=\'choice\'+(key===\'custom\'?\' custom-choice\':\'\');label.innerHTML=\'<input type="radio" name="persona" value="\'+key+\'"><span class="choice-body"><span class="choice-name">\'+p.name+\'<span class="dot" aria-hidden="true"></span></span><span class="desc">\'+p.description+\'</span></span>\';box.append(label)}\nbox.addEventListener(\'change\',()=>{feedback(\'\');update()});$(\'#custom\').addEventListener(\'input\',()=>{feedback(\'\');update()});$(\'#form\').addEventListener(\'submit\',save);$(\'#default\').addEventListener(\'click\',()=>{document.querySelector(\'input[value="default"]\').checked=true;feedback(\'已选择默认小艺，点击保存后生效。自定义描述会保留。\');update()});$(\'#refresh\').addEventListener(\'click\',reload);$(\'#reload\').addEventListener(\'click\',reload);$(\'#retry\').addEventListener(\'click\',()=>boot(null));$(\'#logout\').addEventListener(\'click\',async()=>{if(changed()&&!confirm(\'退出会放弃尚未保存的修改，继续吗？\'))return;try{await api(\'logout\',{method:\'POST\',body:\'{}\'});state=null;gate(\'已退出此浏览器\',\'下次使用时，请在微信发送 我要设置，获取专属链接。\')}catch(e){feedback(e.message,true)}});$(\'#copy\').addEventListener(\'click\',async()=>{try{await navigator.clipboard.writeText(\'我要设置\');$(\'#gate-feedback\').textContent=\'已复制，回到微信粘贴发送。\'}catch{$(\'#gate-feedback\').textContent=\'请长按上方 我要设置 复制，或手动输入。\'}});window.addEventListener(\'beforeunload\',e=>{if(changed()){e.preventDefault();e.returnValue=\'\'}});boot(token);\n</script></body></html>\n'


@web.middleware
async def panel_headers(request, handler):
    response = await handler(request)
    if request.path == '/ai/enterprises/ww0000000000000003/1000031/xiaoyi' or request.path.startswith('/ai/enterprises/ww0000000000000003/1000031/xiaoyi/'):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
        script = re.search(r'<script>(.*?)</script>', PANEL_HTML, re.S).group(1)
        style = re.search(r'<style>(.*?)</style>', PANEL_HTML, re.S).group(1)
        digest = lambda content: base64.b64encode(hashlib.sha256(content.encode()).digest()).decode()
        response.headers['Content-Security-Policy'] = (
            "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'; "
            "connect-src 'self'; script-src 'sha256-" + digest(script) + "'; style-src 'sha256-" + digest(style) + "'; img-src 'self' data:")
    return response


class MemberService(kf.Service):
    def __init__(self, crypto, store, api=None, model=None, enabled=False, agent_id=None):
        super().__init__(crypto, store, api, model, enabled)
        self.member_model = model
        self.agent_id = int(agent_id if agent_id is not None else kf.required('WECOM_AGENT_ID'))
        if self.agent_id <= 0:
            raise ValueError('WECOM_AGENT_ID 无效')
        self.panel_base = os.environ.get('PANEL_PUBLIC_BASE', 'https://www.example.com/ai/enterprises/ww0000000000000003/1000031/xiaoyi/').rstrip('/') + '/'
        parsed = urlsplit(self.panel_base)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path != '/ai/enterprises/ww0000000000000003/1000031/xiaoyi/':
            raise ValueError('PANEL_PUBLIC_BASE 必须为 HTTPS 地址且路径为 /ai/enterprises/ww0000000000000003/1000031/xiaoyi/')
        self.panel_origin = parsed.scheme + '://' + parsed.netloc
        self.store.db.executescript('''
          CREATE TABLE IF NOT EXISTS member_inbox (
            msgid TEXT PRIMARY KEY, user TEXT NOT NULL, agent INTEGER NOT NULL,
            content TEXT NOT NULL, created REAL NOT NULL,
            status TEXT DEFAULT 'pending', answer TEXT,
            attempt INTEGER DEFAULT 0, due REAL DEFAULT 0
          );
          CREATE TABLE IF NOT EXISTS member_personas (
            agent INTEGER NOT NULL, user TEXT NOT NULL,
            selected TEXT NOT NULL DEFAULT 'default',
            custom_prompt TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (agent,user)
          );
          CREATE TABLE IF NOT EXISTS panel_tokens (
            token_hash TEXT PRIMARY KEY, agent INTEGER NOT NULL, user TEXT NOT NULL,
            created REAL NOT NULL, expires REAL NOT NULL
          );
          CREATE TABLE IF NOT EXISTS panel_sessions (
            session_hash TEXT PRIMARY KEY, agent INTEGER NOT NULL, user TEXT NOT NULL,
            created REAL NOT NULL, expires REAL NOT NULL
          );
        ''')
        with self.store.db:
            self.store.db.execute("UPDATE member_inbox SET status='pending' WHERE status='working'")
            self.store.db.execute("DELETE FROM member_inbox WHERE created<? AND status='done'",
                                  (time.time() - 7 * 86400,))

    def session(self, row):
        return 'member:%s:%s' % (row['agent'], row['user'])

    def persona(self, row):
        saved = self.store.db.execute('SELECT selected,custom_prompt FROM member_personas WHERE agent=? AND user=?',
                                      (row['agent'], row['user'])).fetchone()
        key = saved['selected'] if saved else 'default'
        if key == 'custom' and saved['custom_prompt']:
            prompt = saved['custom_prompt']
            revision = hashlib.sha256(prompt.encode('utf-8')).hexdigest()[:16]
            return {'key': 'custom:' + revision, 'name': '自定义小艺',
                    'description': prompt, 'prompt': prompt}
        return dict(PERSONAS.get(key, PERSONAS['default']), key=key if key in PERSONAS else 'default')

    def persona_session(self, row, persona=None):
        persona = persona or self.persona(row)
        base = self.session(row)
        # Preserve the history from before this upgrade under the default persona.
        return base if persona['key'] == 'default' else base + ':persona:' + persona['key']

    def set_persona(self, row, key, custom=None):
        self.store.db.execute('''INSERT INTO member_personas(agent,user,selected) VALUES(?,?,?)
            ON CONFLICT(agent,user) DO UPDATE SET selected=excluded.selected''',
            (row['agent'], row['user'], key))
        if custom is not None:
            self.store.db.execute('UPDATE member_personas SET custom_prompt=? WHERE agent=? AND user=?',
                                  (custom, row['agent'], row['user']))

    def issue_panel_link(self, row):
        """Issue a reusable 30-day link; auth tables store only its hash."""
        raw = secrets.token_urlsafe(32)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        now = time.time()
        with self.store.db:
            self.store.db.execute('DELETE FROM panel_tokens WHERE expires<?', (now,))
            self.store.db.execute('INSERT INTO panel_tokens(token_hash,agent,user,created,expires) VALUES(?,?,?,?,?)',
                                  (digest, row['agent'], row['user'], now, now + 30 * 86400))
        return self.panel_base + '#token=' + raw

    def panel_user(self, request):
        raw = request.cookies.get('xiaoyi_session', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', raw):
            return None
        digest = hashlib.sha256(raw.encode()).hexdigest()
        row = self.store.db.execute('SELECT agent,user FROM panel_sessions WHERE session_hash=? AND expires>?',
                                    (digest, time.time())).fetchone()
        return row

    def panel_payload(self, row):
        persona = self.persona({'agent': row['agent'], 'user': row['user']})
        saved = self.store.db.execute('SELECT selected,custom_prompt FROM member_personas WHERE agent=? AND user=?',
                                      (row['agent'], row['user'])).fetchone()
        result = {'selected': saved['selected'] if saved else 'default',
                'custom_prompt': saved['custom_prompt'] if saved else '',
                'key': persona['key'], 'name': persona['name'],
                'description': persona['description'], 'version': PANEL_VERSION}
        result['revision'] = hashlib.sha256(json.dumps([result['selected'], result['custom_prompt']], ensure_ascii=False).encode()).hexdigest()
        return result

    def panel_write_allowed(self, request):
        # Fixed configured origin, never trust forwarded Host/Proto headers for CSRF.
        return request.headers.get('Origin', '') == self.panel_origin and request.content_type == 'application/json'

    async def panel_session(self, request):
        if not self.panel_write_allowed(request):
            return web.json_response({'error': '请从小艺设置页操作'}, status=403)
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError()
            raw = data.get('token', '')
            if not isinstance(raw, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', raw):
                raise ValueError()
            digest = hashlib.sha256(raw.encode()).hexdigest()
            token = self.store.db.execute('SELECT agent,user FROM panel_tokens WHERE token_hash=? AND expires>? ',
                                          (digest, time.time())).fetchone()
            if token is None:
                return web.json_response({'error': '链接已失效，请在微信里重新发送 我要设置'}, status=401)
            session = secrets.token_urlsafe(32)
            session_hash = hashlib.sha256(session.encode()).hexdigest()
            now = time.time()
            with self.store.db:
                self.store.db.execute('DELETE FROM panel_sessions WHERE expires<?', (now,))
                # Bound the number of remembered browsers per member.
                self.store.db.execute('''DELETE FROM panel_sessions WHERE agent=? AND user=? AND session_hash NOT IN
                    (SELECT session_hash FROM panel_sessions WHERE agent=? AND user=? ORDER BY created DESC LIMIT 4)''',
                    (token['agent'], token['user'], token['agent'], token['user']))
                self.store.db.execute('INSERT INTO panel_sessions(session_hash,agent,user,created,expires) VALUES(?,?,?,?,?)',
                                      (session_hash, token['agent'], token['user'], now, now + 7 * 86400))
                # Opening a link creates a browser session without consuming or
                # extending the link's original 30-day validity window.
            response = web.json_response({'ok': True})
            response.set_cookie('xiaoyi_session', session, max_age=7 * 86400, httponly=True,
                                secure=True, samesite='Lax', path='/ai/enterprises/ww0000000000000003/1000031/xiaoyi')
            return response
        except (ValueError, TypeError, json.JSONDecodeError):
            return web.json_response({'error': '无效链接'}, status=400)

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
                return web.json_response({'error': '设置已在其他页面或微信中更新，请重新加载后再保存'}, status=409)
            selected = data.get('selected', 'default')
            custom = data.get('custom_prompt', '')
            if selected not in PERSONAS and selected != 'custom':
                raise ValueError('人物模板无效')
            if not isinstance(custom, str) or len(custom) > 4000:
                raise ValueError('专属设定最多 4000 字')
            if selected == 'custom' and not custom.strip():
                raise ValueError('请填写专属设定，或选择内置人物')
            with self.store.db:
                self.set_persona(row, selected, custom.strip())
            return web.json_response(self.panel_payload(row))
        except (TypeError, json.JSONDecodeError):
            return web.json_response({'error': '设置格式不正确'}, status=400)
        except ValueError as exc:
            return web.json_response({'error': str(exc) or '设置格式不正确'}, status=400)

    async def panel_page(self, request):
        return web.Response(text=PANEL_HTML, content_type='text/html', charset='utf-8')

    async def panel_logout(self, request):
        if not self.panel_write_allowed(request):
            return web.json_response({'error': '请从小艺设置页操作'}, status=403)
        raw = request.cookies.get('xiaoyi_session', '')
        with self.store.db:
            self.store.db.execute('DELETE FROM panel_sessions WHERE session_hash=?', (hashlib.sha256(raw.encode()).hexdigest(),))
        response = web.json_response({'ok': True})
        response.del_cookie('xiaoyi_session', path='/ai/enterprises/ww0000000000000003/1000031/xiaoyi')
        return response

    def persona_command(self, row, text):
        current = self.persona(row)
        if text in ('我要设置', '/网页设置', '/面板', '/settings'):
            return '这是你的专属人物设置链接（生成后 30 天内可重复打开）：\n' + self.issue_panel_link(row) + '\n\n链接绑定你的应用信息，请勿转发。'
        if text in ('/人物', '/角色', '/personas'):
            lines = ['当前人物：' + current['name']]
            for index, definition in enumerate(PERSONAS.values(), 1):
                lines.append('%s. %s：%s' % (index, definition['name'], definition['description']))
            saved = self.store.db.execute('SELECT custom_prompt FROM member_personas WHERE agent=? AND user=?',
                                          (row['agent'], row['user'])).fetchone()
            if saved and saved['custom_prompt']:
                lines.append('5. 自定义小艺：你保存的个人设定。')
            lines.extend(['', '切换示例：/切换 温柔小艺',
                          '自定义：/设定 你叫小艺，说话温柔简短，称呼我为小林。',
                          '查看设定：/人物设定', '各人物的聊天记录分开保存，/new 清空当前人物记录。',
                          '切换只改变本人的回复风格，微信头像和应用名称保持应用配置。'])
            return '\n'.join(lines)
        if text == '/人物设定':
            return '当前人物：' + current['name'] + '\n设定：' + current['prompt']
        if text in ('/切换', '/恢复默认') or re.match(r'^/切换\s', text):
            choice = '默认小艺' if text == '/恢复默认' else text[len('/切换'):].strip()
            if not choice:
                return '请带上人物名称，例如：/切换 温柔小艺。发送 /人物 查看列表。'
            keys = {definition['name']: key for key, definition in PERSONAS.items()}
            keys.update({str(index): key for index, key in enumerate(PERSONAS, 1)})
            keys.update({'自定义小艺': 'custom', '5': 'custom'})
            key = keys.get(choice)
            if key is None:
                return '没有找到这个人物。发送 /人物 查看列表，例如：/切换 温柔小艺。'
            saved = self.store.db.execute('SELECT custom_prompt FROM member_personas WHERE agent=? AND user=?',
                                          (row['agent'], row['user'])).fetchone()
            if key == 'custom' and (not saved or not saved['custom_prompt']):
                return '你还没有自定义人物。发送 /设定 加上性格、称呼和说话方式即可保存。'
            self.set_persona(row, key)
            selected = self.persona(row)
            return '已切换为' + selected['name'] + '。\n现在可以直接聊天；该人物的已有记录会继续保留。'
        if text == '/设定' or re.match(r'^/设定\s', text):
            custom = text[len('/设定'):].strip()
            if not custom:
                return '用法：/设定 你叫小艺，性格温柔，回答简短，称呼我为小林。\n最多 4000 字；保存后自动启用自定义小艺。'
            if len(custom) > 4000:
                return '人物设定最多 4000 字，请缩短后重新发送。当前人物保持原设定。'
            self.set_persona(row, 'custom', custom)
            return '已保存并启用自定义小艺。\n发送 /人物设定 查看，/恢复默认 回到默认小艺。不同设定的聊天记录分开保存。'
        return None

    async def persona_answer(self, row, persona, history, text):
        # Each concurrent request gets its own prompt; shared model clients stay unchanged.
        model = copy.copy(self.member_model)
        base = getattr(self.member_model, 'prompt', MEMBER_BASE_PROMPT)
        model.prompt = base + '\n\n当前人物风格设定：\n' + persona['prompt'] + (
            '\n\n风格设定仅调整称呼、性格和表达方式；当前能力仍为文字对话。'
            '不要声称自己已经发图、发语音、设置提醒、主动问候或执行了外部操作。')
        return await model.answer(history, text)

    async def callback(self, request):
        if request.method == 'GET':
            return await super().callback(request)
        try:
            envelope = kf.xml_parse(await request.read())
            query = request.query
            clear = self.crypto.decrypt(envelope.findtext('Encrypt', ''),
                                        query.get('msg_signature', ''),
                                        query.get('timestamp', ''), query.get('nonce', ''))
            node = kf.xml_parse(clear)
            if node.findtext('ToUserName') != self.crypto.corp_id:
                raise ValueError()
            if node.findtext('Event') == 'kf_msg_or_event':
                # aiohttp caches request.read(); reuse existing KF validation/queue.
                return await super().callback(request)
            if node.findtext('MsgType') != 'text':
                return web.Response(text='success')
            if node.findtext('AgentID') != str(self.agent_id):
                raise ValueError()
            user, msgid = node.findtext('FromUserName', ''), node.findtext('MsgId', '')
            if user == '@all' or not re.fullmatch(r'[A-Za-z0-9_.@=-]{1,128}', user):
                raise ValueError()
            if not re.fullmatch(r'[0-9]{1,64}', msgid):
                raise ValueError()
            created = float(node.findtext('CreateTime', '0'))
            if not 0 < created <= time.time() + 600:
                raise ValueError()
            if time.time() - created > 86400:
                return web.Response(text='success')
            with self.store.db:
                result = self.store.db.execute('''INSERT OR IGNORE INTO member_inbox
                    (msgid,user,agent,content,created) VALUES(?,?,?,?,?)''',
                    (msgid, user, self.agent_id, node.findtext('Content', ''), created))
            if result.rowcount:
                LOG.info('企业应用文字消息已入队。')
            # Acknowledge before inference; delivery retries must not duplicate work.
            return web.Response(text='success')
        except (ValueError, ET.ParseError, UnicodeError, struct.error):
            return web.Response(status=403, text='invalid callback')

    async def health(self, request):
        return web.json_response({'ok': True, 'mode': 'ai' if self.enabled else 'verify-only',
                                  'member_app': True, 'agent_id': self.agent_id,
                                  'personas': True, 'persona_version': 1,
                                  'last_sync_ok': int(self.last_ok), 'panel_version': PANEL_VERSION})

    async def collector(self):
        # Existing KF collection and its worker remain active under the same app.
        await asyncio.gather(super().collector(), self.member_processor())

    async def member_reply(self, row):
        session = self.session(row)
        try:
            with self.store.db:
                self.store.db.execute("UPDATE member_inbox SET status='working' WHERE msgid=?",
                                      (row['msgid'],))
            cached = row['answer']
            if cached is None:
                text = row['content'].strip()
                remember = False
                persona = self.persona(row)
                history_session = self.persona_session(row, persona)
                # Persist command state and its prepared reply in a single transaction.
                with self.store.db:
                    command = self.persona_command(row, text)
                    if command is not None:
                        stamp = time.strftime('%H:%M:%S', time.gmtime(time.time() + 8 * 3600))
                        cached = json.dumps({'text': kf.shorten(command + '\n\n操作时间：' + stamp),
                                             'remember': False, 'history_session': history_session,
                                             'panel_link': text in ('我要设置', '/网页设置', '/面板', '/settings')}, ensure_ascii=False)
                        self.store.db.execute('UPDATE member_inbox SET answer=? WHERE msgid=?',
                                              (cached, row['msgid']))
                if command is not None:
                    pass
                elif text in ('/new', '/reset', '清空对话'):
                    with self.store.db:
                        self.store.db.execute('DELETE FROM history WHERE session=?', (history_session,))
                    answer = '已清空' + persona['name'] + '的当前会话记录，可以开始新话题。'
                elif text == '/status':
                    checked = time.strftime('%H:%M:%S', time.gmtime(time.time() + 8 * 3600))
                    answer = ('企业应用接入正常，当前模型：' + self.member_model.name +
                              '。\n当前人物：' + persona['name'] + '\n检测时间：' + checked + '（北京时间）')
                elif text == '/help':
                    answer = ('我是小小艺，可以和你文字聊天。\n我要设置 打开专属设置网页\n/人物 查看人物模板\n/切换 温柔小艺 切换人物\n'
                              '/设定 加上描述创建个人设定\n/人物设定 查看设定\n/new 清空当前人物记录\n'
                              '/status 查看模型和当前人物')
                elif not text:
                    answer = '请用文字告诉我你的问题。'
                elif len(text) > 8000:
                    answer = '内容较长，请分成每条不超过 8000 字符后发送。'
                else:
                    answer, remember = await self.persona_answer(row, persona, self.store.history(history_session), text)
                if cached is None:
                    cached = json.dumps({'text': kf.shorten(answer), 'remember': remember,
                                         'history_session': history_session}, ensure_ascii=False)
                    with self.store.db:
                        self.store.db.execute('UPDATE member_inbox SET answer=? WHERE msgid=?',
                                              (cached, row['msgid']))
            response = json.loads(cached)
            sent = await self.api.call('message/send', {
                'touser': row['user'], 'msgtype': 'text', 'agentid': row['agent'],
                'text': {'content': response['text']}, 'safe': 0,
                'enable_duplicate_check': 1, 'duplicate_check_interval': 1800,
            })
            if any(sent.get(name) for name in ('invaliduser', 'invalidparty', 'invalidtag', 'unlicenseduser')):
                raise kf.APIError(81013)
            with self.store.db:
                if response['remember']:
                    history_session = response.get('history_session', session)
                    self.store.append_history(history_session, [('user',row['content'],dict(row).get('created')),('assistant',response['text'],time.time())])
                    self.store.trim_history(history_session)
                self.store.db.execute("UPDATE member_inbox SET status='done' WHERE msgid=?", (row['msgid'],))
                if response.get('panel_link'):
                    self.store.db.execute('UPDATE member_inbox SET answer=NULL WHERE msgid=?', (row['msgid'],))
            LOG.info('企业应用回复已提交。')
        except Exception as exc:
            self.log_failure('企业应用回复', exc)
            attempt = row['attempt'] + 1
            permanent = isinstance(exc, kf.APIError) and exc.code not in (-1, 45009, 45033)
            status = 'failed' if permanent or attempt >= 3 else 'pending'
            with self.store.db:
                self.store.db.execute('''UPDATE member_inbox SET status=?,attempt=?,due=? WHERE msgid=?''',
                    (status, attempt, time.time() + 30 * attempt, row['msgid']))
        finally:
            self.active.discard(session)

    async def member_processor(self):
        while True:
            rows = self.store.db.execute('''SELECT * FROM member_inbox WHERE status='pending'
                AND due<=? ORDER BY created,msgid LIMIT 100''', (time.time(),)).fetchall()
            for row in rows:
                session = self.session(row)
                if session in self.active:
                    continue
                first = self.store.db.execute('''SELECT msgid FROM member_inbox WHERE agent=? AND user=?
                    AND status IN ('pending','working') ORDER BY created,msgid LIMIT 1''',
                    (row['agent'], row['user'])).fetchone()
                if first['msgid'] != row['msgid']:
                    continue
                if time.time() - row['created'] > 86400:
                    with self.store.db:
                        self.store.db.execute("UPDATE member_inbox SET status='failed' WHERE msgid=?", (row['msgid'],))
                    continue
                if len(self.active) >= 2:
                    break
                self.active.add(session)
                task = asyncio.create_task(self.member_reply(row))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
            await asyncio.sleep(0.3)


def create_member_app(service):
    application = kf.create_app(service)
    application.router.add_get('/ai/enterprises/ww0000000000000003/1000031/xiaoyi/', service.panel_page)
    application.router.add_get('/ai/enterprises/ww0000000000000003/1000031/xiaoyi', service.panel_page)
    application.router.add_post('/ai/enterprises/ww0000000000000003/1000031/xiaoyi/api/session', service.panel_session)
    application.router.add_get('/ai/enterprises/ww0000000000000003/1000031/xiaoyi/api/settings', service.panel_settings)
    application.router.add_put('/ai/enterprises/ww0000000000000003/1000031/xiaoyi/api/settings', service.panel_settings)
    application.router.add_post('/ai/enterprises/ww0000000000000003/1000031/xiaoyi/api/logout', service.panel_logout)
    application.middlewares.append(panel_headers)
    return application


async def main_app():
    crypto = kf.Crypto(kf.required('CALLBACK_TOKEN'), kf.required('CALLBACK_AES_KEY'), kf.required('WECOM_CORP_ID'))
    store = kf.Store(os.environ.get('DATABASE_FILE', '/data/kf.sqlite3'))
    http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30, connect=10))
    try:
        enabled = os.environ.get('ENABLE_AI', 'false').lower() == 'true'
        api = kf.WeCom(http, crypto.corp_id, kf.required('WECOM_APP_SECRET')) if enabled else None
        model = kf.Model(http) if enabled else None
        service = MemberService(crypto, store, api, model, enabled)
        if enabled:
            service.member_model = kf.Model(http)
            service.member_model.prompt = os.environ.get('MEMBER_SYSTEM_PROMPT', MEMBER_BASE_PROMPT)
        application = create_member_app(service)
    except BaseException:
        await http.close()
        store.db.close()
        raise
    async def cleanup(application):
        await http.close()
        store.db.close()
    application.on_cleanup.append(cleanup)
    LOG.info('企业应用文字接入已加载，AgentId=%s。', service.agent_id)
    return application


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        web.run_app(main_app(), host='0.0.0.0', port=8088, access_log=None, print=None)
    except ValueError:
        LOG.error('企业应用配置无效，请核对 WECOM_AGENT_ID 等必填项。')
        raise SystemExit(1)


