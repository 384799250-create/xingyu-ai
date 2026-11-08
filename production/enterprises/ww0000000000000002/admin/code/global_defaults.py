"""Global option templates; no credentials, personal profiles or provider calls."""
from contextlib import closing
import ast
import threading
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time

# This schema also drives the admin form. Removed legacy daily caps stay excluded.
SCHEMA = [
 ('split_reply','自然连续回复','回复与消息',True,None),
 ('wait_seconds','回复前等待（秒）','回复与消息',0,(0,10)),
 ('strip_actions','去除动作描写','回复与消息',False,None),
 ('language','文字回复语言','回复与消息','中文',['中文','English','日本語','한국어','粵語']),
 ('merge_messages','合并连续收到的消息','回复与消息',False,None),
 ('merge_seconds','消息合并等待（秒）','回复与消息',3,(1,10)),
 ('proactive','允许主动联系','主动陪伴',False,None),
 ('contact_willingness','联系意愿','主动陪伴','中',{'无':'无','低':'低','中':'中','高':'高'}),
 ('min_interval','联系间隔（分钟）','主动陪伴',120,(0,1440)),
 ('proactive_max_unanswered','连续未回复上限','主动陪伴',2,(0,8)),
 ('proactive_rest_hours','休息时长（小时）','主动陪伴',8,(1,168)),
 ('quiet_enabled','启用免打扰','主动陪伴',True,None),
 ('quiet_start','免打扰开始（当地整点）','主动陪伴',23,(0,23)),
 ('quiet_end','免打扰结束（当地整点）','主动陪伴',8,(0,23)),
 ('weather','允许查询天气','主动陪伴',False,None),
 ('greeting_mode','每日问候方式','主动陪伴','fixed',{'fixed':'固定问候','smart':'智能问候'}),
 ('ai_image','AI 自主发送图片','语音与图片',True,None),
 ('ai_voice','AI 自主发送语音','语音与图片',True,None),
 ('ai_media_scope','自主表达适用场景','语音与图片','both',{'both':'聊天和主动联系','chat':'仅聊天','proactive':'仅主动联系'}),
 ('voice','默认音色','语音与图片','',{'':'沿用各应用默认音色'}),
 ('voice_language','语音语言','语音与图片','zh',{'zh':'中文','en':'英文','ja':'日文'}),
 ('voice_subtitles','外语语音显示中文字幕','语音与图片',False,None),
 ('voice_speed','语速','语音与图片',1.0,(0.5,2.0)),
 ('voice_auto_emotion','语音自动情绪','语音与图片',True,None),
 ('voice_rate','随机语音概率（%）','语音与图片',0,(0,100)),
 ('sticker_rate','随机表情概率（%）','表情与记忆',0,(0,100)),
 ('sticker_match','按语境匹配表情','表情与记忆',True,None),
 ('memory_auto','自动整理长期记忆','表情与记忆',True,None),
 ('memory_suggestions','关闭自动整理时生成偏好建议','表情与记忆',False,None),
 ('memory_scope','用户事实与手动记忆默认保存范围','表情与记忆','*',{'*':'我的全部人物共享','persona':'仅当前人物'}),
]
DEFS={row[0]:row for row in SCHEMA}
FACTORY={row[0]:row[3] for row in SCHEMA}

def validate(options):
    if not isinstance(options,dict) or set(options)-set(DEFS):
        raise ValueError('默认选项字段无效。')
    for key,value in options.items():
        _,label,_,default,rule=DEFS[key]
        if type(default) is bool:
            valid=type(value) is bool
        elif isinstance(rule,tuple):
            valid=(type(value) is int if type(default) is int else type(value) in (int,float))
            valid=valid and math.isfinite(value) and rule[0]<=value<=rule[1]
        elif key=='voice':
            valid=isinstance(value,str) and (value=='' or re.fullmatch(r'[A-Za-z0-9_-]{1,100}',value) is not None)
        else:
            valid=isinstance(value,str) and value in rule
        if not valid:raise ValueError(label+'的值无效，请重新选择。')
    return options

def cross_validate(options):
    if options.get('proactive') and options.get('quiet_enabled',True) and options.get('quiet_start')==options.get('quiet_end'):
        raise ValueError('免打扰开始与结束不能相同。')

def ensure(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS global_option_template(id INTEGER PRIMARY KEY CHECK(id=1),value TEXT NOT NULL,revision INTEGER NOT NULL,updated REAL NOT NULL,actor TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS global_option_versions(revision INTEGER PRIMARY KEY,value TEXT NOT NULL,updated REAL NOT NULL,actor TEXT NOT NULL);
    ''')

def read(database):
    if not Path(database).exists():return {'options':{},'revision':0,'updated':None,'actor':None}
    with closing(sqlite3.connect(database,timeout=10)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='global_option_template'").fetchone():
            return {'options':{},'revision':0,'updated':None,'actor':None}
        row=db.execute('SELECT value,revision,updated,actor FROM global_option_template WHERE id=1').fetchone()
    if not row:return {'options':{},'revision':0,'updated':None,'actor':None}
    return {'options':validate(json.loads(row[0])),'revision':row[1],'updated':row[2],'actor':row[3]}

# Catalogs contain public voice metadata only, isolated by enterprise and agent.
_VOICE_CACHE={}
_VOICE_LOCK=threading.RLock()
VOICE_CACHE_SECONDS=300
VOICE_STALE_SECONDS=3600

def configured_voice_catalog(ops,agent):
    runtime,_,_=ops.resolve(agent)
    # inspect works for stopped containers and never imports the application.
    info=ops.info(agent)
    env=dict(value.split('=',1) for value in info['Config']['Env'] if '=' in value)
    provider=env.get('XIAOYI_TTS_PROVIDER','').strip().lower()
    default=env.get('XIAOYI_TTS_VOICE','').strip()
    if provider=='breeze':
        raw=env.get('XIAOYI_BREEZE_TTS_VOICES','').strip() or env.get('XIAOYI_TTS_VOICES','').strip()
        choices=[]
        for token in raw.split(','):
            token=token.strip()
            if not token:continue
            ident,name=(token.split('=',1)+[''])[:2] if '=' in token else (token,token)
            ident,name=ident.strip(),name.strip() or ident.strip()
            if re.fullmatch(r'voc_[A-Za-z0-9_-]+',ident):choices.append({'id':ident,'name':name})
        choices=choices[:30]
        value=env.get('XIAOYI_BREEZE_TTS_VOICE','').strip() or default
        default=value if re.fullmatch(r'voc_[A-Za-z0-9_-]+',value) else choices[0]['id'] if choices else ''
    elif provider=='tencent' or not all(env.get('XIAOYI_TTS_'+key,'').strip() for key in ('URL','KEY','MODEL')):
        # Read just the literal catalog line, avoiding the large UI AST and imports.
        with (runtime/'suite_app.py').open(encoding='utf8') as source:
            literal=next(line.split('=',1)[1].strip() for line in source if line.startswith('TENCENT_VOICE_CATALOG = '))
        catalog=ast.literal_eval(literal)
        choices=[{'id':key,'name':value[0]} for key,value in catalog.items()]
        default=default if default in catalog else '502001'
    else:
        choices=[{'id':value.strip(),'name':value.strip()} for value in env.get('XIAOYI_TTS_VOICES','').split(',') if value.strip()][:30]
        default=default or 'alloy'
    if not choices:raise ValueError('音色目录为空。')
    return {'choices':choices,'default':default}

def voice_catalog(ops,agent,refresh=False,allow_stale=True):
    key=(str(ops.root.resolve()),agent);now=time.time()
    with _VOICE_LOCK:
        prior=_VOICE_CACHE.get(key)
        if prior and not refresh and now-prior['updated']<VOICE_CACHE_SECONDS:
            return dict(prior['catalog'],cached=True,stale=False,updated=prior['updated'])
    try:catalog=configured_voice_catalog(ops,agent)
    except Exception:
        if allow_stale and prior and now-prior['updated']<VOICE_STALE_SECONDS:
            return dict(prior['catalog'],cached=True,stale=True,updated=prior['updated'])
        raise
    with _VOICE_LOCK:
        _VOICE_CACHE[key]={'catalog':catalog,'updated':now}
        if len(_VOICE_CACHE)>512:
            oldest=min(_VOICE_CACHE,key=lambda item:_VOICE_CACHE[item]['updated'])
            _VOICE_CACHE.pop(oldest,None)
    return dict(catalog,cached=False,stale=False,updated=now)


def configuration(ops,refresh=False):
    saved=read(ops.database);common=None;unavailable=[];catalogs=[];stale=[];agents=ops.agents()
    for agent in agents:
        try:
            catalog=voice_catalog(ops,agent,refresh=refresh);choices={v['id']:v['name'] for v in catalog['choices']}
            common=choices if common is None else {k:v for k,v in common.items() if k in choices}
            catalogs.append({'agent':agent,'default_voice':catalog['default']})
            if catalog.get('stale'):stale.append(agent)
        except Exception:unavailable.append(agent)
    schema=form_schema(saved['options'],common if not unavailable else {})
    return dict(saved,values=dict(FACTORY,**saved['options']),schema=schema,agents=agents,voice_unavailable=unavailable,voice_stale=stale,voice_defaults=catalogs,group_help=GROUP_HELP,form_version=2)


def form_schema(options=None,voices=None):
    options=options or {}
    schema=[]
    for key,label,group,default,rule in SCHEMA:
        field={'key':key,'label':label,'group':group,'default':default,'help':FIELD_HELP.get(key,''),'advanced':key in ADVANCED_FIELDS}
        if type(default) is bool:field['type']='boolean'
        elif isinstance(rule,tuple):field.update(type='number',min=rule[0],max=rule[1],step=0.1 if key=='voice_speed' else 1)
        else:
            items=dict(rule) if isinstance(rule,dict) else {v:v for v in rule}
            if key=='voice':items.update(voices or {})
            # A saved voice remains visible during a temporary catalog outage.
            if key=='voice' and options.get('voice') not in (None,''):
                value=options['voice'];items.setdefault(value,value+'（已保存）')
            field.update(type='select',choices=[{'value':k,'label':v} for k,v in items.items()])
        schema.append(field)
    return schema


def application_schema(ops,agent,options):
    try:
        catalog=voice_catalog(ops,agent)
        voices={v['id']:v['name'] for v in catalog['choices']}
        unavailable=False
    except Exception:
        voices={};unavailable=True
    return {'schema':form_schema(options,voices),'voice_unavailable':unavailable,'group_help':GROUP_HELP}


def application_defaults(db,agent):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='suite_application_defaults'").fetchone():return {}
    row=db.execute('SELECT value FROM suite_application_defaults WHERE agent=?',(agent,)).fetchone()
    return validate(json.loads(row[0])) if row else {}

def update_application(db,agent,changes,overwrite=False):
    """Called inside an existing SQLite write transaction; preserve unrelated data."""
    validate(changes)
    old=dict(FACTORY,**application_defaults(db,agent));new=dict(old,**changes)
    cross_validate(new)
    states=db.execute('SELECT user,value FROM suite_state WHERE agent=?',(agent,)).fetchall()
    prepared=[];consent=[]
    for user,raw in states:
        state=json.loads(raw);personal=state.get('options') or {}
        before=dict(old,**personal);after=dict(new,**personal)
        if overwrite:after.update(changes)
        cross_validate(after)
        if not before['proactive'] and after['proactive']:consent.append(user)
        if overwrite:
            state['options']=dict(personal,**changes)
            if 'ai_image' in changes or 'ai_voice' in changes:state['_autonomous_media_defaults_v1']=True
            if 'split_reply' in changes:state['_natural_reply_default_migrated']=True
            prepared.append((json.dumps(state,ensure_ascii=False),agent,user))
    # Also reset consent for active users with no saved suite state.
    has_activity=db.execute("SELECT 1 FROM sqlite_master WHERE name='suite_activity'").fetchone()
    if has_activity and not old['proactive'] and new['proactive']:
        saved_users={user for user,_ in states}
        consent += [r[0] for r in db.execute('SELECT user FROM suite_activity WHERE agent=?',(agent,)) if r[0] not in saved_users]
    db.execute('CREATE TABLE IF NOT EXISTS suite_application_defaults(agent INTEGER PRIMARY KEY,value TEXT NOT NULL,updated REAL NOT NULL)')
    # Keep app defaults as a patch; factory changes should not freeze unrelated keys.
    merged=dict(application_defaults(db,agent),**changes)
    db.execute('INSERT OR REPLACE INTO suite_application_defaults VALUES(?,?,?)',(agent,json.dumps(merged,ensure_ascii=False),time.time()))
    db.executemany('UPDATE suite_state SET value=? WHERE agent=? AND user=?',prepared)
    if has_activity:
        for user in consent:
            db.execute('INSERT INTO suite_activity(agent,user,consent_at) VALUES(?,?,?) ON CONFLICT(agent,user) DO UPDATE SET consent_at=excluded.consent_at',(agent,user,time.time()))
    if changes:db.execute("UPDATE suite_outbox SET status='cancelled' WHERE agent=? AND kind='proactive' AND status IN ('pending','working')",(agent,))
    return len(prepared)

def check_payload(payload):
    if not isinstance(payload,dict) or set(payload)!={'options','revision','sync_apps','apply_existing','request_id','confirm'}:
        raise ValueError('全局设置请求字段无效。')
    validate(payload['options'])
    if not payload['options']:raise ValueError('请修改至少一个选项后再保存。')
    if type(payload['revision']) is not int or payload['revision']<0:raise ValueError('版本无效，请重新读取。')
    for name in ('sync_apps','apply_existing','confirm'):
        if type(payload[name]) is not bool:raise ValueError('同步范围无效。')
    if payload['confirm'] is not True:raise ValueError('请先确认设置及影响范围。')
    if payload['apply_existing'] and not payload['sync_apps']:raise ValueError('覆盖用户前必须选择同步现有应用。')
    if not isinstance(payload['request_id'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}',payload['request_id']):
        raise ValueError('请求编号无效。')

def submit(ops,actor,payload):
    from admin_ops import Rejected,connect
    if actor['role']!='superadmin':raise Rejected('只有超级管理员可以修改全局默认设置。')
    try:check_payload(payload)
    except ValueError as exc:raise Rejected(str(exc)) from None
    with ops.lock,closing(connect(ops.database)) as db,db:
        previous=db.execute('SELECT id FROM jobs WHERE actor=? AND request_id=?',(actor['username'],payload['request_id'])).fetchone()
        if previous:return {'job':previous[0]}
        current=read(ops.database)
        if current['revision']!=payload['revision']:raise Rejected('全局设置已更新，请重新读取后保存。')
        try:cross_validate(FACTORY | current['options'] | payload['options'])
        except ValueError as exc:raise Rejected(str(exc)) from None
        if db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('pending','running')").fetchone()[0]>=4:raise Rejected('请等待现有维护任务完成后再保存。')
        targets=ops.agents() if payload['sync_apps'] else []
        ident=secrets.token_hex(16)
        db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',(ident,actor['username'],'global_defaults',json.dumps(targets),'pending',None,time.time(),time.time(),payload['request_id']))
    ops.log(actor['username'],'global_defaults',targets,'已排队')
    ops.executor.submit(work,ops,ident,actor,payload,targets)
    return {'job':ident}

def work(ops,ident,actor,payload,targets):
    from admin_ops import Rejected,connect
    results=[]
    def report(status):
        with closing(connect(ops.database)) as db,db:
            db.execute('UPDATE jobs SET status=?,result=?,updated=? WHERE id=?',(status,json.dumps(results,ensure_ascii=False),time.time(),ident))
    def perform():
        verified=ops.authorize(actor['_session'],True)
        if verified['role']!='superadmin':raise Rejected('全局设置需要超级管理员权限，尚未执行。')
        current=read(ops.database)
        if current['revision']!=payload['revision']:raise Rejected('全局设置已更新，请重新读取后保存。')
        options=dict(current['options'],**payload['options']);cross_validate(dict(FACTORY,**options))
        # Preflight every target before committing the future-allocation template.
        for agent in targets:
            ops.resolve(agent)
            path=ops.data_path(agent)
            if not path.exists():raise Rejected('应用 '+str(agent)+' 的数据库暂不可读，尚未保存模板。')
            with closing(connect(path)) as db:
                db.execute('BEGIN IMMEDIATE')
                try:update_application(db,agent,payload['options'],payload['apply_existing'])
                finally:db.rollback()
        if payload['options'].get('voice'):
            for agent in ops.agents():
                choices=voice_catalog(ops,agent,refresh=True,allow_stale=False)['choices']
                if payload['options']['voice'] not in {v['id'] for v in choices}:raise Rejected('所选音色不适用于全部应用，请重新读取或选择沿用应用默认音色。')
        with closing(connect(ops.database)) as db,db:
            ensure(db)
            row=db.execute('SELECT revision FROM global_option_template WHERE id=1').fetchone()
            if (row[0] if row else 0)!=payload['revision']:raise Rejected('全局设置已更新，请重新读取。')
            value=json.dumps(options,ensure_ascii=False);now=time.time();rev=current['revision']+1
            db.execute('INSERT OR REPLACE INTO global_option_template VALUES(1,?,?,?,?)',(value,rev,now,actor['username']))
            db.execute('INSERT INTO global_option_versions VALUES(?,?,?,?)',(rev,value,now,actor['username']))
        results.append({'agent':0,'ok':True,'result':{'template_revision':rev,'future_allocations':True}});report('running')
        for agent in targets:
            try:
                verified=ops.authorize(actor['_session'],True)
                if verified['role']!='superadmin':raise Rejected('全局同步权限已变化，请重新登录后核对。')
                result=ops.execute('configure',agent,{'options':payload['options'],'apply_existing':payload['apply_existing']},verified)
                results.append({'agent':agent,'ok':True,'result':result})
                ops.log(actor['username'],'global_defaults',[agent],'成功')
            except Exception as exc:
                results.append({'agent':agent,'ok':False,'error':str(exc) if isinstance(exc,(Rejected,ValueError)) else '同步失败，模板已保存，请查看应用状态后重试同步。'})
                ops.log(actor['username'],'global_defaults',[agent],'失败')
            report('running')
    report('running')
    try:
        lockpath=ops.root/'app-pool/operation.lock'
        if lockpath.parent.exists() and os.name!='nt':
            import fcntl
            with lockpath.open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX);perform()
        else:perform()
    except Exception as exc:
        results.append({'agent':0,'ok':False,'error':str(exc) if isinstance(exc,(Rejected,ValueError)) else '保存失败，请重新读取并核对维护记录。'})
    status='done' if results and all(v['ok'] for v in results) else 'failed'
    report(status);ops.log(actor['username'],'global_defaults',targets,'成功' if status=='done' else '失败或部分失败')

def inherit(app):
    """Called by allocation under its operation lock, before enabling the app."""
    template=read(app.root.parent.parent/'admin/data/admin.sqlite3')
    if not template['options']:return
    path=app.root/'data/kf.sqlite3'
    with closing(sqlite3.connect(path,timeout=20)) as db,db:
        db.execute('BEGIN IMMEDIATE')
        update_application(db,app.agent,template['options'],False)

FIELD_HELP={'split_reply': '将较长回复自然拆成几条发送。', 'wait_seconds': '收到消息后，等待用户暂停输入再开始回复；不是两条回复之间的发送间隔。', 'strip_actions': '过滤星号、括号包裹的动作描写，适合偏好纯对话的用户。', 'merge_messages': '将用户连续发来的几条消息合并理解、统一回复。', 'merge_seconds': '开启消息合并时，等待连续消息的时间。', 'contact_willingness': '决定主动联系的积极程度；选择“无”时不会主动联系，主动联系总开关仍需开启。', 'proactive': '允许角色主动发来消息，仍遵守联系意愿、免打扰和休息设置。', 'min_interval': '两次主动联系之间的最短间隔。填 0 表示不设这项间隔限制。', 'proactive_max_unanswered': '达到上限后先休息。填 0 表示不设连续未回复次数限制。', 'proactive_rest_hours': '达到连续未回复上限后，暂停主动联系的时长。', 'quiet_enabled': '开启后，在用户当地的免打扰时段暂停主动联系。', 'quiet_start': '按各用户设置的当地时间计算。', 'quiet_end': '启用免打扰时，开始与结束时间不能相同。', 'weather': '允许在陪伴中使用天气信息，用户仍需设置自己的所在地。', 'greeting_mode': '只调整问候内容方式，不会自动开启每日问候或修改发送时间。', 'voice': '沿用各应用默认音色可保留专属声音；选择具体音色才会统一。', 'ai_voice': '允许角色根据对话选择语音表达。', 'voice_rate': '仅控制原有随机语音回复，0% 不代表关闭 AI 自主语音。', 'sticker_rate': '控制随机发送表情的概率，0% 表示不随机附加。', 'memory_scope': '只设置用户事实和手动记忆的默认范围，不改变已有记忆内容。', 'memory_suggestions': '关闭自动整理时，仍可生成待用户确认的偏好建议。'}
ADVANCED_FIELDS={'sticker_rate', 'proactive_max_unanswered', 'strip_actions', 'merge_seconds', 'proactive_rest_hours', 'memory_suggestions', 'greeting_mode', 'wait_seconds', 'voice_rate'}
GROUP_HELP={'回复与消息': '设置对话语言、回复方式与连续消息的处理。', '主动陪伴': '让角色在合适的时候联系用户，也留出安静的时间。', '语音与图片': '设置角色如何使用声音和图片表达。', '表情与记忆': '调整表情习惯，以及记忆的整理和保存范围。'}
