"""Private simulation bridge. No payment API and no authority to block AI or credit money."""
import asyncio
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
import sim_wallet as wallet
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

def account(db,corp,user,created=None):
    if not isinstance(user,str) or not re.fullmatch(r'[^\s\x00-\x1f\x7f]{1,200}',user):raise ValueError('Invalid member')
    r=db.execute('SELECT * FROM wallets WHERE corp=? AND user=?',(corp,user)).fetchone()
    if r:return dict(r)
    ident=secrets.token_hex(16)
    db.execute('INSERT INTO wallets VALUES(?,?,?,0,?,?)',(ident,corp,user,time.time() if created is None else created,'automatic-simulation'))
    return dict(db.execute('SELECT * FROM wallets WHERE id=?',(ident,)).fetchone())

def held(db,ident):
    return db.execute('SELECT COALESCE(SUM(held),0) FROM auto_requests WHERE wallet=? AND expires>?',(ident,time.time())).fetchone()[0]

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
        receipt=self.receipt(agent,row)
        if not receipt:return None
        row=dict(row,billing_state=receipt['state'],billing_version=1)
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
                if row.get('bill_class') not in ('primary','media') or row.get('is_retry'):estimate=0
                if estimate is not None and not 0<=estimate<=wallet.CAP:estimate=None
                available=w['balance']-held(db,w['id'])
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
            args=[]
            if ident:sql+=' AND wallet=?';args.append(ident)
            for e in db.execute(sql+' ORDER BY created,id LIMIT 2000',args).fetchall():
                available=wallet.get_wallet(db,e['wallet'])['balance']-held(db,e['wallet'])
                if available<e['amount']:
                    db.execute("UPDATE events SET state='insufficient' WHERE id=?",(e['id'],));insufficient+=1;continue
                wallet.post(db,e['wallet'],'debit',-e['amount'],'automatic-simulation','自动用量模拟扣费',event=e['id'])
                db.execute("UPDATE events SET state='paid' WHERE id=?",(e['id'],));charged+=1;total+=e['amount']
            db.commit()
        return dict(charged=charged,amount=pricing.money(total),insufficient=insufficient)

    def prepare(self,agent,kind,request_id):
        with self.lock:
            row=self.source(agent,kind,request_id)
            ident=self.register(agent,row,pricing.configuration(self.ops))
            if not ident:return dict(ok=True,mode='simulation',decision='internal')
            with closing(connect(self.ops)) as db:
                r=db.execute('SELECT decision,estimated,held FROM auto_requests WHERE agent=? AND kind=? AND id=?',(agent,kind,request_id)).fetchone()
                return dict(ok=True,mode='simulation',decision=r['decision'],estimated=pricing.money(r['estimated']),reserved=pricing.money(r['held']),enforced=False)

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
                            rows=source.execute('SELECT * FROM '+table+" WHERE billing_turn!='' AND (updated>? OR (updated=? AND id>?)) ORDER BY updated,id LIMIT 500",(updated,updated,last)).fetchall()
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
            return dict(ok=True,mode='simulation',read=read,unavailable=warnings,more=read>=500,**result)

    def balance(self,agent,user,page=1):
        if type(page) is not int or not 1<=page<=100000:raise ValueError('Invalid page')
        with self.lock,closing(connect(self.ops)) as db:
            db.execute('BEGIN IMMEDIATE');w=account(db,self.corp(agent),user);db.commit()
            db.execute('BEGIN');reserve=held(db,w['id'])
            count=db.execute('SELECT COUNT(*) FROM entries WHERE wallet=?',(w['id'],)).fetchone()[0]
            entries=[]
            for r in db.execute('SELECT e.id,e.kind,e.amount,e.balance,e.created,v.agent,v.kind AS resource_kind,v.snapshot FROM entries e LEFT JOIN events v ON v.id=e.event WHERE e.wallet=? ORDER BY e.id DESC LIMIT 20 OFFSET ?',(w['id'],(page-1)*20)):
                d=dict(r);snap=json.loads(d.pop('snapshot') or '{}')
                d.update(amount=pricing.money(d['amount']),balance=pricing.money(d['balance']),scene=snap.get('billing_turn','').split(':')[0],
                         input_tokens=snap.get('input_tokens'),output_tokens=snap.get('output_tokens'),image_count=snap.get('image_count'),generated_chars=snap.get('generated_chars'))
                entries.append(d)
            counts=dict(db.execute('SELECT state,COUNT(*) FROM events WHERE wallet=? GROUP BY state',(w['id'],)).fetchall())
            events=[]
            for r in db.execute('SELECT id,agent,kind,amount,state,reason,created,snapshot,review FROM events WHERE wallet=? ORDER BY created DESC,id DESC LIMIT 20 OFFSET ?',(w['id'],(page-1)*20)):
                d=dict(r);snap=json.loads(d.pop('snapshot'))
                d.update(amount=pricing.money(d['amount']),scene=snap.get('billing_turn','').split(':')[0],
                         input_tokens=snap.get('input_tokens'),output_tokens=snap.get('output_tokens'),image_count=snap.get('image_count'),generated_chars=snap.get('generated_chars'))
                events.append(d)
            return dict(ok=True,mode='simulation',balance=pricing.money(w['balance']),reserved=pricing.money(reserve),available=pricing.money(w['balance']-reserve),entries=entries,count=count,page=page,page_size=20,counts=counts,automatic=True,events=events,event_count=sum(counts.values()))

def settle_wallet(ops,actor,data):
    if actor.get('role') not in ('superadmin','operator'):wallet.reject('只读管理员不能同步模拟扣费。')
    wallet.strict(data,('wallet',));ident=wallet.identifier(data['wallet'])
    with closing(connect(ops)) as db:wallet.get_wallet(db,ident)
    return Engine(ops).sync(ident=ident)

async def serve(ops):
    engine=Engine(ops);servers=[];sockets={};agent_servers={}
    async def rpc(reader,writer,agent,uid):
        try:
            credentials=writer.get_extra_info('socket').getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12)
            if struct.unpack('3i',credentials)[1] not in (0,uid):raise ValueError('Denied')
            data=json.loads(await asyncio.wait_for(reader.readline(),2))
            if not isinstance(data,dict):raise ValueError('Invalid request')
            operation=data.pop('operation',None)
            if operation=='prepare' and set(data)=={'kind','request_id'}:result=await asyncio.to_thread(engine.prepare,agent,**data)
            elif operation=='sync' and not data:result=await asyncio.to_thread(engine.sync,agent)
            elif operation=='balance' and set(data)=={'user','page'}:result=await asyncio.to_thread(engine.balance,agent,**data)
            else:raise ValueError('Invalid operation')
        except Exception:result={'ok':False,'error':'simulation_unavailable'}
        try:writer.write(json.dumps(result).encode()+b'\n');await writer.drain()
        except (OSError,ConnectionError):pass
        finally:writer.close();await writer.wait_closed()
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
            try:await bind();await asyncio.to_thread(engine.sync)
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
