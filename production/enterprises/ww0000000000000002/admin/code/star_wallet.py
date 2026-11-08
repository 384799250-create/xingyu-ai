"""Isolated simulated funds. No payments, real money or AI gate."""
import wallet_closure
from contextlib import closing
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time
import shadow_pricing as pricing

CAP=1000000*1000000000
PAGE=50
LIMIT=1000
EVENT_COLUMNS=('id','agent','user','purpose','model','provider','created','updated','status')

def reject(message):
    from admin_ops import Rejected
    raise Rejected(message)

def connect(ops):
    path=ops.directory/'star-wallet.sqlite3'
    db=sqlite3.connect(str(path),timeout=10);db.row_factory=sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA foreign_keys=ON')
    db.executescript('''
      CREATE TABLE IF NOT EXISTS wallets(
        id TEXT PRIMARY KEY,corp TEXT NOT NULL,user TEXT NOT NULL,balance INTEGER NOT NULL DEFAULT 0 CHECK(balance>=0 AND balance<=1000000000000000),
        created REAL NOT NULL,actor TEXT NOT NULL,UNIQUE(corp,user));
      CREATE TABLE IF NOT EXISTS commands(
        key TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,result TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS events(
        id INTEGER PRIMARY KEY,wallet TEXT NOT NULL REFERENCES wallets(id),agent INTEGER NOT NULL,kind TEXT NOT NULL,request_id TEXT NOT NULL,
        snapshot TEXT NOT NULL,fingerprint TEXT NOT NULL,amount INTEGER,cost INTEGER,revision INTEGER,rule TEXT,state TEXT NOT NULL,
        reason TEXT NOT NULL,review INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL,seen REAL NOT NULL,
        UNIQUE(agent,kind,request_id));
      CREATE TABLE IF NOT EXISTS entries(
        id INTEGER PRIMARY KEY,wallet TEXT NOT NULL REFERENCES wallets(id),kind TEXT NOT NULL,amount INTEGER NOT NULL,
        balance INTEGER NOT NULL CHECK(balance>=0),event INTEGER UNIQUE REFERENCES events(id),
        reversal INTEGER UNIQUE REFERENCES entries(id),actor TEXT NOT NULL,note TEXT NOT NULL,created REAL NOT NULL);
      CREATE INDEX IF NOT EXISTS entry_wallet ON entries(wallet,id);
      CREATE INDEX IF NOT EXISTS event_wallet ON events(wallet,state,id);
      CREATE TABLE IF NOT EXISTS cursors(
        wallet TEXT NOT NULL REFERENCES wallets(id),agent INTEGER NOT NULL,source TEXT NOT NULL,
        updated REAL NOT NULL,request_id TEXT NOT NULL,PRIMARY KEY(wallet,agent,source));
      CREATE TRIGGER IF NOT EXISTS immutable_entries_update BEFORE UPDATE ON entries BEGIN SELECT RAISE(ABORT,'immutable ledger');END;
      CREATE TRIGGER IF NOT EXISTS immutable_entries_delete BEFORE DELETE ON entries BEGIN SELECT RAISE(ABORT,'immutable ledger');END;
      CREATE TRIGGER IF NOT EXISTS immutable_commands_update BEFORE UPDATE ON commands BEGIN SELECT RAISE(ABORT,'immutable command');END;
      CREATE TRIGGER IF NOT EXISTS immutable_commands_delete BEFORE DELETE ON commands BEGIN SELECT RAISE(ABORT,'immutable command');END;
    ''')
    db.executescript("""
      CREATE TABLE IF NOT EXISTS starcoin_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS starcoin_grants(corp TEXT NOT NULL,user TEXT NOT NULL,wallet TEXT NOT NULL UNIQUE REFERENCES wallets(id),created REAL NOT NULL,PRIMARY KEY(corp,user));
      CREATE TRIGGER IF NOT EXISTS immutable_grants_delete BEFORE DELETE ON starcoin_grants BEGIN SELECT RAISE(ABORT,'immutable grant');END;
      CREATE TRIGGER IF NOT EXISTS immutable_grants_update BEFORE UPDATE ON starcoin_grants BEGIN SELECT RAISE(ABORT,'immutable grant');END;
    """)
    db.execute("INSERT OR IGNORE INTO starcoin_meta VALUES('epoch',?)",(str(time.time()),));db.commit()
    db.execute("INSERT OR IGNORE INTO starcoin_meta VALUES('token_usage_policy_epoch',?)",(str(time.time()),));db.commit()
    import daily_bonus
    daily_bonus.bootstrap(db)
    import operating_fee
    operating_fee.bootstrap(db)
    path.chmod(0o600);return db

def strict(data,fields):
    if not isinstance(data,dict) or set(data)!=set(fields):reject('模拟余额参数无效。')

def superadmin(actor):
    if actor.get('role')!='superadmin':reject('只有超级管理员可以开通、充值或冲正模拟余额。')

def identifier(value):
    if not isinstance(value,str) or not re.fullmatch('[a-f0-9]{32}',value):reject('模拟账户编号无效。')
    return value

def get_wallet(db,ident):
    r=db.execute('SELECT * FROM wallets WHERE id=?',(identifier(ident),)).fetchone()
    if not r:reject('模拟账户不存在，请重新选择。')
    return dict(r)

def corp(ops,agent):
    if type(agent) is not int:reject('应用编号无效。')
    _,env,_=ops.resolve(agent)
    # Only the corporation identifier is retained or returned.
    value=next((line.split('=',1)[1].strip() for line in env.read_text('utf8').splitlines() if line.startswith('WECOM_CORP_ID=')),'')
    if not re.fullmatch('[A-Za-z0-9_-]{1,128}',value):reject('此应用的企业标识无法核对。')
    return value

def amount(value):
    if not isinstance(value,str) or not re.fullmatch(r'(0|[1-9][0-9]{0,6})(\.[0-9]{1,2})?',value):reject('模拟充值金额须为正数，最多两位小数。')
    n=int(Decimal(value)*1000000000)
    if not 0<n<=CAP:reject('单次模拟充值应大于零且不超过 100 万元。')
    return n

def command(db,actor,operation,data):
    key=data['request_id']
    if not isinstance(key,str) or not re.fullmatch('[a-f0-9]{32}',key):reject('请刷新页面后重试，操作编号无效。')
    fingerprint=hashlib.sha256(json.dumps([actor['username'],operation,data],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    prior=db.execute('SELECT * FROM commands WHERE key=?',(key,)).fetchone()
    if prior:
        if prior['fingerprint']!=fingerprint:reject('同一操作编号的内容发生变化，请重新发起操作。')
        return key,fingerprint,json.loads(prior['result'])
    return key,fingerprint,None

def complete(db,key,fingerprint,result):
    db.execute('INSERT INTO commands VALUES(?,?,?)',(key,fingerprint,json.dumps(result)))
    db.commit();return result

def create(ops,actor,data):
    superadmin(actor);strict(data,('agent','user','request_id'))
    company=corp(ops,data['agent']);user=data['user']
    if not isinstance(user,str) or not re.fullmatch(r'[^\s\x00-\x1f\x7f]{1,200}',user):reject('请填写有效的企业微信成员 UserID，不能使用昵称或网站账号代替。')
    with closing(connect(ops)) as db:
        db.execute('BEGIN IMMEDIATE');key,fp,prior=command(db,actor,'create',data)
        if prior:return prior
        existing=db.execute('SELECT id FROM wallets WHERE corp=? AND user=?',(company,user)).fetchone()
        ident=existing[0] if existing else secrets.token_hex(16)
        if not existing:db.execute('INSERT INTO wallets VALUES(?,?,?,0,?,?)',(ident,company,user,time.time(),actor['username']))
        return complete(db,key,fp,{'ok':True,'wallet':ident,'existing':bool(existing),'mode':'starcoin'})

def _ledger_post(db,wallet,kind,n,actor,note,event=None,reversal=None,*,respect_holds=True):
    current=get_wallet(db,wallet)
    if not 0<=current['balance']+n<=CAP:reject('模拟余额不足或超过 100 万元上限；尚未写入本笔流水。')
    if respect_holds and n<0 and db.execute("SELECT 1 FROM sqlite_master WHERE name='auto_requests'").fetchone():
        reserve=db.execute('SELECT COALESCE(SUM(held),0) FROM auto_requests WHERE wallet=? AND expires>?',(wallet,time.time())).fetchone()[0]
        if current['balance']+n<reserve:reject('当前存在预留中的模拟额度，请等待请求结束后再操作。')
    # Wallet and ledger change together under BEGIN IMMEDIATE.
    balance=current['balance']+n
    db.execute('UPDATE wallets SET balance=? WHERE id=?',(balance,wallet))
    cur=db.execute('INSERT INTO entries(wallet,kind,amount,balance,event,reversal,actor,note,created) VALUES(?,?,?,?,?,?,?,?,?)',
        (wallet,kind,n,balance,event,reversal,actor,note,time.time()))
    return cur.lastrowid,balance

def post(db,wallet,kind,n,actor,note,event=None,reversal=None):
    import daily_bonus, wallet_closure
    current=get_wallet(db,wallet)
    # Verified payment credits may arrive after closure; never reopen the account.
    if kind!='credit':wallet_closure.require_active(db,current['corp'],current['user'])
    return daily_bonus.post(db,wallet,kind,n,actor,note,event=event,reversal=reversal)


def credit(ops,actor,data):
    superadmin(actor);strict(data,('wallet','amount','note','request_id'))
    n=amount(data['amount']);note=data['note']
    if not isinstance(note,str) or not 1<=len(note.strip())<=200:reject('请填写模拟充值备注（最多 200 字）。')
    with closing(connect(ops)) as db:
        db.execute('BEGIN IMMEDIATE');key,fp,prior=command(db,actor,'credit',data)
        if prior:return prior
        entry,balance=post(db,data['wallet'],'credit',n,actor['username'],note.strip())
        return complete(db,key,fp,{'ok':True,'entry':entry,'balance':pricing.money(balance),'mode':'starcoin'})

def reverse(ops,actor,data):
    superadmin(actor);strict(data,('wallet','entry','note','request_id'))
    if type(data['entry']) is not int or data['entry']<1:reject('请选择有效流水。')
    note=data['note']
    if not isinstance(note,str) or not 1<=len(note.strip())<=200:reject('请填写冲正原因（最多 200 字）。')
    with closing(connect(ops)) as db:
        db.execute('BEGIN IMMEDIATE');key,fp,prior=command(db,actor,'reverse',data)
        if prior:return prior
        wallet=get_wallet(db,data['wallet'])
        entry=db.execute('SELECT * FROM entries WHERE id=? AND wallet=?',(data['entry'],wallet['id'])).fetchone()
        if not entry or entry['kind']=='reversal':reject('只能冲正本账户的原始充值或扣费流水。')
        if db.execute('SELECT 1 FROM entries WHERE reversal=?',(entry['id'],)).fetchone():reject('该流水已冲正，不能重复冲正。')
        ident,balance=post(db,wallet['id'],'reversal',-entry['amount'],actor['username'],note.strip(),reversal=entry['id'])
        if entry['event']:db.execute("UPDATE events SET state='reversed' WHERE id=?",(entry['event'],))
        return complete(db,key,fp,{'ok':True,'entry':ident,'balance':pricing.money(balance),'mode':'starcoin'})

def canonical(row):
    fields=('id','agent','user','kind','purpose','model','provider','created','status','input_tokens','output_tokens','cached_tokens','generated_chars','image_count','is_retry','billing_turn','bill_class','billing_state','billing_version')
    value={k:row[k] for k in fields if k in row}
    raw=json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'))
    financial=dict(value);financial.pop('billing_state',None)
    return raw,hashlib.sha256(json.dumps(financial,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()

def ingest(db,wallet,row,rules,revision):
    raw,fp=canonical(row)
    prior=db.execute('SELECT * FROM events WHERE agent=? AND kind=? AND request_id=?',(row['agent'],row['kind'],row['id'])).fetchone()
    if prior and prior['wallet']!=wallet['id']:reject('用量归属发生冲突，已停止本账户同步，请人工核对。')
    if prior and prior['amount'] is not None:
        if prior['fingerprint']!=fp:db.execute('UPDATE events SET review=1,seen=? WHERE id=?',(time.time(),prior['id']))
        return
    rule=rules.get((row['kind'],row['provider'],row['model']))
    actual_tokens=row.get('billing_version')==2 and row['kind']=='token'
    # Actual provider usage, including helpers/retries, does not depend on delivery.
    # Pending estimates never become debits; missing usage remains unresolved.
    priced=dict(row,status='completed') if actual_tokens and row['status']!='pending' else row
    n,reason=pricing.calculate(priced,rule,'sale');cost,_=pricing.calculate(priced,rule,'cost')
    if actual_tokens:
        if row['status']=='pending':n=None;cost=None;reason='pending_or_unknown'
        elif n is not None:reason='actual_token_usage'
    elif row.get('billing_version')==1:
        if row.get('bill_class') not in ('primary','media') or row.get('is_retry'):
            n=0;reason='internal_excluded'
        elif row.get('billing_state')=='excluded':
            n=0;reason='not_delivered_excluded'
        elif row['status'] in ('error','cancelled','interrupted'):
            n=0;reason='failed_excluded'
        elif row.get('billing_state')!='delivered':
            n=None;reason='awaiting_delivery'
        elif row['status'] in ('pending','unknown'):
            n=None;reason='pending_or_unknown'
        if row['status']=='pending':n=None;reason='pending_or_unknown'
    elif row['status'] in ('pending','unknown'):
        n=None;reason='pending_or_unknown'
    if n is not None and (type(n) is not int or not 0<=n<=CAP):n=None;reason='amount_out_of_range'
    if cost is not None and not 0<=cost<=CAP:cost=None
    state='waiting' if n is None else 'queued' if n>0 else 'excluded'
    values=(raw,fp,n,cost,revision,json.dumps(rule,ensure_ascii=False) if rule else None,state,reason,time.time())
    if prior:
        db.execute('UPDATE events SET snapshot=?,fingerprint=?,amount=?,cost=?,revision=?,rule=?,state=?,reason=?,seen=? WHERE id=?',values+(prior['id'],))
    else:
        db.execute('INSERT INTO events(wallet,agent,kind,request_id,snapshot,fingerprint,amount,cost,revision,rule,state,reason,created,seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (wallet['id'],row['agent'],row['kind'],row['id'])+values[:-1]+(row['created'],values[-1]))

def legacy_settle(ops,actor,data):
    if actor.get('role') not in ('superadmin','operator'):reject('只读管理员不能同步模拟扣费。')
    strict(data,('wallet',));ident=identifier(data['wallet'])
    with closing(connect(ops)) as db:wallet=get_wallet(db,ident)
    config=pricing.configuration(ops);rules={(r['kind'],r['provider'],r['model']):r for r in config['items']}
    warnings=[];read=0;more=False
    for agent in ops.agents():
        try:
            if corp(ops,agent)!=wallet['corp']:continue
            path=ops.data_path(agent).with_name('token-usage.sqlite3')
            if not path.is_file():continue
            with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=3)) as source:
                source.row_factory=sqlite3.Row
                tables={r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table in ('token_requests','media_requests'):
                    if table not in tables:continue
                    with closing(connect(ops)) as db:
                        db.execute('BEGIN IMMEDIATE')
                        cursor=db.execute('SELECT * FROM cursors WHERE wallet=? AND agent=? AND source=?',(ident,agent,table)).fetchone()
                        updated,last=(cursor['updated'],cursor['request_id']) if cursor else (0,'')
                        cols=EVENT_COLUMNS+(('input_tokens','output_tokens','cached_tokens') if table=='token_requests' else ('kind','generated_chars','image_count','is_retry'))
                        rows=source.execute('SELECT '+','.join(cols)+' FROM '+table+' WHERE agent=? AND user=? AND created>=? AND (updated>? OR (updated=? AND id>?)) ORDER BY updated,id LIMIT ?',
                            (agent,wallet['user'],wallet['created'],updated,updated,last,LIMIT+1)).fetchall()
                        more|=len(rows)>LIMIT
                        for r in rows[:LIMIT]:
                            row=dict(r)
                            if table=='token_requests':row['kind']='token'
                            if row['kind'] in pricing.KINDS:ingest(db,wallet,row,rules,config['revision'])
                            updated,last=row['updated'],row['id'];read+=1
                        db.execute('INSERT INTO cursors VALUES(?,?,?,?,?) ON CONFLICT(wallet,agent,source) DO UPDATE SET updated=excluded.updated,request_id=excluded.request_id',(ident,agent,table,updated,last))
                        db.commit()
        except (OSError,sqlite3.Error):warnings.append(agent)
    with closing(connect(ops)) as db:
        db.execute('BEGIN IMMEDIATE')
        # Re-evaluate missing rates even if the source record itself has not changed.
        for e in db.execute('SELECT * FROM events WHERE wallet=? AND amount IS NULL ORDER BY id LIMIT 2000',(ident,)).fetchall():
            ingest(db,wallet,json.loads(e['snapshot']),rules,config['revision'])
        charged=0;total=0;insufficient=0
        for e in db.execute("SELECT * FROM events WHERE wallet=? AND state IN ('queued','insufficient') ORDER BY created,id LIMIT 2000",(ident,)).fetchall():
            if e['review']:continue
            if get_wallet(db,ident)['balance']<e['amount']:
                db.execute("UPDATE events SET state='insufficient' WHERE id=?",(e['id'],));insufficient+=1;continue
            post(db,ident,'debit',-e['amount'],actor['username'],'同步用量模拟扣费',event=e['id'])
            db.execute("UPDATE events SET state='paid' WHERE id=?",(e['id'],));charged+=1;total+=e['amount']
        balance=get_wallet(db,ident)['balance'];db.commit()
        remaining=db.execute("SELECT COUNT(*) FROM events WHERE wallet=? AND state IN ('queued','insufficient')",(ident,)).fetchone()[0]
        more|=remaining>insufficient
    return {'ok':True,'mode':'starcoin','read':read,'charged':charged,'amount':pricing.money(total),'balance':pricing.money(balance),'insufficient':insufficient,'unavailable':sorted(set(warnings)),'more':more}

def page_number(value):
    try:
        n=int(value)
        if not 1<=n<=100000:raise ValueError()
        return n
    except (TypeError,ValueError):reject('页码无效。')

def overview(ops):
    with closing(connect(ops)) as db:
        db.execute('BEGIN')
        rows=[]
        for raw in db.execute('SELECT * FROM wallets WHERE '+wallet_closure.active_sql(db)+' ORDER BY created DESC LIMIT 500'):
            r=dict(raw);r['balance']=pricing.money(r['balance'])
            if (ops.directory/'auto-billing.enabled').is_file():
                import star_billing as auto_billing
                r['reserved']=pricing.money(auto_billing.held(db,r['id']))
            counts=dict(db.execute('SELECT state,COUNT(*) FROM events WHERE wallet=? GROUP BY state',(r['id'],)).fetchall());r['counts']=counts
            rows.append(r)
        count=db.execute('SELECT COUNT(*) FROM wallets WHERE '+wallet_closure.active_sql(db)).fetchone()[0]
    return {'items':rows,'count':count,'limit':500,'mode':'starcoin','automatic':(ops.directory/'auto-billing.enabled').is_file()}

def detail(ops,query):
    if not isinstance(query,dict) or set(query)-{'wallet','page','event_page'}:reject('查询参数无效。')
    page=page_number(query.get('page',1));event_page=page_number(query.get('event_page',1))
    with closing(connect(ops)) as db:
        db.execute('BEGIN');wallet=get_wallet(db,query.get('wallet'))
        summary=db.execute('SELECT COUNT(*),COALESCE(SUM(amount),0) FROM entries WHERE wallet=?',(wallet['id'],)).fetchone()
        wallet['consistent']=summary[1]==wallet['balance'];wallet['balance']=pricing.money(wallet['balance'])
        if (ops.directory/'auto-billing.enabled').is_file():
            import star_billing as auto_billing
            wallet['reserved']=pricing.money(auto_billing.held(db,wallet['id']))
        entries=[]
        for raw in db.execute('SELECT e.*,r.id AS reversed_by,v.agent,v.kind AS resource_kind,v.request_id FROM entries e LEFT JOIN entries r ON r.reversal=e.id LEFT JOIN events v ON v.id=e.event WHERE e.wallet=? ORDER BY e.id DESC LIMIT ? OFFSET ?',(wallet['id'],PAGE,(page-1)*PAGE)):
            r=dict(raw);r['amount']=pricing.money(r['amount']);r['balance']=pricing.money(r['balance']);entries.append(r)
        events=[]
        count=db.execute('SELECT COUNT(*) FROM events WHERE wallet=?',(wallet['id'],)).fetchone()[0]
        for raw in db.execute('SELECT * FROM events WHERE wallet=? ORDER BY created DESC,id DESC LIMIT ? OFFSET ?',(wallet['id'],PAGE,(event_page-1)*PAGE)):
            r=dict(raw);r['amount']=pricing.money(r['amount']);r['cost']=pricing.money(r['cost']);r['snapshot']=json.loads(r['snapshot']);r.pop('rule');r.pop('fingerprint');events.append(r)
    return {'wallet':wallet,'entries':entries,'entry_count':summary[0],'page':page,'events':events,'event_count':count,'event_page':event_page,'page_size':PAGE,'mode':'starcoin'}


def settle(ops,actor,data):
    if (ops.directory/'auto-billing.enabled').is_file():
        import star_billing as auto_billing
        return auto_billing.settle_wallet(ops,actor,data)
    return legacy_settle(ops,actor,data)


# Starcoin values are stored in billionths of yuan; the API exposes millionths of
# a starcoin. Existing simulation data is never imported into this database.
def token_policy_epoch(db):
    return float(db.execute("SELECT value FROM starcoin_meta WHERE key='token_usage_policy_epoch'").fetchone()[0])

def epoch(db):
    return float(db.execute("SELECT value FROM starcoin_meta WHERE key='epoch'").fetchone()[0])

def account(db,company,user,created=None):
    import daily_bonus
    import wallet_closure
    wallet_closure.require_active(db,company,user)
    if not isinstance(user,str) or not re.fullmatch(r'[^\s\x00-\x1f\x7f]{1,200}',user):
        raise ValueError('Invalid member')
    row=db.execute('SELECT * FROM wallets WHERE corp=? AND user=?',(company,user)).fetchone()
    if row:
        daily_bonus.refresh(db,row['id'])
        import operating_fee
        operating_fee.reconcile(db,row['id'])
        return get_wallet(db,row['id'])
    ident=secrets.token_hex(16)
    db.execute('INSERT INTO wallets VALUES(?,?,?,0,?,?)',(ident,company,user,epoch(db),'starcoin-opening'))
    db.execute('INSERT INTO starcoin_grants VALUES(?,?,?,?)',(company,user,ident,time.time()))
    _ledger_post(db,ident,'gift',1000000000,'starcoin-opening','首次开通赠送1000长期星币')
    daily_bonus.refresh(db,ident)
    import operating_fee
    operating_fee.reconcile(db,ident)
    return get_wallet(db,ident)


def coins(value):
    if value is None:return None
    return format(Decimal(str(value))*1000,'f')

def public(value):
    if isinstance(value,list):return [public(x) for x in value]
    if isinstance(value,dict):
        return {k:coins(v) if k in ('amount','balance','reserved','available','estimated','cost','daily_amount','daily_remaining','permanent_balance') else public(v) for k,v in value.items()}
    return value

def star_overview(ops):
    return public(overview(ops))

def star_detail(ops,query):
    return public(detail(ops,query))

# Only a verified payment adapter may be added later. There is no manual credit,
# payment creation, callback or refund endpoint in this release.
def credit(ops,actor,data):reject('充值维护中，暂未开放。')
def create(ops,actor,data):reject('星币账户随成员首次使用自动开通，无需手动创建。')
def reverse(ops,actor,data):reject('星币账本调整暂未开放，请联系维护人员核对。')
