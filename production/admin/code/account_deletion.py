"""Superadmin account closure; durable identity fence and resumable app recycling."""
from contextlib import closing,contextmanager,ExitStack
import hashlib,hmac,json,os,re,secrets,sqlite3,time,subprocess,sys
from pathlib import Path

class ClosureError(Exception):pass

def connection(path):
    db=sqlite3.connect(path,timeout=15);db.row_factory=sqlite3.Row;return db

CORP='ww0000000000000002'
CORPS=(CORP,'ww0000000000000003')

def enterprise_roots(ops):
    roots=[ops.root]
    folder=ops.root/'enterprises'
    if folder.exists():
        for p in sorted(folder.iterdir()):
            if not p.is_dir():continue
            if p.name not in CORPS or p.is_symlink():raise ClosureError('发现未接入注销管理的企业，请先核对。')
            roots.append(p)
    return roots

@contextmanager
def operation_lock(ops):
    with ops.lock,ExitStack() as stack:
        for root in enterprise_roots(ops):
            stream=stack.enter_context((root/'app-pool/operation.lock').open('a'))
            if os.name!='nt':
                import fcntl
                fcntl.flock(stream,fcntl.LOCK_EX)
        yield


def enterprise_for(ops,account):
    with closing(connection(ops.portal_database)) as db:
        user=db.execute('SELECT subject FROM accounts WHERE id=?',(account,)).fetchone()
    if not user:raise ClosureError('账号不存在，或已完成注销。')
    matches=[]
    for root in enterprise_roots(ops):
        with closing(connection(root/'onboarding/jobs.sqlite3')) as db:
            found=bool(db.execute('SELECT 1 FROM jobs WHERE account=?',(str(account),)).fetchone())
        with closing(connection(root/'app-pool/registry.sqlite3')) as db:
            rows=db.execute('SELECT corp,owner FROM apps WHERE owner IS NOT NULL').fetchall()
        if user['subject']:
            key=(root/'app-pool/portal-identity.key').read_bytes()
            found=found or any(hmac.compare_digest(hmac.new(key,json.dumps([row['corp'],row['owner']],separators=(',',':')).encode(),hashlib.sha256).hexdigest(),user['subject']) for row in rows)
        if found:matches.append(root)
        config=root/'admin/data/lifecycle-protected.json'
        if config.exists() and account in json.loads(config.read_text('utf8')).get('accounts',[]):
            raise ClosureError('此账号受保护，不能注销。')
    if len(matches)>1:raise ClosureError('该账号在多个企业存在关联记录，请先核对归属；尚未注销。')
    return matches[0] if matches else ops.root


def enterprise_call(ops,root,action,**payload):
    if root not in enterprise_roots(ops) or root==ops.root:raise ClosureError('企业归属无效。')
    message=dict(payload,action=action,global_root=str(ops.root),root=str(root))
    result=subprocess.run([sys.executable,'-B',str(ops.root/'admin/code/account_deletion_scope.py')],
                          input=json.dumps(message),capture_output=True,text=True,timeout=1200 if action=='execute' else 24)
    if result.returncode:raise ClosureError('企业管理服务尚未完成处理；已保留进度，请检查后继续。')
    response=json.loads(result.stdout)
    if not response.get('ok'):raise ClosureError(response.get('error','企业归属检查失败。'))
    return response['data']


def install(ops):
    with closing(connection(ops.database)) as db,db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS account_deletions(
          id TEXT PRIMARY KEY,account INTEGER NOT NULL UNIQUE,username TEXT NOT NULL,
          actor TEXT NOT NULL,request_id TEXT NOT NULL,plan TEXT NOT NULL,
          status TEXT NOT NULL,phase TEXT NOT NULL,result TEXT NOT NULL DEFAULT '',
          created REAL NOT NULL,updated REAL NOT NULL,UNIQUE(actor,request_id));
        ''')
        db.execute("UPDATE account_deletions SET status='incomplete',result='管理服务重启，请核对后继续注销。',updated=? WHERE status IN ('pending','running')",(time.time(),))
    with closing(connection(ops.portal_database)) as db,db:
        db.executescript('''CREATE TABLE IF NOT EXISTS account_closures(
          account INTEGER PRIMARY KEY,closure TEXT NOT NULL UNIQUE,state TEXT NOT NULL,
          created REAL NOT NULL);''')

def superadmin(actor):
    if actor.get('role')!='superadmin':raise ClosureError('只有超级管理员可注销账号。')

def protection(ops,account):
    with closing(connection(ops.database)) as db:
        if db.execute("SELECT 1 FROM roles WHERE account=? AND role!='member'",(account,)).fetchone():
            raise ClosureError('此账号是管理员，受注销保护。请先核对并调整其后台权限。')
    # A manually recorded website association must retain the same protection
    # as its dedicated main app, even before settings-link confirmation.
    bindings=ops.directory/'application-bindings.json'
    if bindings.exists():
        for entry in json.loads(bindings.read_text('utf8')).values():
            reference=entry.get('website_account')
            if isinstance(reference,dict) and reference.get('id')==account:
                raise ClosureError('此账号关联人工配置的专属应用，受注销保护。')
    p=ops.directory/'lifecycle-protected.json'
    return json.loads(p.read_text('utf8')) if p.exists() else {}

def subject(ops,corp,owner):
    key=(ops.root/'app-pool/portal-identity.key').read_bytes()
    return hmac.new(key,json.dumps([corp,owner],separators=(',',':')).encode(),hashlib.sha256).hexdigest()

def fingerprint(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def controller(ops):
    from lifecycle_worker import Controller
    return Controller(ops.root)

def scan_other_data(ops,owners,allowed):
    for agent in ops.agents():
        if agent in allowed:continue
        path=ops.data_path(agent)
        if not path.exists():continue
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
            for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
                if not re.fullmatch('[A-Za-z0-9_]+',table):raise ClosureError('应用资料结构需要检查，尚未注销。')
                cols={r[1] for r in db.execute('PRAGMA table_info('+table+')')}
                if 'user' in cols and any(db.execute('SELECT 1 FROM '+table+' WHERE user=? LIMIT 1',(o,)).fetchone() for o in owners):
                    raise ClosureError('该成员在其他应用中仍有资料，请先核对归属；尚未注销。')

def member_check(ctl,owner,app):
    from reclaim_support import member_snapshot
    from directory_admin import DirectoryError
    try:member=member_snapshot(ctl.directory,app,owner)
    except DirectoryError as exc:
        if str(exc)=='wecom_code_60111':return None
        raise ClosureError('暂时无法核对企业微信成员，请稍后重新读取。') from None
    managed={int(ctl.pool.PreparedApp(r['agent']).values['POOL_SLOT_DEPARTMENT_ID']) for r in ctl.rows()
             if ctl.pool.PreparedApp(r['agent']).values.get('POOL_SLOT_DEPARTMENT_ID')}
    if (member.get('userid')!=owner or member.get('_privileges_incomplete') or
        not owner.startswith('xy_') or member.get('isleader') or any(member.get('is_leader_in_dept',[])) or
        not member.get('department') or not set(member['department'])<=managed):
        raise ClosureError('此企业微信成员属于人工配置、管理成员或保护范围，不能自动注销。')
    return member

def local_plan(ops,actor,account):
    superadmin(actor)
    if type(account) is not int or account<=0:raise ClosureError('账号编号无效。')
    if account==actor['id']:raise ClosureError('不能注销当前登录的管理员账号。')
    protected=protection(ops,account)
    if account in protected.get('accounts',[]):raise ClosureError('此账号受保护，不能注销。')
    with closing(connection(ops.portal_database)) as db:
        user=db.execute('SELECT * FROM accounts WHERE id=?',(account,)).fetchone()
        fenced=db.execute('SELECT closure FROM account_closures WHERE account=?',(account,)).fetchone()
    if not user:raise ClosureError('账号不存在，或已完成注销。')
    if fenced:raise ClosureError('该账号已进入注销流程，请在注销记录中继续。')
    with closing(connection(ops.root/'onboarding/jobs.sqlite3')) as db:
        jobs=[dict(r) for r in db.execute('SELECT * FROM jobs WHERE account=?',(str(account),))]
    if any(j['state'] in ('queued','provisioning','verifying') for j in jobs):
        raise ClosureError('该账号正在开通应用，请等开通结束后再注销。')
    rows=ops.pool();owners=set();selected={}
    for agent,row in rows.items():
        owner=row.get('owner')
        if owner and user['subject'] and subject(ops,row['corp'],owner)==user['subject']:
            owners.add(owner);selected[agent]=row
    for job in jobs:
        owner=job['owner']
        if not job['agent'] and not user['subject'] and job['state'] in ('waiting_pool','failed'):
            continue  # No application reserved, so provisioning never created a member.
        if not owner.startswith('xy_'):raise ClosureError('此账号关联人工配置成员，不能自动注销。')
        if user['subject'] and not any(subject(ops,r['corp'],owner)==user['subject'] for r in rows.values()):
            raise ClosureError('网站账号与开通记录身份不一致，请先核对。')
        owners.add(owner)
        for agent,row in rows.items():
            if row.get('owner')==owner:selected[agent]=row
    if user['subject'] and not owners:raise ClosureError('无法确定该账号的企业成员归属，请先核对。')
    if len(owners)>1:raise ClosureError('该账号对应多个企业成员，请先人工核对。')
    if owners and not selected:raise ClosureError('企业成员缺少当前应用记录，请先核对后再注销。')
    if owners & set(protected.get('members',[])):raise ClosureError('该企业微信成员受保护，不能注销。')
    ctl=controller(ops)
    apps=[];members=[];member_names={}
    try:
        from reclaim_support import status,binding
        for agent,row in sorted(selected.items()):
            if (ops.root.name not in CORPS and agent<=1000003) or agent in protected.get('agents',[]):raise ClosureError('该账号关联受保护或人工配置的应用，不能自动注销。')
            check=status(ops.root,agent)
            if not check.get('allowed') or check.get('requires_superadmin'):
                raise ClosureError('应用 '+str(agent)+' 不支持账号注销：'+check.get('reason','此应用受保护。'))
            app,job,eligible=ctl.eligible(row)
            if not eligible:raise ClosureError('应用 '+str(agent)+' 受保护，不能自动注销。')
            if str(job['account'])!=str(account):raise ClosureError('应用开通账号不一致，请先核对。')
            member=member_check(ctl,row['owner'],app)
            member_names[row['owner']]=member.get('name','') if member else ''
            ctl.precheck({'owner':row['owner']},app)
            apps.append({'agent':agent,'owner':row['owner'],'corp':row['corp'],
                         'request':row['request_id'],'binding':binding(row),'name':row.get('name','')})
        for owner in sorted(owners):
            entry=next(a for a in apps if a['owner']==owner)
            members.append({'owner':owner,'corp':entry['corp'],'agent':entry['agent'],'name':member_names.get(owner,'')})
        scan_other_data(ops,owners,set(selected))
    finally:ctl.close()
    identity={'account':account,'username':user['username'],'created':user['created'],'subject':user['subject'],
              'salt':user['salt'],'password':user['password']}
    value={'account':account,'username':user['username'],'identity_hash':fingerprint(identity),
           'apps':apps,'members':members,'jobs':[{k:j[k] for k in ('request','owner','agent','state')} for j in jobs]}
    value['binding']=fingerprint(value);return value

def plan(ops,actor,account):
    superadmin(actor)
    if type(account) is not int or account<=0:raise ClosureError('账号编号无效。')
    protection(ops,account)
    root=enterprise_for(ops,account)
    value=local_plan(ops,actor,account) if root==ops.root else enterprise_call(ops,root,'plan',actor=actor,account=account)
    value['enterprise']='legacy' if root==ops.root else root.name
    value.pop('binding',None);value['binding']=fingerprint(value)
    return value


def preview(ops,actor,payload):
    superadmin(actor)
    try:value=plan(ops,actor,payload.get('account'))
    except ClosureError as exc:return {'allowed':False,'reason':str(exc)}
    return {'allowed':True,'account':value['account'],'username':value['username'],'binding':value['binding'],
            'apps':[{'agent':a['agent'],'name':a['name'],'corp_id':a['corp']} for a in value['apps']],
            'members':[{'name':m['name'],'user_id':m['owner'],'corp_id':m['corp']} for m in value['members']],
            'member_count':len(value['members']),'reason':'注销将清理该账号的应用资料、回收应用并移出企业微信；星币账户同步注销并移出正常账户列表，余额不清零、不自动退款，历史订单与流水保留供查账。'}

def listing(ops,actor):
    superadmin(actor)
    with closing(connection(ops.database)) as db:
        rows=[dict(r) for r in db.execute('SELECT id,account,username,status,phase,result,created,updated FROM account_deletions ORDER BY created DESC LIMIT 100')]
    return {'items':rows}

def validate_request(payload):
    if not isinstance(payload,dict) or not re.fullmatch('[A-Za-z0-9_-]{16,80}',str(payload.get('request_id',''))):
        raise ClosureError('请求编号无效，请重新读取账号。')

def submit(ops,actor,payload):
    superadmin(actor);validate_request(payload)
    if set(payload)!={'account','binding','typed_username','confirm','request_id'} or payload['confirm'] is not True:
        raise ClosureError('请确认账号及注销范围。')
    with operation_lock(ops):
        with closing(connection(ops.database)) as db:
            previous=db.execute('SELECT id,account FROM account_deletions WHERE actor=? AND request_id=?',(actor['username'],payload['request_id'])).fetchone()
            if previous:
                if previous['account']!=payload['account']:raise ClosureError('请求编号已用于其他账号，请重新读取。')
                return {'closure':previous['id']}
            if db.execute("SELECT 1 FROM jobs WHERE status IN ('pending','running')").fetchone() or db.execute("SELECT 1 FROM account_deletions WHERE status IN ('pending','running')").fetchone():
                raise ClosureError('后台正在维护，请完成现有任务后再注销。')
        for root in enterprise_roots(ops)[1:]:
            with closing(connection(root/'admin/data/admin.sqlite3')) as db:
                if db.execute("SELECT 1 FROM jobs WHERE status IN ('pending','running')").fetchone():
                    raise ClosureError('其他企业正在维护，请等待现有任务完成。')
        value=plan(ops,actor,payload.get('account'))
        if payload['binding']!=value['binding']:raise ClosureError('账号或应用归属已变化，请重新读取后确认。')
        if payload['typed_username']!=value['username']:raise ClosureError('请输入完整的网站账号确认注销。')
        ident=secrets.token_hex(16);now=time.time()
        with closing(connection(ops.database)) as db,db:
            db.execute('INSERT INTO account_deletions VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (ident,value['account'],value['username'],actor['username'],payload['request_id'],json.dumps(value,ensure_ascii=False),'pending','accepted','',now,now))
        try:fence(ops,ident,value)
        except Exception:
            update(ops,ident,status='incomplete',result='登录冻结尚未确认，请核对后继续注销。')
            raise ClosureError('注销已记录，但登录冻结尚未确认。请刷新注销记录后继续。') from None
        ops.log(actor['username'],'注销账号',[a['agent'] for a in value['apps']],'已受理：'+value['username'])
    ops.executor.submit(work,ops,ident,actor['_session'])
    return {'closure':ident}

def fence(ops,ident,value):
    with closing(connection(ops.portal_database)) as db,db:
        db.execute('INSERT OR IGNORE INTO account_closures VALUES(?,?,?,?)',(value['account'],ident,'closing',time.time()))
        row=db.execute('SELECT closure FROM account_closures WHERE account=?',(value['account'],)).fetchone()
        if not row or row['closure']!=ident:raise ClosureError('账号注销记录不一致，请管理员检查。')
        db.execute('DELETE FROM sessions WHERE account=?',(value['account'],))
    # Revoke only the former owner's settings tokens, even on a retry after a slot
    # was released. Never delete tokens belonging to the next generation.
    root=ops.root if value.get('enterprise','legacy')=='legacy' else ops.root/'enterprises'/value.get('enterprise')
    if root not in enterprise_roots(ops):raise ClosureError('企业归属需要检查。')
    import wallet_closure
    wallet_closure.mark(ops,ident,value,'closing',root=root)
    for app in value['apps']:
        path=root/'apps'/str(app['agent'])/'data/kf.sqlite3'
        if not path.exists():continue
        if path.is_symlink() or not path.resolve().is_relative_to(root):raise ClosureError('设置资料路径需要检查。')
        with closing(connection(path)) as db,db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='panel_tokens'").fetchone():
                db.execute('DELETE FROM panel_tokens WHERE agent=? AND user=?',(app['agent'],app['owner']))


def update(ops,ident,**fields):
    fields['updated']=time.time()
    with closing(connection(ops.database)) as db,db:
        db.execute('UPDATE account_deletions SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',(*fields.values(),ident))

def retry(ops,actor,payload):
    superadmin(actor)
    if set(payload)!={'closure','confirm'} or payload['confirm'] is not True:raise ClosureError('请确认继续注销。')
    with operation_lock(ops),closing(connection(ops.database)) as db,db:
        row=db.execute('SELECT * FROM account_deletions WHERE id=?',(payload['closure'],)).fetchone()
        if not row:raise ClosureError('注销记录不存在。')
        if row['status']=='done':return {'closure':row['id']}
        if row['status'] in ('pending','running'):return {'closure':row['id']}
        db.execute("UPDATE account_deletions SET status='pending',result='',updated=? WHERE id=?",(time.time(),row['id']))
    ops.executor.submit(work,ops,row['id'],actor['_session'])
    return {'closure':row['id']}

def work(ops,ident,session):
    try:
        with operation_lock(ops):
            actor=ops.authorize(session,True);superadmin(actor)
            with closing(connection(ops.database)) as db:record=dict(db.execute('SELECT * FROM account_deletions WHERE id=?',(ident,)).fetchone())
            if record['status']=='done':return
            value=json.loads(record['plan']);global_protected=protection(ops,value['account'])
            if value['account'] in global_protected.get('accounts',[]):raise ClosureError('账号保护设置已变化，注销暂停。')
            if value['account']==actor['id']:raise ClosureError('当前管理员不能注销自身。')
            fence(ops,ident,value)
            update(ops,ident,status='running',result='')
            root=ops.root if value.get('enterprise','legacy')=='legacy' else ops.root/'enterprises'/value.get('enterprise')
            if root==ops.root:
                ctl=controller(ops)
                try:execute(ops,ctl,ident,value)
                finally:ctl.close()
            else:
                enterprise_call(ops,root,'execute',ident=ident,value=value)
                scrub_portal_backups(ops,value['account'],value['username'])
                update(ops,ident,status='done',phase='done',plan='{}',result='网站账号与星币账户已同步注销；关联应用已回收，企业成员已清理。余额、历史订单、流水及审计记录保留供查账。')
        ops.log(actor['username'],'注销账号',[],'完成：'+record['username'])
    except Exception as exc:
        # Never surface raw HTTP errors, credentials or filesystem details.
        error=str(exc) if isinstance(exc,ClosureError) else '注销未完成；已保留处理进度和登录冻结，请检查企业微信及应用状态后继续。'
        update(ops,ident,status='incomplete',result=error)

def execute(ops,ctl,ident,value):
    account=value['account'];protected=protection(ops,account)
    if account in protected.get('accounts',[]) or any(m['owner'] in protected.get('members',[]) for m in value['members']):
        raise ClosureError('成员保护设置已变化，注销暂停；请管理员检查。')
    if any(a['agent'] in protected.get('agents',[]) for a in value['apps']):raise ClosureError('应用保护设置已变化，注销暂停。')
    # Claim every generation before clearing any data. Durable child IDs survive a
    # crash, and completed children may already belong to a new user: never touch them.
    for app in value['apps']:
        if app.get('run'):continue
        row=next((r for r in ctl.rows() if r['agent']==app['agent']),None)
        from reclaim_support import binding
        if not row or binding(row)!=app['binding']:raise ClosureError('应用归属已变化，注销暂停，请管理员检查。')
        app['run']=ctl.create(row,'account_delete')
        update(ops,ident,plan=json.dumps(value,ensure_ascii=False))
    # Stop all original user apps before clearing the first one.
    for app in value['apps']:
        run=ctl.db.execute('SELECT * FROM runs WHERE id=?',(app['run'],)).fetchone()
        if run and run['phase'] in ('done','releasing'):continue
        row=next((r for r in ctl.rows() if r['agent']==app['agent']),None)
        if not row or row['owner']!=app['owner'] or row['request_id']!=app['request']:
            raise ClosureError('应用归属已变化，注销暂停。')
        ctl.run(['docker','stop','-t','20',ctl.pool.PreparedApp(app['agent']).container])
    update(ops,ident,phase='recycling')
    for app in value['apps']:
        # Also remove old manual-reclaim archives and the top-level DB snapshots
        # used by deployments, while the original generation still owns the slot.
        # A completed/releasing child can already have a NEW owner: never purge
        # that slot's backups or files on a parent retry.
        run=ctl.db.execute('SELECT phase FROM runs WHERE id=?',(app['run'],)).fetchone()
        if run and run['phase']=='claimed':
            archived=ops.directory/'reclaimed-backups'/str(app['agent'])
            if archived.exists():ctl.safe_remove(archived,ops.root)
            for backup in ops.root.glob('deploy-backup-*'):
                saved=backup/(str(app['agent'])+'.sqlite3')
                if saved.exists():ctl.safe_remove(saved,ops.root)
        ctl.complete(app['run'])
    update(ops,ident,phase='member')
    for member in value['members']:
        owner=member['owner']
        if ctl.has_other_data(owner,-1):raise ClosureError('成员仍有其他应用或资料，注销暂停，请核对后继续。')
        m=member_check(ctl,owner,ctl.pool.PreparedApp(member['agent']))
        if m:
            ctl.directory.request('user/delete',{'access_token':ctl.directory.token(),'userid':owner})
        # A timeout leaves this phase incomplete; retry rechecks actual membership.
    import wallet_closure
    wallet_closure.mark(ops,ident,value,'closed')
    update(ops,ident,phase='website')
    with closing(connection(ops.root/'onboarding/jobs.sqlite3')) as db,db:
        db.execute('PRAGMA secure_delete=ON')
        db.execute('DELETE FROM jobs WHERE account=?',(str(account),))
    with closing(connection(ops.portal_database)) as db,db:
        db.execute('PRAGMA secure_delete=ON')
        for table in ('sessions','email_codes'):
            db.execute('DELETE FROM '+table+' WHERE account=?',(account,))
        user=db.execute('SELECT username,email FROM accounts WHERE id=?',(account,)).fetchone()
        if user:
            if user['username']!=value['username']:raise ClosureError('网站账号身份变化，注销暂停。')
            db.execute('DELETE FROM registration_codes WHERE email IN (?,?)',(user['username'],user['email']))
            db.execute('DELETE FROM accounts WHERE id=?',(account,))
        db.execute("UPDATE account_closures SET state='closed' WHERE account=? AND closure=?",(account,ident))
    # Keep the immutable audit/transaction ledgers. No personal app recovery archive
    # is created for account deletion, unlike manual app recycling.
    scrub_portal_backups(ops,account,value['username'])
    update(ops,ident,status='running' if getattr(ops,'scoped',False) else 'done',phase='website' if getattr(ops,'scoped',False) else 'done',plan=json.dumps(value,ensure_ascii=False) if getattr(ops,'scoped',False) else '{}',result='网站账号与星币账户已同步注销；关联应用已回收，企业成员已清理。余额、历史订单、流水及审计记录保留供查账。')

def scrub_portal_backups(ops,account,username):
    candidates=set()
    for parent in [p for p in ops.root.glob('deploy-backup-*') if p.is_dir()]+[p for p in (ops.root/'portal').glob('backup-*') if p.is_dir()]:
        candidates.update(parent.rglob('portal.sqlite3'))
    for p in candidates:
        if p.is_symlink() or not p.resolve().is_relative_to(ops.root):raise ClosureError('备份路径需要检查，注销尚未完成。')
        with closing(connection(p)) as db:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'accounts' not in tables:continue
            user=db.execute('SELECT id FROM accounts WHERE id=? AND username=?',(account,username)).fetchone()
            if not user:continue
            db.execute('PRAGMA secure_delete=ON')
            with db:
                for table in ('sessions','email_codes'):
                    if table in tables:db.execute('DELETE FROM '+table+' WHERE account=?',(account,))
                db.execute('DELETE FROM accounts WHERE id=? AND username=?',(account,username))
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)');db.execute('VACUUM')
