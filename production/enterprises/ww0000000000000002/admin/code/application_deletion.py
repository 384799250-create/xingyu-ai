"""Explicit, resumable local removal after a human deletes the self-built WeCom app.

WeCom's public self-built-app API does not offer deletion or a complete enterprise
inventory. Never call an undocumented delete endpoint, or claim a failed token
proves remote deletion. Caller holds the enterprise operation.lock throughout.
"""
from contextlib import closing
import hashlib,json,os,re,secrets,sqlite3,time
from pathlib import Path

class DeletionError(Exception):pass

def db(root,write=False):
    path=Path(root)/'app-pool/registry.sqlite3'
    if path.is_symlink():raise DeletionError('应用池路径需要检查。')
    value=sqlite3.connect(str(path) if write else path.resolve().as_uri()+'?mode=ro',uri=not write,timeout=15)
    value.row_factory=sqlite3.Row
    value.execute('PRAGMA foreign_keys=ON')
    return value

def journal(connection):
    return bool(connection.execute("SELECT 1 FROM sqlite_master WHERE name='application_removals'").fetchone())

def install(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS application_removals(
      agent INTEGER PRIMARY KEY,id TEXT NOT NULL UNIQUE,corp TEXT NOT NULL,name TEXT NOT NULL,
      snapshot TEXT NOT NULL,status TEXT NOT NULL,actor TEXT NOT NULL,created REAL NOT NULL,
      updated REAL NOT NULL,confirmed_by TEXT,confirmed_at REAL)''')

def fingerprint(row):
    return hashlib.sha256(json.dumps(dict(row),sort_keys=True,separators=(',',':')).encode()).hexdigest()

def record(root,agent):
    with closing(db(root)) as connection:
        return dict(value) if journal(connection) and (value:=connection.execute('SELECT * FROM application_removals WHERE agent=?',(agent,)).fetchone()) else None

def active(root,agent):
    value=record(root,agent)
    return bool(value and value['status']!='done')

def ids(root):
    with closing(db(root)) as connection:
        return [r[0] for r in connection.execute("SELECT agent FROM application_removals WHERE status!='done'")] if journal(connection) else []

def superadmin(actor):
    if not actor or actor.get('role')!='superadmin':raise DeletionError('只有超级管理员可删除应用。')

def boundary(root,agent):
    if type(agent) is not int or not 1000003<=agent<=1999999:raise DeletionError('应用 ID 无效。')
    root=Path(root).resolve();base=root/'apps'/str(agent)
    if base.resolve()!=base or base.is_symlink():raise DeletionError('应用路径需要检查，尚未删除。')
    return base

def status(ops,agent):
    no=lambda reason:{'allowed':False,'reason':reason}
    try:
        base=boundary(ops.root,agent)
        with closing(db(ops.root)) as connection:
            row=connection.execute('SELECT * FROM apps WHERE agent=?',(agent,)).fetchone()
            saved=connection.execute('SELECT * FROM application_removals WHERE agent=?',(agent,)).fetchone() if journal(connection) else None
        if saved:
            return {'allowed':saved['status']!='done','phase':saved['status'],'binding':saved['id'],
                'reason':'已从星屿移除；企业微信删除由管理员人工确认。' if saved['status']=='done' else '已退出自动分配。请完成企业微信删除后，再确认从星屿移除。'}
        if not row or not (base/'runtime').is_dir():return no('主服务、共享应用及未接入应用池的应用不支持删除。')
        if row['state']!='free' or row['owner'] or row['request_id']:
            return no('请先使用上方“回收至应用池”，保存用户备份并解除绑定，再删除应用。')
        return {'allowed':True,'phase':'new','binding':fingerprint(row),'reason':'待分配应用可开始删除。先停止服务并退出分配，再到企业微信管理后台删除。'}
    except (OSError,sqlite3.Error,DeletionError):return no('暂时无法核对删除条件，请重新读取。')

def validate(ops,operation,agent,data,actor):
    superadmin(actor)
    required={'binding','typed_agent'}|({'wecom_confirmed'} if operation=='delete_finalize' else set())
    if set(data)!=required or data.get('typed_agent')!=str(agent):raise DeletionError('请输入正确的应用 ID 并核对确认资料。')
    value=status(ops,agent)
    if not value['allowed'] or data.get('binding')!=value.get('binding'):raise DeletionError(value['reason'] if not value['allowed'] else '应用状态已变化，请重新读取后确认。')
    if operation=='delete_finalize':
        if value['phase'] not in ('awaiting_wecom','finalizing'):raise DeletionError('请先完成停止服务并退出分配。')
        if data.get('wecom_confirmed') is not True:raise DeletionError('请先到企业微信删除对应应用，再勾选人工确认。')
    elif value['phase'] not in ('new','preparing','awaiting_wecom'):raise DeletionError('应用正在完成移除，请继续最后一步。')
    return value

def idle(ops,agent):
    # Same enterprise lock as account closure, lifecycle and provisioning.
    with closing(db(ops.root)) as connection:
        row=connection.execute('SELECT * FROM apps WHERE agent=?',(agent,)).fetchone()
        if row and (row['owner'] or row['request_id'] or row['state'] not in ('free','disabled')):
            raise DeletionError('应用已分配或正在开通，尚未删除。')
        if connection.execute("SELECT 1 FROM requests WHERE agent=? AND state IN ('reserved','provisioning','assigned')",(agent,)).fetchone():
            raise DeletionError('应用仍有分配请求，尚未删除。')
    with closing(sqlite3.connect(ops.root/'onboarding/jobs.sqlite3')) as connection:
        if connection.execute('SELECT 1 FROM jobs WHERE agent=?',(agent,)).fetchone():raise DeletionError('应用仍有关联开通记录，请先完成回收。')
    main=ops.root if (ops.root/'portal/data/portal.sqlite3').exists() and ops.root.parent.name!='enterprises' else ops.root.parent.parent
    scope='legacy' if main==ops.root else ops.root.name
    with closing(sqlite3.connect(main/'admin/data/admin.sqlite3')) as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='account_deletions'").fetchone():
            for (raw,) in connection.execute("SELECT plan FROM account_deletions WHERE status!='done'"):
                plan=json.loads(raw)
                if plan.get('enterprise','legacy')==scope and any(a['agent']==agent for a in plan.get('apps',[])):
                    raise DeletionError('此应用属于未完成的账号注销，请先处理注销流程。')
    path=ops.directory/'lifecycle-control.sqlite3'
    if path.exists():
        with closing(sqlite3.connect(path)) as connection:
            if connection.execute("SELECT 1 FROM runs WHERE agent=? AND phase!='done'",(agent,)).fetchone():raise DeletionError('应用回收尚未完成，请先核对回收进度。')
    return dict(row) if row else None

def container(ops,agent,saved):
    base=boundary(ops.root,agent)
    name=('xingyu-'+ops.root.name+'-' if ops.root.parent.name=='enterprises' else 'xiaoyi-app-')+str(agent)
    names=ops.run(['docker','ps','-a','--format','{{.Names}}'],15).splitlines()
    if name not in names:return name,False
    info=json.loads(ops.run(['docker','inspect',name],15))[0]
    mount=next((v for v in info['Mounts'] if v['Destination']=='/data'),None)
    if not mount or Path(mount['Source']).resolve()!=base/'data':raise DeletionError('应用容器数据挂载需要检查，尚未删除。')
    return name,True

def execute(ops,operation,agent,data,actor):
    validate(ops,operation,agent,data,actor);row=idle(ops,agent)
    saved=record(ops.root,agent);base=boundary(ops.root,agent)
    if operation=='delete_prepare':
        if saved is None:
            # Verify identity and isolated data before making the durable fence.
            _,env,_=ops.resolve(agent)
            values=dict(l.split('=',1) for l in env.read_text('utf8').splitlines() if l and not l.startswith('#') and '=' in l)
            if values.get('WECOM_AGENT_ID')!=str(agent) or values.get('WECOM_CORP_ID')!=row['corp']:
                raise DeletionError('应用配置归属不一致，尚未删除。')
            container(ops,agent,None)
            with closing(db(ops.root,True)) as connection,connection:
                connection.execute('BEGIN IMMEDIATE');install(connection)
                current=connection.execute('SELECT * FROM apps WHERE agent=?',(agent,)).fetchone()
                if fingerprint(current)!=data['binding']:raise DeletionError('应用归属已变化，请重新读取。')
                now=time.time()
                connection.execute('INSERT INTO application_removals VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (agent,secrets.token_hex(16),row['corp'],row['name'],json.dumps(row), 'preparing',actor['username'],now,now,None,None))
                connection.execute("UPDATE apps SET state='disabled',lease=NULL WHERE agent=?",(agent,))
            saved=record(ops.root,agent)
        name,exists=container(ops,agent,saved)
        if exists:
            ops.run(['docker','update','--restart=no',name],15)
            ops.run(['docker','stop','-t','20',name],35)
        with closing(db(ops.root,True)) as connection,connection:
            connection.execute("UPDATE application_removals SET status='awaiting_wecom',updated=? WHERE agent=?",(time.time(),agent))
        return {'deletion_phase':'awaiting_wecom','message':'已停止服务并退出分配；请到企业微信管理后台删除，再回这里人工确认。'}
    # Keep an unambiguous record of the human assertion, never a false API verification.
    with closing(db(ops.root,True)) as connection,connection:
        connection.execute("UPDATE application_removals SET status='finalizing',updated=?,confirmed_by=COALESCE(confirmed_by,?),confirmed_at=COALESCE(confirmed_at,?) WHERE agent=?",(time.time(),actor['username'],time.time(),agent))
    name,exists=container(ops,agent,saved)
    archive=ops.directory/'deleted-applications'
    if archive.resolve()!=archive or archive.is_symlink():raise DeletionError('归档路径需要检查，进度已保留。')
    archive.mkdir(mode=0o700,exist_ok=True);archive.chmod(0o700)
    target=archive/saved['id']
    if target.resolve()!=target or target.is_symlink():raise DeletionError('归档路径需要检查，进度已保留。')
    target.mkdir(mode=0o700,exist_ok=True);target.chmod(0o700)
    archived=target/'application'
    if archived.is_symlink():raise DeletionError('归档路径需要检查，进度已保留。')
    if base.exists():
        if archived.exists() or any(p.is_symlink() for p in base.rglob('*')):raise DeletionError('应用目录或归档冲突，请检查后继续。')
        if exists:ops.run(['docker','rm','-f',name],45)
        os.rename(base,archived)
    elif not archived.is_dir():raise DeletionError('找不到原应用目录或私有归档，请检查后继续。')
    elif exists:raise DeletionError('目录已归档但容器仍存在，请检查后继续。')
    with closing(db(ops.root,True)) as connection,connection:
        connection.execute('BEGIN IMMEDIATE')
        current=connection.execute('SELECT * FROM apps WHERE agent=?',(agent,)).fetchone()
        if current and (current['state']!='disabled' or current['owner'] or current['request_id']):raise DeletionError('应用归属已变化，进度已保留。')
        connection.execute('DELETE FROM requests WHERE agent=?',(agent,))
        connection.execute('DELETE FROM apps WHERE agent=?',(agent,))
        connection.execute("INSERT INTO events(at,agent,action,request_id) VALUES(?,?,'application_deleted_human_confirmed',NULL)",(time.time(),agent))
        connection.execute("UPDATE application_removals SET status='done',updated=? WHERE agent=?",(time.time(),agent))
    return {'deletion_phase':'done','message':'已从星屿移除。企业微信删除由管理员人工确认；应用目录已隔离归档，账务记录保留。'}

def listing(ops,actor):
    superadmin(actor)
    with closing(db(ops.root)) as connection:
        values=connection.execute('SELECT agent,id AS binding,corp,name,status AS phase,actor,created,updated,confirmed_by,confirmed_at FROM application_removals ORDER BY updated DESC LIMIT 100').fetchall() if journal(connection) else []
    return {'items':[dict(r) for r in values]}
