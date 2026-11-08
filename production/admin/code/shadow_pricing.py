"""Admin-only reference pricing. Reads usage; never charges or accesses wallets."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import re
import sqlite3
import time

ZONE=timezone(timedelta(hours=8))
KINDS=('token','image','tts')
RATE_FIELDS=('input','output','cached','unit')
NANO=Decimal(1000000000)
# Published 2026 holiday breaks; weekends never become peak by make-up workdays.
# https://github.com/LKI/chinese-calendar (2026 calendar, verified 2026-10-28)
HOLIDAYS_2026=((1,1,3),(2,15,23),(4,4,6),(5,1,5),(6,19,21),(9,25,27),(10,1,7))

def peak_multiplier(created):
    local=datetime.fromtimestamp(created,ZONE)
    if local.year!=2026:return None
    holiday=any(local.month==m and first<=local.day<=last for m,first,last in HOLIDAYS_2026)
    hour=local.hour+local.minute/60
    return 2 if local.weekday()<5 and not holiday and (9<=hour<12 or 14<=hour<18) else 1

def reject(message):
    from admin_ops import Rejected
    raise Rejected(message)

def connect(ops):
    path=ops.directory/'shadow-pricing.sqlite3'
    db=sqlite3.connect(str(path),timeout=10)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript('''
      CREATE TABLE IF NOT EXISTS rate_versions(
       id INTEGER PRIMARY KEY,kind TEXT NOT NULL,provider TEXT NOT NULL,model TEXT NOT NULL,
       rates TEXT NOT NULL,enabled INTEGER NOT NULL,actor TEXT NOT NULL,created REAL NOT NULL);
      CREATE INDEX IF NOT EXISTS rate_key ON rate_versions(kind,provider,model,id);
    ''')
    path.chmod(0o600)
    return db

def rate_number(value):
    if value is None or value=='':return None
    if not isinstance(value,str) or not re.fullmatch(r'(0|[1-9][0-9]{0,7})(\.[0-9]{1,6})?',value):
        reject('价格需为非负金额，最多 6 位小数；留空表示未定价。')
    return str(Decimal(value))

def current_rows(db):
    return [dict(r) for r in db.execute('SELECT * FROM rate_versions WHERE id IN (SELECT MAX(id) FROM rate_versions GROUP BY kind,provider,model) ORDER BY kind,provider,model')]

def configuration(ops):
    with closing(connect(ops)) as db:
        db.execute('BEGIN')
        items=current_rows(db)
        history=[dict(r) for r in db.execute('SELECT * FROM rate_versions ORDER BY id DESC LIMIT 100')]
        revision=db.execute('SELECT COALESCE(MAX(id),0) FROM rate_versions').fetchone()[0]
    for r in items+history:
        r['rates']=json.loads(r['rates']);r['enabled']=bool(r['enabled'])
    return {'items':items,'history':history,'revision':revision,'currency':'CNY','mode':'shadow'}

def save(ops,actor,payload):
    if actor.get('role')!='superadmin':reject('只有超级管理员可以修改试算价格。')
    required={'kind','provider','model','rates','enabled','revision'}
    if not isinstance(payload,dict) or set(payload)!=required:reject('价格资料无效。')
    kind,provider,model=payload['kind'],payload['provider'],payload['model']
    if kind not in KINDS or not isinstance(provider,str) or not re.fullmatch(r'[A-Za-z0-9.-]{1,255}',provider):reject('请选择用量记录中的服务商域名。')
    if not isinstance(model,str) or not 1<=len(model)<=160 or any(ord(c)<32 for c in model):reject('模型名称无效。')
    if type(payload['enabled']) is not bool or type(payload['revision']) is not int:reject('价格版本无效。')
    raw=payload['rates']
    if not isinstance(raw,dict) or set(raw)-{'cost','sale','schedule'} or not {'cost','sale'}<=set(raw):reject('成本价和拟售价资料无效。')
    schedule=raw.get('schedule')
    if schedule not in (None,'deepseek_cn_2026'):reject('分时规则无效。')
    if schedule and (kind!='token' or provider.lower()!='api.deepseek.com' or model not in ('deepseek-flash','deepseek-v4-flash','deepseek-v4-flash-vision-exp')):reject('此分时规则仅适用于 DeepSeek 官方 Flash 模型。')
    rates={}
    for side in ('cost','sale'):
        if not isinstance(raw[side],dict) or set(raw[side])!=set(RATE_FIELDS):reject('价格字段无效。')
        rates[side]={k:rate_number(raw[side][k]) for k in RATE_FIELDS}
        forbidden=('unit',) if kind=='token' else ('input','output','cached')
        if any(rates[side][k] is not None for k in forbidden):reject('价格单位与用量类型不一致。')
    if schedule:rates['schedule']=schedule
    with closing(connect(ops)) as db:
        db.execute('BEGIN IMMEDIATE')
        revision=db.execute('SELECT COALESCE(MAX(id),0) FROM rate_versions').fetchone()[0]
        if revision!=payload['revision']:reject('价表已被其他管理员更新，请重新读取后保存。')
        cur=db.execute('INSERT INTO rate_versions(kind,provider,model,rates,enabled,actor,created) VALUES(?,?,?,?,?,?,?)',
            (kind,provider.lower(),model,json.dumps(rates),int(payload['enabled']),actor['username'],time.time()))
        db.commit()
    ops.log(actor['username'],'保存试算价格',[],kind+' / '+provider.lower()+' / '+model+' / v'+str(cur.lastrowid))
    return {'ok':True,'version':cur.lastrowid,'mode':'shadow'}

def money(nanos):
    return None if nanos is None else format(Decimal(nanos)/NANO,'.9f')

def calculate(row,rule,side):
    # No identity or missing-usage fallback may manufacture a zero price.
    if side=='sale' and row['status'] in ('error','cancelled','interrupted'):
        return 0,'failed_excluded'
    if side=='sale' and row['kind']!='token' and (row.get('is_retry') or row['purpose']=='preview'):
        return 0,'retry_or_preview_excluded'
    if row['status'] in ('pending','unknown','cancelled','interrupted'):
        return None,'pending_or_unknown'
    if row['kind']!='token' and row['status']!='completed':
        return None,'provider_cost_unknown'
    if row['kind']=='token' and row['status'] not in ('completed','error'):
        return None,'pending_or_unknown'
    if row['kind']=='token':
        for name in ('input_tokens','output_tokens'):
            value=row.get(name)
            if type(value) is not int or value<0:return None,'usage_missing'
    if not rule or not rule['enabled']:return None,'unpriced'
    prices=rule['rates'][side]
    multiplier=peak_multiplier(row['created']) if rule['rates'].get('schedule')=='deepseek_cn_2026' else 1
    if multiplier is None:return None,'calendar_unconfirmed'
    if row['kind']=='token':
        if prices['input'] is None or prices['output'] is None:return None,'unpriced'
        inp,out=row['input_tokens'],row['output_tokens']
        amount=Decimal(inp)*Decimal(prices['input'])+Decimal(out)*Decimal(prices['output'])
        if prices['cached'] is not None:
            cached=row.get('cached_tokens')
            if type(cached) is not int or not 0<=cached<=inp:return None,'cache_usage_missing'
            amount+=Decimal(cached)*(Decimal(prices['cached'])-Decimal(prices['input']))
        amount/=1000000
    else:
        if prices['unit'] is None:return None,'unpriced'
        quantity=row.get('image_count') if row['kind']=='image' else row.get('generated_chars')
        if type(quantity) is not int or quantity<0:return None,'usage_missing'
        if row['kind']=='image' and (type(row.get('image_count')) is not int or row['image_count']<1):return None,'usage_missing'
        amount=Decimal(quantity)*Decimal(prices['unit'])/(1000 if row['kind']=='tts' else 1)
    return int((amount*multiplier*NANO).quantize(Decimal('1'))),'estimated'

def query_args(ops,query):
    if not isinstance(query,dict) or set(query)-{'start','end','agent','user','kind','group','page'}:reject('试算查询条件无效。')
    today=datetime.now(ZONE).date()
    try:
        start=datetime.strptime(query.get('start',(today-timedelta(days=29)).isoformat()),'%Y-%m-%d').replace(tzinfo=ZONE)
        end=datetime.strptime(query.get('end',today.isoformat()),'%Y-%m-%d').replace(tzinfo=ZONE)+timedelta(days=1)
        page=int(query.get('page',1))
        if not start<end or end-start>timedelta(days=366) or not 1<=page<=10000:raise ValueError()
        agent=int(query['agent']) if query.get('agent') else None
        if agent is not None:ops.resolve(agent)
    except (ValueError,TypeError):reject('请选择有效日期（最多一年）、应用和页码。')
    kind=query.get('kind','')
    group=query.get('group','app')
    user=query.get('user','')
    if kind and kind not in KINDS or group not in ('app','user','day','model','purpose','request'):reject('试算类型或维度无效。')
    if not isinstance(user,str) or len(user)>200:reject('用户标识无效。')
    return start,end,page,agent,kind,group,user

def report(ops,query,configuration_data=None):
    start,end,page,agent,kind,group,user=query_args(ops,query)
    config=configuration(ops) if configuration_data is None else configuration_data
    rules={(r['kind'],r['provider'],r['model']):r for r in config['items']}
    summary={'requests':0,'cost_known':0,'sale_known':0,'both_known':0,'cost_unknown':0,'sale_unknown':0,'excluded_sale':0,'cost_nanos':0,'sale_nanos':0,'margin_nanos':0,'unattributed':0}
    groups={};catalog={};unavailable=[];missing_ledgers=[];details=[];seen=0
    def blank():return {k:0 for k in summary}
    def add(total,cost,sale,excluded,row):
        total['requests']+=1
        total['cost_known' if cost is not None else 'cost_unknown']+=1
        total['sale_known' if sale is not None else 'sale_unknown']+=1
        total['excluded_sale']+=int(excluded)
        total['unattributed']+=int(not row['user'])
        if cost is not None:total['cost_nanos']+=cost
        if sale is not None:total['sale_nanos']+=sale
        if cost is not None and sale is not None:
            total['both_known']+=1;total['margin_nanos']+=sale-cost
    for a in ([agent] if agent else ops.agents()):
        try:
            path=ops.data_path(a).with_name('token-usage.sqlite3')
            if not path.is_file():missing_ledgers.append(a);continue
            with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
                db.row_factory=sqlite3.Row
                tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table in ('token_requests','media_requests'):
                    if table not in tables:continue
                    if table=='token_requests' and kind and kind!='token' or table=='media_requests' and kind=='token':continue
                    where='agent=? AND created>=? AND created<?';args=[a,start.timestamp(),end.timestamp()]
                    if user:where+=' AND user=?';args.append(user)
                    if table=='media_requests' and kind:where+=' AND kind=?';args.append(kind)
                    # Read only metering columns, never prompts, secrets or message data.
                    columns='id,agent,user,purpose,model,provider,created,status,'+('input_tokens,output_tokens,cached_tokens' if table=='token_requests' else 'kind,generated_chars,image_count,is_retry')
                    for raw in db.execute('SELECT '+columns+' FROM '+table+' WHERE '+where,args):
                        seen+=1
                        if seen>200000:reject('记录超过 20 万条，请缩小日期范围或选择应用。')
                        row=dict(raw);row['kind']='token' if table=='token_requests' else row['kind']
                        if row['kind'] not in KINDS:continue
                        key=(row['kind'],row['provider'],row['model'])
                        if key not in catalog and len(catalog)>=500:reject('模型类型过多，请缩小日期范围或选择应用。')
                        catalog[key]=catalog.get(key,0)+1
                        rule=rules.get(key)
                        cost,cr=calculate(row,rule,'cost');sale,sr=calculate(row,rule,'sale')
                        excluded=sr in ('failed_excluded','retry_or_preview_excluded')
                        add(summary,cost,sale,excluded,row)
                        dimension=str(a) if group=='app' else row['user'] if group=='user' else datetime.fromtimestamp(row['created'],ZONE).date().isoformat() if group=='day' else row['provider']+' / '+row['model'] if group=='model' else row['purpose'] if group=='purpose' else row['id']
                        if group=='request':
                            details.append(dict(row,cost=money(cost),sale=money(sale),margin=money(sale-cost) if cost is not None and sale is not None else None,cost_state=cr,sale_state=sr,version=rule['id'] if rule else None,price_multiplier=peak_multiplier(row['created']) if rule and rule['rates'].get('schedule') else 1,dimension=dimension))
                        else:
                            gkey=(a,dimension)
                            if gkey not in groups and len(groups)>=20000:reject('分组过多，请缩小日期范围或选择应用。')
                            if gkey not in groups:groups[gkey]=dict(blank(),agent=a,dimension=dimension)
                            add(groups[gkey],cost,sale,excluded,row)
        except (sqlite3.Error,OSError):unavailable.append(a)
    def export(total):
        out=dict(total)
        for field in ('cost','sale','margin'):
            value=out.pop(field+'_nanos')
            count=out['both_known'] if field=='margin' else out[field+'_known']
            out[field]=money(value) if count else None
        return out
    if group=='request':
        items=sorted(details,key=lambda r:(-r['created'],r['agent'],r['kind'],r['id']))
    else:
        items=[export(r) for r in groups.values()]
        items.sort(key=lambda r:(-r['requests'],r['agent'],r['dimension']))
    return {'summary':export(summary),'items':items[(page-1)*50:page*50],'count':len(items),'page':page,'page_size':50,'group':group,'mode':'shadow','revision':config['revision'],'calculated_at':time.time(),'unavailable':unavailable,'missing_ledgers':missing_ledgers,
        'catalog':[dict(kind=k[0],provider=k[1],model=k[2],requests=v) for k,v in sorted(catalog.items())],
        'start':start.date().isoformat(),'end':(end-timedelta(days=1)).date().isoformat(),'currency':'CNY'}
