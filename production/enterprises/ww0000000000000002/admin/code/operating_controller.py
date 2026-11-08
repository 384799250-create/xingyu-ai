"""Host-side suspended-generation retirement, private retention, and recovery.

Called only while lifecycle_worker owns this tenant's operation.lock. Never uses
the next occupant's files when retrying a retired generation.
"""
from pathlib import Path
from contextlib import closing
from types import SimpleNamespace
import hashlib,hmac,json,os,re,secrets,shutil,sqlite3,time
import operating_fee as fee
DAY=86400

def central(root):return root.parent.parent if root.parent.name=='enterprises' else root
def ro(path):
 db=sqlite3.connect(Path(path).as_uri()+'?mode=ro',uri=True,timeout=5);db.row_factory=sqlite3.Row;return closing(db)
def book(root):
 import star_wallet
 return closing(star_wallet.connect(SimpleNamespace(root=root,directory=root/'admin/data')))
def enabled(root):
 with ro(root/'admin/data/star-wallet.sqlite3') as db:return fee.enabled(db)
def company(root):return root.name if root.parent.name=='enterprises' else 'ww0000000000000001'
def subject(root,owner):
 key=(root/'app-pool/portal-identity.key').read_bytes()
 return hmac.new(key,json.dumps([company(root),owner],separators=(',',':')).encode(),hashlib.sha256).hexdigest()
def value(root,owner,now=None):
 with book(root) as db:
  w=db.execute('SELECT id FROM wallets WHERE corp=? AND user=?',(company(root),owner)).fetchone()
  if not w:return {'operating_fee_enabled':True,'operating_phase':'active'}
  db.execute('BEGIN IMMEDIATE');fee.reconcile(db,w['id'],now);db.commit()
  return fee.summary(db,w['id'],now)
def augment_policy(ctl,row,result):
 if enabled(ctl.root) and result['enabled']:
  result['reason']='operating_fee';result['operating']=value(ctl.root,row['owner'],ctl.clock())
 return result
def freeze_record(ctl,row):
 job=ctl.job(row)
 if not job or ctl.protected(row,job):raise ValueError('protected_account')
 with book(ctl.root) as db:
  db.execute('BEGIN IMMEDIATE')
  w=db.execute('SELECT id FROM wallets WHERE corp=? AND user=?',(row['corp'],row['owner'])).fetchone()
  s=fee.state(db,w['id']) if w else None
  if not s or s['phase'] not in ('paused','reclaiming') or ctl.clock()<s['paused_at']+7*DAY:
   db.rollback();return False
  if s['phase']=='paused':
   fee.reconcile(db,w['id'],ctl.clock());s=fee.state(db,w['id'])
   if s['phase']!='paused':db.commit();return False
  db.execute("UPDATE operating_accounts SET phase='reclaiming',agent=?,request=?,account=?,updated=? WHERE wallet=?",(row['agent'],row['request_id'],int(job['account']),ctl.clock(),w['id']))
  db.commit();return True
def archive_generation(ctl,run,app):
 from reclaim_support import archive,member_snapshot
 with book(ctl.root) as db:
  w=db.execute('SELECT id FROM wallets WHERE corp=? AND user=?',(app.corp,run['owner'])).fetchone();s=fee.state(db,w['id'])
  if not s or s['phase'] not in ('reclaiming','retained'):raise ValueError('retirement_state_changed')
 path=ctl.home/'reclaimed-backups'/str(run['agent'])/run['id']
 # The member's verified phone permits same-owner recovery after the original
 # managed WeCom member has been removed. This stays in the private archive.
 if not (path/'operating-member.json').exists():
  member=member_snapshot(ctl.directory,app,run['owner'])
  if member.get('userid')!=run['owner'] or not member.get('mobile'):raise ValueError('recovery_member_unavailable')
  archive(ctl.root,run,app)
  meta=path/'operating-member.json';meta.write_text(json.dumps({'userid':run['owner'],'mobile':member['mobile'],'name':member.get('name','鏄熷笨鐢ㄦ埛')},ensure_ascii=False));meta.chmod(0o600)
 with book(ctl.root) as db,db:
  db.execute('UPDATE operating_accounts SET archive=?,updated=? WHERE wallet=?',(str(path),ctl.clock(),w['id']))
def retained(ctl,run):
 with book(ctl.root) as db,db:
  db.execute("UPDATE operating_accounts SET phase='retained',updated=? WHERE agent=? AND request=? AND phase='reclaiming'",(ctl.clock(),run['agent'],run['request']))
def archive_path(root,s):
 path=Path(s['archive']);base=root/'admin/data/reclaimed-backups'
 if not path.is_dir() or path.is_symlink() or not path.resolve().is_relative_to(base.resolve()):raise ValueError('unsafe_retained_archive')
 manifest=json.loads((path/'manifest.json').read_text())
 if manifest['owner']!=s['user'] or manifest['agent']!=s['agent'] or manifest['request']!=s['request']:raise ValueError('retained_archive_identity_changed')
 for rel,digest in manifest['files'].items():
  p=path/rel
  if p.is_symlink() or not p.resolve().is_relative_to(path.resolve()) or hashlib.sha256(p.read_bytes()).hexdigest()!=digest:raise ValueError('retained_archive_integrity_failed')
 return path
def restore_data(root,s,app):
 path=archive_path(root,s);old=s['agent'];new=app.agent;owner=s['user'];data=app.root/'data'
 identity=data.stat();source=path/'data'
 skip={'lifecycle.sqlite3','lifecycle-policy.json','token-usage.sqlite3','billing.sock'}
 for p in source.rglob('*'):
  if p.is_symlink():raise ValueError('unsafe_recovery_file')
  rel=p.relative_to(source)
  if any(part.startswith('.shared-') for part in rel.parts) or rel.name in skip or rel.name.endswith(('-wal','-shm')):continue
  dest=data/rel
  if p.is_dir():dest.mkdir(parents=True,exist_ok=True);continue
  dest.parent.mkdir(parents=True,exist_ok=True)
  if dest.exists() and dest.is_symlink():raise ValueError('unsafe_recovery_destination')
  if p.suffix=='.sqlite3':
   dest.unlink(missing_ok=True)
   with ro(p) as src,closing(sqlite3.connect(dest)) as db:
    src.backup(db);db.execute('PRAGMA foreign_keys=OFF');db.execute('BEGIN IMMEDIATE')
    for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
     if not re.fullmatch('[A-Za-z0-9_]+',table):raise ValueError('unexpected_recovery_table')
     columns={c[1] for c in db.execute('PRAGMA table_info('+table+')')}
     if 'user' in columns and db.execute('SELECT 1 FROM '+table+" WHERE user IS NOT NULL AND user NOT IN (?, '') LIMIT 1",(owner,)).fetchone():raise ValueError('unexpected_recovery_owner')
     if 'agent' in columns:db.execute('UPDATE '+table+' SET agent=? WHERE agent=?',(new,old))
     for col in columns & {'session','session_id'}:
      db.execute('UPDATE '+table+' SET '+col+'=REPLACE('+col+',?,?) WHERE '+col+' LIKE ?',('member:'+str(old)+':'+owner,'member:'+str(new)+':'+owner,'member:'+str(old)+':'+owner+'%'))
     if table in ('panel_tokens','panel_sessions'):db.execute('DELETE FROM '+table)
     if table in ('member_inbox','suite_outbox','member_greeting_outbox','suite_reminders') and 'status' in columns:
      db.execute('UPDATE '+table+" SET status='cancelled' WHERE status IN ('pending','working')")
    db.commit()
    if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('recovery_database_invalid')
  else:shutil.copyfile(p,dest)
  os.chown(dest,identity.st_uid,identity.st_gid);dest.chmod(0o600)
 for p in data.rglob('*'):
  if p.is_dir() and not p.name.startswith('.shared-'):os.chown(p,identity.st_uid,identity.st_gid)
 # Activation may start the app before the registry is committed. This gate
 # blocks all generation until the controller completes the new assignment.
 policy={'agent':new,'enabled':True,'owner':owner,'reason':'operating_fee','operating':{'operating_fee_enabled':True,'operating_phase':'restoring','operating_cycle':s['cycle'],'paused_at':s['paused_at'],'reclaim_at':s['paused_at']+7*DAY,'delete_at':s['paused_at']+15*DAY}}
 p=data/'lifecycle-policy.json';p.write_text(json.dumps(policy));os.chown(p,0,identity.st_gid);p.chmod(0o640)
def recover(ctl,s):
 from directory_admin import DirectoryError
 from reclaim_support import member_snapshot
 path=Path(s['archive'])
 member=None
 if s['restore_phase']!='cleanup':
  path=archive_path(ctl.root,s);member=json.loads((path/'operating-member.json').read_text())
 registry=ctl.pool.Registry(ctl.root/'app-pool/registry.sqlite3')
 try:
  request=s['restore_request'] or 'oprestore_'+s['cycle']
  if s['restore_agent']:
   row=registry.get(s['restore_agent'])
   if row['owner']!=s['user'] or row['request_id']!=request:raise ValueError('recovery_binding_changed')
  else:
   eligible=[]
   for candidate in ctl.pool.preferred_idle_rows(registry.status()):
    app=ctl.pool.PreparedApp(candidate['agent']);app.prepare_idle();app.verify(False,pristine=True);eligible=[app.agent];break
   if not eligible:return False
   row=registry.reserve(company(ctl.root),s['user'],request,eligible);registry.begin(request)
   with book(ctl.root) as db,db:db.execute("UPDATE operating_accounts SET restore_agent=?,restore_request=?,restore_phase='member',updated=? WHERE wallet=?",(row['agent'],request,ctl.clock(),s['wallet']))
   s.update(restore_agent=row['agent'],restore_request=request,restore_phase='member')
  app=ctl.pool.PreparedApp(row['agent']);department=int(app.values['POOL_SLOT_DEPARTMENT_ID'])
  if row['state']=='assigned':
   pass  # Crash after activation: never replay member creation or copy files.
  else:
   if s['restore_phase']=='member':
    try:present=member_snapshot(ctl.directory,app,s['user'])
    except DirectoryError as exc:
     if str(exc)!='wecom_code_60111':raise
     present=None
    if present:
     if present.get('_privileges_incomplete') or present.get('isleader') or any(present.get('is_leader_in_dept',[])):raise ValueError('recovery_member_requires_inspection')
     departments=sorted(set(present.get('department',[]))|{department})
     if department not in present.get('department',[]):ctl.directory.request('user/update',{'access_token':ctl.directory.token()},{'userid':s['user'],'department':departments})
    else:
     ctl.directory.request('user/create',{'access_token':ctl.directory.token()},{'userid':s['user'],'mobile':member['mobile'],'name':member['name'],'department':[department],'enable':1,'to_invite':False})
    if ctl.directory.members_in(department)!={s['user']}:raise ValueError('recovery_department_not_exclusive')
    with book(ctl.root) as db,db:db.execute("UPDATE operating_accounts SET restore_phase='activate' WHERE wallet=?",(s['wallet'],))
   marker=ctl.home/('operating-restore-'+s['wallet']+'.json')
   expected={'cycle':s['cycle'],'agent':app.agent,'owner':s['user'],'request':request}
   prepared=False
   if marker.exists():
    m=json.loads(marker.read_text())
    if m!=expected:raise ValueError('recovery_marker_changed')
    prepared=True
   else:
    app.bind_owner(s['user']);app.verify(False,pristine=True,slot_owner=s['user'])
    marker.write_text(json.dumps(expected));marker.chmod(0o600)
   app.bind_owner(s['user'])
   activated=False
   if app.values.get('ENABLE_AI','').lower()=='true' and app.owner==s['user']:
    try:app.verify(True);activated=True
    except Exception:pass
   if not activated:
    app.activate(recovery_loader=lambda app:restore_data(ctl.root,s,app),recovery_prepared=prepared)
   with ro(ctl.root/'onboarding/jobs.sqlite3') as db:
    profile=db.execute('SELECT * FROM jobs WHERE account=? AND owner=?',(str(s['account']),s['user'])).fetchone()
   if profile and 'display_name' in profile.keys() and profile['display_name'] and profile['avatar']:
    app.update_profile(profile['display_name'],profile['avatar'],profile['avatar_type'])
   registry.complete(app.agent,True)
  with closing(sqlite3.connect(ctl.root/'onboarding/jobs.sqlite3')) as db,db:
   count=db.execute("UPDATE jobs SET agent=?,request=?,state='ready',stage='confirmed',error=NULL,updated=? WHERE account=? AND owner=?",(app.agent,request,ctl.clock(),str(s['account']),s['user'])).rowcount
   if count!=1:raise ValueError('recovery_job_identity_changed')
  with book(ctl.root) as db,db:db.execute("UPDATE operating_accounts SET restore_phase='cleanup' WHERE wallet=?",(s['wallet'],))
  if path.exists():ctl.safe_remove(archive_path(ctl.root,s),ctl.root)
  with book(ctl.root) as db,db:
   db.execute("UPDATE operating_accounts SET phase='active',paused_at=0,cycle='',agent=NULL,request=NULL,archive='',restore_agent=NULL,restore_request='',restore_phase='',updated=? WHERE wallet=?",(ctl.clock(),s['wallet']))
  ctl.policy(dict(registry.get(app.agent)))
  # Successful recovery moves the only personal copy back to the new owner.
  (ctl.home/('operating-restore-'+s['wallet']+'.json')).unlink(missing_ok=True)
  return True
 finally:registry.db.close()
def expire(ctl,s):
 # Only the retired generation is removed. Its former slot may have a new owner.
 root=central(ctl.root);account=s['account'];owner=s['user']
 if not account or not owner.startswith('xy_'):raise ValueError('protected_account')
 for tenant in (root,root/'enterprises/ww0000000000000002',root/'enterprises/ww0000000000000003'):
  p=tenant/'admin/data/lifecycle-protected.json'
  if p.exists():
   protection=json.loads(p.read_text())
   if account in protection.get('accounts',[]) or owner in protection.get('members',[]) or (tenant==ctl.root and s['agent'] in protection.get('agents',[])):raise ValueError('protected_account')
 with ro(root/'admin/data/admin.sqlite3') as db:
  role=db.execute('SELECT role FROM roles WHERE account=?',(account,)).fetchone()
  if role and role['role']!='member':raise ValueError('protected_account')
 for tenant in (root,root/'enterprises/ww0000000000000002',root/'enterprises/ww0000000000000003'):
  with ro(tenant/'app-pool/registry.sqlite3') as db:
   if db.execute("SELECT 1 FROM apps WHERE owner=? AND state!='free'",(owner,)).fetchone():raise ValueError('member_has_live_binding')
 with ro(root/'portal/data/portal.sqlite3') as db:
  account_row=db.execute('SELECT username,subject FROM accounts WHERE id=?',(account,)).fetchone()
  if account_row and account_row['subject'] not in (None,subject(ctl.root,owner)):raise ValueError('website_identity_changed')
 ident='operating-'+s['cycle']
 with book(ctl.root) as db,db:
  db.execute('''CREATE TABLE IF NOT EXISTS wallet_closures(corp TEXT NOT NULL,user TEXT NOT NULL,closure TEXT NOT NULL,account INTEGER NOT NULL,username TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('closing','closed')),created REAL NOT NULL,updated REAL NOT NULL,PRIMARY KEY(corp,user))''')
  db.execute('INSERT OR IGNORE INTO wallet_closures VALUES(?,?,?,?,?,?,?,?)',(company(ctl.root),owner,ident,account,account_row['username'] if account_row else '', 'closing',ctl.clock(),ctl.clock()))
  existing=db.execute('SELECT closure,account FROM wallet_closures WHERE corp=? AND user=?',(company(ctl.root),owner)).fetchone()
  if tuple(existing)!=(ident,account):raise ValueError('closure_identity_changed')
  db.execute("UPDATE operating_accounts SET phase='closing',updated=? WHERE wallet=?",(ctl.clock(),s['wallet']))
  username=db.execute('SELECT username FROM wallet_closures WHERE corp=? AND user=?',(company(ctl.root),owner)).fetchone()[0]
 # Fence website login before deleting either personal copy.
 with closing(sqlite3.connect(root/'portal/data/portal.sqlite3')) as db,db:
  db.execute('INSERT OR IGNORE INTO account_closures VALUES(?,?,?,?)',(account,ident,'closing',ctl.clock()))
  db.execute('DELETE FROM sessions WHERE account=?',(account,))
 path=Path(s['archive'])
 if path.exists():
  path=archive_path(ctl.root,s);ctl.safe_remove(path,ctl.root)
 elif s['phase']!='closing':raise ValueError('missing_retained_archive')
 with closing(sqlite3.connect(ctl.root/'onboarding/jobs.sqlite3')) as db,db:
  db.execute('PRAGMA secure_delete=ON');db.execute('DELETE FROM jobs WHERE account=? AND owner=?',(str(account),owner))
 with closing(sqlite3.connect(root/'portal/data/portal.sqlite3')) as db,db:
  db.execute('PRAGMA secure_delete=ON')
  for table in ('sessions','email_codes'):db.execute('DELETE FROM '+table+' WHERE account=?',(account,))
  if account_row:
   db.execute('DELETE FROM registration_codes WHERE email=?',(account_row['username'],));db.execute('DELETE FROM accounts WHERE id=? AND username=?',(account,account_row['username']))
  db.execute("UPDATE account_closures SET state='closed' WHERE account=? AND closure=?",(account,ident))
 if username:
  import account_deletion
  account_deletion.scrub_portal_backups(SimpleNamespace(root=root),account,username)
 with book(ctl.root) as db,db:
  db.execute("UPDATE wallet_closures SET status='closed',updated=? WHERE corp=? AND user=? AND closure=?",(ctl.clock(),company(ctl.root),owner,ident))
  db.execute("UPDATE operating_accounts SET phase='closed',archive='',updated=? WHERE wallet=?",(ctl.clock(),s['wallet']))
 ctl.audit(s['agent'],'operating_account_closed','continuous_pause_15_days')
def tick(ctl):
 out={'mode':fee.VERSION,'policies':0,'paused':0,'reclaimed':0,'restored':0,'closed':0,'blocked':[]}
 # Continue only fenced generations; old idle cleanups already in progress are
 # quarantined for review rather than completing the obsolete ten-day purge.
 for run in ctl.db.execute("SELECT * FROM runs WHERE phase!='done' AND reason!='account_delete'").fetchall():
  if run['reason']=='idle':out['blocked'].append({'agent':run['agent'],'error':'old_idle_cleanup_requires_review'});continue
  try:ctl.complete(run['id'])
  except Exception:out['blocked'].append({'agent':run['agent'],'error':'recovery_requires_inspection'})
 for row in ctl.rows():
  try:
   p=ctl.policy(row);out['policies']+=1
   if not p['enabled']:continue
   v=p['operating']
   if v.get('operating_phase') in ('paused','reclaiming'):
    out['paused']+=1
    if ctl.clock()>=v.get('reclaim_at',float('inf')):
     if freeze_record(ctl,row):
      ident=ctl.create(row,'operating_reclaim');ctl.complete(ident);out['reclaimed']+=1
    elif ctl.clock()>=v.get('farewell_at',float('inf')):
     # Wake the original bound app for its one authorized free farewell.
     ctl.run(['docker','start',ctl.pool.PreparedApp(row['agent']).container])
  except Exception:out['blocked'].append({'agent':row['agent'],'error':'operating_policy_requires_inspection'})
 with book(ctl.root) as db:
  rows=[dict(r) for r in db.execute("SELECT o.*,w.corp,w.user FROM operating_accounts o JOIN wallets w ON w.id=o.wallet WHERE o.phase IN ('retained','restoring','closing')")]
 for s in rows:
  try:
   with book(ctl.root) as db:
    db.execute('BEGIN IMMEDIATE');fee.reconcile(db,s['wallet'],ctl.clock());db.commit();s.update(fee.state(db,s['wallet']))
   if s['phase']=='restoring':out['restored']+=bool(recover(ctl,s))
   elif s['phase']=='closing' or ctl.clock()>=s['paused_at']+15*DAY:expire(ctl,s);out['closed']+=1
  except Exception:out['blocked'].append({'agent':s['agent'],'error':'retained_account_requires_inspection'})
 return out
