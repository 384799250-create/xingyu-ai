"""Root-owned lifecycle controller. Serializes with onboarding and management jobs."""
import argparse
from contextlib import closing
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import time
import zlib
from lifecycle import Lifecycle,DELETE_SECONDS

class Blocked(Exception):pass

def connect(path):
    db=sqlite3.connect(path,timeout=20);db.row_factory=sqlite3.Row;return db

def atomic(path,raw,mode=0o600,owner=None):
    if path.is_symlink():raise Blocked('unsafe_path')
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.lifecycle-new')
    with temporary.open('wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
    temporary.chmod(mode)
    if owner is not None:os.chown(temporary,*owner)
    os.replace(temporary,path)

def env_read(path):return dict(l.split('=',1) for l in path.read_text().splitlines() if l and not l.startswith('#') and '=' in l)

def default_avatar():
    # Simple nonpersonal placeholder; no external image generation.
    rows=[]
    for y in range(128):
        row=bytearray()
        for x in range(128):
            white=(27<=x<=101 and 30<=y<=87 and (x<33 or x>95 or y<36 or y>81)) or (34<=x<=39 and 84<=y<=101)
            row.extend((255,255,255) if white else (22,99,86))
        rows.append(b'\0'+bytes(row))
    def chunk(kind,data):return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!IIBBBBB',128,128,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(b''.join(rows)))+chunk(b'IEND',b'')

class Controller:
    def __init__(self,root,clock=time.time):
        self.root=Path(root).resolve();self.clock=clock
        self.home=self.root/'admin/data';self.home.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.db=connect(self.home/'lifecycle-control.sqlite3')
        self.db.executescript('''PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,agent INTEGER,owner TEXT,request TEXT,
            account TEXT,reason TEXT,phase TEXT,created REAL,updated REAL,error TEXT);
          CREATE UNIQUE INDEX IF NOT EXISTS lifecycle_one_running ON runs(agent) WHERE phase!='done';
          CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,agent INTEGER,action TEXT,detail TEXT,created REAL);
          CREATE TABLE IF NOT EXISTS manual_authorizations(run TEXT PRIMARY KEY,authorizer INTEGER NOT NULL,
            preserve_member INTEGER NOT NULL CHECK(preserve_member=1));
        ''')
        (self.home/'lifecycle-control.sqlite3').chmod(0o600)
        sys.path[:0]=[str(self.root/'app-pool'),str(self.root/'onboarding')]
        import app_pool
        from directory_admin import Directory
        self.pool=app_pool;self.directory=Directory(self.root/'onboarding')
    def close(self):self.db.close()
    def run(self,args,timeout=60):
        p=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
        if p.returncode:raise Blocked('container_operation_failed')
        return p.stdout
    def audit(self,agent,action,detail=''):
        with self.db:self.db.execute('INSERT INTO events(agent,action,detail,created) VALUES(?,?,?,?)',(agent,action,str(detail)[:200],self.clock()))
    def rows(self):
        with closing(connect(self.root/'app-pool/registry.sqlite3')) as db:
            return [dict(v) for v in db.execute('SELECT * FROM apps ORDER BY agent')]
    def job(self,row):
        with closing(connect(self.root/'onboarding/jobs.sqlite3')) as db:
            value=db.execute('SELECT * FROM jobs WHERE agent=? AND request=? AND owner=?',(row['agent'],row['request_id'],row['owner'])).fetchone()
        return dict(value) if value else None
    def protected(self,row,job):
        if not (row.get('owner') or '').startswith('xy_'):return True
        if not job:return True
        try:account=int(job['account'])
        except (ValueError,TypeError):return True
        with closing(connect(Path('/home/ubuntu/wecom-kf/admin/data/admin.sqlite3'))) as db:
            role=db.execute('SELECT role FROM roles WHERE account=?',(account,)).fetchone()
            if role and role['role']!='member':return True
        # Additional explicit enterprise-admin exemptions may be maintained by the operator.
        p=self.home/'lifecycle-protected.json'
        if p.exists():
            value=json.loads(p.read_text())
            if row['agent'] in value.get('agents',[]) or row['owner'] in value.get('members',[]) or account in value.get('accounts',[]):return True
        return False
    def eligible(self,row):
        app=self.pool.PreparedApp(row['agent']);job=self.job(row)
        return app,job,bool(app.values.get('POOL_SLOT_DEPARTMENT_ID')) and not self.protected(row,job)
    def data_identity(self,app):
        p=app.root/'data/kf.sqlite3'
        return (p if p.exists() else app.root/'data').stat()
    def write_policy(self,app,value):
        path=app.root/'data/lifecycle-policy.json';identity=self.data_identity(app)
        old=json.loads(path.read_text()) if path.exists() else {}
        if value!=old:
            atomic(path,(json.dumps(value,ensure_ascii=False)+'\n').encode(),0o640,
                (0,identity.st_gid) if hasattr(os,'geteuid') and os.geteuid()==0 else None)
        elif hasattr(os,'geteuid') and os.geteuid()==0:
            os.chown(path,0,identity.st_gid);path.chmod(0o640)
    def tracker(self,app):
        path=app.root/'data/lifecycle.sqlite3';identity=self.data_identity(app)
        if hasattr(os,'geteuid') and os.geteuid()==0:
            # SQLite copies the main database's ownership to WAL/SHM sidecars.
            # Set ownership before connecting so both root and the app can use it.
            if not path.exists():
                try:fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
                except FileExistsError:pass
            for suffix in ('','-wal','-shm'):
                p=path.with_name(path.name+suffix)
                if p.exists():
                    if p.is_symlink():raise Blocked('unsafe_lifecycle_database')
                    os.chown(p,identity.st_uid,identity.st_gid);p.chmod(0o600)
        return Lifecycle(app.root/'data',app.agent,self.clock)
    def policy(self,row):
        agent=row['agent'];app,job,eligible=self.eligible(row)
        path=app.root/'data/lifecycle-policy.json'
        old=json.loads(path.read_text()) if path.exists() else {}
        enabled=row['state']=='assigned' and eligible
        value={'agent':agent,'enabled':enabled,'owner':row['owner'] if enabled else '',
            'reason':'idle_policy' if enabled else 'free' if row['state']=='free' else 'protected' if not eligible else 'not_assigned'}
        if enabled:
            value['baseline']=old.get('baseline',self.clock()) if old.get('owner')==row['owner'] and old.get('enabled') else self.clock()
        import operating_controller
        value=operating_controller.augment_policy(self,row,value)
        self.write_policy(app,value)
        return value
    def state(self,row):
        instance=self.tracker(self.pool.PreparedApp(row['agent']))
        try:return instance.status(row['owner'])
        finally:instance.close()
    def superadmin(self,account):
        if type(account) is not int:raise Blocked('superadmin_required')
        with closing(connect(Path('/home/ubuntu/wecom-kf/admin/data/admin.sqlite3'))) as db:
            role=db.execute('SELECT role FROM roles WHERE account=?',(account,)).fetchone()
        if not role or role['role']!='superadmin':raise Blocked('superadmin_required')
    def guard(self,run,row,job):
        auth=self.db.execute('SELECT authorizer FROM manual_authorizations WHERE run=?',(run['id'],)).fetchone()
        if auth:
            if run['reason']!='manual':raise Blocked('invalid_manual_authorization')
            self.superadmin(auth['authorizer'])
            return True
        if self.protected(row,job):raise Blocked('member_protection_changed')
        return False
    def create(self,row,reason,last_used=None,authorizer=None,protected_confirm=False):
        app,job,eligible=self.eligible(row)
        if reason!='manual' and not eligible:raise Blocked('protected_application')
        retain=False
        existing=self.db.execute("SELECT id FROM runs WHERE agent=? AND phase!='done'",(row['agent'],)).fetchone()
        if existing:return existing['id']
        if reason=='idle':
            tracker=self.tracker(app)
            try:ident=tracker.claim_cleanup(row['owner'],last_used)
            finally:tracker.close()
            if not ident:return None
        elif reason=='manual':
            from reclaim_support import status,binding
            check=status(self.root,row['agent'])
            if not check['allowed'] or check['binding']!=binding(row):raise Blocked('manual_reclaim_binding_changed')
            retain=check['requires_superadmin']
            if retain:
                self.superadmin(authorizer)
                if protected_confirm is not True:raise Blocked('protected_confirmation_required')
            elif not eligible:raise Blocked('protected_application')
            self.precheck({'owner':row['owner']},app,retain_member=retain)
            import secrets
            ident=secrets.token_hex(16)
        elif reason=='operating_reclaim':
            import secrets
            ident=secrets.token_hex(16)
        elif reason=='account_delete':
            if not eligible:raise Blocked('protected_application')
            self.precheck({'owner':row['owner']},app)
            import secrets
            ident=secrets.token_hex(16)
        elif reason=='failed':
            import secrets
            ident=secrets.token_hex(16)
            # No automatic recycling of a stopped app that has ever contained user work.
            app.pristine()
        else:raise Blocked('invalid_cleanup_reason')
        with self.db:
            self.db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?)',
                (ident,row['agent'],row['owner'],row['request_id'],str(job['account']) if job else '',reason,'claimed',self.clock(),self.clock(),None))
            if retain:self.db.execute('INSERT INTO manual_authorizations VALUES(?,?,1)',(ident,authorizer))
        self.audit(row['agent'],'cleanup_claimed',reason)
        return ident
    def phase(self,ident,phase):
        with self.db:self.db.execute('UPDATE runs SET phase=?,updated=?,error=NULL WHERE id=?',(phase,self.clock(),ident))
    def safe_remove(self,path,boundary):
        path=Path(path);boundary=Path(boundary).resolve()
        if path.is_symlink() or not path.resolve().is_relative_to(boundary) or path.resolve()==boundary:raise Blocked('unsafe_cleanup_path')
        if path.is_dir():
            if any(p.is_symlink() for p in path.rglob('*')):raise Blocked('symlink_in_cleanup_data')
            shutil.rmtree(path)
        elif path.exists():path.unlink()
    def archive_usage(self,agent,ident):
        source=self.root/'apps'/str(agent)/'data/token-usage.sqlite3'
        target=self.home/'retired-usage'/(str(agent)+'-'+ident+'.sqlite3')
        if target.exists() or not source.exists():return
        target.parent.mkdir(mode=0o700,exist_ok=True)
        temporary=target.with_suffix('.new')
        with closing(connect(source)) as src,closing(connect(temporary)) as dst:
            src.backup(dst)
            with dst:
                for table in ('token_requests','media_requests'):
                    if dst.execute("SELECT 1 FROM sqlite_master WHERE name=?",(table,)).fetchone():dst.execute('UPDATE '+table+" SET user='已回收用户'")
            dst.execute('PRAGMA wal_checkpoint(TRUNCATE)');dst.execute('VACUUM')
            if dst.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise Blocked('usage_archive_invalid')
        temporary.chmod(0o600);os.replace(temporary,target)
    def scrub_backup_database(self,path,run):
        if path.is_symlink():raise Blocked('unsafe_backup_path')
        owner,agent,request=run['owner'],run['agent'],run['request']
        with closing(connect(path)) as db:
            db.execute('PRAGMA secure_delete=ON')
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            with db:
                if 'jobs' in tables and 'phone_hash' in {r[1] for r in db.execute('PRAGMA table_info(jobs)')}:
                    db.execute('DELETE FROM jobs WHERE owner=? AND (agent=? OR request=?)',(owner,agent,request))
                if 'apps' in tables and 'requests' in tables:
                    db.execute('UPDATE apps SET owner=NULL,request_id=NULL,lease=NULL,state=\'free\' WHERE agent=? AND owner=?',(agent,owner))
                    db.execute('DELETE FROM requests WHERE agent=? AND owner=?',(agent,owner))
                if 'accounts' in tables and not self.has_other_data(owner,agent):
                    # Website login identity remains, but old app bindings cannot be restored.
                    key=(self.root/'app-pool/portal-identity.key').read_bytes()
                    corp=next(r['corp'] for r in self.rows() if r['agent']==agent)
                    subject=hmac.new(key,json.dumps([corp,owner],separators=(',',':')).encode(),hashlib.sha256).hexdigest()
                    cols={r[1] for r in db.execute('PRAGMA table_info(accounts)')}
                    updates=[name+'=NULL' for name in ('subject','waiting') if name in cols]
                    if 'onboarding' in cols:updates.append('onboarding=0')
                    if updates and 'subject' in cols:
                        # Awaiting-scan accounts have no subject yet; they must be able to reapply.
                        if owner.startswith('xy_'):
                            db.execute('UPDATE accounts SET '+','.join(updates)+' WHERE subject=? OR (id=? AND subject IS NULL)',(subject,run['account']))
                        else:
                            # Legacy jobs can name an operator's account rather than the app's member.
                            # Only a matching member subject proves a legacy website binding.
                            db.execute('UPDATE accounts SET '+','.join(updates)+' WHERE subject=?',(subject,))
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)');db.execute('VACUUM')
    def purge_backups(self,run):
        agent=run['agent'];base=self.root/'apps'/str(agent)
        targets=[self.home/'backups'/str(agent)]
        targets.extend(p for p in base.iterdir() if p.name.startswith(('backup-','deploy-backup-')))
        targets.extend(p/str(agent) for p in self.root.glob('deploy-backup-*') if p.is_dir())
        targets.extend(p for p in self.root.glob('backup-memory-release-*') if re.search(r'-'+str(agent)+r'(?:-|$)',p.name))
        targets.extend(p for p in base.glob('*') if p.is_file() and (p.suffix=='.sqlite3' or p.name.startswith('app.env.pool-')))
        for p in targets:
            if p.exists():self.safe_remove(p,self.root)
        # Personal data in whole-control-plane recovery snapshots is selectively removed.
        backups=set()
        roots=[p for p in self.root.glob('deploy-backup-*') if p.is_dir()]
        roots.extend(p for parent in ('onboarding','portal') for p in (self.root/parent).glob('backup-*') if p.is_dir())
        for parent in roots:
            for name in ('jobs.sqlite3','registry.sqlite3','portal.sqlite3'):backups.update(parent.rglob(name))
        for p in backups:self.scrub_backup_database(p,run)
    def has_other_data(self,owner,agent):
        for row in self.rows():
            if row['agent']!=agent and row.get('owner')==owner:return True
        binding=self.home/'application-bindings.json'
        if binding.exists() and any(v.get('owner')==owner and str(v.get('agent'))!=str(agent) for v in json.loads(binding.read_text()).values()):return True
        sources=[self.root/'apps'/str(r['agent'])/'data/kf.sqlite3' for r in self.rows() if r['agent']!=agent]
        sources.append(self.root/'1000002/data/kf.sqlite3')
        for path in sources:
            if not path.exists():continue
            with closing(connect(path)) as db:
                for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
                    if not re.fullmatch('[A-Za-z0-9_]+',table):raise Blocked('unexpected_table_name')
                    cols={r[1] for r in db.execute('PRAGMA table_info('+table+')')}
                    if 'user' in cols and db.execute('SELECT 1 FROM '+table+' WHERE user=? LIMIT 1',(owner,)).fetchone():return True
        return False
    def detach_member(self,run,app):
        # Re-read current privilege and identity before any official member mutation.
        row=next(r for r in self.rows() if r['agent']==run['agent'])
        retain=self.guard(run,row,self.job(row))
        if row['owner']!=run['owner']:raise Blocked('member_protection_changed')
        from directory_admin import DirectoryError
        owner=run['owner'];department=int(app.values['POOL_SLOT_DEPARTMENT_ID'])
        from reclaim_support import member_snapshot
        try:member=member_snapshot(self.directory,app,owner)
        except DirectoryError as exc:
            if str(exc)=='wecom_code_60111':return 'member_absent'
            raise
        if member.get('userid')!=owner:raise Blocked('member_identity_mismatch')
        departments=set(member.get('department',[]))
        if retain:
            if department not in departments:return 'protected_member_already_detached'
            remaining=sorted(departments-{department}) or [self.retained_department()]
            body={'userid':owner,'department':remaining}
            leaders=member.get('is_leader_in_dept')
            if isinstance(leaders,list) and len(leaders)==len(member['department']):
                body['is_leader_in_dept']=[dict(zip(member['department'],leaders)).get(d,0) for d in remaining]
            self.directory.request('user/update',{'access_token':self.directory.token()},body)
            return 'protected_member_retained'
        if member.get('_privileges_incomplete'):raise Blocked('member_privileges_require_inspection')
        if not owner.startswith('xy_'):raise Blocked('not_managed_member')
        if member.get('isleader') or any(member.get('is_leader_in_dept',[])):raise Blocked('member_is_leader')
        if department not in departments:return 'member_already_detached'
        managed={int(self.pool.PreparedApp(r['agent']).values['POOL_SLOT_DEPARTMENT_ID']) for r in self.rows() if self.pool.PreparedApp(r['agent']).values.get('POOL_SLOT_DEPARTMENT_ID')}
        others=self.has_other_data(owner,run['agent'])
        if not others and departments<=managed:
            self.directory.request('user/delete',{'access_token':self.directory.token(),'userid':owner})
            return 'managed_member_removed'
        remaining=sorted(departments-{department})
        if not remaining:raise Blocked('other_data_requires_member_retention')
        self.directory.request('user/update',{'access_token':self.directory.token()},{'userid':owner,'department':remaining})
        return 'member_retained_other_data_or_departments'
    def retained_department(self):
        record=json.loads((self.root/'onboarding/department-retained-members.json').read_text())
        department=record['id'];rows=self.directory.departments()
        if record.get('state')!='created' or not any(d['id']==department and d.get('parentid')==record['parent'] for d in rows) or any(d.get('parentid')==department for d in rows):
            raise Blocked('retained_department_changed')
        if any(self.pool.PreparedApp(r['agent']).values.get('POOL_SLOT_DEPARTMENT_ID')==str(department) for r in self.rows()):raise Blocked('retained_department_is_slot')
        return department
    def precheck(self,run,app,retain_member=False):
        from directory_admin import DirectoryError
        owner=run['owner'];department=int(app.values['POOL_SLOT_DEPARTMENT_ID'])
        from reclaim_support import member_snapshot
        try:member=member_snapshot(self.directory,app,owner)
        except DirectoryError as exc:
            if str(exc)!='wecom_code_60111':raise
            member=None
        if member:
            managed={int(self.pool.PreparedApp(r['agent']).values['POOL_SLOT_DEPARTMENT_ID']) for r in self.rows() if self.pool.PreparedApp(r['agent']).values.get('POOL_SLOT_DEPARTMENT_ID')}
            if member.get('userid')!=owner:raise Blocked('member_identity_mismatch')
            if retain_member:
                if department not in member.get('department',[]):raise Blocked('manual_membership_requires_inspection')
                if not set(member['department'])-{department}:self.retained_department()
            elif member.get('_privileges_incomplete'):raise Blocked('member_privileges_require_inspection')
            elif not owner.startswith('xy_') or member.get('isleader') or any(member.get('is_leader_in_dept',[])):
                raise Blocked('protected_member')
            elif department not in member.get('department',[]) or not set(member['department'])<=managed:
                raise Blocked('manual_membership_requires_inspection')
        path=app.root/'data/kf.sqlite3'
        if path.exists():
            with closing(connect(path)) as db:
                for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
                    if not re.fullmatch('[A-Za-z0-9_]+',table):raise Blocked('unexpected_table_name')
                    cols={r[1] for r in db.execute('PRAGMA table_info('+table+')')}
                    if 'user' in cols and db.execute('SELECT 1 FROM '+table+" WHERE user IS NOT NULL AND user NOT IN (?, '') LIMIT 1",(owner,)).fetchone():
                        raise Blocked('unexpected_member_data')
    def complete(self,ident):
        run=dict(self.db.execute('SELECT * FROM runs WHERE id=?',(ident,)).fetchone())
        app=self.pool.PreparedApp(run['agent']);row=next(r for r in self.rows() if r['agent']==run['agent'])
        if run['phase']=='done':return
        if run['phase']=='releasing':
            self.release_slot(run)
            return
        if row['owner']!=run['owner'] or row['request_id']!=run['request']:raise Blocked('cleanup_binding_changed')
        job=self.job(row)
        retain=self.guard(run,row,job or ({'account':run['account']} if run['phase']=='unbinding' else None))
        if run['phase']=='claimed':
            self.precheck(run,app,retain_member=retain)
            self.run(['docker','stop','-t','20',app.container])
            if run['reason']=='operating_reclaim':
                import operating_controller
                operating_controller.archive_generation(self,run,app)
            if run['reason']=='manual':
                from reclaim_support import archive
                archive(self.root,run,app)
            self.archive_usage(run['agent'],ident)
            self.purge_backups(run)
            data=app.root/'data'
            if data.is_symlink() or data.resolve()!=self.root/'apps'/str(run['agent'])/'data':raise Blocked('unsafe_data_directory')
            shared=app.shared_runtime_files()
            for p in data.iterdir():
                if p.name in shared:
                    if p.is_dir():
                        # Keep the mounted cache root, erase every personal asset.
                        for child in p.iterdir():self.safe_remove(child,p)
                    continue
                self.safe_remove(p,data)
            self.phase(ident,'data_purged');run['phase']='data_purged'
        if run['phase']=='data_purged':
            outcome=self.detach_member(run,app)
            self.audit(run['agent'],'member_cleanup',outcome)
            self.phase(ident,'member_detached');run['phase']='member_detached'
        if run['phase']=='member_detached':
            values=dict(app.values,ENABLE_AI='false',XIAOYI_APP_ADMINS='poolslot'+str(run['agent']))
            atomic(app.root/'app.env',''.join(k+'='+values[k]+'\n' for k in sorted(values)).encode())
            self.write_policy(app,{'agent':run['agent'],'enabled':False,'owner':'','reason':'free'})
            if app.container in self.run(['docker','ps','-a','--format','{{.Names}}']).splitlines():
                self.run(['docker','rm','-f',app.container])
            args=json.loads((app.root/'container-run.json').read_text())
            if args[:2]!=['run','-d'] or '--name' not in args or args[args.index('--name')+1]!=app.container:raise Blocked('invalid_container_template')
            self.run(['docker',*args])
            app=self.pool.PreparedApp(run['agent'])
            # Overwrite former user's remote app name/avatar with a nonpersonal placeholder.
            app.update_profile(row['name'],default_avatar(),'image/png')
            self.phase(ident,'verifying');run['phase']='verifying'
        if run['phase']=='verifying':
            app=self.pool.PreparedApp(run['agent'])
            for attempt in range(12):
                try:app.verify(False,pristine=True);break
                except self.pool.PoolError:
                    if attempt==11:raise
                    time.sleep(3)
            # Last protection check precedes releasing the slot and clearing live site binding.
            self.guard(run,row,self.job(row))
            self.phase(ident,'unbinding');run['phase']='unbinding'
        if run['phase']=='unbinding':
            # Keep the slot unavailable until every old binding has been removed.
            if run['reason']!='operating_reclaim':
                self.scrub_backup_database(Path('/home/ubuntu/wecom-kf/portal/data/portal.sqlite3'),run)
            with closing(connect(self.root/'onboarding/jobs.sqlite3')) as db,db:
                db.execute('PRAGMA secure_delete=ON')
                if run['reason']!='operating_reclaim':
                    db.execute('DELETE FROM jobs WHERE request=? AND owner=?',(run['request'],run['owner']))
                else:
                    db.execute("UPDATE jobs SET state='retained',stage='retained',updated=? WHERE request=? AND owner=?",(self.clock(),run['request'],run['owner']))
            self.phase(ident,'releasing');run['phase']='releasing'
            self.release_slot(run)
    def release_slot(self,run):
        # A crash after pool release may allow a NEW assignment before we wake up.
        # This phase never touches files, members, jobs or site bindings again.
        registry=self.pool.Registry(self.root/'app-pool/registry.sqlite3')
        try:
            with registry.transaction():
                current=registry.get(run['agent'])
                if current['owner']==run['owner'] and current['request_id']==run['request']:
                    self.guard(run,dict(current),{'account':run['account']})
                    registry.db.execute("UPDATE apps SET state='free',owner=NULL,request_id=NULL,lease=NULL,verified=? WHERE agent=?",(self.clock(),run['agent']))
                    registry.db.execute('DELETE FROM requests WHERE agent=? AND owner=?',(run['agent'],run['owner']))
                    registry.db.execute('UPDATE events SET request_id=NULL WHERE request_id=?',(run['request'],))
                    registry.event(run['agent'],'manual_reclaimed' if run['reason']=='manual' else 'automatic_reclaimed')
        finally:registry.db.close()
        if run['reason']=='operating_reclaim':
            import operating_controller
            operating_controller.retained(self,run)
        with self.db:self.db.execute("UPDATE runs SET phase='done',owner='',request='',account='',error=NULL,updated=? WHERE id=?",(self.clock(),run['id']))
        self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)');self.db.execute('VACUUM')
        self.audit(run['agent'],'returned_to_pool',run['reason'])
    def tick(self):
        import operating_controller
        if operating_controller.enabled(self.root):return operating_controller.tick(self)
        out={'policies':0,'paused':0,'reclaimed':0,'blocked':[]}
        # Continue interrupted, already-fenced cleanups before accepting new work.
        for run in self.db.execute("SELECT id,agent FROM runs WHERE phase!='done' AND reason!='account_delete'").fetchall():
            try:self.complete(run['id']);out['reclaimed']+=1
            except Exception as exc:
                code=str(exc) if isinstance(exc,(Blocked,self.pool.PoolError)) else 'cleanup_requires_inspection'
                with self.db:self.db.execute('UPDATE runs SET error=?,updated=? WHERE id=?',(code,self.clock(),run['id']))
                out['blocked'].append({'agent':run['agent'],'error':code})
        for row in self.rows():
            if self.db.execute("SELECT 1 FROM runs WHERE agent=? AND phase!='done'",(row['agent'],)).fetchone():continue
            try:
                policy=self.policy(row);out['policies']+=1
                if policy['enabled']:
                    value=self.state(row)
                    if value['state']=='paused':out['paused']+=1
                    if value['state'] in ('expired','reclaiming'):
                        ident=self.create(row,'idle',value['last_used'])
                        if ident:self.complete(ident);out['reclaimed']+=1
                elif row['state']=='failed' and row['verified']<=self.clock()-3600:
                    app,job,eligible=self.eligible(row)
                    if eligible:
                        ident=self.create(row,'failed')
                        if ident:self.complete(ident);out['reclaimed']+=1
            except Exception as exc:
                code=str(exc) if isinstance(exc,(Blocked,self.pool.PoolError)) else 'inspection_required'
                self.audit(row['agent'],'inspection_required',code)
                out['blocked'].append({'agent':row['agent'],'error':code})
        return out

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',default='/home/ubuntu/wecom-kf/enterprises/ww0000000000000002');args=parser.parse_args()
    import fcntl
    root=Path(args.root).resolve()
    from contextlib import ExitStack
    with ExitStack() as stack:
        global_root=root.parent.parent if root.parent.name=='enterprises' else root
        for tenant in (global_root,global_root/'enterprises/ww0000000000000002',global_root/'enterprises/ww0000000000000003'):
            lock=stack.enter_context((tenant/'app-pool/operation.lock').open('a'))
            deadline=time.monotonic()+35
            while True:
                try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    if time.monotonic()>=deadline:print(json.dumps({'busy':True}));return
                    time.sleep(.2)
        controller=Controller(root)
        try:print(json.dumps(controller.tick(),ensure_ascii=False))
        finally:controller.close()

if __name__=='__main__':main()
