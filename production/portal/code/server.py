"""Small isolated public portal. No application credentials or chat data mounted."""
import asyncio
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from aiohttp import web
from mailer import EmailSender, MailUnavailable

BASE = Path(__file__).resolve().parent
COOKIE = 'xiaoyi_portal'
ERRORS = {
    'invalid_link': '请粘贴独立 AI 发给你的完整设置链接，包含 #token 后的内容。',
    'expired_link': '设置链接已过期或无效。请向自己的 AI 发送 我要设置，复制新链接后重试。',
    'application_unavailable': '当前应用暂不可用，请联系管理员核对分配或启用状态。',
    'service_unavailable': '应用连接暂不可用，请稍后再试。',
    'qr_unavailable': '微信二维码尚未就绪或已过期，请联系管理员更新。',
    'pool_empty': '空闲应用已分配完，请登记等待补充。',
    'invalid_phone': '请输入有效的中国大陆手机号。',
    'onboarding_daily_limit': '今日开通申请较多，请稍后再试或联系管理员。',
    'confirmation_not_ready': '应用尚未准备好，请稍后重新检查开通进度。',
    'confirmation_mismatch': '这个设置链接不属于本次开通的 AI。请向页面显示的应用发送 我要设置，复制新链接。',
    'phone_already_requested': '这个手机号已申请过 AI，请使用原网页账号登录。',
    'request_identity_mismatch': '此账号已提交开通申请，请查看原申请进度。',
    'email_not_configured': '邮箱验证服务尚未配置，请联系管理员。',
    'email_unverified': '请先验证这个邮箱，再用于找回密码。',
    'email_code_invalid': '验证码不正确或已过期，请重新获取。',
}


class Problem(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status


def digest(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()


def normalize_email(value):
    if not isinstance(value, str):
        raise Problem('请输入有效的邮箱地址。')
    value = value.strip().lower()
    if len(value) > 254 or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+", value):
        raise Problem('请输入有效的邮箱地址。')
    return value


class UnixBridge:
    def __init__(self, path, timeout=8):
        self.path, self.timeout = path, timeout

    async def call(self, action, **fields):
        writer = None
        try:
            async with asyncio.timeout(self.timeout):
                reader, writer = await asyncio.open_unix_connection(self.path, limit=900000)
                writer.write(json.dumps({'action': action, **fields}).encode()+b'\n')
                await writer.drain()
                value = json.loads(await reader.readline())
                if not value.get('ok'):
                    raise Problem(ERRORS.get(value.get('error'), ERRORS['service_unavailable']), 409)
                return value['data']
        except Problem:
            raise
        except Exception:
            raise Problem(ERRORS['service_unavailable'], 503) from None
        finally:
            if writer:
                writer.close()
                await writer.wait_closed()


class Portal:
    def __init__(self, database, bridge, origin, secure=True, clock=time.time, onboarding=None, email_sender=None):
        self.db = sqlite3.connect(str(database), isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS accounts (
              id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
              salt TEXT NOT NULL, password TEXT NOT NULL, created REAL NOT NULL,
              subject TEXT UNIQUE, waiting REAL, email TEXT, email_verified REAL,
              profile_name TEXT, avatar BLOB, avatar_type TEXT);
            CREATE TABLE IF NOT EXISTS sessions (
              hash TEXT PRIMARY KEY, account INTEGER NOT NULL REFERENCES accounts(id),
              csrf TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS account_closures (
              account INTEGER PRIMARY KEY, closure TEXT NOT NULL UNIQUE,
              state TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS limits (key TEXT PRIMARY KEY, start REAL NOT NULL, hits INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS email_codes (
              id INTEGER PRIMARY KEY, account INTEGER NOT NULL REFERENCES accounts(id),
              email TEXT NOT NULL, purpose TEXT NOT NULL, code_hash TEXT NOT NULL,
              created REAL NOT NULL, expires REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
              used REAL);
            CREATE TABLE IF NOT EXISTS registration_codes (
              email TEXT PRIMARY KEY, code_hash TEXT NOT NULL, created REAL NOT NULL,
              expires REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, used REAL);
            CREATE INDEX IF NOT EXISTS email_codes_lookup ON email_codes(account,purpose,expires,used);
        ''')
        self.bridge, self.origin, self.secure, self.clock = bridge, origin.rstrip('/'), secure, clock
        self.onboarding = onboarding
        self.email_sender = email_sender or EmailSender()
        columns = {r[1] for r in self.db.execute('PRAGMA table_info(accounts)')}
        if 'email' not in columns:
            self.db.execute('ALTER TABLE accounts ADD COLUMN email TEXT')
        if 'email_verified' not in columns:
            self.db.execute('ALTER TABLE accounts ADD COLUMN email_verified REAL')
        if 'profile_name' not in columns:
            self.db.execute('ALTER TABLE accounts ADD COLUMN profile_name TEXT')
        if 'avatar' not in columns:
            self.db.execute('ALTER TABLE accounts ADD COLUMN avatar BLOB')
        if 'avatar_type' not in columns:
            self.db.execute('ALTER TABLE accounts ADD COLUMN avatar_type TEXT')
        if 'onboarding' not in {r[1] for r in self.db.execute('PRAGMA table_info(accounts)')}:
            self.db.execute('ALTER TABLE accounts ADD COLUMN onboarding INTEGER NOT NULL DEFAULT 0')
        self.hash_slots = asyncio.Semaphore(2)

    def limit(self, key, maximum, seconds):
        key, now = digest(key), self.clock()
        self.db.execute('DELETE FROM limits WHERE start<?', (now-86400,))
        self.db.execute('INSERT INTO limits(key,start,hits) VALUES(?,?,1) ON CONFLICT(key) DO UPDATE SET '
                        'start=CASE WHEN start<=? THEN excluded.start ELSE start END, '
                        'hits=CASE WHEN start<=? THEN 1 ELSE hits+1 END', (key, now, now-seconds, now-seconds))
        if self.db.execute('SELECT hits FROM limits WHERE key=?', (key,)).fetchone()[0] > maximum:
            raise Problem('操作太频繁，请稍后再试。', 429)

    def user(self, request, required=True):
        raw = request.cookies.get(COOKIE, '')
        session = None
        if re.fullmatch(r'[A-Za-z0-9_-]{43}', raw):
            session = self.db.execute('SELECT a.*, s.csrf, s.hash AS session_hash FROM sessions s JOIN accounts a ON a.id=s.account '
                                      'WHERE s.hash=? AND s.expires>?', (digest(raw), self.clock())).fetchone()
        if session and self.db.execute('SELECT 1 FROM account_closures WHERE account=?',(session['id'],)).fetchone():
            session=None
        if required and not session:
            raise Problem('请先登录。', 401)
        return session

    async def data(self, request, authenticated=False):
        if request.headers.get('Origin') != self.origin or request.headers.get('X-Requested-With') != 'xiaoyi-portal':
            raise Problem('请求来源无效，请刷新页面后重试。', 403)
        if request.content_type != 'application/json':
            raise Problem('请使用页面上的表单提交。', 415)
        if authenticated:
            user = self.user(request)
            if not hmac.compare_digest(request.headers.get('X-CSRF-Token', ''), user['csrf']):
                raise Problem('页面验证已失效，请刷新后重试。', 403)
        try:
            value = await request.json()
        except web.HTTPException:
            raise
        except Exception:
            raise Problem('提交内容无效。') from None
        if not isinstance(value, dict):
            raise Problem('提交内容无效。')
        return value

    def login_name(self, value):
        if not isinstance(value,str):raise Problem('请输入邮箱或原账号。')
        value=value.strip().lower()
        if '@' in value:return normalize_email(value)
        if not re.fullmatch('[a-zA-Z0-9_]{3,32}',value):raise Problem('请输入注册邮箱；已有账号也可使用原账号。')
        return value

    def credentials(self, data):
        username=self.login_name(data.get('username',''))
        password=data.get('password','')
        if not isinstance(password,str) or not 6<=len(password)<=128 or len(password.encode())>512:
            raise Problem('密码请设置为 6–128 个字符。')
        return username,password

    def account_identity(self, data):
        return self.login_name(data.get('username','')),normalize_email(data.get('email',''))

    def email_in_use(self,address,except_account=-1,verified_only=False):
        return self.db.execute("SELECT 1 FROM accounts WHERE id!=? AND (lower(username)=? OR (lower(trim(email))=?"+
            (" AND email_verified IS NOT NULL" if verified_only else "")+")) LIMIT 1",(except_account,address,address)).fetchone() is not None

    async def registration_request(self,request):
        data=await self.data(request)
        address=normalize_email(data.get('email',''))
        self.limit('register-mail-ip:'+self.ip(request),12,3600)
        self.limit('register-mail-hour:'+address,5,3600)
        self.limit('register-mail-minute:'+address,1,60)
        if self.email_in_use(address):raise Problem('这个邮箱已登记，请登录原账号或找回密码。',409)
        code=f'{secrets.randbelow(1000000):06d}';now=self.clock();code_hash=digest(code)
        self.db.execute('DELETE FROM registration_codes WHERE expires<? OR used IS NOT NULL',(now,))
        self.db.execute('INSERT INTO registration_codes(email,code_hash,created,expires) VALUES(?,?,?,?) '
                        'ON CONFLICT(email) DO UPDATE SET code_hash=excluded.code_hash,created=excluded.created,expires=excluded.expires,attempts=0,used=NULL',
                        (address,code_hash,now,now+600))
        try:await self.email_sender.send_code(address,code,'register')
        except BaseException as exc:
            self.db.execute('DELETE FROM registration_codes WHERE email=? AND code_hash=? AND created=?',(address,code_hash,now))
            if isinstance(exc,MailUnavailable):
                raise Problem(ERRORS['email_not_configured'] if str(exc)=='not_configured' else '邮件暂时发送失败，请稍后重试。',503) from None
            raise
        return web.json_response({'ok':True,'message':'验证码已发送，10 分钟内有效。'})

    def registration_proof(self,address,code):
        if not isinstance(code,str) or not re.fullmatch(r'[0-9]{6}',code):raise Problem(ERRORS['email_code_invalid'])
        row=self.db.execute('SELECT * FROM registration_codes WHERE email=? AND used IS NULL AND expires>?',(address,self.clock())).fetchone()
        if not row or row['attempts']>=5:raise Problem(ERRORS['email_code_invalid'])
        self.db.execute('UPDATE registration_codes SET attempts=attempts+1 WHERE email=?',(address,))
        if not hmac.compare_digest(row['code_hash'],digest(code)):raise Problem(ERRORS['email_code_invalid'])
        return row

    @staticmethod
    def profile(data, optional=True):
        name = data.get('display_name', '')
        if not isinstance(name, str):
            raise Problem('请输入 AI 伙伴昵称。')
        name = name.strip()
        if not optional and not name:
            raise Problem('请输入 AI 伙伴昵称。')
        if name and (not 1 <= len(name) <= 32 or any(ord(c) < 32 or ord(c) == 127 for c in name)):
            raise Problem('AI 伙伴昵称请设置为 1–32 个字符。')
        raw, mime = None, None
        value = data.get('avatar')
        if value:
            if not isinstance(value, str) or not value.startswith('data:image/') or ';base64,' not in value:
                raise Problem('头像格式不正确，请重新选择 JPG 或 PNG 图片。')
            header, encoded = value.split(';base64,', 1)
            mime = header[5:].lower()
            if mime not in ('image/jpeg', 'image/png') or len(encoded) > 700000:
                raise Problem('头像请使用 JPG 或 PNG，压缩后不超过 512KB。')
            try:
                raw = base64.b64decode(encoded, validate=True)
            except Exception:
                raise Problem('头像格式不正确，请重新选择图片。') from None
            if len(raw) > 512 * 1024:
                raise Problem('头像请使用 JPG 或 PNG，压缩后不超过 512KB。')
            valid = (mime == 'image/png' and raw.startswith(b'\x89PNG\r\n\x1a\n')) or (mime == 'image/jpeg' and raw.startswith(b'\xff\xd8\xff'))
            if not valid:
                raise Problem('头像格式不正确，请重新选择 JPG 或 PNG 图片。')
        return name or None, raw, mime

    def new_email_code(self, account, address, purpose):
        code = f'{secrets.randbelow(1000000):06d}'
        now = self.clock()
        self.db.execute('DELETE FROM email_codes WHERE expires<? OR used IS NOT NULL', (now,))
        self.db.execute('DELETE FROM email_codes WHERE account=? AND purpose=? AND used IS NULL', (account, purpose))
        self.db.execute('INSERT INTO email_codes(account,email,purpose,code_hash,created,expires) VALUES(?,?,?,?,?,?)',
                        (account, address, purpose, digest(code), now, now + 600))
        return code

    async def send_code(self, account, address, purpose):
        code = self.new_email_code(account, address, purpose)
        try:
            await self.email_sender.send_code(address, code, purpose)
        except MailUnavailable as exc:
            self.db.execute('DELETE FROM email_codes WHERE account=? AND purpose=? AND used IS NULL', (account, purpose))
            raise Problem(ERRORS['email_not_configured'] if str(exc) == 'not_configured' else '邮件暂时发送失败，请稍后重试。', 503) from None
        except BaseException:
            self.db.execute('DELETE FROM email_codes WHERE account=? AND purpose=? AND used IS NULL', (account, purpose))
            raise

    async def verify_code(self, account, address, purpose, code):
        if not isinstance(code, str) or not re.fullmatch(r'\d{6}', code):
            raise Problem(ERRORS['email_code_invalid'], 400)
        row = self.db.execute('SELECT * FROM email_codes WHERE account=? AND email=? AND purpose=? AND used IS NULL AND expires>? ORDER BY id DESC LIMIT 1',
                              (account, address, purpose, self.clock())).fetchone()
        if not row:
            raise Problem(ERRORS['email_code_invalid'], 400)
        self.db.execute('UPDATE email_codes SET attempts=attempts+1 WHERE id=?', (row['id'],))
        if row['attempts'] >= 5 or not hmac.compare_digest(row['code_hash'], digest(code)):
            raise Problem(ERRORS['email_code_invalid'], 400)
        self.db.execute('UPDATE email_codes SET used=? WHERE id=?', (self.clock(), row['id']))

    async def hash(self, password, salt):
        async with self.hash_slots:
            return await asyncio.to_thread(password_hash, password, salt)

    def create_session(self, account):
        if self.db.execute('SELECT 1 FROM account_closures WHERE account=?',(account,)).fetchone():
            raise Problem('该账号已进入注销流程，无法登录。请联系管理员。',403)
        if not self.db.execute('SELECT 1 FROM accounts WHERE id=?',(account,)).fetchone():
            raise Problem('账号已注销，请重新注册。',403)
        raw, csrf, now = secrets.token_urlsafe(32), secrets.token_urlsafe(32), self.clock()
        self.db.execute('DELETE FROM sessions WHERE expires<=?', (now,))
        self.db.execute('DELETE FROM sessions WHERE account=? AND hash NOT IN '
                        '(SELECT hash FROM sessions WHERE account=? ORDER BY created DESC LIMIT 4)', (account, account))
        self.db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)', (digest(raw), account, csrf, now, now+7*86400))
        response = web.json_response({'ok': True})
        response.set_cookie(COOKIE, raw, max_age=7*86400, path='/ai/', httponly=True, secure=self.secure, samesite='Strict')
        return response

    def ip(self, request):
        # Gateway replaces this header, and this container has no public host port.
        return request.headers.get('X-Real-IP', request.remote or 'unknown')[:100]

    async def register(self, request):
        data=await self.data(request)
        self.limit('register:'+self.ip(request),5,3600)
        address=normalize_email(data.get('email') or data.get('username',''))
        if data.get('username') and normalize_email(data['username'])!=address:
            raise Problem('注册邮箱不一致，请刷新页面后重试。')
        username,password=self.credentials({'username':address,'password':data.get('password')})
        if self.email_in_use(address):raise Problem('这个邮箱已登记，请登录原账号或找回密码。',409)
        proof=self.registration_proof(address,data.get('code',''))
        profile_name,avatar,avatar_type=self.profile(data)
        salt=secrets.token_hex(16);hashed=await self.hash(password,salt)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if self.email_in_use(address):raise Problem('这个邮箱已登记，请登录原账号或找回密码。',409)
            consumed=self.db.execute('UPDATE registration_codes SET used=? WHERE email=? AND code_hash=? AND created=? '
                                    'AND used IS NULL AND expires>? AND attempts<=5',
                                    (self.clock(),address,proof['code_hash'],proof['created'],self.clock()))
            if consumed.rowcount!=1:raise Problem(ERRORS['email_code_invalid'])
            cur=self.db.execute('INSERT INTO accounts(id,username,salt,password,created,email,email_verified,profile_name,avatar,avatar_type) SELECT (SELECT COALESCE(MAX(id),0)+1 FROM (SELECT id FROM accounts UNION ALL SELECT account AS id FROM account_closures)),?,?,?,?,?,?,?,?,?',
                                (username,salt,hashed,self.clock(),address,self.clock(),profile_name,avatar,avatar_type))
            self.db.execute('COMMIT')
        except BaseException as exc:
            self.db.execute('ROLLBACK')
            if isinstance(exc,sqlite3.IntegrityError):raise Problem('这个邮箱已登记，请登录原账号或找回密码。',409) from None
            raise
        return self.create_session(cur.lastrowid)

    async def login(self, request):
        data = await self.data(request)
        self.limit('login-ip:'+self.ip(request), 40, 600)
        username, password = self.credentials(data)
        self.limit('login-name:'+username, 12, 600)
        account = self.db.execute('SELECT * FROM accounts WHERE username=?', (username,)).fetchone()
        hashed = await self.hash(password, account['salt'] if account else '00'*16)
        latest = self.db.execute('SELECT password FROM accounts WHERE username=?', (username,)).fetchone()
        if not account or not latest or not hmac.compare_digest(hashed, latest['password']):
            raise Problem('账号或密码不正确。', 401)
        return self.create_session(account['id'])

    async def me(self, request):
        user = self.user(request, False)
        if not user:
            return web.json_response({'authenticated': False})
        result = {'authenticated': True, 'username': user['username'], 'csrf': user['csrf'],
                  'email': user['email'] or '', 'email_verified': bool(user['email_verified']),
                  'profile_name': user['profile_name'] or '', 'has_avatar': bool(user['avatar']),
                  'state': 'waiting' if user['waiting'] else 'unbound', 'application': None}
        if user['subject']:
            result['state'] = 'unavailable'
            try:
                app = await self.bridge.call('status', subject=user['subject'])
                result.update(state='assigned', application={'name': app['name'], 'agent': app['agent']})
            except Problem as exc:
                result['message'] = exc.message
        if self.onboarding and (not user['subject'] or user['onboarding']):
            try:
                provision = await self.onboarding.call('status', account=str(user['id']))
                subject = provision.pop('subject', None)
                if provision['state'] != 'ready':
                    subject = None
                result['onboarding'] = provision
                if provision['state'] != 'needs_phone':
                    self.db.execute('UPDATE accounts SET onboarding=1 WHERE id=?', (user['id'],))
                elif user['onboarding']:
                    result['onboarding'].update(state='needs_inspection', stage='submission_uncertain', error='submission_uncertain')
                if provision['state'] == 'awaiting_scan':
                    result.update(state='awaiting_scan', application=provision['application'])
                if subject:
                    if not re.fullmatch(r'[a-f0-9]{64}', subject) or (user['subject'] and user['subject'] != subject):
                        raise Problem('应用绑定不一致，请联系管理员检查。', 409)
                    try:
                        changed = self.db.execute('UPDATE accounts SET subject=?,waiting=NULL WHERE id=? AND (subject IS NULL OR subject=?)',
                                                  (subject, user['id'], subject))
                    except sqlite3.IntegrityError:
                        raise Problem('该 AI 已连接其他账号，请联系管理员检查。', 409) from None
                    if changed.rowcount != 1:
                        raise Problem('应用绑定已变化，请刷新页面。', 409)
                    result.update(state='awaiting_scan' if provision['state']=='awaiting_scan' else 'assigned',
                                  application=provision['application'])
            except Problem as exc:
                result['onboarding_error'] = exc.message
        return web.json_response(result)

    async def bind(self, request):
        data = await self.data(request, True)
        user = self.user(request)
        if user['onboarding']:
            raise Problem('此账号已提交开通申请，请等待开通完成。', 409)
        self.limit('bind:'+str(user['id']), 10, 600)
        app = await self.bridge.call('verify', link=data.get('link'))
        if user['subject'] and user['subject'] != app['subject']:
            raise Problem('当前账号已经绑定另一位成员，不能覆盖。', 409)
        try:
            changed = self.db.execute('UPDATE accounts SET subject=?,waiting=NULL WHERE id=? AND onboarding=0 AND (subject IS NULL OR subject=?)',
                                      (app['subject'], user['id'], app['subject']))
            if changed.rowcount != 1:
                raise Problem('当前账号已经绑定另一位成员，不能覆盖。', 409)
        except sqlite3.IntegrityError:
            raise Problem('这位成员已经绑定其他网页账号，请使用原账号登录。', 409) from None
        return web.json_response({'ok': True})

    async def update_profile(self, request):
        data = await self.data(request, True)
        user = self.user(request)
        if user['subject'] or user['onboarding']:
            raise Problem('开通申请已经开始，不能重复修改伙伴资料。', 409)
        name, avatar, avatar_type = self.profile(data, optional=False)
        self.db.execute('UPDATE accounts SET profile_name=?,avatar=?,avatar_type=? WHERE id=?',
                        (name, avatar, avatar_type, user['id']))
        return web.json_response({'ok': True, 'profile_name': name, 'has_avatar': bool(avatar)})

    async def waiting(self, request):
        await self.data(request, True)
        user = self.user(request)
        if user['subject']:
            raise Problem('你已经绑定 AI，无需申请。', 409)
        self.db.execute('UPDATE accounts SET waiting=COALESCE(waiting,?) WHERE id=?', (self.clock(), user['id']))
        return web.json_response({'ok': True})

    async def open_settings(self, request):
        await self.data(request, True)
        user = self.user(request)
        self.limit('open:'+str(user['id']), 20, 600)
        if not user['subject']:
            raise Problem('请先绑定自己的 AI。', 409)
        app = await self.bridge.call('mint', subject=user['subject'])
        return web.json_response({'url': app['url']})

    def onboarding_user(self, request):
        user = self.user(request)
        if user['subject'] and not user['onboarding']:
            raise Problem('你已有自己的 AI，无需重复开通。', 409)
        if not self.onboarding:
            raise Problem(ERRORS['service_unavailable'], 503)
        return user

    async def provision(self, request):
        data = await self.data(request, True)
        user = self.onboarding_user(request)
        if data.get('consent') is not True:
            raise Problem('请先确认使用自己的微信手机号开通专属 AI。')
        submitted_name = data.get('display_name', user['profile_name'] or '星屿用户')
        submitted_avatar = data.get('avatar') if 'avatar' in data else (('data:'+str(user['avatar_type'])+';base64,'+base64.b64encode(user['avatar']).decode()) if user['avatar'] else '')
        name, avatar, avatar_type = self.profile({'display_name': submitted_name, 'avatar': submitted_avatar}, optional=False)
        self.limit('provision:'+str(user['id']), 12, 600)
        self.limit('provision-ip:'+self.ip(request), 5, 3600)
        changed = self.db.execute('UPDATE accounts SET onboarding=1 WHERE id=? AND subject IS NULL AND onboarding=0', (user['id'],))
        if changed.rowcount != 1:
            raise Problem('开通申请已提交，请刷新页面查看进度。', 409)
        try:
            self.db.execute('UPDATE accounts SET profile_name=?,avatar=?,avatar_type=? WHERE id=?',
                            (name, avatar, avatar_type, user['id']))
            result = await self.onboarding.call('submit', account=str(user['id']), phone=data.get('phone'),
                                                display_name=name or '星屿用户',
                                                avatar=base64.b64encode(avatar).decode() if avatar else None,
                                                avatar_type=avatar_type)
        except Problem as exc:
            if exc.status == 409 and exc.message != ERRORS['service_unavailable']:
                self.db.execute('UPDATE accounts SET onboarding=0 WHERE id=? AND subject IS NULL', (user['id'],))
            raise
        return web.json_response({'ok': True, 'state': result['state']})

    async def confirm_provision(self, request):
        data = await self.data(request, True)
        user = self.onboarding_user(request)
        self.limit('confirm:'+str(user['id']), 10, 600)
        proof = await self.bridge.call('verify', link=data.get('link'))
        result = await self.onboarding.call('confirm', account=str(user['id']), subject=proof['subject'])
        return web.json_response({'ok': True, 'state': result['state']})

    async def retry_provision(self, request):
        await self.data(request, True)
        user = self.onboarding_user(request)
        self.limit('provision-retry:'+str(user['id']), 6, 600)
        await self.onboarding.call('retry_pool', account=str(user['id']))
        return web.json_response({'ok': True})

    async def onboarding_qr(self, request):
        user = self.onboarding_user(request)
        result = await self.onboarding.call('qr', account=str(user['id']))
        try:
            body = base64.b64decode(result['image'], validate=True)
        except Exception:
            raise Problem(ERRORS['qr_unavailable'], 409) from None
        if not 100 < len(body) < 200000:
            raise Problem(ERRORS['qr_unavailable'], 409)
        # onboarding-qr-format-v1: serving type must match actual image bytes.
        if body.startswith(b'\x89PNG\r\n\x1a\n'):
            content_type = 'image/png'
        elif body.startswith(b'\xff\xd8\xff'):
            content_type = 'image/jpeg'
        else:
            raise Problem(ERRORS['qr_unavailable'], 409)
        return web.Response(body=body, content_type=content_type)

    async def logout(self, request):
        await self.data(request, True)
        user = self.user(request)
        self.db.execute('DELETE FROM sessions WHERE hash=?', (user['session_hash'],))
        response = web.json_response({'ok': True})
        response.del_cookie(COOKIE, path='/ai/', secure=self.secure, httponly=True, samesite='Strict')
        return response

    async def change_password(self, request):
        data = await self.data(request, True)
        user = self.user(request)
        self.limit('password:'+str(user['id']), 6, 600)
        _, new = self.credentials({'username': user['username'], 'password': data.get('password')})
        old = data.get('old_password', '')
        if not isinstance(old, str) or len(old) > 128:
            raise Problem('当前密码不正确。', 401)
        hashed_old = await self.hash(old, user['salt'])
        latest = self.db.execute('SELECT password FROM accounts WHERE id=?', (user['id'],)).fetchone()
        if not latest or not hmac.compare_digest(hashed_old, latest['password']):
            raise Problem('当前密码不正确。', 401)
        salt = secrets.token_hex(16)
        hashed = await self.hash(new, salt)
        self.user(request)  # Session may have been revoked during password hashing.
        self.db.execute('BEGIN IMMEDIATE')
        try:
            changed = self.db.execute('UPDATE accounts SET salt=?,password=? WHERE id=? AND password=?', (salt, hashed, user['id'], latest['password']))
            if changed.rowcount != 1:
                raise Problem('密码已在其他窗口修改，请重新登录。', 409)
            self.db.execute('DELETE FROM sessions WHERE account=?', (user['id'],))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
        return self.create_session(user['id'])

    async def email_send(self, request):
        data = await self.data(request, True)
        user = self.user(request)
        address = normalize_email(data.get('email') or user['email'] or '')
        if self.email_in_use(address,user['id'],verified_only=True):raise Problem('这个邮箱已关联其他账号，请使用该账号登录。',409)
        if user['email_verified'] and address != user['email']:
            raise Problem('已验证邮箱不能直接更换，请联系管理员。', 409)
        if user['email_verified']:
            return web.json_response({'ok': True, 'message': '这个邮箱已经验证，可用于找回密码。'})
        self.limit('email-send:'+str(user['id']), 3, 3600)
        self.limit('email-send-minute:'+str(user['id']), 1, 60)
        self.limit('email-send-address:'+address, 6, 3600)
        self.limit('email-send-ip:'+self.ip(request), 12, 3600)
        self.db.execute('UPDATE accounts SET email=?,email_verified=NULL WHERE id=? AND (email IS NULL OR email_verified IS NULL OR email=?)',
                        (address, user['id'], address))
        await self.send_code(user['id'], address, 'verify')
        return web.json_response({'ok': True, 'message': '验证码已发送，请检查邮箱。'})

    async def email_verify(self, request):
        data = await self.data(request, True)
        user = self.user(request)
        address = normalize_email(data.get('email') or user['email'] or '')
        self.limit('email-verify:'+str(user['id']), 10, 600)
        if self.email_in_use(address,user['id'],verified_only=True):raise Problem('这个邮箱已关联其他账号，请使用该账号登录。',409)
        await self.verify_code(user['id'], address, 'verify', data.get('code', ''))
        changed = self.db.execute('UPDATE accounts SET email=?,email_verified=? WHERE id=? AND email=?',
                                  (address, self.clock(), user['id'], address))
        if changed.rowcount != 1:
            raise Problem('邮箱状态已变化，请刷新页面。', 409)
        return web.json_response({'ok': True})

    async def recover_request(self, request):
        data = await self.data(request)
        username, address = self.account_identity(data)
        self.limit('recover-email-ip:'+self.ip(request), 6, 3600)
        self.limit('recover-email-name:'+username, 5, 3600)
        self.limit('recover-email-minute:'+username, 1, 60)
        account = self.db.execute('SELECT id,email,email_verified FROM accounts WHERE username=?', (username,)).fetchone()
        if not account or account['email'] != address or not account['email_verified']:
            raise Problem('账号和邮箱不匹配，或该邮箱尚未验证。', 409)
        await self.send_code(account['id'], address, 'recover')
        return web.json_response({'ok': True, 'message': '验证码已发送，请检查邮箱。'})

    async def recover(self, request):
        data = await self.data(request)
        self.limit('recover:'+self.ip(request), 6, 3600)
        username, password = self.credentials(data)
        if data.get('code') or data.get('email'):
            address = normalize_email(data.get('email', ''))
            code = data.get('code', '')
            self.limit('recover-code:'+username, 8, 600)
            account = self.db.execute('SELECT * FROM accounts WHERE username=? AND email=? AND email_verified IS NOT NULL',
                                      (username, address)).fetchone()
            if not account:
                raise Problem('账号和邮箱不匹配，或该邮箱尚未验证。', 409)
            await self.verify_code(account['id'], address, 'recover', code)
        else:
            proof = await self.bridge.call('verify', link=data.get('link'))
            account = self.db.execute('SELECT * FROM accounts WHERE username=? AND subject=?', (username, proof['subject'])).fetchone()
            if not account:
                raise Problem('账号与设置链接不匹配，请核对原账号。', 409)
        salt = secrets.token_hex(16)
        hashed = await self.hash(password, salt)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('UPDATE accounts SET salt=?,password=? WHERE id=?', (salt, hashed, account['id']))
            self.db.execute('DELETE FROM sessions WHERE account=?', (account['id'],))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
        return self.create_session(account['id'])

    def application(self):
        @web.middleware
        async def safety(request, handler):
            try:
                response = await handler(request)
            except Problem as exc:
                response = web.json_response({'error': exc.message}, status=exc.status)
            except web.HTTPException as exc:
                response = web.json_response({'error': '请求无法处理，请刷新后重试。'}, status=exc.status)
            except Exception:
                # Never emit submitted links, credentials or DB exception details.
                response = web.json_response({'error': '服务暂不可用，请稍后重试。'}, status=503)
            response.headers.update({
                'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
                'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY',
                # Avatar previews use a browser-local object URL; blob: never leaves this page.
                'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; media-src 'self' blob:; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
            })
            # Only the specific admin document can be embedded by this same origin.
            # The API still authenticates and authorizes every enterprise request.
            if (request.path in ('/ai/admin', '/ai/admin/') and
                request.query.get('embedded') == 'enterprise' and
                request.query.get('enterprise') in ('legacy', 'ww0000000000000002', 'ww0000000000000003')):
                response.headers['X-Frame-Options'] = 'SAMEORIGIN'
                response.headers['Content-Security-Policy'] = response.headers['Content-Security-Policy'].replace("frame-ancestors 'none'", "frame-ancestors 'self'")
            return response

        # Registration may contain one cropped avatar (at most 512 KiB after
        # compression) encoded as a data URL.
        app = web.Application(client_max_size=900000, middlewares=[safety])
        async def index(request):
            return web.Response(text=(BASE/'site/index.html').read_text(encoding='utf-8'), content_type='text/html')
        async def account_page(request):
            return web.Response(text=(BASE/'account.html').read_text(encoding='utf-8'), content_type='text/html')
        async def site_asset(request):
            name = request.match_info['path']
            manifest = json.loads((BASE/'site/asset-manifest.json').read_text())
            if name not in manifest:
                raise web.HTTPNotFound()
            path = BASE/'site'/name
            types = {'.css': 'text/css', '.js': 'application/javascript', '.woff2': 'font/woff2', '.woff': 'font/woff', '.png': 'image/png', '.webp': 'image/webp', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.svg': 'image/svg+xml', '.txt': 'text/plain'}
            return web.Response(body=path.read_bytes(), content_type=types[path.suffix])
        async def register_page(request):
            return web.Response(text=(BASE/'register.html').read_text(encoding='utf-8'), content_type='text/html')
        async def demo_page(request):
            return web.Response(text=(BASE/'demo.html').read_text(encoding='utf-8'), content_type='text/html')
        async def help_page(request):
            return web.Response(text=(BASE/'site/help/index.html').read_text(encoding='utf-8'), content_type='text/html')
        async def asset(request):
            name = request.match_info['name']
            if name not in ('style.css', 'app.js', 'help.css', 'landing.css', 'register.css', 'register.js', 'demo.css', 'demo.js'):
                raise web.HTTPNotFound()
            return web.Response(body=(BASE/name).read_bytes(), content_type='text/css' if name.endswith('css') else 'application/javascript')
        async def health(request):
            self.db.execute('SELECT 1')
            return web.json_response({'ok': True})
        app.router.add_get('/ai/', index)
        app.router.add_get('/ai/account', account_page)
        app.router.add_get('/ai/account/', account_page)
        app.router.add_get('/ai/site/{path:.*}', site_asset)
        app.router.add_get('/ai/register', register_page)
        app.router.add_get('/ai/register/', register_page)
        app.router.add_get('/ai/demo', demo_page)
        app.router.add_get('/ai/demo/', demo_page)
        app.router.add_get('/ai/help', help_page)
        app.router.add_get('/ai/help/', help_page)
        app.router.add_get('/ai/{name:style.css|app.js|help.css|landing.css|register.css|register.js|demo.css|demo.js}', asset)
        # Keep the landing stylesheet explicit; the dotted alternation above
        # is intentionally retained for compatibility with the older assets.
        app.router.add_get('/ai/landing.css', lambda request: web.Response(body=(BASE/'landing.css').read_bytes(), content_type='text/css'))
        app.router.add_get('/health', health)
        app.router.add_get('/ai/api/me', self.me)
        app.router.add_get('/ai/api/onboarding/qr', self.onboarding_qr)
        for path, handler in [('register', self.register), ('register/request', self.registration_request), ('login', self.login), ('bind', self.bind), ('profile', self.update_profile),
                              ('waiting', self.waiting), ('open', self.open_settings), ('logout', self.logout),
                              ('password', self.change_password), ('recover', self.recover),
                              ('email/send', self.email_send), ('email/verify', self.email_verify),
                              ('recover/request', self.recover_request),
                              ('onboarding/confirm', self.confirm_provision), ('onboarding/start', self.provision),
                              ('onboarding/retry', self.retry_provision)]:
            app.router.add_post('/ai/api/'+path, handler)
        async def close(app):
            self.db.close()
        app.on_cleanup.append(close)
        from console_web import install_console
        install_console(self, app)
        from mini_gateway import install_mini
        install_mini(self, app)
        from operating_web import install
        install(self,app)
        return app


if __name__ == '__main__':
    os.umask(0o077)
    origin = os.environ.get('PORTAL_ORIGIN', 'https://www.example.com')
    portal = Portal(os.environ.get('PORTAL_DATABASE', '/data/portal.sqlite3'),
                    UnixBridge(os.environ.get('PORTAL_BRIDGE', '/bridge/bridge.sock')), origin,
                    secure=origin.startswith('https://'),
                    onboarding=UnixBridge(os.environ.get('PORTAL_ONBOARDING', '/bridge/onboarding.sock'), timeout=28),
                    email_sender=EmailSender(os.environ.get('EMAIL_CONFIG', '/data/email.json')))
    web.run_app(portal.application(), host=os.environ.get('PORTAL_HOST', '0.0.0.0'),
                port=int(os.environ.get('PORTAL_PORT', '8091')), access_log=None, print=None)
