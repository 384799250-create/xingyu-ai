"""Read-only reclamation eligibility and private, generation-scoped recovery archives."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import time
import urllib.request
import urllib.parse

class ArchiveError(Exception):pass

def ro(path):
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=10)
    db.row_factory=sqlite3.Row
    return db

def binding(row):
    return hashlib.sha256(json.dumps([row['agent'],row['corp'],row['owner'],row['request_id']],separators=(',',':')).encode()).hexdigest()

def member_snapshot(directory,app,owner):
    """Contact-sync tokens may list IDs and write members, but cannot read user/get (48009).
    Use the app token for readable privilege fields and the contact API for complete memberships.
    Never interpret the app's visibility-filtered department list as a complete list.
    """
    from directory_admin import DirectoryError
    try:return directory.request('user/get',{'access_token':directory.token(),'userid':owner})
    except DirectoryError as exc:
        if str(exc)!='wecom_code_48009':raise
    departments=set();cursor='';seen=set()
    for _ in range(100):
        value=directory.request('user/list_id',{'access_token':directory.token()},{'limit':10000,'cursor':cursor})
        rows=value.get('dept_user')
        if not isinstance(rows,list):raise DirectoryError('member_response_invalid')
        for row in rows:
            if row.get('userid')==owner:
                if type(row.get('department')) is not int:raise DirectoryError('member_response_invalid')
                departments.add(row['department'])
        cursor=value.get('next_cursor','')
        if not cursor:break
        if cursor in seen:raise DirectoryError('pagination_loop')
        seen.add(cursor)
    else:raise DirectoryError('pagination_limit')
    if not departments:raise DirectoryError('wecom_code_60111')
    def api(endpoint,params):
        try:
            url='https://qyapi.weixin.qq.com/cgi-bin/'+endpoint+'?'+urllib.parse.urlencode(params)
            with urllib.request.urlopen(url,timeout=15) as response:value=json.load(response)
            if value.get('errcode',0):raise DirectoryError('wecom_code_'+str(int(value['errcode'])))
            return value
        except DirectoryError:raise
        except Exception:raise DirectoryError('member_read_failed') from None
    token=api('gettoken',{'corpid':app.values['WECOM_CORP_ID'],'corpsecret':app.values['WECOM_APP_SECRET']}).get('access_token')
    if not token:raise DirectoryError('token_missing')
    try:member=api('user/get',{'access_token':token,'userid':owner})
    except DirectoryError as exc:
        if str(exc)!='wecom_code_60111' or int(app.values['POOL_SLOT_DEPARTMENT_ID']) in departments:raise
        return {'userid':owner,'department':sorted(departments),'_privileges_incomplete':True}
    if member.get('userid')!=owner:raise DirectoryError('member_identity_mismatch')
    visible=member.get('department',[]);leaders=member.get('is_leader_in_dept',[])
    if set(visible)!=departments or len(leaders)!=len(visible):
        member.pop('is_leader_in_dept',None);member['_privileges_incomplete']=True
    else:
        flags=dict(zip(visible,leaders));member['is_leader_in_dept']=[flags[d] for d in sorted(departments)]
    member['department']=sorted(departments)
    return member

def status(root,agent):
    root=Path(root).resolve()
    def no(reason):return {'allowed':False,'reason':reason}
    if type(agent) is not int or agent<=1000003:return no('此应用使用主服务部署，尚未接入应用池，不能直接回收。')
    try:
        with closing(ro(root/'app-pool/registry.sqlite3')) as db:
            row=db.execute('SELECT * FROM apps WHERE agent=?',(agent,)).fetchone()
        if not row:return no('应用不在自动分配池中。')
        if row['state']=='free':return no('应用已在应用池中，等待分配。')
        journal=root/'admin/data/lifecycle-control.sqlite3'
        if journal.exists():
            with closing(ro(journal)) as db:
                if db.execute("SELECT 1 FROM runs WHERE agent=? AND phase!='done'",(agent,)).fetchone():return no('应用正在回收或等待检查，请勿重复操作。')
        if row['state'] not in ('assigned','failed','disabled'):return no('应用正在开通，请等待开通流程结束。')
        if not row['owner']:return no('应用归属缺失，请先核对。')
        protected=not row['owner'].startswith('xy_') or not row['request_id']
        with closing(ro(root/'onboarding/jobs.sqlite3')) as db:
            job=db.execute('SELECT account FROM jobs WHERE agent=? AND request=? AND owner=?',(agent,row['request_id'],row['owner'])).fetchone()
        account=None
        if job:
            try:account=int(job['account'])
            except (TypeError,ValueError):return no('开通账号需要检查。')
            with closing(ro(root/'admin/data/admin.sqlite3')) as db:
                role=db.execute('SELECT role FROM roles WHERE account=?',(account,)).fetchone()
            if role and role['role']!='member':protected=True
        elif row['owner'].startswith('xy_'):return no('找不到当前开通记录，请先核对应用归属。')
        else:protected=True
        protection=root/'admin/data/lifecycle-protected.json'
        if protection.exists():
            value=json.loads(protection.read_text('utf8'))
            if agent in value.get('agents',[]) or row['owner'] in value.get('members',[]) or account in value.get('accounts',[]):protected=True
        env=root/'apps'/str(agent)/'app.env'
        if env.is_symlink():return no('应用配置路径需要检查。')
        values=dict(line.split('=',1) for line in env.read_text('utf8').splitlines() if '=' in line and not line.startswith('#'))
        department=values.get('POOL_SLOT_DEPARTMENT_ID','')
        if not department.isdigit() or int(department)<=0:return no('应用尚未准备独立分配部门，请先完成应用池配置。')
        return {'allowed':True,'requires_superadmin':protected,'preserve_member':protected,
            'reason':'自动回收仍受保护；仅超级管理员可手动回收。回收保留企业成员及其他应用的数据和绑定。' if protected else '回收后解除当前应用绑定，应用恢复为待分配。',
            'binding':binding(row)}
    except (OSError,sqlite3.Error,ValueError,TypeError,KeyError):return no('暂时无法核对回收条件，请重新读取或检查开通记录。')

def archive(root,run,app):
    """Called only after the app stops. Keep this archive outside ordinary restore paths."""
    root=Path(root).resolve();agent=run['agent'];ident=run['id']
    if type(agent) is not int or agent<=1000003 or not re.fullmatch('[a-f0-9]{32}',ident):raise ArchiveError('invalid_archive_identity')
    base=root/'admin/data/reclaimed-backups'/str(agent)
    if base.resolve()!=base:raise ArchiveError('unsafe_archive_path')
    base.mkdir(parents=True,exist_ok=True,mode=0o700)
    # Every ancestor beneath admin/data is private, including one created by mkdir(parents=True).
    (base.parent).chmod(0o700);base.chmod(0o700)
    target=base/ident;stage=base/(ident+'.pending')
    expected={'id':ident,'agent':agent,'owner':run['owner'],'request':run['request']}
    if target.exists():
        if target.is_symlink():raise ArchiveError('unsafe_archive_path')
        manifest=json.loads((target/'manifest.json').read_text('utf8'))
        if any(manifest.get(k)!=v for k,v in expected.items()):raise ArchiveError('archive_binding_mismatch')
        for name,digest in manifest['files'].items():
            path=target/name
            if path.is_symlink() or not path.resolve().is_relative_to(target) or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise ArchiveError('archive_integrity_failed')
        return ident
    if stage.exists():
        if stage.is_symlink() or any(p.is_symlink() for p in stage.rglob('*')):raise ArchiveError('unsafe_archive_path')
        shutil.rmtree(stage)
    stage.mkdir(mode=0o700)
    try:
        source=app.root/'data'
        if source.is_symlink() or source.resolve()!=root/'apps'/str(agent)/'data':raise ArchiveError('unsafe_data_directory')
        shared=app.shared_runtime_files()
        files=[]
        for p in source.rglob('*'):
            if p.parent==source and p.name in shared and p.is_socket():continue
            if p.parent==source and p.name=='billing.sock':
                if (p.is_symlink() or not p.is_socket() or p.stat().st_uid!=source.stat().st_uid
                        or p.stat().st_mode & 0o777!=0o600):raise ArchiveError('unsafe_billing_socket')
                continue
            files.append((p,Path('data')/p.relative_to(source)))
        files.extend((app.root/name,Path(name)) for name in ('app.env','container-run.json') if (app.root/name).exists())
        files.extend((p,Path('runtime')/p.name) for p in (app.root/'runtime').glob('*.py'))
        for original,relative in files:
            mode=original.lstat().st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):raise ArchiveError('unsafe_archive_source')
            if not original.resolve().is_relative_to(app.root.resolve()):raise ArchiveError('unsafe_archive_source')
            destination=stage/relative
            destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            if original.is_dir():destination.mkdir(exist_ok=True,mode=0o700);continue
            if original.name.endswith(('.sqlite3-wal','.sqlite3-shm')):continue
            if original.suffix=='.sqlite3':
                with closing(ro(original)) as src,closing(sqlite3.connect(destination)) as dst:
                    src.backup(dst)
                    if dst.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ArchiveError('archive_database_invalid')
            else:shutil.copyfile(original,destination)
            destination.chmod(0o600)
        hashes={str(p.relative_to(stage)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in stage.rglob('*') if p.is_file()}
        for p in stage.rglob('*'):
            if p.is_dir():p.chmod(0o700)
        manifest=dict(expected,created=time.time(),reason='manual_reclaim',files=hashes)
        path=stage/'manifest.json'
        with path.open('w',encoding='utf8') as stream:json.dump(manifest,stream,ensure_ascii=False);stream.flush();os.fsync(stream.fileno())
        path.chmod(0o600);os.replace(stage,target)
        return ident
    except BaseException:
        # Preserve an incomplete snapshot for inspection; the next attempt replaces only this staging directory.
        raise
