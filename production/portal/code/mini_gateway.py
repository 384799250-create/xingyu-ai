"""Native settings adapter. Original account authentication; no payment access.

Panel credentials stay server-side. Application identity comes only from the
existing subject bridge, never a client-provided member, enterprise, or URL.
"""
import asyncio
import copy
import hashlib
import hmac
import json
import re
import secrets
from urllib.parse import urlsplit, parse_qs, urlencode

import aiohttp
from aiohttp import web
from mini_identity import MiniPortal, Identity

class Problem(Exception):
    def __init__(self,message,status=400):
        self.message,self.status = message,status

VERSION = 'mini-command-20261104.1'
PREFIX = '/ai/mini/v1'
FIELDS = {'display_name':40,'name':40,'nickname':40,'relationship':80,
          'personality':1600,'background':1200,'mood':120,'style':500,
          'catchphrases':300,'avoid':300,'do':800,'dont':800,'anchor':500,
          'worldbook':6000,'prompt':60000}
BUILTINS = {'default':'默认小艺','gentle':'温柔小艺','rational':'理性小艺','encourage':'鼓励小艺'}
SLOTS = ('p1','p2','p3','p4','p5')
PERSONAS = (*BUILTINS,'custom',*SLOTS,'official')
VOICE = ('voice','voice_speed','voice_rate','voice_language','voice_subtitles','voice_auto_emotion','ai_voice')


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def validate_source(value):
    if (not isinstance(value,dict) or set(value) != {'id','version','digest','requestId','syncApplication'}
        or not isinstance(value['id'],str) or not re.fullmatch('[a-f0-9]{24}',value['id'])
        or type(value['version']) is not int or not 1 <= value['version'] <= 1000000
        or not isinstance(value['digest'],str) or not re.fullmatch('[a-f0-9]{64}',value['digest'])
        or not isinstance(value['requestId'],str) or not re.fullmatch('[a-f0-9]{32}',value['requestId'])
        or type(value['syncApplication']) is not bool):
        raise Problem('模板来源无效，请重新选择。')


def settings_dto(raw, application):
    profiles = copy.deepcopy(raw['suite']['profiles'])
    if raw.get('legacy_persona_profile') and raw.get('selected') == 'official':
        profiles['official'] = copy.deepcopy(raw['legacy_persona_profile'])
    for key, title in BUILTINS.items():
        profiles.setdefault(key,{})
        if not profiles[key].get('display_name'):
            profiles[key]['display_name'] = title
    for key in list(profiles):
        if key not in BUILTINS and key != raw['selected'] and not any(profiles[key].values()):
            del profiles[key]
    profiles.setdefault(raw['selected'],{})
    if not profiles[raw['selected']].get('display_name'):
        profiles[raw['selected']]['display_name'] = raw.get('name') or '我的人格'
    return {'state':{'revision':raw['revision'],'selected':raw['selected'],'scope':sha(application['subject']),
                     'profiles':profiles,'profileLimit':len(PERSONAS),'remote':True,'templateDrafts':{}},
            'application':{'id':'current','name':application['name']},
            'applicationAdmin':bool(raw.get('application_admin'))}


def build_write(raw, data, prepared):
    """Merge explicit persona edits into a fresh original payload, preserving all else."""
    if not isinstance(data,dict) or set(data) != {'revision','selected','edits','templateDrafts'}:
        raise Problem('设置格式不正确。')
    if data['revision'] != raw['revision']:
        raise Problem('设置已在其他页面修改。草稿已保留，请重新读取后核对。',409)
    if data['selected'] not in PERSONAS or not isinstance(data['edits'],dict) or len(data['edits']) > len(PERSONAS):
        raise Problem('请选择有效的人格。')
    if not isinstance(data['templateDrafts'],dict) or len(data['templateDrafts']) > 5:
        raise Problem('模板草稿格式不正确。')
    suite = copy.deepcopy(raw['suite'])
    for slot, patch in data['edits'].items():
        if slot not in PERSONAS or not isinstance(patch,dict) or set(patch)-set(FIELDS):
            raise Problem('人格字段无效。')
        target = suite['profiles'].setdefault(slot,{})
        for field, value in patch.items():
            if not isinstance(value,str) or len(value) > FIELDS[field]:
                raise Problem('人格内容过长，请检查后重试。')
            target[field] = value
    voices = copy.deepcopy(raw.get('persona_voices',{}))
    voices[raw['selected']] = {key:raw['suite']['options'][key] for key in VOICE}
    applications, references = {}, {}
    request_ids = set()
    for slot, choice in data['templateDrafts'].items():
        validate_source(choice)
        if slot not in SLOTS or any(raw['suite']['profiles'].get(slot,{}).values()):
            raise Problem('新模板需要放入空闲人格，请重新读取后核对。',409)
        if choice['requestId'] in request_ids:
            raise Problem('模板请求编号重复。')
        request_ids.add(choice['requestId'])
        item = prepared[slot]
        if any(item['snapshot'][key] != choice[key] for key in ('id','version','digest')):
            raise Problem('模板已更新，请重新选择后保存。',409)
        # Client may edit the prepared profile, while voice/reference provenance
        # is always reconstructed from the actual published template on server.
        profile = copy.deepcopy(item['profile'])
        profile.update(suite['profiles'].get(slot,{}))
        suite['profiles'][slot] = profile
        voices[slot] = copy.deepcopy(item.get('voice_options') or voices[raw['selected']])
        source = {key:choice[key] for key in ('id','version','digest')}
        source.update(local=False,request_id=choice['requestId'])
        applications[slot] = dict(source,enabled=choice['syncApplication'])
        if item['snapshot'].get('reference'):
            references[slot] = dict(source,revision=raw.get('character_references',{}).get(slot,''))
    target_voice = voices.get(data['selected']) or raw.get('persona_voice_fallback') or voices[raw['selected']]
    suite['options'].update(target_voice)
    voices[data['selected']] = copy.deepcopy(target_voice)
    return {'revision':raw['revision'],'selected':data['selected'],
            'custom_prompt':raw.get('custom_prompt',''),'suite':suite,
            'greeting':{key:raw['greeting'][key] for key in ('enabled','time','text')},
            'persona_voice_drafts':voices,'template_application_drafts':applications,
            'template_reference_drafts':references}


class PanelTransport:
    def __init__(self, portal):
        self.portal = portal
        self.http = None
        self.cache = {}

    async def close(self):
        if self.http:
            await self.http.close()
        self.cache.clear()

    async def request(self, url, method='GET', data=None, cookie=None):
        if self.http is None:
            self.http = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar(),
                                              timeout=aiohttp.ClientTimeout(total=45),trust_env=False)
        headers = {'Origin':self.portal.origin}
        if cookie:
            headers['Cookie'] = 'xiaoyi_session=' + cookie
        try:
            async with self.http.request(method,url,json=data,headers=headers,allow_redirects=False) as response:
                # Limit template image responses, never follow a server redirect.
                chunks,size = [],0
                async for chunk in response.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > 3*1024*1024:
                        raise Problem('模板内容过大，请联系管理员检查。',502)
                    chunks.append(chunk)
                body = b''.join(chunks)
                if response.status not in (200,400,401,403,409,429):
                    raise Problem('应用暂时连接不上，请稍后重试。',503)
                try:
                    value = json.loads(body)
                except (ValueError,UnicodeError):
                    raise Problem('应用返回异常，请稍后重试。',502) from None
                if response.status != 200:
                    message = value.get('error') if isinstance(value,dict) else None
                    raise Problem(message if isinstance(message,str) and len(message)<400 else '操作未完成，请重试。',response.status)
                return value, response.cookies.get('xiaoyi_session')
        except (aiohttp.ClientError,asyncio.TimeoutError):
            raise Problem('连接中断，保存结果尚未确认。修改已保留，请读取最新设置后核对。',503) from None

    def validate_url(self, url, agent):
        if not isinstance(url,str):
            raise Problem('应用连接信息异常。',503)
        parsed, origin = urlsplit(url), urlsplit(self.portal.origin)
        pattern = r'/ai/(?:enterprises/ww0000000000000002/)?' + re.escape(str(agent)) + r'/xiaoyi/'
        if agent == 1000003:
            pattern = '(?:' + pattern + r'|/xiaoyi/)'
        fragment = parse_qs(parsed.fragment)
        if (parsed.scheme != 'https' or parsed.netloc != origin.netloc or parsed.query
            or not re.fullmatch(pattern,parsed.path) or set(fragment) != {'token'}
            or len(fragment['token']) != 1 or not re.fullmatch('[A-Za-z0-9_-]{32,128}',fragment['token'][0])):
            raise Problem('应用连接信息异常。',503)
        return self.portal.origin+parsed.path+'api/', fragment['token'][0]

    async def call(self, subject, endpoint, method='GET', data=None):
        if endpoint != 'settings' and not re.fullmatch(r'official-templates(?:\?id=[a-f0-9]{24}&version=[0-9]{1,7}&prepare=1)?',endpoint):
            raise Problem('请求不受支持。')
        application = await self.portal.bridge.call('status',subject=subject)
        now = self.portal.clock()
        self.cache = {k:v for k,v in self.cache.items() if v['expires'] > now}
        cached = self.cache.get(subject)
        if not cached or cached['agent'] != application['agent']:
            if len(self.cache) >= 128:
                self.cache.pop(next(iter(self.cache)))
            minted = await self.portal.bridge.call('mint',subject=subject)
            if minted['agent'] != application['agent'] or minted['subject'] != subject:
                raise Problem('应用归属已变化，请重新登录。',409)
            base, token = self.validate_url(minted['url'],application['agent'])
            _, cookie = await self.request(base+'session','POST',{'token':token})
            if cookie is None or not re.fullmatch('[A-Za-z0-9_-]{32,128}',cookie.value):
                raise Problem('应用会话创建失败，请重试。',503)
            cached = {'agent':application['agent'],'base':base,'cookie':cookie.value,'expires':now+6*86400}
            self.cache[subject] = cached
        try:
            result, _ = await self.request(cached['base']+endpoint,method,data,cached['cookie'])
        except Problem as exc:
            if exc.status == 401:
                self.cache.pop(subject,None)
            raise
        return result, application


class MiniGateway:
    def __init__(self, portal, transport=None):
        self.portal = MiniPortal(portal)
        self.transport = transport or PanelTransport(self.portal)
        self.slots = asyncio.Semaphore(4)
        self.locks = {}
        portal.db.executescript('''CREATE TABLE IF NOT EXISTS mini_sessions (
            hash TEXT PRIMARY KEY,account INTEGER NOT NULL REFERENCES accounts(id),
            credential TEXT NOT NULL,subject TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS mini_session_account ON mini_sessions(account);''')
        self.identity = Identity(self,Problem)

    def origin(self, request):
        if request.headers.get('Origin') not in (None,self.portal.origin) or request.query_string:
            raise Problem('请求来源无效。',403)

    async def body(self, request):
        self.origin(request)
        if request.content_type != 'application/json':
            raise Problem('请使用小程序提交设置。',415)
        try:
            value = await request.json()
        except (ValueError,UnicodeError):
            raise Problem('请求格式无效。') from None
        if not isinstance(value,dict):
            raise Problem('请求格式无效。')
        return value

    def user(self, request):
        self.origin(request)
        value = request.headers.get('Authorization','')
        if not re.fullmatch('Bearer [A-Za-z0-9_-]{43}',value):
            raise Problem('请先登录星屿账号。',401)
        row = self.portal.db.execute('''SELECT a.*,m.hash AS mini_hash,m.credential AS mini_credential,
            m.subject AS mini_subject FROM mini_sessions m JOIN accounts a ON a.id=m.account
            WHERE m.hash=? AND m.expires>?''',(sha(value[7:]),self.portal.clock())).fetchone()
        if not row:
            identity = self.identity.user(value[7:])
            if identity: return identity
        if (not row or not hmac.compare_digest(row['mini_credential'],sha(row['salt']+':'+row['password']))
            or row['subject'] != row['mini_subject']
            or self.portal.db.execute('SELECT 1 FROM account_closures WHERE account=?',(row['id'],)).fetchone()):
            raise Problem('登录已过期，请重新登录。',401)
        return row

    async def authenticate(self, request, data):
        if set(data) != {'username','password'}:
            raise Problem('请输入原星屿账号和密码。')
        self.portal.limit('login-ip:'+self.portal.ip(request),40,600)
        username,password = self.portal.credentials(data)
        self.portal.limit('login-name:'+username,12,600)
        account = self.portal.db.execute('SELECT * FROM accounts WHERE username=?',(username,)).fetchone()
        hashed = await self.portal.hash(password,account['salt'] if account else '00'*16)
        current = self.portal.db.execute('SELECT * FROM accounts WHERE username=?',(username,)).fetchone()
        if (not account or not current or current['id'] != account['id']
            or not hmac.compare_digest(hashed,current['password'])):
            raise Problem('账号或密码不正确。',401)
        if self.portal.db.execute('SELECT 1 FROM account_closures WHERE account=?',(current['id'],)).fetchone():
            raise Problem('该账号正在注销，无法登录。',403)
        if not current['subject']:
            current = await self.identity.resolve_account(current)
        if not current['subject']:
            raise Problem('请用原应用的设置链接完成首次连接。',409)
        await self.portal.bridge.call('status',subject=current['subject'])
        latest = self.portal.db.execute('SELECT * FROM accounts WHERE id=?',(current['id'],)).fetchone()
        if (not latest or latest['subject'] != current['subject'] or latest['password'] != current['password']
            or self.portal.db.execute('SELECT 1 FROM account_closures WHERE account=?',(current['id'],)).fetchone()):
            raise Problem('账号状态已变化，请重新登录。',401)
        return latest

    async def login(self, request):
        current = await self.authenticate(request,await self.body(request))
        token,now = secrets.token_urlsafe(32),self.portal.clock()
        self.portal.db.execute('DELETE FROM mini_sessions WHERE expires<=?',(now,))
        self.portal.db.execute('DELETE FROM mini_sessions WHERE account=? AND hash NOT IN '
                               '(SELECT hash FROM mini_sessions WHERE account=? ORDER BY created DESC LIMIT 4)',(current['id'],current['id']))
        self.portal.db.execute('INSERT INTO mini_sessions VALUES(?,?,?,?,?,?)',
            (sha(token),current['id'],sha(current['salt']+':'+current['password']),current['subject'],now,now+7*86400))
        return web.json_response({'session':token,'expiresAt':now+7*86400})

    async def logout(self, request):
        user = self.user(request)
        await self.body(request)
        self.portal.db.execute('DELETE FROM mini_sessions WHERE hash=?',(user['mini_hash'],))
        self.portal.db.execute('DELETE FROM mini_identity_sessions WHERE hash=?',(user['mini_hash'],))
        return web.json_response({'ok':True})

    async def execute(self, request, operation):
        user = self.user(request)
        self.portal.limit('mini:'+str(user['id']),120,60)
        subject = user['subject']
        # Bounded locks: remove an unlocked entry once no waiter remains.
        lock,refs = self.locks.get(subject,(asyncio.Lock(),0))
        self.locks[subject] = (lock,refs+1)
        try:
            async with self.slots,lock:
                self.user(request)
                return await operation(subject)
        finally:
            current = self.locks[subject]
            if current[1] == 1:
                del self.locks[subject]
            else:
                self.locks[subject] = (current[0],current[1]-1)

    async def applications(self, request):
        async def action(subject):
            item = await self.portal.bridge.call('status',subject=subject)
            return web.json_response({'items':[{'id':'current','name':item['name']}]})
        return await self.execute(request,action)

    async def settings(self, request):
        self.user(request)
        data = await self.body(request) if request.method == 'PUT' else None
        async def action(subject):
            raw,app = await self.transport.call(subject,'settings')
            if data is None:
                return web.json_response(settings_dto(raw,app))
            # Validate before any template lookup. Never trust a client member ID.
            build_write(raw,data,{}) if not data.get('templateDrafts') else None
            if not isinstance(data,dict) or set(data) != {'revision','selected','edits','templateDrafts'}:
                raise Problem('设置格式不正确。')
            if data['revision'] != raw['revision']:
                raise Problem('设置已在其他页面修改。草稿已保留，请重新读取后核对。',409)
            choices = data['templateDrafts']
            if not isinstance(choices,dict) or len(choices)>5:
                raise Problem('模板草稿格式不正确。')
            prepared = {}
            for slot,choice in choices.items():
                validate_source(choice)
                if slot not in SLOTS:
                    raise Problem('模板人格位置无效。')
                prepared[slot],_ = await self.transport.call(subject,'official-templates?'+urlencode(
                    {'id':choice['id'],'version':choice['version'],'prepare':1}))
            payload = build_write(raw,data,prepared)
            self.user(request)
            saved,app = await self.transport.call(subject,'settings','PUT',payload)
            result = settings_dto(saved,app)
            messages = []
            for choice in choices.values():
                receipt = saved.get('template_application_receipts',{}).get(choice['requestId'],{})
                if receipt.get('message'):
                    messages.append(receipt['message'])
            result['notice'] = ' '.join(dict.fromkeys(messages))
            return web.json_response(result)
        return await self.execute(request,action)

    async def templates(self, request):
        async def action(subject):
            result,_ = await self.transport.call(subject,'official-templates')
            return web.json_response({'items':[{'id':v['id'],'revision':v['version'],
                'title':v['name'],'description':v['intro'],'hasVoice':bool(v.get('voice')),
                'hasReference':bool(v.get('has_reference'))} for v in result['items']]})
        return await self.execute(request,action)

    async def prepare(self, request):
        self.user(request)
        data = await self.body(request)
        if (set(data) != {'id','version'} or not isinstance(data['id'],str)
            or not re.fullmatch('[a-f0-9]{24}',data['id']) or type(data['version']) is not int
            or not 1 <= data['version'] <= 1000000):
            raise Problem('模板版本无效。')
        async def action(subject):
            item,_ = await self.transport.call(subject,'official-templates?'+urlencode(
                {'id':data['id'],'version':data['version'],'prepare':1}))
            snapshot = item['snapshot']
            return web.json_response({'profile':item['profile'],'source':dict(
                id=snapshot['id'],version=snapshot['version'],digest=snapshot['digest'],
                requestId=secrets.token_hex(16),syncApplication=True),
                'notice':item.get('notice',''),'hasVoice':bool(item.get('voice_options')),
                'hasReference':bool(snapshot.get('reference'))})
        return await self.execute(request,action)


def install_mini(portal, app):
    gateway = MiniGateway(portal)
    def safe(handler):
        async def wrapped(request):
            try:
                return await handler(request)
            except Problem as exc:
                return web.json_response({'error':exc.message},status=exc.status)
        return wrapped
    app.router.add_get(PREFIX+'/status',lambda request:web.json_response({'ready':True,'version':VERSION,'login':'application-binding','wechatReady':bool(gateway.identity.credentials()),'phoneReady':gateway.identity.phone.ready(),'settingsEntryReady':gateway.identity.entry.ready()}))
    app.router.add_post(PREFIX+'/wechat/session',safe(gateway.identity.wechat_session))
    app.router.add_post(PREFIX+'/entry/generate',safe(gateway.identity.entry.generate))
    app.router.add_post(PREFIX+'/entry/session',safe(gateway.identity.entry.connect))
    app.router.add_post(PREFIX+'/phone/resolve',safe(gateway.identity.phone.resolve))
    app.router.add_post(PREFIX+'/phone/apply',safe(gateway.identity.phone.apply))
    app.router.add_post(PREFIX+'/phone/progress',safe(gateway.identity.phone.progress))
    app.router.add_get(PREFIX+'/applications/current/join',safe(gateway.identity.phone.join))
    app.router.add_post(PREFIX+'/wechat/bind/account',safe(gateway.identity.bind_account))
    app.router.add_post(PREFIX+'/link/session',safe(gateway.identity.bind_link))
    app.router.add_post(PREFIX+'/wechat/unbind',safe(gateway.identity.unbind))
    app.router.add_post(PREFIX+'/session',safe(gateway.login))
    app.router.add_post(PREFIX+'/logout',safe(gateway.logout))
    app.router.add_get(PREFIX+'/applications',safe(gateway.applications))
    app.router.add_get(PREFIX+'/applications/current/settings',safe(gateway.settings))
    app.router.add_put(PREFIX+'/applications/current/settings',safe(gateway.settings))
    app.router.add_get(PREFIX+'/applications/current/templates',safe(gateway.templates))
    app.router.add_post(PREFIX+'/applications/current/templates/prepare',safe(gateway.prepare))
    async def close(app):
        await gateway.transport.close()
        await gateway.identity.close()
    app.on_cleanup.insert(0,close)
