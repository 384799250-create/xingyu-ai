from contextlib import closing
"""Narrow local bridge: no HTTP, Docker, model access, or arbitrary file operations."""
import argparse
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
import time
from urllib.parse import urlsplit, parse_qs


class Denied(Exception):
    pass


class Bridge:
    def __init__(self, root, key, origin='https://www.example.com', clock=time.time):
        self.root = Path(root)
        self.key = key
        self.origin = origin.rstrip('/')
        self.clock = clock

    def subject(self, row):
        value = json.dumps([row['corp'], row['owner']], separators=(',', ':')).encode()
        return hmac.new(self.key, value, hashlib.sha256).hexdigest()

    def rows(self):
        with closing(sqlite3.connect((self.root/'app-pool/registry.sqlite3').as_uri()+'?mode=ro', uri=True, timeout=3)) as db, db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute('SELECT agent,corp,owner,name,state FROM apps WHERE owner IS NOT NULL')]

    def app(self, row):
        if row['state'] != 'assigned':
            raise Denied('application_unavailable')
        agent = row['agent']
        if not isinstance(agent, int) or agent <= 1000002:
            raise Denied('application_unavailable')
        root = self.root/'apps'/str(agent)
        env = root/'app.env'
        if root.is_symlink() or env.is_symlink() or (os.name != 'nt' and env.stat().st_mode & 0o077):
            raise Denied('application_unavailable')
        values = dict(line.split('=', 1) for line in env.read_text().splitlines() if line and not line.startswith('#'))
        owners = [s.strip() for s in values.get('XIAOYI_APP_ADMINS', '').split(',') if s.strip()]
        if (values.get('WECOM_AGENT_ID') != str(agent) or values.get('WECOM_CORP_ID') != row['corp']
                or owners != [row['owner']] or values.get('ENABLE_AI', '').lower() not in ('1', 'true', 'yes', 'on')):
            raise Denied('application_unavailable')
        database = root/'data/kf.sqlite3'
        if database.is_symlink() or not database.is_file():
            raise Denied('application_unavailable')
        return database

    def payload(self, row):
        return {'subject': self.subject(row), 'agent': row['agent'], 'name': row['name'], 'state': row['state']}

    def parse_link(self, value):
        if not isinstance(value, str) or len(value) > 2048:
            raise Denied('invalid_link')
        u = urlsplit(value.strip())
        if u.scheme != 'https' or u.netloc != urlsplit(self.origin).netloc or u.query:
            raise Denied('invalid_link')
        match = re.fullmatch(r'/ai/enterprises/ww0000000000000002/([1-9][0-9]{6,9})/xiaoyi/?', u.path)
        params = parse_qs(u.fragment, strict_parsing=True)
        raw = params.get('token', [''])[0]
        if not match or set(params) != {'token'} or len(params['token']) != 1 or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', raw):
            raise Denied('invalid_link')
        return int(match[1]), hashlib.sha256(raw.encode()).hexdigest()

    def call(self, data):
        if not isinstance(data, dict):
            raise Denied('invalid_request')
        action = data.get('action')
        rows = self.rows()
        if action == 'verify' and set(data) == {'action', 'link'}:
            try:
                agent, digest = self.parse_link(data['link'])
            except (ValueError, KeyError):
                raise Denied('invalid_link') from None
            row = next((r for r in rows if r['agent'] == agent), None)
            if not row:
                raise Denied('invalid_link')
            database = self.app(row)
            with closing(sqlite3.connect(database.as_uri()+'?mode=ro', uri=True, timeout=3)) as db, db:
                token = db.execute('SELECT agent,user FROM panel_tokens WHERE token_hash=? AND expires>?', (digest, self.clock())).fetchone()
            if not token or token != (row['agent'], row['owner']):
                raise Denied('expired_link')
            return self.payload(row)
        if action in ('status', 'mint') and set(data) == {'action', 'subject'}:
            subject = data['subject']
            if not isinstance(subject, str) or not re.fullmatch('[a-f0-9]{64}', subject):
                raise Denied('invalid_identity')
            row = next((r for r in rows if hmac.compare_digest(self.subject(r), subject)), None)
            if not row:
                raise Denied('application_unavailable')
            database = self.app(row)
            result = self.payload(row)
            if action == 'mint':
                raw = secrets.token_urlsafe(32)
                now = self.clock()
                with closing(sqlite3.connect(database, timeout=3)) as db, db:
                    db.execute('DELETE FROM panel_tokens WHERE expires<?', (now,))
                    db.execute('INSERT INTO panel_tokens(token_hash,agent,user,created,expires) VALUES(?,?,?,?,?)',
                               (hashlib.sha256(raw.encode()).hexdigest(), row['agent'], row['owner'], now, now+30*86400))
                result['url'] = f"{self.origin}/ai/enterprises/ww0000000000000002/{row['agent']}/xiaoyi/#token={raw}"
            return result
        raise Denied('invalid_request')


def serve(args):
    keypath = Path(args.key)
    if not keypath.exists():
        fd = os.open(keypath, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as out:
            out.write(secrets.token_bytes(32))
    if keypath.is_symlink() or keypath.stat().st_mode & 0o077:
        raise RuntimeError('Unsafe key file permissions')
    bridge = Bridge(args.root, keypath.read_bytes())

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.request.settimeout(5)
            _, uid, _ = struct.unpack('3i', self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid not in (0, args.uid):
                return
            try:
                raw = self.rfile.readline(8193)
                if len(raw) > 8192 or not raw.endswith(b'\n'):
                    raise Denied('invalid_request')
                result = {'ok': True, 'data': bridge.call(json.loads(raw))}
            except Denied as exc:
                result = {'ok': False, 'error': str(exc)}
            except Exception:
                result = {'ok': False, 'error': 'service_unavailable'}
            try:
                self.wfile.write(json.dumps(result, ensure_ascii=False).encode()+b'\n')
            except OSError:
                pass

    class Server(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True
        request_queue_size = 16

    path = Path(args.socket)
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    os.chown(path.parent, 0, args.uid)
    os.chmod(path.parent, 0o750)
    if path.exists():
        if not path.is_socket():
            raise RuntimeError('Socket path is not a socket')
        path.unlink()
    with Server(str(path), Handler) as server:
        os.chown(path, 0, args.uid)
        os.chmod(path, 0o660)
        server.serve_forever()


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--root', default='/home/ubuntu/wecom-kf/enterprises/ww0000000000000002')
    p.add_argument('--socket', default='/run/xiaoyi-portal/bridge.sock')
    p.add_argument('--key', default='/home/ubuntu/wecom-kf/enterprises/ww0000000000000002/app-pool/portal-identity.key')
    p.add_argument('--uid', type=int, default=10001)
    serve(p.parse_args())
