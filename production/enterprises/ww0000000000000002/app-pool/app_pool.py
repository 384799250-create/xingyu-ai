"""Root-only control plane for prepared, isolated WeCom applications. No public API."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request


class PoolError(Exception):
    pass


def identity(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_@.\-]{1,128}', value):
        raise PoolError('invalid_identity')
    return value


class Registry:
    """Short SQLite transactions; uniqueness is enforced in the database."""
    def __init__(self, path, clock=time.time):
        self.clock = clock
        self.db = sqlite3.connect(str(path), timeout=20, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS apps (
            agent INTEGER PRIMARY KEY CHECK(agent>1000002), corp TEXT NOT NULL,
            name TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN
              ('free','reserved','provisioning','assigned','disabled','failed')),
            owner TEXT, request_id TEXT UNIQUE, lease REAL, verified REAL NOT NULL);
          CREATE UNIQUE INDEX IF NOT EXISTS one_app_per_owner ON apps(corp,owner)
            WHERE owner IS NOT NULL;
          CREATE TABLE IF NOT EXISTS requests (
            id TEXT PRIMARY KEY, corp TEXT NOT NULL, owner TEXT NOT NULL,
            agent INTEGER NOT NULL REFERENCES apps(agent), state TEXT NOT NULL,
            created REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY, at REAL NOT NULL, agent INTEGER NOT NULL,
            action TEXT NOT NULL, request_id TEXT);
        ''')

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def event(self, agent, action, request=None):
        self.db.execute('INSERT INTO events(at,agent,action,request_id) VALUES(?,?,?,?)',
                        (self.clock(), agent, action, request))

    def get(self, agent):
        row = self.db.execute('SELECT * FROM apps WHERE agent=?', (agent,)).fetchone()
        if row is None:
            raise PoolError('unknown_application')
        return dict(row)

    def register(self, proof, assigned=False):
        owner = identity(proof['owner']) if assigned else None
        state = 'assigned' if assigned else 'free'
        with self.transaction():
            corp = self.db.execute('SELECT corp FROM apps LIMIT 1').fetchone()
            if corp and corp['corp'] != proof['corp']:
                raise PoolError('corporation_mismatch')
            prior = self.db.execute('SELECT * FROM apps WHERE agent=?', (proof['agent'],)).fetchone()
            if prior:
                if (prior['corp'], prior['state'], prior['owner']) != (proof['corp'], state, owner):
                    raise PoolError('already_registered_do_not_overwrite')
                return dict(prior)
            try:
                self.db.execute('INSERT INTO apps VALUES(?,?,?,?,?,NULL,NULL,?)',
                    (proof['agent'], proof['corp'], proof['name'], state, owner, self.clock()))
            except sqlite3.IntegrityError:
                raise PoolError('owner_already_has_application') from None
            self.event(proof['agent'], 'import_assigned' if assigned else 'register_free')
        return self.get(proof['agent'])

    def expire(self):
        # Only unused reservations expire. Provisioning/failed applications are never recycled.
        rows = self.db.execute("SELECT agent,request_id FROM apps WHERE state='reserved' AND lease<=?",
                               (self.clock(),)).fetchall()
        for row in rows:
            self.db.execute("UPDATE requests SET state='expired' WHERE id=?", (row['request_id'],))
            self.db.execute("UPDATE apps SET state='free',owner=NULL,request_id=NULL,lease=NULL WHERE agent=?", (row['agent'],))
            self.event(row['agent'], 'reservation_expired', row['request_id'])

    def reserve(self, corp, owner, request, eligible):
        identity(owner)
        identity(request)
        with self.transaction():
            self.expire()
            previous = self.db.execute('SELECT * FROM requests WHERE id=?', (request,)).fetchone()
            if previous:
                if (previous['corp'], previous['owner']) != (corp, owner):
                    raise PoolError('request_identity_mismatch')
                if previous['state'] not in ('reserved', 'provisioning', 'assigned'):
                    raise PoolError('request_not_reusable')
                return self.get(previous['agent'])
            existing = self.db.execute('SELECT * FROM apps WHERE corp=? AND owner=?', (corp, owner)).fetchone()
            if existing:
                # Do not issue multiple allocations even if callers use new request IDs.
                raise PoolError('owner_already_has_application')
            selected = next((r for r in self.db.execute(
                "SELECT * FROM apps WHERE state='free' AND corp=? ORDER BY agent", (corp,))
                if r['agent'] in eligible), None)
            if selected is None:
                raise PoolError('no_verified_free_application')
            agent = selected['agent']
            self.db.execute("INSERT INTO requests VALUES(?,?,?,?,'reserved',?)", (request, corp, owner, agent, self.clock()))
            self.db.execute("UPDATE apps SET state='reserved',owner=?,request_id=?,lease=? WHERE agent=?",
                            (owner, request, self.clock()+900, agent))
            self.event(agent, 'reserved', request)
        return self.get(agent)

    def begin(self, request):
        with self.transaction():
            self.expire()
            row = self.db.execute('SELECT * FROM apps WHERE request_id=?', (request,)).fetchone()
            if row is None:
                raise PoolError('reservation_missing_or_expired')
            if row['state'] == 'assigned':
                return dict(row)
            if row['state'] != 'reserved':
                raise PoolError('application_needs_operator_inspection')
            self.db.execute("UPDATE apps SET state='provisioning',lease=NULL WHERE agent=?", (row['agent'],))
            self.db.execute("UPDATE requests SET state='provisioning' WHERE id=?", (request,))
            self.event(row['agent'], 'provisioning', request)
        return self.get(row['agent'])

    def complete(self, agent, success):
        with self.transaction():
            row = self.get(agent)
            if row['state'] != 'provisioning':
                raise PoolError('not_provisioning')
            state = 'assigned' if success else 'failed'
            self.db.execute('UPDATE apps SET state=?,verified=? WHERE agent=?', (state, self.clock(), agent))
            self.db.execute('UPDATE requests SET state=? WHERE id=?', (state, row['request_id']))
            self.event(agent, state, row['request_id'])
        return self.get(agent)

    def disable(self, agent):
        with self.transaction():
            row = self.get(agent)
            self.db.execute("UPDATE apps SET state='disabled',lease=NULL WHERE agent=?", (agent,))
            if row['request_id']:
                self.db.execute("UPDATE requests SET state='disabled' WHERE id=?", (row['request_id'],))
            self.event(agent, 'disabled', row['request_id'])
        return self.get(agent)

    def status(self):
        with self.transaction():
            self.expire()
        return [dict(r) for r in self.db.execute('SELECT * FROM apps ORDER BY agent')]


class PreparedApp:
    """Only supports already installed namespaced runtimes; never creates WeCom apps."""
    def __init__(self, agent):
        if not isinstance(agent, int) or not 1000003 <= agent < 2147483647:
            raise PoolError('invalid_agent')
        self.agent = agent
        self.root = Path('/home/ubuntu/wecom-kf/enterprises/ww0000000000000002/apps') / str(agent)
        self.container = 'xingyu-ww0000000000000002-' + str(agent)
        if not self.root.is_dir() or self.root.is_symlink():
            raise PoolError('application_not_prepared')
        self.values = {}
        path = self.root/'app.env'
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise PoolError('unsafe_environment_permissions')
        for line in path.read_text().splitlines():
            if line and not line.startswith('#'):
                key, value = line.split('=', 1)
                self.values[key] = value
        if self.values.get('WECOM_AGENT_ID') != str(agent):
            raise PoolError('environment_identity_mismatch')
        self.owners = [identity(v.strip()) for v in self.values.get('XIAOYI_APP_ADMINS','').split(',') if v.strip()]
        if len(self.owners) != 1:
            raise PoolError('exactly_one_preverified_owner_required')
        self.owner = self.owners[0]
        self.corp = self.values.get('WECOM_CORP_ID', '')
        if self.corp != 'ww0000000000000002':raise PoolError('corporation_mismatch')

    def docker(self, *args, check=True, timeout=35):
        result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=timeout)
        if check and result.returncode:
            raise PoolError('docker_operation_failed')
        return result

    def slot_members(self, department, owner=None):
        """Prove a dedicated, leaf department using the official directory API."""
        import importlib.util
        directory_root = self.root.parent.parent/'onboarding'
        spec = importlib.util.spec_from_file_location('xiaoyi_directory', directory_root/'directory_admin.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        directory = module.Directory(directory_root)
        records = [p for p in directory_root.glob('department-slot-*.json')
                   if json.loads(p.read_text()).get('id') == department]
        if len(records) != 1:
            raise PoolError('slot_department_not_prepared')
        record = json.loads(records[0].read_text())
        rows = directory.departments()
        if (record.get('state') != 'created' or not any(r['id'] == department and
                r.get('parentid') == record.get('parent') for r in rows) or
                any(r.get('parentid') == department for r in rows)):
            raise PoolError('slot_department_changed')
        for path in self.root.parent.glob('*/app.env'):
            if path.parent == self.root:
                continue
            values = dict(l.split('=', 1) for l in path.read_text().splitlines() if '=' in l)
            if values.get('POOL_SLOT_DEPARTMENT_ID') == str(department):
                raise PoolError('slot_department_shared')
        expected = {identity(owner)} if owner else set()
        if directory.members_in(department) != expected:
            raise PoolError('slot_department_members_mismatch')

    def official(self, existing_binding=False, slot_owner=None):
        def api(endpoint, params):
            try:
                with urllib.request.urlopen('https://qyapi.weixin.qq.com/cgi-bin/' + endpoint + '?' +
                        urllib.parse.urlencode(params), timeout=12) as response:
                    result = json.load(response)
                if result.get('errcode', 0):
                    raise PoolError('wecom_error_' + str(int(result['errcode'])))
                return result
            except PoolError:
                raise
            except Exception:
                # Request exceptions can include the full URL with Secret/token.
                raise PoolError('wecom_network_or_response_error') from None
        token = api('gettoken', {'corpid': self.corp, 'corpsecret': self.values.get('WECOM_APP_SECRET','')}).get('access_token')
        if not token:
            raise PoolError('application_credentials_missing')
        app = api('agent/get', {'access_token': token, 'agentid': self.agent})
        if app.get('agentid') != self.agent:
            raise PoolError('application_credentials_mismatch')
        allowed = {u['userid'] for u in app.get('allow_userinfos', {}).get('user', [])}
        # Dedicated applications must be scoped to the one configured member, not a whole department.
        parties = app.get('allow_partys', {}).get('partyid', [])
        tags = app.get('allow_tags', {}).get('tagid', [])
        exclusive = allowed == {self.owner} and not parties and not tags
        slot_department = self.values.get('POOL_SLOT_DEPARTMENT_ID', '')
        slot_visibility = False
        if slot_department.isdigit():
            slot_visibility = set(parties) == {int(slot_department)} and not tags and not allowed
            if not slot_visibility:
                raise PoolError('slot_visibility_changed')
            expected_owner = slot_owner or (None if self.owner.startswith('poolslot') else self.owner)
            self.slot_members(int(slot_department), expected_owner)
            return str(app.get('name',''))[:128], expected_owner == self.owner
        if not exclusive:
            if not existing_binding:
                if not slot_visibility:
                    raise PoolError('configure_single_member_visibility_first')
                return str(app.get('name',''))[:128], False
            # Importing an existing working owner does not widen access: the unchanged
            # runtime already rejects every other sender/session. Verify membership too.
            member = api('user/get', {'access_token':token,'userid':self.owner})
            if (member.get('userid') != self.owner or member.get('status') != 1 or
                    (self.owner not in allowed and not set(member.get('department',[])).intersection(parties))):
                raise PoolError('existing_owner_not_verified_in_visible_range')
        return str(app.get('name',''))[:128], exclusive

    def update_profile(self, display_name, avatar=None, avatar_type=None):
        """Update the WeCom agent card before enabling the isolated application."""
        if not isinstance(display_name, str) or not 1 <= len(display_name) <= 32:
            raise PoolError('invalid_application_profile')
        if any(ord(char) < 32 or ord(char) == 127 for char in display_name):
            raise PoolError('invalid_application_profile')
        if avatar is not None:
            if not isinstance(avatar, (bytes, bytearray)) or avatar_type not in ('image/jpeg', 'image/png'):
                raise PoolError('invalid_application_profile')
            if len(avatar) > 512 * 1024:
                raise PoolError('invalid_application_profile')
            valid = ((avatar_type == 'image/png' and bytes(avatar).startswith(b'\x89PNG\r\n\x1a\n')) or
                     (avatar_type == 'image/jpeg' and bytes(avatar).startswith(b'\xff\xd8\xff')))
            if not valid:
                raise PoolError('invalid_application_profile')

        def api(endpoint, params, body=None, headers=None):
            try:
                url = 'https://qyapi.weixin.qq.com/cgi-bin/' + endpoint + '?' + urllib.parse.urlencode(params)
                request = urllib.request.Request(url, data=body, headers=headers or {}, method='POST' if body else 'GET')
                with urllib.request.urlopen(request, timeout=20) as response:
                    result = json.load(response)
                if result.get('errcode', 0):
                    raise PoolError('wecom_error_' + str(int(result['errcode'])))
                return result
            except PoolError:
                raise
            except Exception:
                raise PoolError('wecom_profile_update_failed') from None

        token_result = api('gettoken', {'corpid': self.corp, 'corpsecret': self.values.get('WECOM_APP_SECRET', '')})
        token = token_result.get('access_token')
        if not token:
            raise PoolError('application_credentials_missing')
        logo_media_id = None
        if avatar is not None:
            boundary = b'----xiaoyi-' + secrets.token_hex(12).encode()
            filename = b'avatar.png' if avatar_type == 'image/png' else b'avatar.jpg'
            parts = [b'--' + boundary + b'\r\n',
                     b'Content-Disposition: form-data; name="media"; filename="' + filename + b'"\r\n',
                     b'Content-Type: ' + avatar_type.encode() + b'\r\n\r\n',
                     bytes(avatar), b'\r\n--' + boundary + b'--\r\n']
            upload = api('media/upload', {'access_token': token, 'type': 'image'}, b''.join(parts),
                         {'Content-Type': 'multipart/form-data; boundary=' + boundary.decode()})
            logo_media_id = upload.get('media_id')
            if not isinstance(logo_media_id, str) or not logo_media_id:
                raise PoolError('wecom_profile_update_failed')
        payload = {'agentid': self.agent, 'name': display_name}
        if logo_media_id:
            payload['logo_mediaid'] = logo_media_id
        api('agent/set', {'access_token': token}, json.dumps(payload, ensure_ascii=False).encode(),
            {'Content-Type': 'application/json; charset=utf-8'})

    def bind_owner(self, owner):
        """Bind a free department-scoped slot to the reserved owner before startup."""
        owner = identity(owner)
        if not self.values.get('POOL_SLOT_DEPARTMENT_ID'):
            raise PoolError('application_owner_mismatch')
        # Keep the disabled container and its environment consistent until activation.
        # The runtime owner and ENABLE_AI are changed together in one restart.
        self.pending_owner = owner

    def shared_runtime_files(self, cache_empty=False):
        """Recognize only this release's verified IPC/code, never member data."""
        path=self.root/'manifest.json'
        if not path.exists():return set()
        if path.is_symlink() or path.stat().st_mode & 0o022:raise PoolError('unsafe_shared_manifest')
        manifest=json.loads(path.read_text())
        if not manifest.get('shared_runtime_version'):return set()
        expected={'ww0000000000000001:1000003': 'xingyu-shared-ww0000000000000001-g1', 'ww0000000000000001:1000004': 'xingyu-shared-ww0000000000000001-g1', 'ww0000000000000001:1000005': 'xingyu-shared-ww0000000000000001-g1', 'ww0000000000000001:1000006': 'xingyu-shared-ww0000000000000001-g1', 'ww0000000000000001:1000007': 'xingyu-shared-ww0000000000000001-g1', 'ww0000000000000001:1000008': 'xingyu-shared-ww0000000000000001-g1', 'ww0000000000000001:1000009': 'xingyu-shared-ww0000000000000001-g1', 'ww0000000000000002:1000002': 'xingyu-shared-ww0000000000000002-g1', 'ww0000000000000002:1000003': 'xingyu-shared-ww0000000000000002-g1', 'ww0000000000000002:1000004': 'xingyu-shared-ww0000000000000002-g1', 'ww0000000000000002:1000005': 'xingyu-shared-ww0000000000000002-g1', 'ww0000000000000002:1000006': 'xingyu-shared-ww0000000000000002-g1', 'ww0000000000000002:1000007': 'xingyu-shared-ww0000000000000002-g1', 'ww0000000000000002:1000008': 'xingyu-shared-ww0000000000000002-g1', 'ww0000000000000002:1000009': 'xingyu-shared-ww0000000000000002-g1', 'ww0000000000000002:1000010': 'xingyu-shared-ww0000000000000002-g1', 'ww0000000000000002:1000011': 'xingyu-shared-ww0000000000000002-g1', 'ww0000000000000002:1000012': 'xingyu-shared-ww0000000000000002-g2', 'ww0000000000000002:1000013': 'xingyu-shared-ww0000000000000002-g2', 'ww0000000000000002:1000014': 'xingyu-shared-ww0000000000000002-g2', 'ww0000000000000002:1000015': 'xingyu-shared-ww0000000000000002-g2', 'ww0000000000000002:1000016': 'xingyu-shared-ww0000000000000002-g2', 'ww0000000000000002:1000017': 'xingyu-shared-ww0000000000000002-g2', 'ww0000000000000002:1000018': 'xingyu-shared-ww0000000000000002-g2', 'ww0000000000000002:1000019': 'xingyu-shared-ww0000000000000002-g2', 'ww0000000000000002:1000020': 'xingyu-shared-ww0000000000000002-g2', 'ww0000000000000002:1000021': 'xingyu-shared-ww0000000000000002-g2', 'ww0000000000000002:1000022': 'xingyu-shared-ww0000000000000002-g3', 'ww0000000000000002:1000024': 'xingyu-shared-ww0000000000000002-g4', 'ww0000000000000002:1000025': 'xingyu-shared-ww0000000000000002-g4', 'ww0000000000000002:1000026': 'xingyu-shared-ww0000000000000002-g4', 'ww0000000000000002:1000027': 'xingyu-shared-ww0000000000000002-g4', 'ww0000000000000002:1000028': 'xingyu-shared-ww0000000000000002-g4', 'ww0000000000000002:1000029': 'xingyu-shared-ww0000000000000002-g4', 'ww0000000000000002:1000030': 'xingyu-shared-ww0000000000000002-g4', 'ww0000000000000002:1000031': 'xingyu-shared-ww0000000000000002-g4', 'ww0000000000000002:1000032': 'xingyu-shared-ww0000000000000002-g4', 'ww0000000000000002:1000033': 'xingyu-shared-ww0000000000000002-g4', 'ww0000000000000002:1000034': 'xingyu-shared-ww0000000000000002-g5', 'ww0000000000000002:1000035': 'xingyu-shared-ww0000000000000002-g5', 'ww0000000000000002:1000036': 'xingyu-shared-ww0000000000000002-g5', 'ww0000000000000002:1000037': 'xingyu-shared-ww0000000000000002-g5', 'ww0000000000000002:1000038': 'xingyu-shared-ww0000000000000002-g5', 'ww0000000000000002:1000039': 'xingyu-shared-ww0000000000000002-g5', 'ww0000000000000002:1000040': 'xingyu-shared-ww0000000000000002-g5', 'ww0000000000000002:1000041': 'xingyu-shared-ww0000000000000002-g5', 'ww0000000000000002:1000042': 'xingyu-shared-ww0000000000000002-g5', 'ww0000000000000002:1000043': 'xingyu-shared-ww0000000000000002-g5'}.get(self.corp+':'+str(self.agent))
        if (not expected or manifest.get('shared_runtime_version')!='shared-runtime-fleet10-20261103.1'
                or manifest.get('shared_runtime_group')!=expected):raise PoolError('unknown_shared_runtime')
        data=self.root/'data';owner=data.stat().st_uid;client=data/'.shared-runtime-client.py'
        if (client.is_symlink() or not client.is_file() or client.stat().st_uid not in (0,owner)
                or client.stat().st_mode & 0o022
                or hashlib.sha256(client.read_bytes()).hexdigest()!='ed4472987a481227d2b0b7fc0fe176fb02b1c12a652d931ace1522ac01c683df'):raise PoolError('unsafe_shared_client')
        accepted={'.shared-runtime-client.py'}
        for name in ('.shared-control.sock','.shared-http.sock'):
            sock=data/name
            if sock.exists() or sock.is_symlink():
                if (sock.is_symlink() or not sock.is_socket() or sock.stat().st_uid!=owner
                        or sock.stat().st_mode & 0o777!=0o600):raise PoolError('unsafe_shared_socket')
                accepted.add(name)
        cache=data/'.shared-preview-cache'
        if cache.exists() or cache.is_symlink():
            if (cache.is_symlink() or not cache.is_dir() or cache.stat().st_uid!=owner
                    or cache.stat().st_mode & 0o777!=0o700):raise PoolError('unsafe_shared_cache')
            if cache_empty and any(cache.iterdir()):raise PoolError('application_has_data_do_not_reassign')
            accepted.add('.shared-preview-cache')
        return accepted

    def pristine(self):
        # Keep metadata only; all user/job/session tables must be empty. No silent wipe or migration.
        data = self.root/'data'
        if data.is_symlink() or not data.is_dir():
            raise PoolError('unsafe_data_directory')
        allowed = {'lifecycle-policy.json','lifecycle.sqlite3','lifecycle.sqlite3-wal','lifecycle.sqlite3-shm','kf.sqlite3', 'kf.sqlite3-wal', 'kf.sqlite3-shm', 'token-usage.sqlite3', 'token-usage.sqlite3-wal', 'token-usage.sqlite3-shm'}
        shared=self.shared_runtime_files(cache_empty=True)
        for entry in data.iterdir():
            if entry.name in shared:continue
            if entry.name == 'billing.sock':
                # The per-app billing bridge recreates this private IPC endpoint
                # even for unused slots. It is infrastructure, not member data.
                # Accept only a real private socket owned by this data directory's UID.
                details = entry.lstat()
                if (entry.is_symlink() or not entry.is_socket()
                        or details.st_uid != data.stat().st_uid or details.st_mode & 0o777 != 0o600):
                    raise PoolError('unsafe_billing_socket')
                continue
            if entry.name not in allowed:
                raise PoolError('application_has_data_do_not_reassign')
        policy=data/'lifecycle-policy.json'
        if policy.exists():
            if policy.is_symlink():raise PoolError('unsafe_lifecycle_policy')
            value=json.loads(policy.read_text())
            if value.get('agent')!=self.agent or value.get('enabled') is not False or value.get('owner')!='':
                raise PoolError('application_has_data_do_not_reassign')
        tracker=data/'lifecycle.sqlite3'
        if tracker.is_symlink():raise PoolError('unsafe_lifecycle_database')
        if tracker.exists():
            connection=sqlite3.connect(tracker.as_uri()+'?mode=ro',uri=True,timeout=5)
            try:
                for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
                    quoted='"'+table.replace('"','""')+'"'
                    if connection.execute('SELECT 1 FROM '+quoted+' LIMIT 1').fetchone():raise PoolError('application_has_data_do_not_reassign')
            finally:connection.close()
        usage=data/'token-usage.sqlite3'
        if usage.is_symlink():raise PoolError('unsafe_usage_database')
        if usage.exists():
            connection=sqlite3.connect(usage.as_uri()+'?mode=ro',uri=True,timeout=5)
            try:
                for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
                    quoted='"'+table.replace('"','""')+'"'
                    if connection.execute('SELECT 1 FROM '+quoted+' LIMIT 1').fetchone():
                        raise PoolError('application_has_data_do_not_reassign')
            finally:connection.close()
        database = data/'kf.sqlite3'
        if database.exists():
            connection = sqlite3.connect(database.as_uri()+'?mode=ro', uri=True, timeout=5)
            try:
                for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
                    if table == 'suite_history_migrations':
                        # Only the known system migration marker is infrastructure.
                        # Never exempt arbitrary versions, columns, or other data tables.
                        schema = connection.execute('PRAGMA table_info(suite_history_migrations)').fetchall()
                        expected = [(0, 'version', 'TEXT', 0, None, 1),
                                    (1, 'applied', 'REAL', 0, None, 0)]
                        if schema != expected:
                            raise PoolError('application_has_data_do_not_reassign')
                        for version, applied in connection.execute('SELECT version,applied FROM suite_history_migrations'):
                            if (version != 'persona-slot-history-20261101.1'
                                    or not isinstance(applied, (int, float))
                                    or not 0 < applied < float('inf')):
                                raise PoolError('application_has_data_do_not_reassign')
                        continue
                    if table == 'suite_application_defaults':
                        # Application-level rules configured by the authorized console are not member data.
                        for agent, value in connection.execute('SELECT agent,value FROM suite_application_defaults'):
                            allowed = {'memory_auto', 'min_interval', 'memory_suggestions', 'quiet_end', 'ai_image', 'greeting_mode', 'merge_seconds', 'sticker_match', 'voice_auto_emotion', 'memory_scope', 'proactive_max_unanswered', 'weather', 'voice', 'voice_rate', 'quiet_start', 'voice_language', 'strip_actions', 'voice_speed', 'proactive_rest_hours', 'ai_media_scope', 'voice_subtitles', 'split_reply', 'wait_seconds', 'language', 'ai_voice', 'sticker_rate', 'proactive', 'merge_messages'}
                            options = json.loads(value)
                            if agent != self.agent or not isinstance(options, dict) or set(options) - allowed:
                                raise PoolError('invalid_application_defaults')
                        continue
                    if table == 'metadata':
                        if connection.execute("SELECT 1 FROM metadata WHERE key!='started' LIMIT 1").fetchone():
                            raise PoolError('application_has_data_do_not_reassign')
                        continue
                    quoted = '"' + table.replace('"','""') + '"'
                    if connection.execute('SELECT 1 FROM '+quoted+' LIMIT 1').fetchone():
                        raise PoolError('application_has_data_do_not_reassign')
            finally:
                connection.close()

    def verify_local(self, enabled, pristine=False):
        if self.values.get('ENABLE_AI') != str(enabled).lower():
            raise PoolError('unexpected_enabled_state')
        manifest = json.loads((self.root/'manifest.json').read_text())
        if manifest['agent_id'] != self.agent:
            raise PoolError('runtime_identity_mismatch')
        for filename, digest in manifest['runtime'].items():
            if Path(filename).name != filename:
                raise PoolError('unsafe_manifest')
            path = self.root/filename if filename in ('pilot_entry.py','setup.py') else self.root/'runtime'/filename
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise PoolError('runtime_hash_mismatch')
        info = json.loads(self.docker('inspect', self.container).stdout)[0]
        if info['Config'].get('Labels', {}).get('xiaoyi.pilot.agent') != str(self.agent):
            raise PoolError('container_identity_mismatch')
        running_env = dict(v.split('=',1) for v in info['Config']['Env'] if '=' in v)
        for key in ('WECOM_AGENT_ID','WECOM_CORP_ID','WECOM_APP_SECRET','CALLBACK_TOKEN','CALLBACK_AES_KEY','ENABLE_AI','XIAOYI_APP_ADMINS'):
            if running_env.get(key) != self.values.get(key):
                raise PoolError('running_environment_mismatch')
        mounts = {m['Destination']:m['Source'] for m in info['Mounts']}
        if mounts.get('/data') != str(self.root/'data') or info['HostConfig'].get('PortBindings'):
            raise PoolError('data_or_network_isolation_mismatch')
        if pristine:
            self.pristine()
        return info

    def prepare_idle(self):
        """Wake only this unassigned slot; the caller holds operation.lock."""
        info = self.verify_local(False, pristine=True)
        if info['State'].get('Running'):
            return
        if info['State'].get('Status') not in ('exited', 'created'):
            raise PoolError('idle_container_state_requires_inspection')
        # Verify the official scope before waking a dormant runtime.
        self.official()
        self.docker('start', self.container)
        deadline = time.monotonic() + 30
        code = "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8088/health',timeout=2)"
        try:
            while time.monotonic() < deadline:
                result = self.docker('exec', self.container, 'python', '-c', code,
                                     check=False, timeout=5)
                if result.returncode == 0:
                    return
                time.sleep(1)
            raise PoolError('idle_application_start_timeout')
        except BaseException:
            self.docker('stop', '-t', '10', self.container, check=False)
            raise

    def hibernate(self):
        """Stop a proven empty, disabled slot; never delete or change its data."""
        info = self.verify_local(False, pristine=True)
        if info['State'].get('Paused') or info['State'].get('Restarting'):
            raise PoolError('idle_container_state_requires_inspection')
        if info['State'].get('Status') not in ('running', 'exited', 'created'):
            raise PoolError('idle_container_state_requires_inspection')
        if info['HostConfig'].get('RestartPolicy', {}).get('Name') != 'unless-stopped':
            self.docker('update', '--restart', 'unless-stopped', self.container)
        was_running = bool(info['State'].get('Running'))
        if was_running:
            self.docker('stop', '-t', '10', self.container)
        return was_running

    def verify(self, enabled, pristine=False, existing_binding=False, slot_owner=None):
        name, exclusive = self.official(existing_binding=existing_binding, slot_owner=slot_owner)
        self.verify_local(enabled)
        code = "import json,urllib.request;print(json.dumps(json.load(urllib.request.urlopen('http://127.0.0.1:8088/health',timeout=3))))"
        health = json.loads(self.docker('exec', self.container, 'python', '-c', code).stdout)
        app = health.get('independent_app', {})
        if not health.get('ok') or app.get('agent_id') != self.agent or app.get('ai_enabled') != enabled or not app.get('restricted_to_test_owners'):
            raise PoolError('application_health_or_owner_guard_failed')
        self.callback()
        if pristine:
            self.pristine()
        return {'agent':self.agent,'corp':self.corp,'owner':self.owner,'name':name,
                'visibility_exclusive':exclusive,'runtime_owner_only':True}

    def callback(self):
        # Run the encrypted challenge inside the app; credentials never leave that container.
        code = r'''
import base64,hashlib,json,os,secrets,struct,time,urllib.parse,urllib.request
from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
key=base64.b64decode(os.environ['CALLBACK_AES_KEY']+'=')
clear=('pool-check-'+secrets.token_hex(8)).encode()
raw=secrets.token_bytes(16)+struct.pack('!I',len(clear))+clear+os.environ['WECOM_CORP_ID'].encode()
pad=32-len(raw)%32;raw+=bytes([pad])*pad
cipher=Cipher(algorithms.AES(key),modes.CBC(key[:16])).encryptor()
encrypted=base64.b64encode(cipher.update(raw)+cipher.finalize()).decode()
q={'timestamp':str(int(time.time())),'nonce':secrets.token_hex(8),'echostr':encrypted}
q['msg_signature']=hashlib.sha1(''.join(sorted([os.environ['CALLBACK_TOKEN'],q['timestamp'],q['nonce'],encrypted])).encode()).hexdigest()
try:
 r=urllib.request.urlopen('https://www.example.com/wecom/enterprises/ww0000000000000002/apps/'+os.environ['WECOM_AGENT_ID']+'?'+urllib.parse.urlencode(q),timeout=10)
 ok=r.status==200 and r.read()==clear
except Exception:
 ok=False
print(json.dumps({'ok':ok}))
'''
        if not json.loads(self.docker('exec',self.container,'python','-c',code).stdout).get('ok'):
            raise PoolError('public_callback_verification_failed')

    def activate(self, recovery_loader=None, recovery_prepared=False):
        owner = getattr(self, 'pending_owner', self.owner)
        if not recovery_prepared:
            self.verify(False, pristine=True, slot_owner=owner if self.values.get('POOL_SLOT_DEPARTMENT_ID') else None)
        # Inherit the authorized template before enabling a freshly assigned slot.
        sys.path.insert(0,str(self.root.parent.parent/'admin/code'))
        from global_defaults import inherit
        if not recovery_prepared:inherit(self)
        if recovery_loader is not None:
            self.docker('stop','-t','20',self.container,check=False)
            recovery_loader(self)
        args = json.loads((self.root/'container-run.json').read_text())
        # Run file was created by the installer, never supplied by a browser.
        if args[:2] != ['run','-d'] or '--name' not in args or args[args.index('--name')+1] != self.container:
            raise PoolError('container_run_identity_mismatch')
        self.docker('stop','-t','20',self.container,check=recovery_loader is None)
        self.docker('rm',self.container,check=recovery_loader is None)
        updated = dict(self.values, ENABLE_AI='true', XIAOYI_APP_ADMINS=owner)
        temp = self.root/('app.env.pool-'+secrets.token_hex(6))
        with os.fdopen(os.open(temp,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600),'w') as handle:
            handle.write(''.join(k+'='+updated[k]+'\n' for k in sorted(updated)))
        os.replace(temp,self.root/'app.env')
        self.values = updated
        self.owner, self.owners = owner, [owner]
        try:
            self.docker(*args)
            # Full verification below is authoritative; retry only readiness, never paid generation.
            for _ in range(15):
                result = self.docker('exec',self.container,'python','-c',
                    "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8088/health',timeout=2)", check=False)
                if result.returncode == 0:
                    break
                time.sleep(1)
            self.verify(True)
        except BaseException:
            self.docker('stop','-t','20',self.container,check=False)
            # Preserve failed assignment and all data for inspection; do not put it back in the pool.
            raise


def preferred_idle_rows(rows, factory=PreparedApp):
    """Prefer a running warm slot over a dormant one, without waking any slot."""
    free = [r for r in rows if r['state'] == 'free']
    if not free:
        return []
    apps = [factory(row['agent']) for row in free]
    infos = json.loads(apps[0].docker('inspect', *(app.container for app in apps)).stdout)
    running = {info['Name'].lstrip('/') for info in infos if info['State'].get('Running')}
    return [row for _, row in sorted(zip(apps, free),
            key=lambda pair: (pair[0].container not in running, pair[1]['agent']))]


def reconcile_free(registry, target, factory=PreparedApp):
    """Caller must hold operation.lock, shared with onboarding and reclaim."""
    if type(target) is not int or not 0 <= target <= 5:
        raise PoolError('invalid_standby_target')
    result = {'target': target, 'warm': [], 'started': [], 'stopped': [], 'dormant': [], 'blocked': []}
    rows = [dict(row) for row in registry.db.execute("SELECT * FROM apps WHERE state='free' ORDER BY agent").fetchall()]
    for row in preferred_idle_rows(rows, factory):
        if row['owner'] is not None or row['request_id'] is not None:
            result['blocked'].append({'agent': row['agent'], 'error': 'free_slot_has_binding'})
            continue
        try:
            app = factory(row['agent'])
            if app.corp != row['corp']:
                raise PoolError('corporation_mismatch')
            info = app.verify_local(False, pristine=True)
            if len(result['warm']) < target and app.values.get('POOL_SLOT_DEPARTMENT_ID'):
                try:
                    app.prepare_idle()
                    app.verify(False, pristine=True)
                    result['warm'].append(row['agent'])
                    if not info['State'].get('Running'):
                        result['started'].append(row['agent'])
                        registry.event(row['agent'], 'standby_started')
                    continue
                except Exception as exc:
                    # A failed warm-up does not prevent trying another empty slot.
                    error = str(exc) if isinstance(exc, PoolError) else 'standby_wakeup_failed'
                    if not re.fullmatch(r'[A-Za-z0-9_]{1,100}', error):
                        error = 'standby_inspection_required'
                    result['blocked'].append({'agent': row['agent'], 'error': error})
            stopped = app.hibernate()
            result['stopped' if stopped else 'dormant'].append(row['agent'])
            if stopped:
                registry.event(row['agent'], 'standby_stopped')
        except Exception as exc:
            error = str(exc) if isinstance(exc, PoolError) else 'standby_inspection_required'
            if not re.fullmatch(r'[A-Za-z0-9_]{1,100}', error):
                error = 'standby_inspection_required'
            result['blocked'].append({'agent': row['agent'], 'error': error})
    return result


def public(row):
    return {k:row[k] for k in ('agent','name','state','lease')}


def stop_application(agent):
    if not isinstance(agent,int) or agent < 1000003:
        raise PoolError('invalid_agent')
    result = subprocess.run(['docker','stop','-t','20','xingyu-ww0000000000000002-'+str(agent)],
                            capture_output=True,timeout=35)
    if result.returncode:
        raise PoolError('cannot_stop_application_operator_inspection_required')


def recover_interrupted(registry, stop=stop_application):
    """Called under the host lock. A previous provisioning process is no longer alive."""
    recovered = []
    for row in registry.status():
        if row['state'] in ('failed','disabled'):
            # Retry stop even after a previous Docker outage or an out-of-band restart.
            stop(row['agent'])
        elif row['state'] == 'provisioning':
            stop(row['agent'])
            registry.complete(row['agent'],False)
            recovered.append(row['agent'])
    return recovered


def activate_binding(registry, request, factory=PreparedApp, stop=stop_application):
    row = registry.begin(request)
    if row['state'] == 'assigned':
        return row
    try:
        app = factory(row['agent'])
        if app.corp != row['corp']:
            raise PoolError('binding_identity_mismatch')
        if app.owner != row['owner']:
            if not hasattr(app, 'bind_owner'):
                raise PoolError('binding_identity_mismatch')
            app.bind_owner(row['owner'])
        app.activate()
        return registry.complete(row['agent'],True)
    except BaseException:
        # Includes failure to commit after startup. Keep owner/data and quarantine it.
        try:
            stop(row['agent'])
        finally:
            if registry.get(row['agent'])['state'] == 'provisioning':
                registry.complete(row['agent'],False)
        raise


def main():
    parser = argparse.ArgumentParser(description='星屿应用池（服务器管理员命令；不接受网页直接分配）')
    parser.add_argument('action', choices=['status','import-assigned','register-free','reserve','activate','disable','audit','recover'])
    parser.add_argument('--agent',type=int)
    parser.add_argument('--owner',help='企业微信内部 UserID，不能填昵称、手机号或 external_userid')
    parser.add_argument('--request',help='管理员生成的幂等请求编号')
    args = parser.parse_args()
    if not hasattr(os,'geteuid') or os.geteuid() != 0:
        raise PoolError('server_root_required')
    import fcntl
    root = Path('/home/ubuntu/wecom-kf/enterprises/ww0000000000000002/app-pool')
    root.mkdir(mode=0o700,exist_ok=True)
    os.chmod(root,0o700)
    os.umask(0o077)
    with (root/'operation.lock').open('a') as lock:
        # Serializes Docker side effects as well as the transactional DB allocations.
        fcntl.flock(lock,fcntl.LOCK_EX)
        registry = Registry(root/'registry.sqlite3')
        try:
            recovered = recover_interrupted(registry)
            if args.action == 'status':
                rows = registry.status()
                result = {'applications':[public(r) for r in rows],
                          'counts':{s:sum(r['state']==s for r in rows) for s in
                            ('free','reserved','provisioning','assigned','disabled','failed')}}
            elif args.action in ('import-assigned','register-free'):
                app = PreparedApp(args.agent)
                assigned = args.action == 'import-assigned'
                proof = app.verify(assigned,pristine=not assigned,existing_binding=assigned)
                result = public(registry.register(proof,assigned))
                result['visibility_exclusive'] = proof['visibility_exclusive']
                result['runtime_owner_only'] = proof['runtime_owner_only']
            elif args.action == 'reserve':
                identity(args.owner)
                identity(args.request)
                rows = registry.status()
                if not rows:
                    raise PoolError('no_verified_free_application')
                eligible = []
                for row in rows:
                    if row['state'] != 'free':
                        continue
                    app = PreparedApp(row['agent'])
                    if not app.values.get('POOL_SLOT_DEPARTMENT_ID') and app.owner != args.owner:
                        continue
                    app.verify(False,pristine=True)
                    eligible.append(row['agent'])
                result = public(registry.reserve(rows[0]['corp'],args.owner,args.request,eligible))
            elif args.action == 'activate':
                identity(args.request)
                result = public(activate_binding(registry,args.request))
            elif args.action == 'disable':
                row = registry.get(args.agent)
                app = PreparedApp(args.agent)
                # Docker stop, not just a website flag: scheduled outbound work also stops.
                app.docker('stop','-t','20',app.container)
                result = public(registry.disable(args.agent))
            elif args.action == 'recover':
                result = {'recovered':recovered}
            else:
                rows = registry.status()
                checks = []
                for row in rows:
                    app = PreparedApp(row['agent'])
                    if row['state'] not in ('assigned','free'):
                        checks.append({'agent':row['agent'],'state':row['state'],'needs_inspection':True})
                        continue
                    proof = app.verify(row['state']=='assigned',pristine=row['state']=='free',
                                       existing_binding=row['state']=='assigned' and row['request_id'] is None)
                    if proof['corp'] != row['corp'] or (row['owner'] and proof['owner'] != row['owner']):
                        raise PoolError('binding_identity_mismatch')
                    checks.append({'agent':row['agent'],'verified':True,'state':row['state'],
                                   'visibility_exclusive':proof['visibility_exclusive'], 'runtime_owner_only':True})
                result = {'checks':checks,'model_calls':0,'messages_sent':0}
            print(json.dumps({'ok':True,**result},ensure_ascii=False))
        finally:
            registry.db.close()


if __name__ == '__main__':
    try:
        main()
    except PoolError as exc:
        print(json.dumps({'ok':False,'error':str(exc)}))
        sys.exit(1)
    except Exception:
        # Do not leak paths, environment, request URLs or credentials in unexpected exceptions.
        print(json.dumps({'ok':False,'error':'operator_inspection_required'}))
        sys.exit(1)
