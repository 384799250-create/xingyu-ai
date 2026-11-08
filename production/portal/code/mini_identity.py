"""Wechat code authentication and explicit first-use ownership proof.

OpenID is never accepted from clients. Existing enterprise identities, wallets
and website accounts are not inferred from an unrelated WeChat identifier.
"""
import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3

import aiohttp
from aiohttp import web

APPID = 'wx0000000000000000'
CREDENTIALS = Path('/data/miniprogram-credentials.json')


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class MiniBridge:
    def __init__(self, original): self.original = original
    async def call(self, action, **fields):
        original = self.original
        if action in ('phone','phone-progress') and hasattr(original,'path'):
            original = type(original)(original.path,timeout=45)
        return await original.call('mini-' + action, **fields)


class MiniPortal:
    def __init__(self, original):
        self.original = original
        self.bridge = MiniBridge(original.bridge)
    def __getattr__(self, name): return getattr(self.original, name)


class Identity:
    def __init__(self, gateway, error):
        self.gateway, self.portal, self.error = gateway, gateway.portal, error
        self.http = None
        self.exchange_slots = asyncio.Semaphore(4)
        from mini_phone import PhoneConnect
        self.phone = PhoneConnect(self)
        from mini_entry import Entry
        self.entry = Entry(self)
        self.portal.db.executescript('''
            CREATE TABLE IF NOT EXISTS mini_bindings (
                id TEXT PRIMARY KEY,appid TEXT NOT NULL,open_hash TEXT NOT NULL,
                subject TEXT NOT NULL,account INTEGER,credential TEXT,
                created REAL NOT NULL,UNIQUE(appid,open_hash),UNIQUE(appid,subject));
            CREATE TABLE IF NOT EXISTS mini_binding_tickets (
                hash TEXT PRIMARY KEY,open_hash TEXT NOT NULL,expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS mini_identity_sessions (
                hash TEXT PRIMARY KEY,binding TEXT,subject TEXT NOT NULL,
                account INTEGER,credential TEXT,created REAL NOT NULL,expires REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS mini_identity_binding ON mini_identity_sessions(binding);
        ''')

    def credentials(self):
        try:
            if CREDENTIALS.is_symlink(): return None
            st = CREDENTIALS.stat()
            if os.name != 'nt' and (st.st_mode & 0o077 or st.st_uid != os.geteuid()): return None
            value = json.loads(CREDENTIALS.read_text())
            if not isinstance(value, dict): return None
            if value.get('appid') == APPID and re.fullmatch(r'[A-Za-z0-9]{32}', value.get('secret', '')):
                return value
        except (OSError, ValueError, TypeError): pass
        return None

    async def close(self):
        if self.http: await self.http.close()

    async def exchange(self, code):
        if not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9_\-]{8,256}', code):
            raise self.error('微信登录凭证无效，请重新打开小程序。')
        config = self.credentials()
        if not config:
            raise self.error('微信登录尚未配置，可先用原应用的设置链接连接。', 503)
        if self.http is None:
            self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12), trust_env=False,
                                              cookie_jar=aiohttp.DummyCookieJar())
        try:
            async with self.exchange_slots:
                async with self.http.get('https://api.weixin.qq.com/sns/jscode2session', params={
                    'appid': APPID, 'secret': config['secret'], 'js_code': code,
                    'grant_type': 'authorization_code'}, allow_redirects=False) as response:
                    raw = await response.content.read(16385)
                    if response.status != 200 or len(raw) > 16384: raise ValueError()
                    value = json.loads(raw)
                    if not isinstance(value, dict): raise ValueError()
            if value.get('errcode') or not isinstance(value.get('openid'), str) or not re.fullmatch(r'[A-Za-z0-9_\-]{16,128}', value['openid']):
                raise self.error('微信登录未完成，请重新点击连接。', 401)
            # Do not retain session_key, UnionID or raw OpenID.
            return digest(APPID + ':' + value['openid'])
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, UnicodeError):
            raise self.error('微信登录暂时连接不上，请稍后重试。', 503) from None

    def account_valid(self, account, credential, subject):
        if account is None: return True
        row = self.portal.db.execute('SELECT * FROM accounts WHERE id=?', (account,)).fetchone()
        return bool(row and row['subject'] == subject and hmac.compare_digest(
            digest(row['salt'] + ':' + row['password']), credential or '')
            and not self.portal.db.execute('SELECT 1 FROM account_closures WHERE account=?', (account,)).fetchone())

    def user(self, token):
        row = self.portal.db.execute('SELECT * FROM mini_identity_sessions WHERE hash=? AND expires>?',
                                    (digest(token), self.portal.clock())).fetchone()
        if not row or not self.account_valid(row['account'], row['credential'], row['subject']):
            return None
        if row['binding']:
            binding = self.portal.db.execute('SELECT * FROM mini_bindings WHERE id=?', (row['binding'],)).fetchone()
            if not binding or binding['subject'] != row['subject'] or binding['credential'] != row['credential']:
                return None
        return dict(row, id='identity:' + (row['binding'] or row['hash']), mini_hash=row['hash'])

    def issue(self, subject, account=None, credential=None, binding=None):
        now, token = self.portal.clock(), secrets.token_urlsafe(32)
        self.portal.db.execute('DELETE FROM mini_identity_sessions WHERE expires<=?', (now,))
        if binding:
            self.portal.db.execute('DELETE FROM mini_identity_sessions WHERE binding=? AND hash NOT IN '
                '(SELECT hash FROM mini_identity_sessions WHERE binding=? ORDER BY created DESC LIMIT 4)', (binding, binding))
        self.portal.db.execute('INSERT INTO mini_identity_sessions VALUES(?,?,?,?,?,?,?)',
            (digest(token), binding, subject, account, credential, now, now + 7 * 86400))
        return web.json_response({'session': token, 'expiresAt': now + 7 * 86400, 'wechatBound': bool(binding)})

    def limit(self, request):
        self.portal.limit('mini-wechat-ip:' + self.portal.ip(request), 40, 600)

    async def wechat_session(self, request):
        data = await self.gateway.body(request)
        if set(data) != {'code'}: raise self.error('请重新发起微信登录。')
        self.limit(request)
        open_hash = await self.exchange(data['code'])
        bound = self.portal.db.execute('SELECT * FROM mini_bindings WHERE appid=? AND open_hash=?', (APPID, open_hash)).fetchone()
        if bound and self.account_valid(bound['account'], bound['credential'], bound['subject']):
            await self.portal.bridge.call('status', subject=bound['subject'])
            # Recheck after the bridge await; password/closure/unbind can change meanwhile.
            fresh = self.portal.db.execute('SELECT * FROM mini_bindings WHERE id=?', (bound['id'],)).fetchone()
            if (fresh and dict(fresh) == dict(bound)
                    and self.account_valid(fresh['account'], fresh['credential'], fresh['subject'])):
                return self.issue(bound['subject'], bound['account'], bound['credential'], bound['id'])
            raise self.error('应用关联已变化，请重新连接。', 409)
        now, ticket = self.portal.clock(), secrets.token_urlsafe(32)
        self.portal.db.execute('DELETE FROM mini_binding_tickets WHERE expires<=? OR open_hash=?', (now, open_hash))
        self.portal.db.execute('INSERT INTO mini_binding_tickets VALUES(?,?,?)', (digest(ticket), open_hash, now + 600))
        return web.json_response({'needsBinding': True, 'ticket': ticket, 'expiresAt': now + 600, 'hasEnrollment': bool(self.phone.enrollment(open_hash))})

    def ticket(self, value):
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', value):
            raise self.error('首次连接已过期，请重新点击微信连接。', 401)
        row = self.portal.db.execute('SELECT * FROM mini_binding_tickets WHERE hash=? AND expires>?',
                                    (digest(value), self.portal.clock())).fetchone()
        if not row: raise self.error('首次连接已过期，请重新点击微信连接。', 401)
        return row

    def finish(self, ticket, subject, account=None, entry_hash=None):
        proof = self.ticket(ticket)
        account_id = account['id'] if account else None
        credential = digest(account['salt'] + ':' + account['password']) if account else None
        if not self.account_valid(account_id, credential, subject):
            raise self.error('账号状态已变化，请重新连接。', 401)
        db = self.portal.db
        try:
            db.execute('BEGIN IMMEDIATE')
            proof = self.ticket(ticket)
            existing = db.execute('SELECT * FROM mini_bindings WHERE appid=? AND open_hash=?', (APPID, proof['open_hash'])).fetchone()
            other = db.execute('SELECT id FROM mini_bindings WHERE appid=? AND subject=?', (APPID, subject)).fetchone()
            if other and (not existing or other['id'] != existing['id']):
                raise self.error('这个应用已连接另一个微信，请使用原微信或联系管理员。', 409)
            if existing and existing['subject'] != subject:
                raise self.error('此微信已连接其他应用，请先解除原连接。', 409)
            ident = existing['id'] if existing else secrets.token_hex(16)
            if existing:
                db.execute('UPDATE mini_bindings SET account=?,credential=? WHERE id=?', (account_id, credential, ident))
                db.execute('DELETE FROM mini_identity_sessions WHERE binding=?', (ident,))
            else:
                db.execute('INSERT INTO mini_bindings VALUES(?,?,?,?,?,?,?)',
                    (ident, APPID, proof['open_hash'], subject, account_id, credential, self.portal.clock()))
            if db.execute('DELETE FROM mini_binding_tickets WHERE hash=?', (proof['hash'],)).rowcount != 1:
                raise self.error('连接已完成，请重新打开小程序。', 409)
            if entry_hash is not None:
                if db.execute('UPDATE mini_entry_grants SET claimed=? WHERE hash=? AND claimed IS NULL AND expires>? AND subject=?', (proof['open_hash'],entry_hash,self.portal.clock(),subject)).rowcount != 1:
                    raise self.error('设置入口已使用或过期，请重新发送 我要设置。',409)
            db.execute('COMMIT')
        except BaseException:
            if db.in_transaction: db.execute('ROLLBACK')
            raise
        return self.issue(subject, account_id, credential, ident)

    async def bind_account(self, request):
        data = await self.gateway.body(request)
        if set(data) != {'ticket', 'username', 'password'}: raise self.error('请输入原星屿账号和密码。')
        self.ticket(data['ticket'])
        account = await self.gateway.authenticate(request, {k: data[k] for k in ('username', 'password')})
        return self.finish(data['ticket'], account['subject'], account)

    async def bind_link(self, request):
        data = await self.gateway.body(request)
        if set(data) not in ({'link'}, {'link', 'ticket'}): raise self.error('请粘贴原应用发来的完整设置链接。')
        self.limit(request)
        if 'ticket' in data: self.ticket(data['ticket'])
        application = await self.portal.bridge.call('verify', link=data['link'])
        # Do not create/claim a website account, or change the owner of any app.
        account = self.portal.db.execute('SELECT * FROM accounts WHERE subject=?', (application['subject'],)).fetchone()
        if account and not self.account_valid(account['id'], digest(account['salt'] + ':' + account['password']), application['subject']):
            raise self.error('该账号正在注销，无法连接。', 403)
        if 'ticket' in data: return self.finish(data['ticket'], application['subject'], account)
        return self.issue(application['subject'], account['id'] if account else None,
                          digest(account['salt'] + ':' + account['password']) if account else None)

    async def unbind(self, request):
        user = self.gateway.user(request)
        data = await self.gateway.body(request)
        if data != {}: raise self.error('请求格式无效。')
        binding = user.get('binding') if isinstance(user, dict) else None
        if not binding: raise self.error('当前连接没有绑定微信。', 409)
        db = self.portal.db
        try:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM mini_identity_sessions WHERE binding=?', (binding,))
            db.execute('DELETE FROM mini_bindings WHERE id=?', (binding,))
            db.execute('COMMIT')
        except BaseException:
            if db.in_transaction: db.execute('ROLLBACK')
            raise
        return web.json_response({'ok': True})

    async def resolve_account(self, account):
        if account['subject'] or not self.portal.onboarding: return account
        provision = await self.portal.onboarding.call('status', account=str(account['id']))
        value = provision.get('subject')
        if provision.get('state') == 'ready' and isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value):
            await self.portal.bridge.call('status', subject=value)
            try:
                self.portal.db.execute('UPDATE accounts SET subject=?,waiting=NULL WHERE id=? AND subject IS NULL AND password=?',
                                       (value, account['id'], account['password']))
            except sqlite3.IntegrityError:
                raise self.error('应用已关联其他账号，请使用原账号连接。', 409) from None
            return self.portal.db.execute('SELECT * FROM accounts WHERE id=?', (account['id'],)).fetchone()
        raise self.error('此账号的应用尚未完成关联。已有应用请改用它发来的设置链接连接。', 409)
