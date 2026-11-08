"""Private starcoin billing bridge; prepaid gifts and a closed recharge surface."""
import asyncio
import wallet_closure
from contextlib import closing
import json
import logging
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import struct
import threading
import time
import star_wallet as wallet
from decimal import Decimal
import shadow_pricing as pricing

LOG=logging.getLogger('xiaoyi-sim-billing')
TABLES={'token':'token_requests','image':'media_requests','tts':'media_requests'}

def connect(ops):
    db=wallet.connect(ops)
    db.executescript('''
      CREATE TABLE IF NOT EXISTS auto_requests(
        agent INTEGER NOT NULL,kind TEXT NOT NULL,id TEXT NOT NULL,wallet TEXT NOT NULL REFERENCES wallets(id),
        turn_id TEXT NOT NULL,rule TEXT,revision INTEGER,estimated INTEGER,held INTEGER NOT NULL DEFAULT 0,
        expires REAL NOT NULL,decision TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(agent,kind,id));
      CREATE INDEX IF NOT EXISTS auto_holds ON auto_requests(wallet,expires);
      CREATE TABLE IF NOT EXISTS auto_cursors(agent INTEGER,source TEXT,updated REAL,id TEXT,PRIMARY KEY(agent,source));
      CREATE TABLE IF NOT EXISTS auto_health(agent INTEGER PRIMARY KEY,checked REAL NOT NULL,error TEXT NOT NULL);
    ''')
    return db

def ro(path):
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=3);db.row_factory=sqlite3.Row
    return closing(db)

account=wallet.account

def held(db,ident):
    return db.execute('SELECT COALESCE(SUM(held),0) FROM auto_requests WHERE wallet=? AND expires>?',(ident,time.time())).fetchone()[0]

def daily_tick(engine):
    import daily_bonus
    with engine.lock,closing(connect(engine.ops)) as db:
        db.execute('BEGIN IMMEDIATE')
        if daily_bonus.active(db):
            # Known members share one wallet across applications in this enterprise.
            owners={(r['corp'],r['user']) for r in db.execute('SELECT corp,user FROM wallets')}
            registry=engine.ops.root/'app-pool/registry.sqlite3'
            if registry.exists():
                with ro(registry) as source:
                    for r in source.execute("SELECT corp,owner FROM apps WHERE owner!='' AND state NOT IN ('free','provisioning','failed')"):
                        if not r['owner'].startswith(('poolslot','verify-')):owners.add((r['corp'],r['owner']))
            for company,user in owners:
                if not engine.frozen(company,user):account(db,company,user)
        else:
            for row in db.execute('SELECT id FROM wallets').fetchall():daily_bonus.refresh(db,row['id'])
        db.commit()

class Engine:
    def __init__(self,ops):
        self.ops=ops;self.paths={};self.corps={};self.lock=threading.RLock()
        with closing(connect(ops)):pass

    def path(self,agent):
        if agent not in self.paths:self.paths[agent]=self.ops.data_path(agent).with_name('token-usage.sqlite3')
        return self.paths[agent]

    def corp(self,agent):
        if agent not in self.corps:self.corps[agent]=wallet.corp(self.ops,agent)
        return self.corps[agent]

    def source(self,agent,kind,ident):
        if kind not in TABLES or not isinstance(ident,str) or not re.fullmatch('[a-f0-9]{32}',ident):raise ValueError('Invalid source')
        with ro(self.path(agent)) as db:
            raw=db.execute('SELECT * FROM '+TABLES[kind]+' WHERE id=? AND agent=?',(ident,agent)).fetchone()
        if not raw:raise ValueError('Source missing')
        row=dict(raw)
        row['kind']='token' if kind=='token' else row['kind']
        if row['kind']!=kind:raise ValueError('Source kind mismatch')
        return row

    def receipt(self,agent,row):
        turn=row.get('billing_turn')
        if not turn:return None
        with ro(self.path(agent)) as db:
            r=db.execute('SELECT * FROM billing_turns WHERE id=? AND user=?',(turn,row['user'])).fetchone()
        if not r:return None
        r=dict(r)
        if r['state']=='excluded':return r
        # Recover a committed delivery even when a process died before writing its meter receipt.
        try:
            with ro(self.path(agent).with_name('kf.sqlite3')) as db:
                if r['scene']=='chat':
                    source=db.execute('SELECT status,answer FROM member_inbox WHERE msgid=? AND agent=? AND user=?',(r['source_id'],agent,row['user'])).fetchone()
                    if source:
                        if source['status']=='done':
                            plan=json.loads(source['answer'] or '{}')
                            r['state']='delivered' if (plan.get('remember') or plan.get('media')) and not plan.get('tool_error') else 'excluded'
                        elif source['status'] in ('failed','cancelled'):r['state']='excluded'
                elif r['scene']=='proactive':
                    source=db.execute('SELECT status,payload FROM suite_outbox WHERE id=? AND agent=? AND user=?',(r['source_id'],agent,row['user'])).fetchone()
                    if source:
                        if source['status']=='done':r['state']='delivered' if source['payload']!='[NO_OP]' else 'excluded'
                        elif source['status'] in ('failed','cancelled'):r['state']='excluded'
                elif r['scene']=='greeting':
                    source=db.execute('SELECT status FROM member_greeting_outbox WHERE id=? AND agent=? AND user=?',(r['source_id'],agent,row['user'])).fetchone()
                    if source:
                        if source['status']=='done':r['state']='delivered'
                        elif source['status'] in ('failed','cancelled'):r['state']='excluded'
                else:r['state']='excluded'
        except (sqlite3.Error,ValueError):
            # A recorded delivered receipt remains valid; absent evidence is never invented.
            pass
        return r

    def register(self,agent,row,config):
        with closing(connect(self.ops)) as db:
            actual_tokens=row['kind']=='token' and row['created']>=wallet.token_policy_epoch(db)
        # No synthetic delivery receipt: 'usage' identifies per-request billing.
        receipt={'id':row.get('billing_turn') or 'token:'+row['id'],'state':'usage','created':row['created']} if actual_tokens else self.receipt(agent,row)
        if not receipt:return None
        row=dict(row,billing_state=receipt['state'],billing_version=2 if actual_tokens else 1)
        rules={(r['kind'],r['provider'],r['model']):r for r in config['items']}
        with closing(connect(self.ops)) as db:
            db.execute('BEGIN IMMEDIATE')
            w=account(db,self.corp(agent),row['user'],receipt['created'])
            if row['created']<w['created']:
                db.commit();return None
            prior=db.execute('SELECT * FROM auto_requests WHERE agent=? AND kind=? AND id=?',(agent,row['kind'],row['id'])).fetchone()
            if prior and prior['wallet']!=w['id']:raise ValueError('Source owner changed')
            rule=json.loads(prior['rule']) if prior and prior['rule'] else rules.get((row['kind'],row['provider'],row['model']))
            revision=prior['revision'] if prior and prior['rule'] else config['revision']
            if not prior:
                estimate_row=dict(row,status='completed',input_tokens=row.get('estimate_input'),output_tokens=row.get('estimate_output'),cached_tokens=0,
                                  generated_chars=row.get('submitted_chars',0),image_count=1)
                estimate,_=pricing.calculate(estimate_row,rule,'sale')
                price_known=estimate is not None
                if not actual_tokens and (row.get('bill_class') not in ('primary','media') or row.get('is_retry')):estimate=0
                if estimate is not None and not 0<=estimate<=wallet.CAP:estimate=None
                available=w['balance']-held(db,w['id'])
                import operating_fee
                op=operating_fee.state(db,w['id'])
                if op:available-=op['debt']
                reservation=estimate if estimate is not None and available>=estimate else 0
                decision='unpriced' if estimate is None else 'reserved' if reservation or estimate==0 else 'insufficient'
                db.execute('INSERT INTO auto_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (agent,row['kind'],row['id'],w['id'],receipt['id'],json.dumps(rule) if rule and price_known else None,revision,estimate,reservation,time.time()+600,decision,row['created']))
            elif not prior['rule'] and rule and pricing.calculate(dict(row,status='completed',input_tokens=0,output_tokens=0,cached_tokens=0),rule,'sale')[0] is not None:
                db.execute('UPDATE auto_requests SET rule=?,revision=? WHERE agent=? AND kind=? AND id=?',(json.dumps(rule),revision,agent,row['kind'],row['id']))
            use_rules={(row['kind'],row['provider'],row['model']):rule} if rule else {}
            wallet.ingest(db,w,row,use_rules,revision)
            # Requests that failed, were excluded, finished delivery, or outlived their lease release holds.
            if row['status']!='pending' and (row['status']!='completed' or receipt['state'] not in ('working','pending')) or receipt['state']=='excluded':
                db.execute('UPDATE auto_requests SET held=0 WHERE agent=? AND kind=? AND id=?',(agent,row['kind'],row['id']))
            db.execute('UPDATE auto_requests SET held=0 WHERE expires<=?',(time.time(),))
            db.commit()
            return w['id']

    def charge(self,ident=None):
        charged=0;total=0;insufficient=0
        with closing(connect(self.ops)) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE auto_requests SET held=0 WHERE expires<=?',(time.time(),))
            sql="SELECT * FROM events WHERE state IN ('queued','insufficient') AND review=0 AND EXISTS (SELECT 1 FROM auto_requests r WHERE r.agent=events.agent AND r.kind=events.kind AND r.id=events.request_id AND r.wallet=events.wallet)"
            sql+=' AND '+wallet_closure.active_sql(db,'events')
            args=[]
            if ident:sql+=' AND wallet=?';args.append(ident)
            for e in db.execute(sql+' ORDER BY created,id LIMIT 2000',args).fetchall():
                available=wallet.get_wallet(db,e['wallet'])['balance']-held(db,e['wallet'])
                if available<e['amount']:
                    db.execute("UPDATE events SET state='insufficient' WHERE id=?",(e['id'],));insufficient+=1;continue
                wallet.post(db,e['wallet'],'debit',-e['amount'],'starcoin-usage','用量星币扣费',event=e['id'])
                db.execute("UPDATE events SET state='paid' WHERE id=?",(e['id'],));charged+=1;total+=e['amount']
            db.commit()
        return dict(charged=charged,amount=pricing.money(total),insufficient=insufficient)

    def prepare(self,agent,kind,request_id):
        with self.lock:
            row=self.source(agent,kind,request_id)
            ident=self.register(agent,row,pricing.configuration(self.ops))
            if not ident:return dict(ok=True,mode='starcoin',decision='internal')
            with closing(connect(self.ops)) as db:
                r=db.execute('SELECT decision,estimated,held FROM auto_requests WHERE agent=? AND kind=? AND id=?',(agent,kind,request_id)).fetchone()
                return dict(ok=True,mode='starcoin',decision=r['decision'],estimated=pricing.money(r['estimated']),reserved=pricing.money(r['held']),enforced=False)

    def sync(self,agent=None,ident=None):
        with self.lock:
            config=pricing.configuration(self.ops);read=0;warnings=[]
            for a in [agent] if agent is not None else self.ops.agents():
                try:
                    with ro(self.path(a)) as source:
                        if not source.execute("SELECT 1 FROM sqlite_master WHERE name='billing_turns'").fetchone():continue
                        for table in ('token_requests','media_requests'):
                            with closing(connect(self.ops)) as db:
                                cursor=db.execute('SELECT updated,id FROM auto_cursors WHERE agent=? AND source=?',(a,table)).fetchone()
                            updated,last=tuple(cursor) if cursor else (0,'')
                            with closing(connect(self.ops)) as db:policy_epoch=wallet.token_policy_epoch(db)
                            eligible="(billing_turn!='' OR (created>=? AND user!=''))" if table=='token_requests' else "billing_turn!=''"
                            params=(policy_epoch,updated,updated,last) if table=='token_requests' else (updated,updated,last)
                            rows=source.execute('SELECT * FROM '+table+' WHERE '+eligible+" AND (updated>? OR (updated=? AND id>?)) ORDER BY updated,id LIMIT 500",params).fetchall()
                            for raw in rows:
                                r=dict(raw)
                                if table=='token_requests':r['kind']='token'
                                self.register(a,r,config);read+=1
                            if rows:
                                with closing(connect(self.ops)) as db,db:
                                    # Never rewind the cursor when a manual sync and worker race.
                                    db.execute('INSERT INTO auto_cursors VALUES(?,?,?,?) ON CONFLICT(agent,source) DO UPDATE SET updated=excluded.updated,id=excluded.id WHERE (excluded.updated,excluded.id)>(auto_cursors.updated,auto_cursors.id)',(a,table,rows[-1]['updated'],rows[-1]['id']))
                    # Delivery updates do not alter provider usage; recheck unresolved receipts too.
                    with closing(connect(self.ops)) as db:
                        pending=db.execute("SELECT r.kind,r.id FROM auto_requests r JOIN events e ON e.agent=r.agent AND e.kind=r.kind AND e.request_id=r.id WHERE r.agent=? AND e.review=0 AND (e.amount IS NULL OR r.held>0) ORDER BY e.seen,e.id LIMIT 1000",(a,)).fetchall()
                    for r in pending:
                        try:self.register(a,self.source(a,r['kind'],r['id']),config)
                        except ValueError:
                            # Reclaimed applications may have removed the old source ledger.
                            # Preserve its financial audit trail without blocking the next owner.
                            with closing(connect(self.ops)) as db,db:
                                db.execute('UPDATE auto_requests SET held=0 WHERE agent=? AND kind=? AND id=?',(a,r['kind'],r['id']))
                                db.execute("UPDATE events SET review=1,reason='source_unavailable' WHERE agent=? AND kind=? AND request_id=?",(a,r['kind'],r['id']))
                    with closing(connect(self.ops)) as db,db:db.execute('INSERT OR REPLACE INTO auto_health VALUES(?,?,?)',(a,time.time(),''))
                except (OSError,sqlite3.Error,ValueError):
                    warnings.append(a)
                    with closing(connect(self.ops)) as db,db:db.execute('INSERT OR REPLACE INTO auto_health VALUES(?,?,?)',(a,time.time(),'source_unavailable'))
            result=self.charge(ident)
            if ident:
                with closing(connect(self.ops)) as db:balance=wallet.get_wallet(db,ident)['balance']
                result['balance']=pricing.money(balance)
            return dict(ok=True,mode='starcoin',read=read,unavailable=warnings,more=read>=500,**result)

    def balance(self,agent,user,page=1,view="requests",group=None):
        if type(page) is not int or not 1<=page<=100000:raise ValueError('Invalid page')
        if view not in ('requests','turns','recharges') or (group is not None and (view!='turns' or not isinstance(group,str) or not re.fullmatch('[1-9][0-9]{0,17}',group))):raise ValueError('Invalid bill view')
        with self.lock,closing(connect(self.ops)) as db:
            db.execute('BEGIN IMMEDIATE');w=account(db,self.corp(agent),user)
            import daily_bonus
            stamp=time.time();daily_bonus.refresh(db,w['id'],stamp);w=wallet.get_wallet(db,w['id'])
            bonus=daily_bonus.summary(db,w['id'],stamp);db.commit()
            db.execute('BEGIN');reserve=held(db,w['id'])
            def turn_receipt(source_agent,turn):
                try:
                    receipt=self.receipt(source_agent,{'billing_turn':turn,'user':user})
                    return receipt['state'] if receipt else None
                except (OSError,sqlite3.Error,ValueError):return None
            if view=='turns' and group is not None:
                import turn_billing
                detail=turn_billing.detail(db,w['id'],group,page,turn_receipt)
                return dict(ok=True,mode='starcoin',balance=pricing.money(w['balance']),reserved=pricing.money(reserve),available=pricing.money(w['balance']-reserve),**bonus,**detail)
            # Read-only, independently paginated credits; filter before LIMIT/OFFSET.
            if view=='recharges':
                count=db.execute("SELECT COUNT(*) FROM entries WHERE wallet=? AND kind IN ('credit','gift','daily_gift') AND amount>0",(w['id'],)).fetchone()[0]
                entries=[]
                for raw in db.execute("SELECT id,kind,amount,created FROM entries WHERE wallet=? AND kind IN ('credit','gift','daily_gift') AND amount>0 ORDER BY id DESC LIMIT 20 OFFSET ?",(w['id'],(page-1)*20)):
                    item=dict(raw);item['amount']=pricing.money(item['amount']);entries.append(item)
                return dict(ok=True,mode='starcoin',record_view='recharges-v1',balance=pricing.money(w['balance']),reserved=pricing.money(reserve),available=pricing.money(w['balance']-reserve),**bonus,entries=entries,count=count,page=page,page_size=20)
            count=db.execute('SELECT COUNT(*) FROM entries WHERE wallet=?',(w['id'],)).fetchone()[0]
            entries=[]
            for r in db.execute('SELECT e.id,e.kind,e.amount,e.balance,e.created,v.agent,v.kind AS resource_kind,v.snapshot FROM entries e LEFT JOIN events v ON v.id=e.event WHERE e.wallet=? ORDER BY e.id DESC LIMIT 20 OFFSET ?',(w['id'],(page-1)*20)):
                d=dict(r);snap=json.loads(d.pop('snapshot') or '{}')
                d.update(amount=pricing.money(d['amount']),balance=pricing.money(d['balance']),scene=snap.get('billing_turn','').split(':')[0],
                         purpose=snap.get('purpose',''),is_retry=bool(snap.get('is_retry')),billing_version=snap.get('billing_version',1),
                         input_tokens=snap.get('input_tokens'),output_tokens=snap.get('output_tokens'),image_count=snap.get('image_count'),generated_chars=snap.get('generated_chars'))
                entries.append(d)
            counts=dict(db.execute('SELECT state,COUNT(*) FROM events WHERE wallet=? GROUP BY state',(w['id'],)).fetchall())
            events=[]
            for r in db.execute('SELECT id,agent,kind,amount,state,reason,created,snapshot,review FROM events WHERE wallet=? ORDER BY created DESC,id DESC LIMIT 20 OFFSET ?',(w['id'],(page-1)*20)):
                d=dict(r);snap=json.loads(d.pop('snapshot'))
                d.update(amount=pricing.money(d['amount']),scene=snap.get('billing_turn','').split(':')[0],
                         purpose=snap.get('purpose',''),is_retry=bool(snap.get('is_retry')),billing_version=snap.get('billing_version',1),
                         input_tokens=snap.get('input_tokens'),output_tokens=snap.get('output_tokens'),image_count=snap.get('image_count'),generated_chars=snap.get('generated_chars'))
                events.append(d)
            result=dict(ok=True,mode='starcoin',balance=pricing.money(w['balance']),reserved=pricing.money(reserve),available=pricing.money(w['balance']-reserve),**bonus,entries=entries,count=count,page=page,page_size=20,counts=counts,automatic=True,events=events,event_count=sum(counts.values()))
            if view=='turns':
                import turn_billing
                result.update(turn_billing.overview(db,w['id'],page,turn_receipt))
                result['usage_summary']=usage_summary(db,w['id'])
            return result

def usage_summary(db,ident,as_of=None):
    """Rolling settled totals, with independently summed usage-only periods."""
    stamp=time.time() if as_of is None else as_of
    rows=db.execute("""SELECT COALESCE(v.created,e.created) AS used_at,
        -e.amount AS amount,e.actor AS actor
        FROM entries e LEFT JOIN events v ON v.id=e.event AND v.wallet=e.wallet
        WHERE e.wallet=? AND e.kind='debit' AND e.amount<0
        AND COALESCE(v.created,e.created)>=? AND COALESCE(v.created,e.created)<=?
        AND NOT EXISTS(SELECT 1 FROM entries r WHERE r.reversal=e.id AND r.wallet=e.wallet)""",
        (ident,stamp-30*86400,stamp))
    totals={1:0,7:0,30:0};usage_only={1:0,7:0,30:0}
    for row in rows:
        for days in totals:
            if row['used_at']>=stamp-days*86400:
                totals[days]+=row['amount']
                if row['actor']!='daily-operation-fee':usage_only[days]+=row['amount']
    return {'version':'rolling-usage-v1','as_of':stamp,
            'periods':[{'days':days,'amount':pricing.money(total)} for days,total in totals.items()],
            'usage_only_periods':[{'days':days,'amount':pricing.money(total)} for days,total in usage_only.items()]}



def settle_wallet(ops,actor,data):
    if actor.get('role') not in ('superadmin','operator'):wallet.reject('只读管理员不能同步模拟扣费。')
    wallet.strict(data,('wallet',));ident=wallet.identifier(data['wallet'])
    with closing(connect(ops)) as db:wallet.get_wallet(db,ident)
    return Engine(ops).sync(ident=ident)


class StarOps:
    """Billing alone includes the independent pilot; no allocation-pool changes."""
    def __init__(self,ops):
        self.original=ops;self.root=ops.root;self.directory=ops.directory
    def __getattr__(self,key):return getattr(self.original,key)
    def agents(self):
        result=self.original.agents()
        if (self.root/'1000002/runtime').is_dir():result=sorted(set(result+[1000002]))
        return result
    def resolve(self,agent):
        if agent==1000002 and (self.root/'1000002/runtime').is_dir():
            return self.root/'1000002/runtime',self.root/'1000002/app.env','xingyu-'+self.root.name+'-1000002'
        return self.original.resolve(agent)
    def data_path(self,agent):
        if agent==1000002 and (self.root/'1000002/runtime').is_dir():return self.root/'1000002/data/kf.sqlite3'
        return self.original.data_path(agent)

BaseEngine=Engine
class Engine(BaseEngine):
    def __init__(self,ops):super().__init__(ops if isinstance(ops,StarOps) else StarOps(ops))
    def register(self,agent,row,config):
        with closing(connect(self.ops)) as db:
            if row['created']<wallet.epoch(db):return None
        return super().register(agent,row,config)
    def claim_daily(self,agent,user,day):
        if not isinstance(day,str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}',day):raise ValueError('领取日期无效，请刷新后重试。')
        with self.lock:
            _,env,_=self.ops.resolve(agent)
            values=dict(l.split('=',1) for l in env.read_text().splitlines() if '=' in l and not l.startswith('#'))
            owners=[v.strip() for v in values.get('XIAOYI_APP_ADMINS','').split(',') if v.strip()]
            if user not in owners or self.frozen(self.corp(agent),user):raise PermissionError('领取身份无效')
            import daily_bonus
            with closing(connect(self.ops)) as db:
                db.execute('BEGIN IMMEDIATE')
                w=account(db,self.corp(agent),user)
                result=daily_bonus.claim(db,w['id'],day)
                import operating_fee
                operating_fee.reconcile(db,w['id'])
                db.commit()
            return dict(result,balance=self.balance(agent,user))

    def frozen(self,company,user):
        # The central closure fence survives application recycling and prevents
        # delayed provider work while a website account is being closed.
        import wallet_closure
        path=self.ops.directory/'star-wallet.sqlite3'
        if path.is_file():
            with ro(path) as db:
                if wallet_closure.state(db,company,user):return True
        global_root=self.ops.root.parent.parent if self.ops.root.parent.name=='enterprises' else self.ops.root
        path=global_root/'admin/data/admin.sqlite3'
        if not path.exists():return False
        with ro(path) as db:
            rows=db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='account_deletions'").fetchall()
            if not rows:return False
            for row in db.execute('SELECT plan FROM account_deletions'):
                plan=json.loads(row['plan'])
                if any(x.get('corp')==company and x.get('owner')==user for x in plan.get('members',[])+plan.get('apps',[])):
                    return True
        return False
    def prepare(self,agent,kind,request_id):
        with self.lock:
            row=self.source(agent,kind,request_id)
            if self.frozen(self.corp(agent),row['user']):
                return dict(ok=True,mode='starcoin',enforced=True,allowed=False,decision='account_frozen')
            ident=self.register(agent,row,pricing.configuration(self.ops))
            with closing(connect(self.ops)) as db:
                db.execute('BEGIN IMMEDIATE')
                w=account(db,self.corp(agent),row['user'])
                r=db.execute('SELECT * FROM auto_requests WHERE agent=? AND kind=? AND id=?',(agent,kind,request_id)).fetchone()
                # An already-admitted turn can finish its internal helpers. A
                # different turn cannot spend that turn's reserved funds.
                available=w['balance']-held(db,w['id'])
                import operating_fee
                op=operating_fee.state(db,w['id'])
                if op:available-=op['debt']
                turn_hold=db.execute('SELECT COALESCE(SUM(held),0) FROM auto_requests WHERE wallet=? AND turn_id=? AND expires>?',
                                    (w['id'],row.get('billing_turn',''),time.time())).fetchone()[0] if row.get('billing_turn') else 0
                decision=r['decision'] if r else 'internal'
                allowed=bool(r and decision=='reserved') or decision=='internal'
                if r and r['estimated']==0:allowed=True
                if row['created']<wallet.epoch(db):allowed=False;decision='before_cutover'
                elif available<=0 and not (turn_hold or (r and r['held']>0 and r['expires']>time.time())):allowed=False;decision='insufficient'
                if r and r['decision'] in ('insufficient','unpriced'):allowed=False
                if row.get('billing_turn'):
                    denied=db.execute("SELECT decision FROM auto_requests WHERE wallet=? AND turn_id=? AND decision IN ('insufficient','unpriced') LIMIT 1",(w['id'],row['billing_turn'])).fetchone()
                    if denied:allowed=False;decision=denied['decision']
                if db.execute("SELECT 1 FROM events WHERE wallet=? AND state='insufficient' LIMIT 1",(w['id'],)).fetchone():
                    allowed=False;decision='insufficient'
                if not allowed:
                    # The caller records cancellation locally before contacting
                    # any provider. This host writes only its own financial DB.
                    db.execute('UPDATE auto_requests SET held=0 WHERE agent=? AND kind=? AND id=?',(agent,kind,request_id))
                db.commit()
                return dict(ok=True,mode='starcoin',enforced=True,allowed=allowed,decision=decision,
                            estimated=wallet.coins(pricing.money(r['estimated'])) if r else '0',
                            reserved=wallet.coins(pricing.money(r['held'])) if r else '0')
    def balance(self,agent,user,page=1,view="requests",group=None):
        result=wallet.public(super().balance(agent,user,page,view=view,group=group))
        with closing(connect(self.ops)) as db:
            policy_epoch=wallet.token_policy_epoch(db)
            import operating_fee
            w=db.execute('SELECT id FROM wallets WHERE corp=? AND user=?',(self.corp(agent),user)).fetchone()
            operating=operating_fee.summary(db,w['id']) if w else {}
        result.update(operating)
        if operating:
            result['balance']=operating['net_balance']
            result['available']=str(max(0,Decimal(result['available'])-Decimal(operating['operating_debt'])))
        result.update(billing_policy='actual-token-usage-v1',billing_policy_effective_at=policy_epoch,
                      mode='starcoin',currency='星币',exchange_rate=1000,initial_gift='1000',recharge_enabled=False,
                      recharge_status='maintenance',low_balance=Decimal(result['available'])<200,enforced=True)
        return result

def star_overview(ops):
    engine=Engine(ops)
    return wallet.star_overview(engine.ops)

def star_detail(ops,query):
    engine=Engine(ops)
    return wallet.star_detail(engine.ops,query)


async def alipay_proxy(ops,agent,user,action,payload):
    """Only the billing host can reach this root-owned checkout socket."""
    writer=None
    try:
        async with asyncio.timeout(18):
            reader,writer=await asyncio.open_unix_connection('/run/xingyu-alipay/service.sock',limit=262144)
            writer.write(json.dumps({'corp':wallet.corp(ops,agent),'agent':agent,'user':user,'action':action,'payload':payload}).encode()+b'\n')
            await writer.drain();result=json.loads(await reader.readline())
            if not isinstance(result,dict):raise ValueError('Invalid payment response')
            return result
    except (OSError,ValueError,TimeoutError):return {'ok':False,'error':'充值服务暂不可用，请稍后查询原订单'}
    finally:
        if writer:
            writer.close()
            try:await writer.wait_closed()
            except OSError:pass


async def wechat_proxy(ops,agent,user,action,payload):
    """Only the billing host can reach this root-owned checkout socket."""
    writer=None
    try:
        async with asyncio.timeout(18):
            reader,writer=await asyncio.open_unix_connection('/run/xingyu-wechat/service.sock',limit=262144)
            writer.write(json.dumps({'corp':wallet.corp(ops,agent),'agent':agent,'user':user,'action':action,'payload':payload}).encode()+b'\n')
            await writer.drain();result=json.loads(await reader.readline())
            if not isinstance(result,dict):raise ValueError('Invalid payment response')
            return result
    except (OSError,ValueError,TimeoutError):return {'ok':False,'error':'充值服务暂不可用，请稍后查询原订单'}
    finally:
        if writer:
            writer.close()
            try:await writer.wait_closed()
            except OSError:pass


BILLING_RECOVERY_VERSION=1
BILLING_OPERATIONS=frozenset(('prepare','sync','balance','persona_templates','payment','wechat_payment','daily_claim'))

def billing_error_code(error):
    # Classify without recording exception messages, SQL, payloads or user data.
    if isinstance(error,sqlite3.Error):
        code=getattr(error,'sqlite_errorcode',0) or 0
        return 'database_busy' if code & 255 in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED) else 'database_error'
    if isinstance(error,PermissionError):return 'permission_denied'
    if isinstance(error,ValueError):return 'invalid_request'
    if isinstance(error,OSError):return 'io_error'
    return 'internal_error'

async def serve(ops):
    ops=StarOps(ops);engine=Engine(ops);servers=[];sockets={};agent_servers={}
    async def rpc(reader,writer,agent,uid):
        operation=None;data={}
        try:
            credentials=writer.get_extra_info('socket').getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12)
            if struct.unpack('3i',credentials)[1] not in (0,uid):raise PermissionError('Denied')
            data=json.loads(await asyncio.wait_for(reader.readline(),2))
            if not isinstance(data,dict):raise ValueError('Invalid request')
            operation=data.pop('operation',None)
            if operation=='persona_templates' and set(data)=={'action','payload'}:
                import persona_templates
                result=await asyncio.to_thread(persona_templates.public,data['action'],data['payload'])
            elif operation=='payment' and set(data)=={'user','action','payload'}:result=await alipay_proxy(ops,agent,**data)
            elif operation=='wechat_payment' and set(data)=={'user','action','payload'}:result=await wechat_proxy(ops,agent,**data)
            elif operation=='prepare' and set(data)=={'kind','request_id'}:result=await asyncio.to_thread(engine.prepare,agent,**data)
            elif operation=='sync' and not data:result=await asyncio.to_thread(engine.sync,agent)
            elif operation=='balance' and {'user','page'}<=set(data)<={'user','page','view','group'}:result=await asyncio.to_thread(engine.balance,agent,**data)
            elif operation=='daily_claim' and set(data)=={'user','day'}:result=await asyncio.to_thread(engine.claim_daily,agent,**data)
            else:raise ValueError('Invalid operation')
        except Exception as error:
            code=billing_error_code(error)
            op=operation if isinstance(operation,str) and operation in BILLING_OPERATIONS else '-'
            ident=data.get('request_id') if isinstance(data,dict) else None
            ident=ident if isinstance(ident,str) and re.fullmatch('[a-f0-9]{32}',ident) else '-'
            LOG.warning('Starcoin RPC failed agent=%s operation=%s request=%s code=%s',agent,op,ident,code)
            result={'ok':False,'error':'starcoin_unavailable','error_code':code}
        try:writer.write(json.dumps(result).encode()+b'\n');await writer.drain()
        except (OSError,ConnectionError):pass
        finally:
            writer.close()
            try:
                async with asyncio.timeout(.2):await writer.wait_closed()
            except (OSError,asyncio.TimeoutError):pass
    async def bind():
        for agent in ops.agents():
            if agent in sockets:
                if sockets[agent].is_socket():continue
                # Reclaim clears the data directory, including its old socket.
                # Rebind for the next owner without restarting the host service.
                old=agent_servers.pop(agent);old.close();await old.wait_closed();sockets.pop(agent)
            path=engine.path(agent).with_name('billing.sock');uid=path.parent.stat().st_uid
            if path.exists():
                if not path.is_socket():raise RuntimeError('Unsafe socket path')
                path.unlink()
            server=await asyncio.start_unix_server(lambda r,w,a=agent,u=uid:rpc(r,w,a,u),str(path),limit=4096)
            os.chown(path,uid,-1);os.chmod(path,0o600);sockets[agent]=path;servers.append(server);agent_servers[agent]=server
    await bind()
    try:
        while True:
            try:await bind();await asyncio.to_thread(daily_tick,engine);await asyncio.to_thread(engine.sync)
            except Exception:LOG.exception('Simulation reconciliation failed')
            await asyncio.sleep(5)
    finally:
        for server in servers:server.close();await server.wait_closed()
        for path in sockets.values():path.unlink(missing_ok=True)

if __name__=='__main__':
    from admin_ops import AdminOps
    # Avoid AdminOps.__init__: a billing restart must never interrupt administrator jobs.
    ops=object.__new__(AdminOps);ops.root=Path('/home/ubuntu/wecom-kf/enterprises/ww0000000000000002');ops.directory=ops.root/'admin/data'
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve(ops))
