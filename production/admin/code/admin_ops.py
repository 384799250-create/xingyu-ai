"""Allowlisted host operations for Xiaoyi. No command strings or arbitrary paths from clients."""
from contextlib import closing
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
import global_defaults
import application_deletion
import application_names
from urllib.parse import urlsplit

OPTIONS = {'min_interval': (0,1440,120), 'proactive_max_unanswered': (0,8,2),
           'proactive_rest_hours': (1,168,8), 'quiet_start': (0,23,23), 'quiet_end': (0,23,8)}
BOOL_OPTIONS = {'proactive':False,'split_reply':True,'merge_messages':False,'weather':False}
ENV_FIELDS = {'model':'DEEPSEEK_MODEL', 'base_url':'DEEPSEEK_BASE_URL', 'api_key':'DEEPSEEK_API_KEY', 'ai_enabled':'ENABLE_AI'}
SOURCE_NAMES = ('suite_app.py', 'companion_app.py', 'reply_pipeline.py', 'token_meter.py', 'lifecycle.py', 'autonomous_media.py', 'memory_engine.py')

def source_names(agent):
    return SOURCE_NAMES
VERSION = 'common-v2-20261102.6'

class Rejected(Exception):
    pass

def connect(path, ro=False):
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro' if ro else str(path),uri=ro,timeout=10)
    db.row_factory=sqlite3.Row
    return db

def read_env(path):
    return dict(line.split('=',1) for line in path.read_text('utf8').splitlines() if line and not line.lstrip().startswith('#') and '=' in line)

def atomic(path, data, mode=None):
    tmp=path.with_name(path.name+'.admin-'+secrets.token_hex(5))
    previous=path.stat() if path.exists() else None
    try:
        fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,mode or (previous.st_mode&0o777 if previous else 0o600))
        with os.fdopen(fd,'wb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        if previous and hasattr(os,'chown'):os.chown(tmp,previous.st_uid,previous.st_gid)
        os.replace(tmp,path)
    finally:
        if tmp.exists():tmp.unlink()

class AdminOps:
    def __init__(self, root, portal_database=None):
        self.root=Path(root).resolve()
        self.directory=self.root/'admin/data';self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.database=self.directory/'admin.sqlite3'
        self.portal_database=Path(portal_database or self.root/'portal/data/portal.sqlite3')
        self.lock=threading.RLock()
        self.executor=concurrent.futures.ThreadPoolExecutor(max_workers=1,thread_name_prefix='xiaoyi-admin')
        with closing(connect(self.database)) as db,db:
            db.executescript('''
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS roles(account INTEGER PRIMARY KEY,role TEXT NOT NULL,created REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,actor TEXT NOT NULL,operation TEXT NOT NULL,
                agents TEXT NOT NULL,result TEXT NOT NULL,created REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,actor TEXT NOT NULL,operation TEXT NOT NULL,
                agents TEXT NOT NULL,status TEXT NOT NULL,result TEXT,created REAL NOT NULL,updated REAL NOT NULL,
                request_id TEXT NOT NULL,UNIQUE(actor,request_id));
            ''')
            db.execute("UPDATE jobs SET status='interrupted',result=?,updated=? WHERE status IN ('pending','running')",
                (json.dumps({'message':'管理服务重启，操作结果需要核对，未自动重试。'},ensure_ascii=False),time.time()))
        os.chmod(self.database,0o600)
        import account_deletion
        account_deletion.install(self)

    def run(self,args,timeout=45):
        try:
            p=subprocess.run(args,capture_output=True,text=True,encoding='utf8',errors='replace',timeout=timeout)
        except subprocess.TimeoutExpired:
            raise Rejected('操作超时，可能已执行，请刷新状态后核对。') from None
        if p.returncode:raise Rejected('服务器操作未完成；原始凭证与详细命令不会显示。')
        return p.stdout

    def agents(self):
        values={1000003}
        registry=self.root/'app-pool/registry.sqlite3'
        if registry.is_file():
            with closing(connect(registry,True)) as db:
                values.update(r[0] for r in db.execute('SELECT agent FROM apps') if type(r[0]) is int and 1000004<=r[0]<=1999999)
        return sorted(a for a in values if a==1000003 or (self.root/'apps'/str(a)/'runtime').is_dir())

    def resolve(self,agent):
        if type(agent) is not int or agent not in self.agents():raise Rejected('应用不存在。')
        runtime=self.root if agent==1000003 else self.root/'apps'/str(agent)/'runtime'
        env=self.root/'.env' if agent==1000003 else runtime.parent/'app.env'
        for p in (runtime,env):
            if p.is_symlink() or not p.resolve().is_relative_to(self.root):raise Rejected('应用路径校验失败。')
        return runtime,env,'wecom-kf-kf-assistant-1' if agent==1000003 else 'xiaoyi-app-'+str(agent)

    def info(self,agent):
        _,_,container=self.resolve(agent)
        try:return json.loads(self.run(['docker','inspect',container],10))[0]
        except (ValueError,IndexError):raise Rejected('无法读取应用容器。') from None

    def data_path(self,agent,info=None):
        if info is None:info=self.info(agent)
        mount=next((m for m in info['Mounts'] if m['Destination']=='/data'),None)
        if not mount:raise Rejected('数据挂载不存在。')
        root=Path(mount['Source']).resolve()
        # The only accepted external path is Docker's named data volume.
        if not root.is_relative_to(self.root) and not root.is_relative_to(Path('/var/lib/docker/volumes')):
            raise Rejected('数据挂载不在允许范围内。')
        return root/'kf.sqlite3'

    def identity(self,token):
        if not isinstance(token,str) or not re.fullmatch('[a-f0-9]{64}',token):raise Rejected('请先登录管理账号。')
        with closing(connect(self.portal_database,True)) as db:
            user=db.execute('SELECT a.id,a.username FROM sessions s JOIN accounts a ON a.id=s.account WHERE s.hash=? AND s.expires>?',
                (token,time.time())).fetchone()
        if not user:raise Rejected('登录已过期，请重新登录。')
        with closing(connect(self.portal_database,True)) as db:
            if db.execute('SELECT 1 FROM account_closures WHERE account=?',(user['id'],)).fetchone():
                raise Rejected('该账号正在注销，无法登录。')
        with closing(connect(self.database,True)) as db:
            role=db.execute('SELECT role FROM roles WHERE account=?',(user['id'],)).fetchone()
        return dict(user,role=role[0] if role else None)

    def authorize(self,token,write=False):
        actor=self.identity(token)
        if actor['role'] not in ('superadmin','operator','viewer'):raise Rejected('此账号没有总后台权限。')
        if write and actor['role'] not in ('superadmin','operator'):raise Rejected('只读管理员不能修改应用。')
        actor['_session']=token
        return actor

    def log(self,actor,operation,agents,result):
        with closing(connect(self.database)) as db,db:
            db.execute('INSERT INTO audit(actor,operation,agents,result,created) VALUES(?,?,?,?,?)',
                (actor,operation,json.dumps(agents),result,time.time()))

    def pool(self):
        p=self.root/'app-pool/registry.sqlite3'
        if not p.exists():return {}
        with closing(connect(p,True)) as db:
            rows={r['agent']:dict(r) for r in db.execute('SELECT agent,corp,name,state,owner,request_id FROM apps')}
        binding=self.directory/'application-bindings.json'
        if binding.exists():
            values=json.loads(binding.read_text('utf8'))
            for key,value in values.items():
                if key!='1000003' or value.get('agent')!=1000003 or not value.get('owner'):raise Rejected('人工应用归属配置无效。')
                rows[1000003]={'agent':1000003,'name':value['name'],'state':'assigned','owner':value['owner'],'owner_name':value.get('owner_name',''),
                    'corp':read_env(self.root/'.env').get('WECOM_CORP_ID','')}
        self.owner_labels(rows)
        return rows

    def owner_labels(self,rows):
        # Presentation only: the registry owner remains the actual WeCom UserID.
        import hmac
        names={(r['corp'],r['owner']):r['owner_name'] for r in rows.values() if r.get('corp') and r.get('owner') and r.get('owner_name')}
        accounts={};subjects={};jobs={}
        if self.portal_database.is_file():
            with closing(connect(self.portal_database,True)) as db:
                for account in db.execute('SELECT id,username,subject FROM accounts'):
                    accounts[str(account['id'])]=dict(account)
                    if account['subject']:subjects[account['subject']]=dict(account)
        path=self.root/'onboarding/jobs.sqlite3'
        if path.is_file():
            with closing(connect(path,True)) as db:
                for job in db.execute("SELECT account,request,owner,agent FROM jobs WHERE state IN ('awaiting_scan','ready')"):
                    jobs[(job['agent'],job['owner'],job['request'])]=str(job['account'])
        keypath=self.root/'app-pool/portal-identity.key'
        key=keypath.read_bytes() if keypath.is_file() else None
        for row in rows.values():
            owner=row.get('owner');account=None
            if owner and row.get('corp') and key:
                subject=hmac.new(key,json.dumps([row['corp'],owner],separators=(',',':')).encode(),hashlib.sha256).hexdigest()
                account=subjects.get(subject)
            if owner and account is None and row.get('state')=='assigned' and row.get('request_id'):
                # Before settings-link confirmation, require the current allocation generation.
                candidate=accounts.get(jobs.get((row['agent'],owner,row['request_id'])))
                if candidate and not candidate['subject']:account=candidate
            row['owner_name']=(row.get('owner_name') or names.get((row.get('corp'),owner),'')) if owner else ''
            row['owner_account']=account['username'] if account else ''
            row['owner_display']=(row['owner_name'] or row['owner_account'] or '已绑定成员') if owner else ''

    def accounts(self):
        import account_listing
        return account_listing.listing(self)

    def lifecycle_status(self,agent):
        journal=self.directory/'lifecycle-control.sqlite3'
        if journal.exists():
            with closing(connect(journal,True)) as db:
                if db.execute("SELECT 1 FROM runs WHERE agent=? AND phase!='done'",(agent,)).fetchone():return {'enabled':True,'state':'reclaiming'}
        if agent==1000003:return {'enabled':False,'state':'protected'}
        data=self.root/'apps'/str(agent)/'data';policy=data/'lifecycle-policy.json'
        if not policy.exists():return {'enabled':False,'state':'protected'}
        p=json.loads(policy.read_text())
        if not p.get('enabled'):return {'enabled':False,'state':p.get('reason','protected')}
        last=p.get('baseline',time.time());state='active'
        if (data/'lifecycle.sqlite3').exists():
            with closing(connect(data/'lifecycle.sqlite3',True)) as db:
                row=db.execute('SELECT owner,last_used,state FROM lifecycle_state WHERE agent=?',(agent,)).fetchone()
                if row:
                    if row['owner']!=p.get('owner'):return {'enabled':True,'state':'inspection_required'}
                    last=row['last_used'];state=row['state']
        if state!='reclaiming':state='expired' if time.time()>=last+10*86400 else 'paused' if time.time()>=last+7*86400 else state
        return {'enabled':True,'state':state,'last_used':last,'pause_at':last+7*86400,'delete_at':last+10*86400}

    def overview(self):
        pool=self.pool();out=[];agents=self.agents()
        containers={agent:self.resolve(agent)[2] for agent in agents};infos=None
        if containers:
            try:
                batch=json.loads(self.run(['docker','inspect',*containers.values()],10))
                if not isinstance(batch,list) or not all(isinstance(item,dict) and isinstance(item.get('Name'),str) for item in batch):raise ValueError('invalid_container_report')
                infos={item['Name'].lstrip('/'):item for item in batch}
            except (Rejected,OSError,ValueError):pass
        for agent in agents:
            runtime,env,_=self.resolve(agent)
            entry={'agent':agent,'name':pool.get(agent,{}).get('name','共享应用'),
                'pool_state':pool.get(agent,{}).get('state','shared'),'owner':pool.get(agent,{}).get('owner') or '',
                'owner_display':pool.get(agent,{}).get('owner_display',''),'owner_name':pool.get(agent,{}).get('owner_name',''),
                'owner_account':pool.get(agent,{}).get('owner_account',''),
                'version':None,'running':False,'healthy':False,'queue':{},'error_count':0}
            try:
                info=self.info(agent) if infos is None else infos.get(containers[agent])
                if info is None:raise Rejected('无法读取应用容器。')
                config=read_env(env)
                entry.update(running=bool(info['State']['Running']),ai_enabled=config.get('ENABLE_AI','false').lower()=='true',
                    model=config.get('DEEPSEEK_MODEL',''),health=info['State'].get('Health',{}).get('Status','unknown'))
                entry['healthy']=entry['running'] and entry['health'] in ('healthy','unknown')
                source=(runtime/'suite_app.py').read_text('utf8')
                entry['version']=VERSION if 'media_meter_version=1' in source else ('adopted-admin-20261028.2' if 'token_meter_version=1' in source else ('adopted-admin-20261028.1' if 'management_defaults_version=1' in source else ('adopted-2' if 'adopted_reply_version=2' in source else 'legacy')))
                entry['lifecycle']=self.lifecycle_status(agent)
                path=self.data_path(agent,info)
                if path.exists():
                    with closing(connect(path,True)) as db:
                        entry['name']=application_names.display_name(db,agent,entry['owner'],entry['name'])
                        entry['queue']={r[0]:r[1] for r in db.execute('SELECT status,COUNT(*) FROM member_inbox GROUP BY status')}
                        entry['error_count']=db.execute("SELECT COUNT(*) FROM member_inbox WHERE status='failed'").fetchone()[0]
            except (Rejected,sqlite3.Error,OSError,ValueError):entry['unavailable']=True
            out.append(entry)
        return {'items':out,'release':VERSION,'timestamp':time.time()}

    def settings(self,agent):
        _,env,_=self.resolve(agent);v=read_env(env)
        options=dict(global_defaults.FACTORY)
        path=self.data_path(agent)
        if path.exists():
            with closing(connect(path,True)) as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='suite_application_defaults'").fetchone():
                    row=db.execute('SELECT value FROM suite_application_defaults WHERE agent=?',(agent,)).fetchone()
                    if row:options.update(json.loads(row[0]))
        ownership=self.pool().get(agent,{})
        return {'ownership':{key:ownership.get(key,'') or '' for key in ('owner','owner_display','owner_name','owner_account','state')},
            'agent':agent,'model':v.get('DEEPSEEK_MODEL',''),'base_url':v.get('DEEPSEEK_BASE_URL',''),
            'api_key_configured':bool(v.get('DEEPSEEK_API_KEY')),'ai_enabled':v.get('ENABLE_AI','false').lower()=='true','options':options,
            'version':VERSION,'lifecycle':self.lifecycle_status(agent),'reclaim':self.reclaim_status(agent),'backups':self.backups(agent),'events':self.events(agent),
            **global_defaults.application_schema(self,agent,options)}

    def events(self,agent):
        path=self.data_path(agent)
        if not path.exists():return []
        with closing(connect(path,True)) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='companion_events'").fetchone():return []
            # Status-only log: do not send chat bodies or exception details/keys to the browser.
            return [dict(r) for r in db.execute('SELECT status,COUNT(*) AS count,MAX(updated) AS latest FROM companion_events GROUP BY status')]

    def backups(self,agent):
        folder=self.directory/'backups'/str(agent)
        if not folder.exists():return []
        out=[]
        for p in sorted(folder.glob('*/manifest.json'),reverse=True)[:30]:
            if not p.parent.is_symlink():
                try:
                    m=json.loads(p.read_text());out.append({'id':p.parent.name,'created':m['created'],'reason':m['reason']})
                except (OSError,ValueError,KeyError):pass
        return out

    def backup(self,agent,reason):
        runtime,env,_=self.resolve(agent)
        ident=time.strftime('%Y%m%d-%H%M%S',time.gmtime())+'-'+secrets.token_hex(4)
        target=self.directory/'backups'/str(agent)/ident;target.mkdir(parents=True,mode=0o700)
        try:
            for name in source_names(agent):
                p=runtime/name
                if p.exists():shutil.copyfile(p,target/name);os.chmod(target/name,0o600)
            if env.exists():shutil.copyfile(env,target/'config.env');os.chmod(target/'config.env',0o600)
            source=self.data_path(agent)
            if source.exists():
                with closing(connect(source,True)) as src,closing(connect(target/'database.sqlite3')) as dst:
                    src.backup(dst)
                    if dst.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise Rejected('数据库备份校验失败。')
                os.chmod(target/'database.sqlite3',0o600)
            usage=source.with_name('token-usage.sqlite3')
            if usage.is_file():
                with closing(connect(usage,True)) as src,closing(connect(target/'token-usage.sqlite3')) as dst:
                    src.backup(dst)
                    if dst.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise Rejected('用量账本备份校验失败。')
                os.chmod(target/'token-usage.sqlite3',0o600)
            tracker=source.with_name('lifecycle.sqlite3')
            if tracker.is_file():
                with closing(connect(tracker,True)) as src,closing(connect(target/'lifecycle.sqlite3')) as dst:src.backup(dst)
                os.chmod(target/'lifecycle.sqlite3',0o600)
            manifest={'agent':agent,'created':time.time(),'reason':reason,
                'files':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in target.iterdir() if p.is_file()}}
            atomic(target/'manifest.json',json.dumps(manifest).encode())
            return ident
        except BaseException:
            shutil.rmtree(target);raise

    def wait_health(self,agent):
        _,_,container=self.resolve(agent)
        for attempt in range(12):
            try:
                code="import urllib.request,json;d=json.load(urllib.request.urlopen('http://127.0.0.1:8088/health',timeout=2));assert d.get('ok') and d.get('agent_id')=="+str(agent)
                self.run(['docker','exec',container,'python','-c',code],5);return
            except Rejected:time.sleep(.5)
        raise Rejected('应用未通过启动检查，请核对状态或从备份恢复。')

    def change_env(self,agent,fields):
        _,path,_=self.resolve(agent)
        text=path.read_text('utf8');lines=text.splitlines();seen=set();output=[]
        for line in lines:
            key=line.partition('=')[0]
            if key in fields:
                if key not in seen:output.append(key+'='+fields[key]);seen.add(key)
            else:output.append(line)
        output.extend(k+'='+v for k,v in fields.items() if k not in seen)
        atomic(path,('\n'.join(output)+'\n').encode(),0o600)

    def recreate(self,agent,running):
        if agent==1000003:
            args=['docker','compose','--project-directory',str(self.root),'-f',str(self.root/'compose.yaml')]
            if running:self.run(args+['up','-d','--no-deps','--force-recreate','--no-build','--pull','never','kf-assistant'],90)
            else:self.run(args+['create','--force-recreate','--no-build','--pull','never','kf-assistant'],90)
        else:
            _,_,container=self.resolve(agent)
            args=json.loads((self.root/'apps'/str(agent)/'container-run.json').read_text())
            # Container launch template is host owned, never supplied by HTTP.
            if args[:5]!=['run','-d','--name',container,'--label']:raise Rejected('容器启动模板无法识别。')
            self.run(['docker','stop','-t','20',container],35)
            self.run(['docker','rm',container])
            self.run(['docker',*(args if running else ['create',*args[2:]])],60)
        if running:self.wait_health(agent)

    def validate_config(self,data):
        if not isinstance(data,dict) or set(data)-{'model','base_url','api_key','ai_enabled','options','apply_existing'}:
            raise Rejected('配置字段无效。')
        for name in ('model','base_url','api_key'):
            if name in data and (not isinstance(data[name],str) or len(data[name])>512 or any(c in data[name] for c in '\r\n\x00') or any(c.isspace() for c in data[name])):
                raise Rejected('模型配置格式无效。')
        if 'model' in data and not re.fullmatch(r'[A-Za-z0-9_./:@-]{1,120}',data['model']):raise Rejected('模型名称格式无效。')
        if 'base_url' in data:
            p=urlsplit(data['base_url'])
            if p.scheme!='https' or not p.hostname or p.username or p.password or p.query or p.fragment:
                raise Rejected('模型地址必须为不含密码、查询参数的 HTTPS 地址。')
        for k in ('ai_enabled','apply_existing'):
            if k in data and type(data[k]) is not bool:raise Rejected('开关格式无效。')
        try:global_defaults.validate(data.get('options',{}))
        except ValueError as exc:raise Rejected(str(exc)) from None
        return data

    def configure(self,agent,data):
        self.validate_config(data)
        runtime,env,_=self.resolve(agent)
        if data.get('ai_enabled') and self.pool().get(agent,{}).get('state') not in (None,'assigned'):
            raise Rejected('此应用尚未完成分配，不能绕过开通流程启用 AI。')
        current=read_env(env)
        if data.get('base_url') and data['base_url']!=current.get('DEEPSEEK_BASE_URL') and not data.get('api_key'):
            raise Rejected('更换模型服务地址时请同时填写对应的新密钥，避免把原密钥发送给其他服务。')
        if data.get('ai_enabled') and not current.get('WECOM_APP_SECRET'):raise Rejected('企业应用凭证未配置。')
        if data.get('ai_enabled') and not (data.get('api_key') or current.get('DEEPSEEK_API_KEY')):raise Rejected('请先配置模型密钥。')
        defaults=dict(self.settings(agent)['options']);defaults.update(data.get('options',{}))
        if defaults['proactive'] and defaults.get('quiet_enabled',True) and defaults['quiet_start']==defaults['quiet_end']:raise Rejected('免打扰开始与结束不能相同。')
        backup=self.backup(agent,'修改配置')
        fields={ENV_FIELDS[k]:('true' if v else 'false') if type(v) is bool else v for k,v in data.items() if k in ENV_FIELDS and not(k=='api_key' and not v)}
        was_running=self.info(agent)['State']['Running']
        changed=0
        try:
            if fields:self.change_env(agent,fields)
            if fields and any(current.get(k)!=v for k,v in fields.items()):self.recreate(agent,was_running)
            path=self.data_path(agent)
            with closing(connect(path)) as db,db:
                db.execute('BEGIN IMMEDIATE')
                try:changed=global_defaults.update_application(db,agent,data.get('options',{}),data.get('apply_existing',False))
                except ValueError as exc:raise Rejected(str(exc)) from None
        except BaseException:
            # No historical database is restored over messages received during a configuration change.
            saved=self.directory/'backups'/str(agent)/backup/'config.env'
            atomic(env,saved.read_bytes(),0o600)
            if fields:self.recreate(agent,was_running)
            raise
        return {'backup':backup,'updated_users':changed,'defaults_saved':True,'restart_required':False}

    def sync_manifest(self,agent):
        if agent==1000003:return
        runtime,_,_=self.resolve(agent);path=runtime.parent/'manifest.json'
        if not path.exists():return
        manifest=json.loads(path.read_text('utf8'))
        if manifest.get('agent_id')!=agent:raise Rejected('应用版本清单身份不一致。')
        for name in source_names(agent):
            if (runtime/name).exists():manifest.setdefault('runtime',{})[name]=hashlib.sha256((runtime/name).read_bytes()).hexdigest()
        atomic(path,(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n').encode())

    def restore(self,agent,ident,automatic=False,include_database=True):
        if not isinstance(ident,str) or not re.fullmatch(r'\d{8}-\d{6}-[a-f0-9]{8}',ident):raise Rejected('备份编号无效。')
        folder=self.directory/'backups'/str(agent)/ident
        if folder.is_symlink() or not folder.is_dir():raise Rejected('备份不存在。')
        manifest=json.loads((folder/'manifest.json').read_text())
        if manifest['agent']!=agent:raise Rejected('备份不属于这个应用。')
        for name,digest in manifest['files'].items():
            if name not in (*source_names(agent),'config.env','database.sqlite3','token-usage.sqlite3','lifecycle.sqlite3') or hashlib.sha256((folder/name).read_bytes()).hexdigest()!=digest:
                raise Rejected('备份校验失败，未恢复。')
        runtime,env,container=self.resolve(agent);running=self.info(agent)['State']['Running']
        if agent==1000003 and (self.directory/'application-bindings.json').exists() and ('lifecycle.py' not in manifest['files'] or 'dedicated_binding_version=1' not in (folder/'lifecycle.py').read_text('utf8')):
            raise Rejected('旧备份不含专属应用保护，不能覆盖当前代码。')
        if 'lifecycle_version=1' in (runtime/'suite_app.py').read_text('utf8') and ('lifecycle.py' not in manifest['files'] or 'lifecycle_version=1' not in (folder/'suite_app.py').read_text('utf8')):
            raise Rejected('旧备份不包含使用期限保护，不能覆盖当前代码。请导出资料后选择新版备份。')
        if 'automatic_memory_version=2' in (runtime/'suite_app.py').read_text('utf8') and ('memory_engine.py' not in manifest['files'] or 'automatic_memory_version=2' not in (folder/'suite_app.py').read_text('utf8')):
            raise Rejected('旧备份不含已恢复的自动记忆流程，不能覆盖当前版本。')
        if 'persona_history_continuity_version=1' in (runtime/'suite_app.py').read_text('utf8') and ('suite_app.py' not in manifest['files'] or 'persona_history_continuity_version=1' not in (folder/'suite_app.py').read_text('utf8')):
            raise Rejected('旧备份不含人格修改后的对话连续性保护，不能覆盖当前版本。')
        if 'memory_retention_version=1' in (runtime/'suite_app.py').read_text('utf8') and ('memory_engine.py' not in manifest['files'] or 'MEMORY_RETENTION_VERSION = 1' not in (folder/'memory_engine.py').read_text('utf8')):
            raise Rejected('旧备份不含记忆归档与召回保护，不能覆盖当前版本。')
        if 'rolling_summary_version=1' in (runtime/'suite_app.py').read_text('utf8') and ('suite_app.py' not in manifest['files'] or 'rolling_summary_version=1' not in (folder/'suite_app.py').read_text('utf8')):
            raise Rejected('旧备份不含滚动摘要，不能覆盖当前版本。')
        if 'application_profile_save_version=1' in (runtime/'suite_app.py').read_text('utf8') and ('suite_app.py' not in manifest['files'] or 'application_profile_save_version=1' not in (folder/'suite_app.py').read_text('utf8')):
            raise Rejected('旧备份不含应用头像统一保存修复，不能覆盖当前版本。')
        if 'template_application_identity_version=1' in (runtime/'suite_app.py').read_text('utf8') and ('suite_app.py' not in manifest['files'] or 'template_application_identity_version=1' not in (folder/'suite_app.py').read_text('utf8')):
            raise Rejected('旧备份不含模板应用名称和头像同步功能，不能覆盖当前版本。')
        if 'template_reference_version=1' in (runtime/'suite_app.py').read_text('utf8') and any(name not in manifest['files'] or ('template_reference_version=1' if name=='suite_app.py' else 'def prepare_template_references') not in (folder/name).read_text('utf8') for name in ('suite_app.py','companion_app.py')):
            raise Rejected('旧备份不含模板参考图功能，不能覆盖当前版本。')
        if 'recent_history_turns=24' in (runtime/'suite_app.py').read_text('utf8') and ('suite_app.py' not in manifest['files'] or 'recent_history_turns=24' not in (folder/'suite_app.py').read_text('utf8')):
            raise Rejected('旧备份不含24轮近期对话保留，不能覆盖当前版本。')
        if 'require_starcoin' in (runtime/'token_meter.py').read_text('utf8') and ('token_meter.py' not in manifest['files'] or 'require_starcoin' not in (folder/'token_meter.py').read_text('utf8')):
            raise Rejected('旧备份不含星币余额保护，不能覆盖当前版本。')
        previous=None if automatic else self.backup(agent,'恢复前快照')
        try:
            if running:self.run(['docker','stop','-t','20',container],35)
            for name in source_names(agent):
                if (folder/name).exists():atomic(runtime/name,(folder/name).read_bytes())
            self.sync_manifest(agent)
            if (folder/'config.env').exists():atomic(env,(folder/'config.env').read_bytes(),0o600)
            if include_database and (folder/'database.sqlite3').exists():
                with closing(connect(folder/'database.sqlite3',True)) as src,closing(connect(self.data_path(agent))) as dst:
                    src.backup(dst)
            self.recreate(agent,running)
        except BaseException:
            if previous:
                # Automatic rollback uses the running state captured before the failed restore.
                self.restore(agent,previous,automatic=True)
                if running and not self.info(agent)['State']['Running']:
                    self.run(['docker','start',container]);self.wait_health(agent)
            raise
        return {'restored':ident,'previous_backup':previous}

    def release(self,agent):
        folder=self.root/'admin/releases'/VERSION/str(agent)
        if not folder.is_dir() or folder.is_symlink():raise Rejected('此应用没有已验证的版本包。')
        manifest=json.loads((folder/'manifest.json').read_text())
        for name in source_names(agent):
            raw=(folder/name).read_bytes()
            if hashlib.sha256(raw).hexdigest()!=manifest.get(name):raise Rejected('版本包校验失败。')
            compile(raw,name,'exec')
        runtime,_,container=self.resolve(agent);running=self.info(agent)['State']['Running']
        binding=self.directory/'application-bindings.json'
        if agent==1000003 and binding.exists() and 'dedicated_binding_version=1' not in (folder/'lifecycle.py').read_text('utf8'):
            raise Rejected('此版本不含专属应用保护，无法更新。')
        backup=self.backup(agent,'更新版本')
        try:
            for name in source_names(agent):atomic(runtime/name,(folder/name).read_bytes())
            self.sync_manifest(agent)
            if running:
                if agent==1000003:self.recreate(agent,True)
                else:self.run(['docker','restart','-t','20',container]);self.wait_health(agent)
        except BaseException:
            self.restore(agent,backup,automatic=True,include_database=False)
            if running and not self.info(agent)['State']['Running']:
                self.run(['docker','start',container]);self.wait_health(agent)
            raise
        return {'version':VERSION,'backup':backup,'running':running}

    def reclaim_status(self,agent):
        from reclaim_support import status
        return status(self.root,agent)

    def validate_reclaim(self,agent,data,actor=None):
        if set(data)!={'binding','typed_agent','protected_confirm'} or type(data['protected_confirm']) is not bool or not isinstance(data['binding'],str) or not re.fullmatch('[a-f0-9]{64}',data['binding']) or data['typed_agent']!=str(agent):
            raise Rejected('请输入正确的应用 ID 并确认回收影响。')
        value=self.reclaim_status(agent)
        if not value['allowed']:raise Rejected(value['reason'])
        if value['binding']!=data['binding']:raise Rejected('应用归属已变化，请重新读取后再确认，尚未执行回收。')
        if value.get('requires_superadmin'):
            if not actor or actor['role']!='superadmin':raise Rejected('只有超级管理员可手动回收受保护应用。')
            if data['protected_confirm'] is not True:raise Rejected('请勾选受保护应用的额外回收确认。')

    def reclaim(self,agent,data,actor=None):
        self.validate_reclaim(agent,data,actor)
        from lifecycle_worker import Controller
        controller=Controller(self.root)
        ident=None
        try:
            row=next(r for r in controller.rows() if r['agent']==agent)
            ident=controller.create(row,'manual',authorizer=actor['id'] if actor else None,protected_confirm=data['protected_confirm'])
            controller.complete(ident)
            return {'pool_state':'free','reclaim_backup':ident}
        except Exception:
            if ident:
                with controller.db:controller.db.execute('UPDATE runs SET error=?,updated=? WHERE id=?',('manual_reclaim_requires_inspection',time.time(),ident))
                raise Rejected('回收未完成，已保留处理进度，应用暂未进入待分配。请核对应用状态，勿重复提交。') from None
            raise Rejected('回收前检查未通过，尚未清理用户数据。请核对成员保护、部门范围和开通记录。') from None
        finally:controller.close()

    def execute(self,operation,agent,data,actor=None):
        try:
            if operation in ('delete_prepare','delete_finalize'):
                return application_deletion.execute(self,operation,agent,data,actor)
            if application_deletion.active(self.root,agent):
                raise Rejected('应用正在删除，暂不能执行其他维护操作。请到维护任务继续删除。')
        except application_deletion.DeletionError as exc:raise Rejected(str(exc)) from None
        _,_,container=self.resolve(agent)
        with closing(connect(self.database,True)) as db:
            for item in db.execute("SELECT plan FROM account_deletions WHERE status!='done'"):
                if json.loads(item['plan']).get('enterprise','legacy')=='legacy' and any(a['agent']==agent for a in json.loads(item['plan']).get('apps',[])):
                    raise Rejected('此应用属于未完成的账号注销，请先继续或检查注销流程。')
        journal=self.directory/'lifecycle-control.sqlite3'
        if journal.exists():
            with closing(connect(journal,True)) as db:
                if db.execute("SELECT 1 FROM runs WHERE agent=? AND phase!='done'",(agent,)).fetchone():
                    raise Rejected('应用正在回收或等待回收检查，暂不能执行维护操作。')
        if operation=='reclaim':return self.reclaim(agent,data,actor)
        if operation=='backup':return {'backup':self.backup(agent,'手动备份')}
        if operation=='configure':return self.configure(agent,data)
        if operation=='release':return self.release(agent)
        if operation=='restore':return self.restore(agent,data.get('backup'))
        if operation in ('start','restart'):
            if self.pool().get(agent,{}).get('state') in ('failed','disabled','provisioning'):
                raise Rejected('该应用处于开通失败、停用或开通中，请先处理开通流程。')
            self.run(['docker',operation,container],60);self.wait_health(agent)
            return {'running':True}
        if operation=='stop':
            self.run(['docker','stop','-t','20',container],35);return {'running':False}
        raise Rejected('此操作不受支持。')

    def submit(self,actor,payload):
        if set(payload)-{'operation','agents','data','request_id','confirm'}:raise Rejected('请求字段无效。')
        op=payload.get('operation');agents=payload.get('agents');data=payload.get('data',{})
        if op not in ('start','stop','restart','backup','restore','release','configure','reclaim','delete_prepare','delete_finalize'):raise Rejected('操作无效。')
        deleting=op in ('delete_prepare','delete_finalize')
        if deleting:
            try:application_deletion.superadmin(actor)
            except application_deletion.DeletionError as exc:raise Rejected(str(exc)) from None
            with closing(connect(self.database)) as db:
                previous=db.execute('SELECT id FROM jobs WHERE actor=? AND request_id=?',(actor['username'],payload.get('request_id',''))).fetchone()
                if previous:return {'job':previous[0]}
        available=self.agents()+(application_deletion.ids(self.root) if deleting else [])
        if not isinstance(agents,list) or not 1<=len(agents)<=30 or any(type(a) is not int or a not in available for a in agents):raise Rejected('请选择有效的应用。')
        agents=list(dict.fromkeys(agents))
        if not isinstance(data,dict):raise Rejected('操作资料无效。')
        if op=='configure':self.validate_config(data)
        elif deleting:
            if len(agents)!=1:raise Rejected('每次只能删除一个应用。')
            try:application_deletion.validate(self,op,agents[0],data,actor)
            except application_deletion.DeletionError as exc:raise Rejected(str(exc)) from None
        elif op=='reclaim':
            if len(agents)!=1:raise Rejected('每次只能回收一个应用。')
            if set(data)!={'binding','typed_agent','protected_confirm'}:raise Rejected('回收确认资料无效。')
        elif set(data)-({'backup'} if op=='restore' else set()):raise Rejected('操作参数无效。')
        if op=='restore' and len(agents)!=1:raise Rejected('每次只能恢复一个应用。')
        if op!='backup' and payload.get('confirm') is not True:raise Rejected('请确认本次影响范围。')
        request_id=payload.get('request_id','')
        if not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}',request_id):raise Rejected('请求编号无效。')
        with self.lock,closing(connect(self.database)) as db,db:
            previous=db.execute('SELECT id FROM jobs WHERE actor=? AND request_id=?',(actor['username'],request_id)).fetchone()
            if previous:return {'job':previous[0]}
            if op=='reclaim':self.validate_reclaim(agents[0],data,actor)
            if db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('pending','running')").fetchone()[0]>=4:raise Rejected('后台任务较多，请等待现有任务完成。')
            ident=secrets.token_hex(16)
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',
                (ident,actor['username'],op,json.dumps(agents),'pending',None,time.time(),time.time(),request_id))
        self.log(actor['username'],op,agents,'已排队')
        self.executor.submit(self.work,ident,actor['username'],op,agents,data,actor['_session'])
        return {'job':ident}

    def work(self,ident,actor,op,agents,data,session):
        with closing(connect(self.database)) as db,db:db.execute("UPDATE jobs SET status='running',updated=? WHERE id=?",(time.time(),ident))
        results=[]
        for agent in agents:
            try:
                verified=self.authorize(session,True)
                lockpath=self.root/'app-pool/operation.lock'
                if lockpath.parent.exists() and os.name!='nt':
                    import fcntl
                    with lockpath.open('a') as lock:
                        fcntl.flock(lock,fcntl.LOCK_EX)
                        verified=self.authorize(session,True)
                        result=self.execute(op,agent,data,verified)
                else:result=self.execute(op,agent,data,verified)
                value={'agent':agent,'ok':True,'result':result}
                self.log(actor,op,[agent],'成功')
            except Exception as exc:
                value={'agent':agent,'ok':False,'error':str(exc) if isinstance(exc,Rejected) else '操作失败，已停止此应用的后续操作，请核对状态。'}
                self.log(actor,op,[agent],'失败')
            results.append(value)
            with closing(connect(self.database)) as db,db:
                db.execute('UPDATE jobs SET result=?,updated=? WHERE id=?',(json.dumps(results,ensure_ascii=False),time.time(),ident))
        with closing(connect(self.database)) as db,db:
            db.execute('UPDATE jobs SET status=?,updated=? WHERE id=?',('done' if all(r['ok'] for r in results) else 'failed',time.time(),ident))

    def role(self,actor,data):
        lockpath=self.root/'app-pool/operation.lock'
        if lockpath.parent.exists() and os.name!='nt':
            import fcntl
            with lockpath.open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                return self._role(actor,data)
        return self._role(actor,data)

    def _role(self,actor,data):
        if actor['role']!='superadmin':raise Rejected('只有超级管理员可分配后台权限。')
        if set(data)!={'username','role'} or not isinstance(data['username'],str) or data['role'] not in ('superadmin','operator','viewer','member'):
            raise Rejected('权限资料无效。')
        with closing(connect(self.portal_database,True)) as db:user=db.execute('SELECT id,username FROM accounts WHERE username=?',(data['username'].lower(),)).fetchone()
        if not user:raise Rejected('账号不存在，请先在网站注册。')
        with closing(connect(self.portal_database,True)) as db:
            if db.execute('SELECT 1 FROM account_closures WHERE account=?',(user['id'],)).fetchone():
                raise Rejected('该账号正在注销，不能调整权限。')
        with self.lock,closing(connect(self.database)) as db,db:
            old=db.execute('SELECT role FROM roles WHERE account=?',(user['id'],)).fetchone()
            if old and old[0]=='superadmin' and data['role']!='superadmin' and db.execute("SELECT COUNT(*) FROM roles WHERE role='superadmin'").fetchone()[0]<=1:
                raise Rejected('不能撤销最后一位超级管理员。')
            if data['role']=='member':db.execute('DELETE FROM roles WHERE account=?',(user['id'],))
            else:db.execute('INSERT OR REPLACE INTO roles VALUES(?,?,?)',(user['id'],data['role'],time.time()))
        self.log(actor['username'],'分配权限',[],user['username']+' → '+data['role'])
        return {'ok':True}

    def call(self,request):
        if not isinstance(request,dict) or set(request)-{'action','session','payload'}:raise Rejected('请求无效。')
        action=request.get('action');payload=request.get('payload',{})
        actor=self.authorize(request.get('session'),action in ('submit','role','shadow_price_save','wallet_create','wallet_credit','wallet_reverse','wallet_settle','global_defaults_save','account_deletion_submit','account_deletion_retry','task_attention'))
        if action in ('public_sticker_analyze','public_sticker_convert'):
            import public_sticker_upload
            try:return public_sticker_upload.analyze(self,actor,payload) if action=='public_sticker_analyze' else public_sticker_upload.convert(actor,payload)
            except ValueError as exc:raise Rejected(str(exc)) from None
        if isinstance(action,str) and action.startswith('public_sticker_'):
            import public_stickers
            try:return public_stickers.admin(actor,action.removeprefix('public_sticker_'),payload)
            except public_stickers.PoolError as exc:raise Rejected(str(exc)) from None
        if action=='identity':return {'username':actor['username'],'role':actor['role'],'options_schema':global_defaults.form_schema()}
        if action=='global_defaults':
            if not isinstance(payload,dict) or set(payload)-{'refresh_voices'} or ('refresh_voices' in payload and type(payload['refresh_voices']) is not bool):raise Rejected('音色读取参数无效。')
            return global_defaults.configuration(self,refresh=payload.get('refresh_voices',False))
        if action=='global_defaults_save':return global_defaults.submit(self,actor,payload)
        if action in ('ops_dashboard','ops_inventory','ops_onboarding','ops_payments','ops_wallets','ops_wallet','ops_business','ops_customer'):
            import operations_report
            return operations_report.call(self,action,payload)
        if action in ('template_list','template_detail','template_save','template_publish','template_withdraw','template_default','template_reference_analyze','template_card_parse','template_voice_preview'):
            import persona_templates as templates
            try:
                if action=='template_voice_preview':
                    from template_voice_preview import preview
                    return preview(self,actor,payload)
                if action=='template_card_parse':
                    from persona_card_import import analyze_card
                    return analyze_card(self,actor,payload)
                if action=='template_reference_analyze':return templates.analyze_reference(self,actor,payload)
                if action=='template_list':return templates.admin_read(payload)
                if action=='template_detail':return templates.admin_detail(payload)
                return templates.write(actor,action.removeprefix('template_'),payload)
            except templates.TemplateError as exc:raise Rejected(str(exc)) from None
        if action=='overview':return self.overview()
        if action=='detail':
            value=self.settings(payload.get('agent'))
            value['deletion']=application_deletion.status(self,payload.get('agent')) if actor['role']=='superadmin' else {'allowed':False,'reason':'只有超级管理员可删除应用。'}
            if application_deletion.active(self.root,payload.get('agent')):
                value['reclaim'].update(allowed=False,reason='应用正在删除，请到维护任务继续处理。')
            if value['reclaim'].get('requires_superadmin') and actor['role']!='superadmin':
                value['reclaim'].update(allowed=False,reason='此应用受自动回收保护；只有超级管理员可手动回收。')
            return value
        if action=='application_deletions':
            try:return application_deletion.listing(self,actor)
            except application_deletion.DeletionError as exc:raise Rejected(str(exc)) from None
        if action=='accounts':
            import admin_queries
            return admin_queries.accounts(self,payload)
        if action=='task_attention':
            import admin_queries
            return admin_queries.acknowledge(self,actor,payload)
        if action in ('account_deletion_preview','account_deletion_submit','account_deletion_list','account_deletion_retry'):
            import account_deletion
            try:
                return {'account_deletion_preview':account_deletion.preview,'account_deletion_submit':account_deletion.submit,
                        'account_deletion_list':lambda ops,who,data:account_deletion.listing(ops,who),
                        'account_deletion_retry':account_deletion.retry}[action](self,actor,payload)
            except account_deletion.ClosureError as exc:raise Rejected(str(exc)) from None
        if action in ('shadow_report','shadow_prices','shadow_price_save'):
            import shadow_pricing
            if action=='shadow_report':
                from star_billing import StarOps
                return shadow_pricing.report(StarOps(self),payload)
            if action=='shadow_prices':return shadow_pricing.configuration(self)
            return shadow_pricing.save(self,actor,payload)
        if action in ('wallet_overview','wallet_detail','wallet_create','wallet_credit','wallet_reverse','wallet_settle'):
            if action not in ('wallet_overview','wallet_detail'):raise Rejected('历史模拟账本已归档，仅供查询。')
            import sim_wallet
            if action=='wallet_overview':return sim_wallet.overview(self)
            if action=='wallet_detail':return sim_wallet.detail(self,payload)
            return {'wallet_create':sim_wallet.create,'wallet_credit':sim_wallet.credit,'wallet_reverse':sim_wallet.reverse,'wallet_settle':sim_wallet.settle}[action](self,actor,payload)
        if action in ('starcoin_overview','starcoin_detail'):
            import star_billing
            if action=='starcoin_overview':return star_billing.star_overview(self)
            return star_billing.star_detail(self,payload)
        if action=='token_usage':
            from usage_report import report
            from star_billing import StarOps
            return report(StarOps(self),payload)
        if action=='submit':return self.submit(actor,payload)
        if action=='role':return self.role(actor,payload)
        if action in ('jobs','audit'):
            import admin_queries
            return admin_queries.records(self,action,payload)
        raise Rejected('请求不受支持。')
