"""Mini-only identity proofs. Existing website bridge permissions are unchanged."""
from contextlib import closing
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

LEGACY_CORP = 'ww0000000000000001'
LEGACY_DATABASE = Path('/var/lib/docker/volumes/wecom-kf_kf-data/_data/kf.sqlite3')


def legacy_rows(bridge):
    Denied = sys.modules[type(bridge).__module__].Denied
    path = bridge.root / '.env'
    if path.is_symlink() or (os.name != 'nt' and path.stat().st_mode & 0o077):
        raise Denied('application_unavailable')
    values = dict(line.split('=', 1) for line in path.read_text().splitlines()
                  if line and not line.startswith('#') and '=' in line)
    values = {k: v.strip().strip('\"\'') for k, v in values.items()}
    if (values.get('WECOM_CORP_ID') != LEGACY_CORP or values.get('WECOM_AGENT_ID') != '1000003'
            or values.get('ENABLE_AI', '').lower() not in ('1', 'true', 'yes', 'on')):
        raise Denied('application_unavailable')
    owners = [v.strip() for v in values.get('XIAOYI_APP_ADMINS', '').split(',') if v.strip()]
    return [{'agent': 1000003, 'corp': LEGACY_CORP, 'owner': v, 'name': '大肥鱼', 'state': 'assigned'}
            for v in owners if re.fullmatch(r'[A-Za-z0-9_@.\-]{1,128}', v)]


def subject(bridge, row):
    value = json.dumps(['mini-legacy-admin-v1', row['corp'], row['agent'], row['owner']], separators=(',', ':'))
    return hmac.new(bridge.key, value.encode(), hashlib.sha256).hexdigest()


def dispatch(bridge, data):
    Denied = sys.modules[type(bridge).__module__].Denied
    from enterprise_router import bridge_route
    action = data.get('action')
    if action in ('mini-phone','mini-phone-progress'):
        from mini_phone_bridge import resolve
        return resolve(bridge,data)
    if action not in ('mini-verify', 'mini-status', 'mini-mint'):
        raise Denied('invalid_request')
    admin = enterprise_admin_dispatch(bridge, data)
    if admin is not None:
        return admin
    row = None
    if action == 'mini-verify' and set(data) == {'action', 'link'}:
        value = data['link']
        if not isinstance(value, str) or len(value) > 2048:
            raise Denied('invalid_link')
        u = urlsplit(value.strip())
        if u.path in ('/xiaoyi', '/xiaoyi/'):
            if u.scheme != 'https' or u.netloc != urlsplit(bridge.origin).netloc or u.query:
                raise Denied('invalid_link')
            try:
                params = parse_qs(u.fragment, strict_parsing=True)
                raw = params['token'][0]
                if set(params) != {'token'} or len(params['token']) != 1 or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', raw):
                    raise ValueError()
            except (KeyError, ValueError):
                raise Denied('invalid_link') from None
            database = LEGACY_DATABASE
            if database.is_symlink() or not database.is_file():
                raise Denied('application_unavailable')
            with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=3)) as db:
                token = db.execute('SELECT agent,user FROM panel_tokens WHERE token_hash=? AND expires>?',
                                   (hashlib.sha256(raw.encode()).hexdigest(), bridge.clock())).fetchone()
            row = next((r for r in legacy_rows(bridge) if token == (1000003, r['owner'])), None)
            if row is None:
                raise Denied('expired_link')
    elif action in ('mini-status', 'mini-mint') and set(data) == {'action', 'subject'}:
        value = data['subject']
        if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
            raise Denied('invalid_identity')
        row = next((r for r in legacy_rows(bridge) if hmac.compare_digest(subject(bridge, r), value)), None)
    else:
        raise Denied('invalid_request')
    if row:
        result = dict(subject=subject(bridge, row), agent=1000003, name=row['name'], state='assigned')
        if action == 'mini-mint':
            raw, now = secrets.token_urlsafe(32), bridge.clock()
            if LEGACY_DATABASE.is_symlink() or not LEGACY_DATABASE.is_file():
                raise Denied('application_unavailable')
            with closing(sqlite3.connect(LEGACY_DATABASE, timeout=3)) as db, db:
                db.execute('DELETE FROM panel_tokens WHERE expires<?', (now,))
                db.execute('INSERT INTO panel_tokens(token_hash,agent,user,created,expires) VALUES(?,?,?,?,?)',
                           (hashlib.sha256(raw.encode()).hexdigest(), 1000003, row['owner'], now, now + 30*86400))
            result['url'] = bridge.origin + '/xiaoyi/#token=' + raw
        return result
    # Assigned applications retain all original owner, enterprise and state checks.
    normal = dict(data, action=action[5:])
    routed = bridge_route(bridge.root, normal, bridge)
    if routed is not None:
        if not routed['ok']:
            raise Denied(routed.get('error', 'service_unavailable'))
        return routed['data']
    return bridge.call(normal)


"""Fixed second-enterprise administrator proof; mini actions only."""
ENTERPRISE_ADMIN_CORP = 'ww0000000000000002'
ENTERPRISE_ADMIN_PATH = '/ai/enterprises/' + ENTERPRISE_ADMIN_CORP + '/1000002/xiaoyi/'

def enterprise_admin_rows(bridge):
    Denied = sys.modules[type(bridge).__module__].Denied
    root = bridge.root / 'enterprises' / ENTERPRISE_ADMIN_CORP / '1000002'
    for directory in [root.parent.parent, root.parent, root]:
        if directory.is_symlink():
            raise Denied('application_unavailable')
    path = root / 'app.env'
    if not path.is_file():
        return []
    if path.is_symlink() or (os.name != 'nt' and path.stat().st_mode & 0o077):
        raise Denied('application_unavailable')
    values = dict(line.split('=', 1) for line in path.read_text().splitlines()
                  if line and not line.startswith('#') and '=' in line)
    values = {key: value.strip().strip('\"\'') for key, value in values.items()}
    if (values.get('WECOM_CORP_ID') != ENTERPRISE_ADMIN_CORP
            or values.get('WECOM_AGENT_ID') != '1000002'
            or values.get('ENABLE_AI', '').lower() not in ('1', 'true', 'yes', 'on')):
        raise Denied('application_unavailable')
    owners = [value.strip() for value in values.get('XIAOYI_APP_ADMINS', '').split(',') if value.strip()]
    return [{'agent': 1000002, 'corp': ENTERPRISE_ADMIN_CORP, 'owner': owner,
             'name': '大肥鱼', 'state': 'assigned'} for owner in owners
            if re.fullmatch(r'[A-Za-z0-9_@.\-]{1,128}', owner)]

def enterprise_admin_subject(bridge, row):
    value = json.dumps(['mini-enterprise-admin-v1', row['corp'], row['agent'], row['owner']], separators=(',', ':'))
    return hmac.new(bridge.key, value.encode(), hashlib.sha256).hexdigest()

def enterprise_admin_dispatch(bridge, data):
    Denied = sys.modules[type(bridge).__module__].Denied
    action = data.get('action')
    row = None
    raw = None
    if action == 'mini-verify' and set(data) == {'action', 'link'}:
        link = data['link']
        if not isinstance(link, str) or len(link) > 2048:
            return None
        parsed = urlsplit(link.strip())
        if parsed.path not in (ENTERPRISE_ADMIN_PATH, ENTERPRISE_ADMIN_PATH.rstrip('/')):
            return None
        if parsed.scheme != 'https' or parsed.netloc != urlsplit(bridge.origin).netloc or parsed.query:
            raise Denied('invalid_link')
        try:
            params = parse_qs(parsed.fragment, strict_parsing=True)
            raw = params['token'][0]
            if set(params) != {'token'} or len(params['token']) != 1 or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', raw):
                raise ValueError()
        except (KeyError, ValueError):
            raise Denied('invalid_link') from None
        rows = enterprise_admin_rows(bridge)
    elif action in ('mini-status', 'mini-mint') and set(data) == {'action', 'subject'}:
        value = data['subject']
        if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
            return None
        try:
            rows = enterprise_admin_rows(bridge)
        except Denied:
            return None
        row = next((item for item in rows if hmac.compare_digest(enterprise_admin_subject(bridge, item), value)), None)
        if row is None:
            return None
    else:
        return None
    directory = bridge.root / 'enterprises' / ENTERPRISE_ADMIN_CORP / '1000002' / 'data'
    database = directory / 'kf.sqlite3'
    if directory.is_symlink() or database.is_symlink() or not database.is_file():
        raise Denied('application_unavailable')
    if raw is not None:
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=3)) as db:
            token = db.execute('SELECT agent,user FROM panel_tokens WHERE token_hash=? AND expires>?',
                               (hashlib.sha256(raw.encode()).hexdigest(), bridge.clock())).fetchone()
        row = next((item for item in rows if token == (1000002, item['owner'])), None)
        if row is None:
            raise Denied('expired_link')
    result = {'subject': enterprise_admin_subject(bridge, row), 'agent': 1000002,
              'name': row['name'], 'state': 'assigned'}
    if action == 'mini-mint':
        raw, now = secrets.token_urlsafe(32), bridge.clock()
        with closing(sqlite3.connect(database, timeout=3)) as db, db:
            db.execute('INSERT INTO panel_tokens(token_hash,agent,user,created,expires) VALUES(?,?,?,?,?)',
                       (hashlib.sha256(raw.encode()).hexdigest(), 1000002, row['owner'], now, now + 30*86400))
        result['url'] = bridge.origin + ENTERPRISE_ADMIN_PATH + '#token=' + raw
    return result
