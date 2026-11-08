"""Chat-issued short-lived settings entry, independent of phone entitlement."""
import asyncio
import json
from pathlib import Path
import re
import secrets
from urllib.parse import urlsplit
import aiohttp
from aiohttp import web
from mini_identity import APPID, digest

CONFIG = Path('/data/miniprogram-entry.json')

class Entry:
    def __init__(self, identity):
        self.identity, self.portal, self.gateway = identity, identity.portal, identity.gateway
        self.error = identity.error
        self.portal.db.executescript('''CREATE TABLE IF NOT EXISTS mini_entry_grants(
            hash TEXT PRIMARY KEY, request_hash TEXT UNIQUE NOT NULL, subject TEXT NOT NULL,
            account INTEGER, credential TEXT, expires REAL NOT NULL, claimed TEXT,
            state TEXT NOT NULL, url TEXT);''')

    def ready(self):
        try:
            if CONFIG.is_symlink(): return False
            config = json.loads(CONFIG.read_text())
            return (config.get('appid') == APPID and config.get('enabled') is True
                and config.get('release_verified') is True and config.get('url_link_verified') is True
                and bool(self.identity.credentials()))
        except (OSError, ValueError, TypeError, AttributeError): return False

    def require(self):
        if not self.ready(): raise self.error('小程序直达入口尚未开放，请先使用原网页设置。', 503)

    def row(self, raw):
        if not isinstance(raw, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', raw):
            raise self.error('设置入口无效，请回聊天重新发送 我要设置。', 401)
        row = self.portal.db.execute('SELECT * FROM mini_entry_grants WHERE hash=? AND expires>?',
            (digest(raw), self.portal.clock())).fetchone()
        if not row or row['state'] != 'ready':
            raise self.error('设置入口已失效，请回聊天重新发送 我要设置。', 401)
        return row

    def account(self, grant):
        if not self.identity.account_valid(grant['account'], grant['credential'], grant['subject']):
            raise self.error('应用归属或账号状态已变化，请重新发送 我要设置。', 401)
        row = self.portal.db.execute('SELECT * FROM accounts WHERE subject=?', (grant['subject'],)).fetchone()
        if (row['id'] if row else None) != grant['account']:
            raise self.error('应用关联已变化，请重新发送 我要设置。', 409)
        return row

    async def url_link(self, raw, expires):
        token = await self.identity.phone.access_token()
        # access_token initialized the shared HTTP client. Never follow redirects.
        try:
            async with self.identity.exchange_slots:
                async with self.identity.http.post('https://api.weixin.qq.com/wxa/generate_urllink',
                    params={'access_token': token}, json={'path':'pages/settings/index',
                    'query':'entry='+raw, 'expire_type':0, 'expire_time':int(expires),
                    'env_version':'release'}, allow_redirects=False) as response:
                    body = await response.content.read(16385)
                    if response.status != 200 or len(body) > 16384: raise ValueError()
                    value = json.loads(body)
                    if not isinstance(value, dict) or value.get('errcode'): raise ValueError()
                    url = value.get('url_link')
                    if not isinstance(url, str) or len(url) > 1024: raise ValueError()
                    parsed = urlsplit(url)
                    if parsed.scheme != 'https' or parsed.netloc not in ('wxaurl.cn','wxmpurl.cn') or parsed.fragment:
                        raise ValueError()
                    return url
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, UnicodeError):
            raise self.error('小程序入口暂未生成，请使用本次网页设置链接。', 503) from None

    async def generate(self, request):
        data = await self.gateway.body(request); self.require()
        if set(data) != {'link','requestId'} or not isinstance(data['requestId'],str) or not re.fullmatch(r'[a-f0-9]{64}',data['requestId']):
            raise self.error('入口请求无效。')
        self.portal.limit('mini-entry-ip:'+self.portal.ip(request),40,600)
        # Only an existing private settings link proves the callback sender's app.
        app = await self.portal.bridge.call('verify',link=data['link'])
        self.portal.limit('mini-entry-subject:'+app['subject'],15,600)
        account = self.portal.db.execute('SELECT * FROM accounts WHERE subject=?',(app['subject'],)).fetchone()
        account_id = account['id'] if account else None
        credential = digest(account['salt']+':'+account['password']) if account else None
        if not self.identity.account_valid(account_id,credential,app['subject']):
            raise self.error('账号状态暂不可用。',403)
        now = self.portal.clock(); key = digest(app['subject']+':'+data['requestId'])
        db = self.portal.db
        db.execute('DELETE FROM mini_entry_grants WHERE expires<=?',(now,))
        existing = db.execute('SELECT * FROM mini_entry_grants WHERE request_hash=?',(key,)).fetchone()
        if existing:
            self.account(existing)
            if existing['state']=='ready':return web.json_response({'url':existing['url'],'expiresAt':existing['expires']})
            raise self.error('入口生成结果尚未确认，请使用原网页设置。',503)
        raw = secrets.token_urlsafe(32); expires = now+300
        db.execute('INSERT INTO mini_entry_grants VALUES(?,?,?,?,?,?,NULL,?,NULL)',
            (digest(raw),key,app['subject'],account_id,credential,expires,'creating'))
        try:
            url = await self.url_link(raw,expires)
        except BaseException:
            db.execute("UPDATE mini_entry_grants SET state='unknown' WHERE hash=?",(digest(raw),))
            raise
        db.execute("UPDATE mini_entry_grants SET state='ready',url=? WHERE hash=?",(url,digest(raw)))
        return web.json_response({'url':url,'expiresAt':expires})

    async def connect(self, request):
        data = await self.gateway.body(request); self.require()
        if set(data) != {'entry','code'}:raise self.error('请从自己聊天中的设置入口进入。')
        self.row(data['entry']); self.identity.limit(request)
        self.portal.limit('mini-entry-grant:'+digest(data['entry']),8,600)
        open_hash = await self.identity.exchange(data['code'])
        grant = self.row(data['entry']); self.account(grant)
        await self.portal.bridge.call('status',subject=grant['subject'])
        grant = self.row(data['entry']); account = self.account(grant)
        if grant['claimed']:
            binding = self.portal.db.execute('SELECT * FROM mini_bindings WHERE appid=? AND open_hash=?', (APPID,open_hash)).fetchone()
            if (grant['claimed']!=open_hash or not binding or binding['subject']!=grant['subject']
                or binding['account']!=grant['account'] or binding['credential']!=grant['credential']):
                raise self.error('这个入口已使用，请回自己的聊天重新发送 我要设置。',409)
            return self.identity.issue(binding['subject'],binding['account'],binding['credential'],binding['id'])
        ticket = secrets.token_urlsafe(32)
        self.portal.db.execute('INSERT INTO mini_binding_tickets VALUES(?,?,?)', (digest(ticket),open_hash,self.portal.clock()+60))
        try:
            # Claim and identity binding commit in one transaction.
            return self.identity.finish(ticket,grant['subject'],account,entry_hash=grant['hash'])
        finally:
            self.portal.db.execute('DELETE FROM mini_binding_tickets WHERE hash=?',(digest(ticket),))
