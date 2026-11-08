"""Private onboarding worker. Web ownership requires the assigned AI's settings link."""
import argparse
import base64
from contextlib import closing
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import socket
import socketserver
import sqlite3
import struct
import sys
import threading
import time
from directory_admin import Directory, DirectoryError


class OnboardingError(Exception):
    pass


class Onboarding:
    def __init__(self, root, key, directory=None, pool=None, clock=time.time):
        self.root, self.key, self.clock = Path(root), key, clock
        self.home = self.root/'onboarding'
        self.home.mkdir(mode=0o700, exist_ok=True)
        self.db = sqlite3.connect(str(self.home/'jobs.sqlite3'), isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS jobs (
            account TEXT PRIMARY KEY, request TEXT UNIQUE NOT NULL, phone_hash TEXT UNIQUE NOT NULL,
            phone TEXT, owner TEXT UNIQUE NOT NULL, agent INTEGER,
            display_name TEXT, avatar BLOB, avatar_type TEXT,
            state TEXT NOT NULL, stage TEXT NOT NULL, error TEXT, created REAL NOT NULL, updated REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS worker_state (id INTEGER PRIMARY KEY CHECK(id=1), heartbeat REAL NOT NULL);
        ''')
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(jobs)')}
        for name, definition in (('display_name', 'TEXT'), ('avatar', 'BLOB'), ('avatar_type', 'TEXT')):
            if name not in columns:
                self.db.execute(f'ALTER TABLE jobs ADD COLUMN {name} {definition}')
        self.directory = directory
        self.pool = pool

    def close(self):
        self.db.close()

    def mac(self, *values):
        return hmac.new(self.key, json.dumps(values, separators=(',', ':')).encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def account(value):
        if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,15}', value):
            raise OnboardingError('invalid_account')
        return value

    def pool_module(self):
        if self.pool is None:
            sys.path.insert(0, str(self.root/'app-pool'))
            import app_pool
            self.pool = app_pool
        return self.pool

    def row(self, account):
        return self.db.execute('SELECT * FROM jobs WHERE account=?', (account,)).fetchone()

    def free_count(self):
        path = self.root/'app-pool/registry.sqlite3'
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=3)) as db:
            return db.execute("SELECT COUNT(*) FROM apps WHERE state='free'").fetchone()[0]

    # onboarding-qr-format-v1: sniff bytes, never trust the legacy .png filename.
    @staticmethod
    def qr_image_header(path):
        with path.open('rb') as image:
            return image.read(8)

    @staticmethod
    def qr_image_type(body):
        if body.startswith(b'\x89PNG\r\n\x1a\n'):
            return 'image/png'
        if body.startswith(b'\xff\xd8\xff'):
            return 'image/jpeg'
        return None

    def qr_ready(self):
        try:
            metadata = json.loads((self.home/'wechat-plugin.json').read_text())
            image = self.home/'wechat-plugin.png'
            return (metadata['kind'] == 'wechat_plugin' and metadata['expires_at'] > self.clock()+60
                    and image.is_file() and not image.is_symlink() and 100 < image.stat().st_size < 200000
                    and self.qr_image_type(self.qr_image_header(image)) is not None)
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def status(self, account):
        row = self.row(account)
        heartbeat = self.db.execute('SELECT heartbeat FROM worker_state WHERE id=1').fetchone()
        result = {'verification_method': 'settings_link', 'free': self.free_count(), 'qr_ready': self.qr_ready(),
                  'worker_ready': bool(heartbeat and heartbeat[0] > self.clock()-20)}
        if not row:
            return dict(result, state='needs_phone')
        result.update(state=row['state'], stage=row['stage'], error=row['error'])
        if row['agent'] and row['state'] in ('awaiting_scan', 'ready'):
            pool = self.pool_module()
            registry = pool.Registry(self.root/'app-pool/registry.sqlite3')
            try:
                app = registry.get(row['agent'])
                if app['state'] != 'assigned' or app['owner'] != row['owner']:
                    raise OnboardingError('application_unavailable')
                result.update(application={'agent':app['agent'], 'name':app['name']})
                if row['state'] == 'ready':
                    result['subject'] = self.subject(app)
            finally:
                registry.db.close()
        return result

    def subject(self, row):
        value = json.dumps([row['corp'], row['owner']], separators=(',', ':')).encode()
        return hmac.new(self.key, value, hashlib.sha256).hexdigest()

    def submit(self, account, phone, display_name=None, avatar=None, avatar_type=None):
        account = self.account(account)
        if not isinstance(phone, str):
            raise OnboardingError('invalid_phone')
        phone = phone.strip()
        if re.fullmatch(r'1[3-9][0-9]{9}', phone):
            phone = '+86'+phone
        if not re.fullmatch(r'\+861[3-9][0-9]{9}', phone):
            raise OnboardingError('invalid_phone')
        if display_name is None:
            display_name = '星屿用户'
        if not isinstance(display_name, str):
            raise OnboardingError('invalid_profile')
        display_name = display_name.strip()
        if not 1 <= len(display_name) <= 32 or any(ord(char) < 32 or ord(char) == 127 for char in display_name):
            raise OnboardingError('invalid_profile')
        avatar_bytes = None
        if avatar is not None:
            if not isinstance(avatar, str) or avatar_type not in ('image/jpeg', 'image/png'):
                raise OnboardingError('invalid_profile')
            try:
                avatar_bytes = base64.b64decode(avatar, validate=True)
            except Exception:
                raise OnboardingError('invalid_profile') from None
            if len(avatar_bytes) > 512 * 1024:
                raise OnboardingError('invalid_profile')
            valid = ((avatar_type == 'image/png' and avatar_bytes.startswith(b'\x89PNG\r\n\x1a\n')) or
                     (avatar_type == 'image/jpeg' and avatar_bytes.startswith(b'\xff\xd8\xff')))
            if not valid:
                raise OnboardingError('invalid_profile')
        existing = self.row(account)
        if existing:
            if existing['phone_hash'] != self.mac('phone', phone):
                raise OnboardingError('request_identity_mismatch')
            return self.status(account)
        if not self.qr_ready():
            raise OnboardingError('qr_unavailable')
        # Capacity exhaustion keeps the original consent and request for later.
        waiting = self.free_count() < 1
        now = self.clock()
        try:
            self.db.execute('BEGIN IMMEDIATE')
            if self.db.execute('SELECT COUNT(*) FROM jobs WHERE created>?', (now-86400,)).fetchone()[0] >= 20:
                raise OnboardingError('onboarding_daily_limit')
            self.db.execute('INSERT INTO jobs(account,request,phone_hash,phone,owner,agent,display_name,avatar,avatar_type,state,stage,error,created,updated) '
                            'VALUES(?,?,?,?,?,NULL,?,?,?, ?,?,?,?,?)',
                (account, 'signup_'+secrets.token_hex(16), self.mac('phone', phone), phone,
                 'xy_'+self.mac('owner', account)[:24], display_name, avatar_bytes, avatar_type,
                 'waiting_pool' if waiting else 'queued', 'waiting_pool' if waiting else 'queued',
                 'pool_empty' if waiting else None, now, now))
            self.db.execute('COMMIT')
        except sqlite3.IntegrityError:
            self.db.execute('ROLLBACK')
            if self.row(account):
                return self.status(account)
            raise OnboardingError('phone_already_requested') from None
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise
        return self.status(account)

    def confirm(self, account, subject):
        # Only the trusted portal supplies a subject, after the identity bridge has
        # verified the private settings link obtained from this member's WeChat AI.
        # A submitted phone number, shared QR, or arbitrary first chat is not proof.
        status = self.status(account)
        if status['state'] not in ('awaiting_scan', 'ready'):
            raise OnboardingError('confirmation_not_ready')
        if not isinstance(subject, str) or not re.fullmatch(r'[a-f0-9]{64}', subject):
            raise OnboardingError('confirmation_mismatch')
        row = self.row(account)
        pool = self.pool_module()
        registry = pool.Registry(self.root/'app-pool/registry.sqlite3')
        try:
            app = registry.get(row['agent'])
            if app['state'] != 'assigned' or app['owner'] != row['owner']:
                raise OnboardingError('application_unavailable')
            if not hmac.compare_digest(self.subject(app), subject):
                raise OnboardingError('confirmation_mismatch')
            self.mark(account, 'ready', 'confirmed')
        finally:
            registry.db.close()
        return self.status(account)

    def mark(self, account, state, stage, error=None, agent=None):
        self.db.execute('UPDATE jobs SET state=?,stage=?,error=?,agent=COALESCE(?,agent),updated=? WHERE account=?',
                        (state, stage, error, agent, self.clock(), account))

    def work_once(self):
        pool = self.pool_module()
        import fcntl
        with (self.root/'app-pool/operation.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            # Only resume capacity waits that have never reserved an application or
            # attempted a member write. Keep inspection/interrupted jobs quarantined.
            job = self.db.execute("SELECT * FROM jobs WHERE state='queued' OR "
                "(state='waiting_pool' AND stage='waiting_pool' AND error='pool_empty' "
                "AND agent IS NULL AND phone IS NOT NULL) ORDER BY created,account LIMIT 1").fetchone()
            if not job:
                return False
            if job['state'] == 'waiting_pool' and (self.free_count() < 1 or not self.qr_ready()):
                return False
            try:
                self.account_available(job['account'])
            except OnboardingError:
                self.mark(job['account'], 'needs_inspection', 'stopped', 'account_unavailable')
                return True
            self.mark(job['account'], 'provisioning', 'reserving')
            registry = pool.Registry(self.root/'app-pool/registry.sqlite3')
            agent, started = None, False
            try:
                eligible = []
                rows = registry.status()
                for row in pool.preferred_idle_rows(rows):
                    if row['state'] != 'free':
                        continue
                    candidate = pool.PreparedApp(row['agent'])
                    if not candidate.values.get('POOL_SLOT_DEPARTMENT_ID'):
                        continue
                    candidate.prepare_idle()
                    candidate.verify(False, pristine=True)
                    eligible.append(row['agent'])
                    break  # Keep every other unused application dormant.
                if not eligible:
                    self.mark(job['account'], 'waiting_pool', 'waiting_pool', 'pool_empty')
                    return True
                row = registry.reserve(rows[0]['corp'], job['owner'], job['request'], eligible)
                agent = row['agent']
                registry.begin(job['request']); started = True
                self.mark(job['account'], 'provisioning', 'creating_member', agent=agent)
                app = pool.PreparedApp(agent)
                department = int(app.values['POOL_SLOT_DEPARTMENT_ID'])
                directory = self.directory or Directory(self.home)
                # No invitation. The supplied phone selects the member; web ownership
                # is confirmed later with the member's private WeChat settings link.
                directory.request('user/create', {'access_token':directory.token()},
                    {'userid':job['owner'], 'name':'星屿用户', 'mobile':job['phone'].removeprefix('+86'),
                     'department':[department], 'enable':1, 'to_invite':False})
                if directory.members_in(department) != {job['owner']}:
                    raise OnboardingError('slot_department_members_mismatch')
                self.mark(job['account'], 'provisioning', 'updating_profile', agent=agent)
                app.update_profile(job['display_name'], job['avatar'], job['avatar_type'])
                self.mark(job['account'], 'provisioning', 'activating', agent=agent)
                app.bind_owner(job['owner']); app.activate()
                registry.complete(agent, True)
                self.mark(job['account'], 'awaiting_scan', 'scan_qr', agent=agent)
                self.db.execute('UPDATE jobs SET phone=NULL WHERE account=?', (job['account'],))
            except Exception as exc:
                # Never repeat an uncertain member creation or recycle its application automatically.
                safe = str(exc) if isinstance(exc, (pool.PoolError, DirectoryError, OnboardingError)) else 'operator_inspection_required'
                if not re.fullmatch(r'[A-Za-z0-9_]{1,100}', safe):
                    safe = 'operator_inspection_required'
                if started:
                    try:
                        pool.stop_application(agent)
                    finally:
                        if registry.get(agent)['state'] == 'provisioning':
                            registry.complete(agent, False)
                self.mark(job['account'], 'needs_inspection', 'stopped', safe, agent)
            finally:
                registry.db.close()
                # Replenishment is independent of the completed member write.
                # The maintenance service waits for this operation lock to release.
                try:
                    import subprocess
                    subprocess.run(['systemctl', 'start', '--no-block', 'xingyu-pool-standby.service'],
                                   capture_output=True, timeout=3, check=False)
                except Exception:
                    pass  # The periodic standby timer is the fallback.
            return True

    def recover(self):
        pool = self.pool_module()
        registry = pool.Registry(self.root/'app-pool/registry.sqlite3')
        try:
            for job in self.db.execute("SELECT * FROM jobs WHERE state IN ('provisioning','verifying')").fetchall():
                app = registry.get(job['agent']) if job['agent'] else None
                if app and app['state'] == 'assigned' and app['owner'] == job['owner']:
                    self.mark(job['account'], 'awaiting_scan', 'scan_qr')
                    self.db.execute('UPDATE jobs SET phone=NULL WHERE account=?', (job['account'],))
                else:
                    self.mark(job['account'], 'needs_inspection', 'interrupted', 'interrupted_no_automatic_retry')
        finally:
            registry.db.close()

    def account_available(self, account):
        # The account closure fence is shared with the website. Mutation checks
        # run under the same operation lock as account deletion.
        path=Path('/home/ubuntu/wecom-kf/portal/data/portal.sqlite3')
        if not path.exists():raise OnboardingError('account_unavailable')
        with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as db:
            if not db.execute('SELECT 1 FROM accounts WHERE id=?',(account,)).fetchone():
                raise OnboardingError('account_unavailable')
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='account_closures'").fetchone() and db.execute('SELECT 1 FROM account_closures WHERE account=?',(account,)).fetchone():
                raise OnboardingError('account_unavailable')

    def call(self, data):
        if isinstance(data,dict) and data.get('action') in ('submit','confirm','retry_pool'):
            import fcntl
            with (self.root/'app-pool/operation.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                self.account_available(self.account(data.get('account')))
                return self._call(data)
        return self._call(data)

    def _call(self, data):
        if not isinstance(data, dict):
            raise OnboardingError('invalid_request')
        account = self.account(data.get('account'))
        action = data.get('action')
        if action == 'status' and set(data) == {'action', 'account'}:
            return self.status(account)
        if action == 'submit' and {'action','account','phone'}.issubset(data) and set(data) <= {
                'action','account','phone','display_name','avatar','avatar_type'}:
            return self.submit(account, data['phone'], data.get('display_name'), data.get('avatar'), data.get('avatar_type'))
        if action == 'confirm' and set(data) == {'action','account','subject'}:
            return self.confirm(account, data['subject'])
        if action == 'qr' and set(data) == {'action','account'}:
            status = self.status(account)
            if status['state'] not in ('awaiting_scan', 'ready') or not status['qr_ready']:
                raise OnboardingError('qr_unavailable')
            body = (self.home/'wechat-plugin.png').read_bytes()
            content_type = self.qr_image_type(body)
            if not 100 < len(body) < 200000 or content_type is None:
                raise OnboardingError('qr_unavailable')
            return {'image':base64.b64encode(body).decode(), 'content_type':content_type}
        if action == 'retry_pool' and set(data) == {'action','account'}:
            # Idempotent manual fallback; status reads never trigger allocation.
            if self.free_count() > 0 and self.qr_ready():
                self.db.execute("UPDATE jobs SET state='queued',stage='queued',error=NULL WHERE account=? "
                    "AND state='waiting_pool' AND stage='waiting_pool' AND error='pool_empty' "
                    "AND agent IS NULL AND phone IS NOT NULL", (account,))
            return self.status(account)
        raise OnboardingError('invalid_request')


def serve(args):
    root = Path(args.root)
    key = (root/'app-pool/portal-identity.key').read_bytes()
    stop = threading.Event()

    def worker():
        service = Onboarding(root, key)
        try:
            service.recover()
            while not stop.is_set():
                try:
                    service.db.execute('INSERT INTO worker_state VALUES(1,?) ON CONFLICT(id) DO UPDATE SET heartbeat=excluded.heartbeat', (time.time(),))
                    service.work_once()
                except Exception:
                    service.recover()  # Quarantine interrupted work; never replay a member write.
                stop.wait(2)
        finally:
            service.close()

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.request.settimeout(25)
            _, uid, _ = struct.unpack('3i', self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid not in (0, args.uid):
                return
            service = None
            try:
                raw = self.rfile.readline(900000)
                if len(raw)>899999 or not raw.endswith(b'\n'):
                    raise OnboardingError('invalid_request')
                service = Onboarding(root, key)
                parsed=json.loads(raw)
                if isinstance(parsed,dict) and parsed.get('action') in ('mini-phone-lookup','mini-phone-progress'):
                    from mini_phone import call as phone_call
                    result={'ok':True,'data':phone_call(service,parsed)}
                else:
                    result = {'ok':True,'data':service.call(parsed)}
            except OnboardingError as exc:
                result = {'ok':False,'error':str(exc)}
            except Exception:
                result = {'ok':False,'error':'service_unavailable'}
            finally:
                if service: service.close()
            try:
                self.wfile.write(json.dumps(result, ensure_ascii=False).encode()+b'\n')
            except OSError:
                pass

    class Server(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True
        request_queue_size = 16

    path = Path(args.socket)
    path.parent.mkdir(mode=0o750,parents=True,exist_ok=True)
    os.chown(path.parent,0,args.uid);os.chmod(path.parent,0o750)
    if path.exists():
        if not path.is_socket(): raise RuntimeError('Unexpected socket path')
        path.unlink()
    with Server(str(path),Handler) as server:
        os.chown(path,0,args.uid);os.chmod(path,0o660)
        thread = threading.Thread(target=worker,daemon=True);thread.start()
        try: server.serve_forever()
        finally: stop.set();thread.join(timeout=30)


if __name__ == '__main__':
    os.umask(0o077)
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',default='/home/ubuntu/wecom-kf/enterprises/ww0000000000000002')
    parser.add_argument('--socket',default='/run/xiaoyi-portal/onboarding.sock')
    parser.add_argument('--uid',type=int,default=10001)
    serve(parser.parse_args())
