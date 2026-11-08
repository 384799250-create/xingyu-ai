"""Phone consent, existing-app resolution, and explicit new-app enrollment."""
import asyncio
import json
from pathlib import Path
import re
import secrets
import sqlite3
from aiohttp import web
from mini_identity import APPID, digest

PHONE_CONFIG = Path('/data/miniprogram-phone.json')


class PhoneConnect:
    def __init__(self, identity):
        self.identity, self.gateway, self.portal = identity, identity.gateway, identity.portal
        self.error = identity.error
        self.token_cache = None
        self.token_lock = asyncio.Lock()
        self.proofs = {}
        self.portal.db.executescript('''CREATE TABLE IF NOT EXISTS mini_enrollments (
            open_hash TEXT PRIMARY KEY,account INTEGER UNIQUE NOT NULL,
            proofs TEXT NOT NULL,created REAL NOT NULL);''')

    def ready(self):
        try:
            if PHONE_CONFIG.is_symlink(): return False
            value = json.loads(PHONE_CONFIG.read_text())
            return (value.get('appid') == APPID and value.get('enabled') is True
                    and value.get('qualification_verified') is True
                    and bool(self.identity.credentials()))
        except (OSError, ValueError, TypeError, AttributeError): return False

    def require(self):
        if not self.ready(): raise self.error('手机号连接尚未开放，可使用下方其他连接方式。', 503)

    async def official(self, path, data, token=None):
        # Only these two fixed official operations are available, no redirects.
        if path not in ('cgi-bin/stable_token', 'wxa/business/getuserphonenumber'):
            raise self.error('请求不受支持。')
        import aiohttp
        if self.identity.http is None:
            self.identity.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12),
                trust_env=False, cookie_jar=aiohttp.DummyCookieJar())
        try:
            async with self.identity.exchange_slots:
                async with self.identity.http.post('https://api.weixin.qq.com/'+path, json=data,
                        params={'access_token': token} if token else None, allow_redirects=False) as response:
                    raw = await response.content.read(16385)
                    if response.status != 200 or len(raw) > 16384: raise ValueError()
                    result = json.loads(raw)
                    if not isinstance(result, dict): raise ValueError()
            return result
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, UnicodeError):
            raise self.error('微信手机号授权结果尚未确认，请重新授权后再试。', 503) from None

    async def access_token(self):
        async with self.token_lock:
            now = self.portal.clock()
            if self.token_cache and self.token_cache['expires'] > now + 120:
                return self.token_cache['token']
            config = self.identity.credentials()
            if not config: raise self.error('微信连接暂不可用。', 503)
            result = await self.official('cgi-bin/stable_token', dict(grant_type='client_credential',
                appid=APPID, secret=config['secret'], force_refresh=False))
            if not isinstance(result.get('access_token'), str) or result.get('errcode'):
                raise self.error('微信手机号连接暂不可用，请稍后再试。', 503)
            self.token_cache = {'token': result['access_token'], 'expires': now + min(int(result.get('expires_in', 7200)), 7200)}
            return self.token_cache['token']

    async def phone_number(self, code):
        if not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9_\-]{8,256}', code):
            raise self.error('手机号授权未完成，请重新点击授权。')
        result = await self.official('wxa/business/getuserphonenumber', {'code': code}, await self.access_token())
        if result.get('errcode'):
            raise self.error('微信未能完成手机号授权，请重新点击授权或使用其他连接方式。', 403)
        info = result.get('phone_info')
        if not isinstance(info, dict): raise self.error('微信手机号返回异常，请重新授权。', 502)
        mark = info.get('watermark')
        if (not isinstance(mark, dict) or mark.get('appid') != APPID or str(info.get('countryCode')) != '86'
                or not isinstance(info.get('purePhoneNumber'), str)
                or not re.fullmatch(r'1[3-9][0-9]{9}', info['purePhoneNumber'])
                or type(mark.get('timestamp')) not in (int, float)
                or not -120 <= self.portal.clock()-mark['timestamp'] <= 600):
            raise self.error('请使用当前微信的中国大陆手机号重新授权。', 403)
        return '+86' + info['purePhoneNumber']

    def proof(self, value):
        self.proofs = {k:v for k,v in self.proofs.items() if v['expires'] > self.portal.clock()}
        result = self.proofs.get(value) if isinstance(value, str) else None
        if not result: raise self.error('手机号确认已过期，请重新授权。', 401)
        return result

    def enrollment(self, open_hash):
        return self.portal.db.execute('SELECT * FROM mini_enrollments WHERE open_hash=?', (open_hash,)).fetchone()

    def adopt(self, open_hash, account, proofs):
        db = self.portal.db
        row = db.execute('SELECT * FROM accounts WHERE id=?', (int(account),)).fetchone()
        if not row or db.execute('SELECT 1 FROM account_closures WHERE account=?', (row['id'],)).fetchone():
            raise self.error('账号状态暂不可用，请联系管理员。', 403)
        try:
            db.execute('INSERT INTO mini_enrollments VALUES(?,?,?,?) ON CONFLICT(open_hash) DO NOTHING',
                       (open_hash, int(account), json.dumps(proofs), self.portal.clock()))
        except sqlite3.IntegrityError:
            raise self.error('此应用正在由另一个微信连接，请使用原微信。', 409) from None
        current = self.enrollment(open_hash)
        if not current or current['account'] != int(account):
            raise self.error('此微信已有开通记录，请先查看原记录。', 409)

    async def finish(self, ticket, found):
        proof = self.identity.ticket(ticket)
        account = None
        account_id = found.get('account')
        if account_id:
            account = self.portal.db.execute('SELECT * FROM accounts WHERE id=?', (int(account_id),)).fetchone()
            if not account or self.portal.db.execute('SELECT 1 FROM account_closures WHERE account=?', (int(account_id),)).fetchone():
                raise self.error('账号状态暂不可用。', 403)
            if account['subject'] and account['subject'] != found['subject']:
                raise self.error('账号的应用归属不一致，请联系管理员。', 409)
            # The private phone bridge checked current directory ownership and
            # assignment. Reuse the existing confirmation workflow after proof.
            await self.portal.onboarding.call('confirm', account=str(account_id), subject=found['subject'])
            try:
                self.portal.db.execute('UPDATE accounts SET subject=?,waiting=NULL WHERE id=? AND password=? AND (subject IS NULL OR subject=?)',
                    (found['subject'], account['id'], account['password'], found['subject']))
            except sqlite3.IntegrityError:
                raise self.error('应用已关联其他账号，请联系管理员。', 409) from None
            account = self.portal.db.execute('SELECT * FROM accounts WHERE id=?', (int(account_id),)).fetchone()
        else:
            account = self.portal.db.execute('SELECT * FROM accounts WHERE subject=?', (found['subject'],)).fetchone()
        await self.portal.bridge.call('status', subject=found['subject'])
        response = self.identity.finish(ticket, found['subject'], account)
        self.portal.db.execute('DELETE FROM mini_enrollments WHERE open_hash=?', (proof['open_hash'],))
        value = json.loads(response.body)
        # Joining WeChat is still a real platform action, never simulated.
        if account_id:
            try:
                qr = await self.portal.onboarding.call('qr', account=str(account_id))
                value['joinQr'] = 'data:'+qr['content_type']+';base64,'+qr['image']
            except Exception: value['joinNotice'] = '应用已准备好。添加到微信的二维码暂未读取，请到原网页查看加入方式。'
        return web.json_response(value)

    async def resolve(self, request):
        data = await self.gateway.body(request)
        self.require()
        if set(data) != {'ticket', 'code'}: raise self.error('请通过微信按钮授权手机号。')
        ticket = self.identity.ticket(data['ticket'])
        self.identity.limit(request)
        self.portal.limit('mini-phone:'+ticket['open_hash'], 8, 600)
        phone = await self.phone_number(data['code'])
        found = await self.portal.bridge.call('phone', phone=phone)
        self.identity.ticket(data['ticket'])
        if found['state'] == 'existing': return await self.finish(data['ticket'], found)
        if found['state'] == 'pending':
            self.adopt(ticket['open_hash'], found['account'], found['proofs'])
            return web.json_response({'pending': True, 'state': 'preparing'})
        if found['state'] != 'new': raise self.error('应用归属尚未确认，请稍后重试。', 409)
        self.proofs = {k:v for k,v in self.proofs.items() if v['expires'] > self.portal.clock()}
        if len(self.proofs) >= 128: raise self.error('当前连接较多，请稍后重试。', 429)
        nonce = secrets.token_urlsafe(32)
        self.proofs[nonce] = dict(open_hash=ticket['open_hash'], phone=phone,
            proofs=found['proofs'], expires=self.portal.clock()+600)
        return web.json_response({'needsApplication': True, 'proof': nonce})

    async def apply(self, request):
        data = await self.gateway.body(request)
        self.require()
        if set(data) != {'ticket', 'proof', 'consent'} or data.get('consent') is not True:
            raise self.error('请确认使用已授权的微信手机号开通应用。')
        ticket, proof = self.identity.ticket(data['ticket']), self.proof(data['proof'])
        if ticket['open_hash'] != proof['open_hash']: raise self.error('微信身份已变化，请重新授权。', 403)
        self.portal.limit('mini-apply-ip:'+self.portal.ip(request), 5, 3600)
        # A final fresh lookup prevents a new allocation if another request has
        # assigned an existing application since the phone was authorized.
        found = await self.portal.bridge.call('phone', phone=proof['phone'])
        self.identity.ticket(data['ticket'])
        self.proof(data['proof'])
        if found['state'] == 'existing': return await self.finish(data['ticket'], found)
        db = self.portal.db
        enrollment = self.enrollment(ticket['open_hash'])
        if found['state'] == 'pending':
            self.adopt(ticket['open_hash'], found['account'], found['proofs'])
            return web.json_response({'pending': True, 'state': 'preparing'})
        if found['state'] != 'new': raise self.error('应用状态已变化，请重新读取。', 409)
        if not enrollment:
            # A managed account gives the original allocator its existing global
            # account ID. No login password is exposed or reused for another user.
            salt = secrets.token_hex(16)
            password = await self.portal.hash(secrets.token_urlsafe(48), salt)
            self.identity.ticket(data['ticket'])
            self.proof(data['proof'])
            try:
                db.execute('BEGIN IMMEDIATE')
                enrollment = self.enrollment(ticket['open_hash'])
                if not enrollment:
                    cur = db.execute('INSERT INTO accounts(id,username,salt,password,created,profile_name,onboarding) '
                        'SELECT (SELECT COALESCE(MAX(id),0)+1 FROM (SELECT id FROM accounts UNION ALL SELECT account AS id FROM account_closures)),?,?,?,?,?,1',
                        ('wx_'+secrets.token_hex(16), salt, password, self.portal.clock(), '星屿用户'))
                    self.adopt(ticket['open_hash'], cur.lastrowid, found['proofs'])
                db.execute('COMMIT')
            except BaseException:
                if db.in_transaction: db.execute('ROLLBACK')
                raise
            enrollment = self.enrollment(ticket['open_hash'])
        self.proofs.pop(data['proof'], None)
        # Original submit is idempotent by account+phone and enforces pool,
        # operation locks, account closures and daily application limits.
        try:
            result = await self.portal.onboarding.call('submit', account=str(enrollment['account']),
                phone=proof['phone'], display_name='星屿用户', avatar=None, avatar_type=None)
        except Exception:
            raise self.error('开通结果尚未确认，请点击“查看开通进度”，不要重复申请。', 503) from None
        return web.json_response({'pending': True, 'state': result['state']})

    async def progress(self, request):
        data = await self.gateway.body(request)
        self.require()
        if set(data) != {'ticket'}: raise self.error('请重新连接微信。')
        ticket = self.identity.ticket(data['ticket'])
        row = self.enrollment(ticket['open_hash'])
        if not row: raise self.error('尚无开通记录，请先授权手机号。', 409)
        self.portal.limit('mini-progress:'+ticket['open_hash'], 30, 60)
        found = await self.portal.bridge.call('phone-progress', account=str(row['account']), proofs=json.loads(row['proofs']))
        if found['state'] == 'existing': return await self.finish(data['ticket'], found)
        state = found['state']
        messages = {'waiting_pool': '应用名额暂时已满，已保留你的申请。',
                    'needs_inspection': '开通需要管理员检查，申请已保留。',
                    'queued': '申请已收到，正在安排应用。', 'provisioning': '正在准备你的应用。',
                    'verifying': '正在检查应用是否就绪。'}
        return web.json_response({'pending': True, 'state': state,
            'message': messages.get(state, '应用正在准备中，可以稍后再查看。')})

    async def join(self, request):
        user = self.gateway.user(request)
        account = user['account'] if 'account' in user.keys() else user['id']
        if account is None:
            return web.json_response({'joinNotice': '你已连接原来的应用，回到原微信聊天窗口即可。'})
        self.portal.limit('mini-join:'+str(account), 10, 60)
        await self.portal.bridge.call('status', subject=user['subject'])
        qr = await self.portal.onboarding.call('qr', account=str(account))
        self.gateway.user(request)
        return web.json_response({'joinQr': 'data:'+qr['content_type']+';base64,'+qr['image']})
