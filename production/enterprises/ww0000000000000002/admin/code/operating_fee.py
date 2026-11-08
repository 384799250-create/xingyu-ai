"""Daily 300-starcoin fee, separate auditable receivables, and suspension cycles.

Wallet cash stays nonnegative and its immutable ledger stays reconcilable. A fee
may create a receivable, shown as a negative net balance; incoming funds settle it
before admitting new model work. All writes join the caller's BEGIN IMMEDIATE.
"""
import secrets,time
from decimal import Decimal
from datetime import datetime,timedelta,timezone
AMOUNT=300_000_000
DAY=86400
VERSION='operating-policy-20261105.1'
SCHEMA='''
CREATE TABLE IF NOT EXISTS operating_accounts(
 wallet TEXT PRIMARY KEY REFERENCES wallets(id),debt INTEGER NOT NULL DEFAULT 0 CHECK(debt>=0 AND debt<=300000000),
 phase TEXT NOT NULL DEFAULT 'active',cycle TEXT NOT NULL DEFAULT '',paused_at REAL NOT NULL DEFAULT 0,
 agent INTEGER,request TEXT,account INTEGER,archive TEXT NOT NULL DEFAULT '',restore_agent INTEGER,
 restore_request TEXT NOT NULL DEFAULT '',restore_phase TEXT NOT NULL DEFAULT '',restored_at REAL NOT NULL DEFAULT 0,
 updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS operating_charges(
 id INTEGER PRIMARY KEY,wallet TEXT NOT NULL REFERENCES wallets(id),day TEXT NOT NULL,
 amount INTEGER NOT NULL CHECK(amount=300000000),created REAL NOT NULL,UNIQUE(wallet,day));
CREATE TABLE IF NOT EXISTS operating_payments(
 entry INTEGER PRIMARY KEY REFERENCES entries(id),charge INTEGER NOT NULL REFERENCES operating_charges(id),
 amount INTEGER NOT NULL CHECK(amount>0));
CREATE TRIGGER IF NOT EXISTS immutable_operating_charge_update BEFORE UPDATE ON operating_charges BEGIN SELECT RAISE(ABORT,'immutable operating charge');END;
CREATE TRIGGER IF NOT EXISTS immutable_operating_charge_delete BEFORE DELETE ON operating_charges BEGIN SELECT RAISE(ABORT,'immutable operating charge');END;
CREATE TRIGGER IF NOT EXISTS immutable_operating_payment_update BEFORE UPDATE ON operating_payments BEGIN SELECT RAISE(ABORT,'immutable operating payment');END;
CREATE TRIGGER IF NOT EXISTS immutable_operating_payment_delete BEFORE DELETE ON operating_payments BEGIN SELECT RAISE(ABORT,'immutable operating payment');END;
'''

def bootstrap(db):db.executescript(SCHEMA)
def day_info(stamp):
 dt=datetime.fromtimestamp(stamp,timezone(timedelta(hours=8)))
 return dt.date().isoformat(),(dt.replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days=1)).timestamp()
def enabled(db):return bool(db.execute("SELECT 1 FROM starcoin_meta WHERE key='operating_fee_enabled' AND value='1'").fetchone())
def start(db):
 r=db.execute("SELECT value FROM starcoin_meta WHERE key='operating_fee_start'").fetchone();return float(r[0]) if r else 0
def state(db,ident):
 row=db.execute('SELECT * FROM operating_accounts WHERE wallet=?',(ident,)).fetchone();return dict(row) if row else None
def holds(db,ident,now):
 if not db.execute("SELECT 1 FROM sqlite_master WHERE name='auto_requests'").fetchone():return 0
 return db.execute('SELECT COALESCE(SUM(held),0) FROM auto_requests WHERE wallet=? AND expires>?',(ident,now)).fetchone()[0]
def require_transaction(db):
 if not db.in_transaction:raise RuntimeError('BEGIN IMMEDIATE required for operating fees')
def reconcile(db,ident,now=None):
 import daily_bonus,star_wallet as wallet,wallet_closure
 require_transaction(db);stamp=time.time() if now is None else now
 if not enabled(db) or not start(db) or stamp<start(db):return
 w=wallet.get_wallet(db,ident)
 if wallet_closure.state(db,w['corp'],w['user']) or w['user'].startswith(('poolslot','verify-')):return
 db.execute('INSERT OR IGNORE INTO operating_accounts(wallet,updated) VALUES(?,?)',(ident,stamp))
 s=state(db,ident)
 if s['phase'] in ('closed','closing'):return
 # A retiring generation is fenced under operation.lock before its files move.
 if s['phase']=='reclaiming':return
 daily_bonus.refresh(db,ident,stamp)
 # Already-admitted provider requests retain their reservations until settlement.
 if holds(db,ident,stamp):return
 db.execute('SAVEPOINT operating_reconcile')
 try:
  day,_=day_info(stamp)
  # One pass settles an old receivable; the second assesses today's fee after
  # recovery. No missed date is ever backfilled.
  for _ in range(2):
   s=state(db,ident);w=wallet.get_wallet(db,ident)
   if s['debt']==0 and s['phase'] not in ('retained','restoring') and w['balance']>0:
    prior=db.execute('SELECT id FROM operating_charges WHERE wallet=? AND day=?',(ident,day)).fetchone()
    if not prior:
     db.execute('INSERT INTO operating_charges(wallet,day,amount,created) VALUES(?,?,?,?)',(ident,day,AMOUNT,stamp))
     db.execute('UPDATE operating_accounts SET debt=? WHERE wallet=?',(AMOUNT,ident))
   s=state(db,ident);w=wallet.get_wallet(db,ident)
   if not s['debt'] or w['balance']<=0:break
   charge=db.execute('''SELECT c.id,c.day,c.amount,c.amount-COALESCE((SELECT SUM(p.amount) FROM operating_payments p WHERE p.charge=c.id),0) AS outstanding
      FROM operating_charges c WHERE c.wallet=? ORDER BY c.id DESC LIMIT 1''',(ident,)).fetchone()
   if not charge or charge['outstanding']!=s['debt']:raise ValueError('Operating receivable mismatch')
   take=min(w['balance'],s['debt'])
   entry,_=daily_bonus.post(db,ident,'debit',-take,'daily-operation-fee',charge['day']+' 运营费'+('（补足欠额）' if take<charge['amount'] or s['debt']<charge['amount'] else ''))
   db.execute('INSERT INTO operating_payments VALUES(?,?,?)',(entry,charge['id'],take))
   db.execute('UPDATE operating_accounts SET debt=debt-? WHERE wallet=?',(take,ident))
  s=state(db,ident);w=wallet.get_wallet(db,ident);net=w['balance']-s['debt']
  if net<=0 and s['phase']=='active':
   db.execute("UPDATE operating_accounts SET phase='paused',cycle=?,paused_at=?,updated=? WHERE wallet=?",(secrets.token_hex(16),stamp,stamp,ident))
  elif net>0 and s['phase']=='paused':
   db.execute("UPDATE operating_accounts SET phase='active',paused_at=0,cycle='',updated=? WHERE wallet=?",(stamp,ident))
  elif net>0 and s['phase']=='retained' and stamp<s['paused_at']+15*DAY:
   db.execute("UPDATE operating_accounts SET phase='restoring',restored_at=?,updated=? WHERE wallet=?",(stamp,stamp,ident))
  db.execute('RELEASE operating_reconcile')
 except BaseException:
  db.execute('ROLLBACK TO operating_reconcile');db.execute('RELEASE operating_reconcile');raise

def summary(db,ident,now=None):
 import star_wallet as wallet
 stamp=time.time() if now is None else now;s=state(db,ident);w=wallet.get_wallet(db,ident)
 debt=s['debt'] if s else 0;paused=s['paused_at'] if s else 0
 rows=db.execute('''SELECT c.day,c.amount,c.created,COALESCE(SUM(p.amount),0) AS paid
   FROM operating_charges c LEFT JOIN operating_payments p ON p.charge=c.id
   WHERE c.wallet=? GROUP BY c.id ORDER BY c.id DESC LIMIT 31''',(ident,)).fetchall()
 return {'operating_fee_version':VERSION,'operating_fee_enabled':enabled(db),'operating_fee_start':start(db),
  'operating_fee_amount':'300','operating_debt':str(Decimal(debt)/1000000),'net_balance':str(Decimal(w['balance']-debt)/1000000),
  'operating_phase':s['phase'] if s else 'active','operating_cycle':s['cycle'] if s else '',
  'paused_at':paused,'reclaim_at':paused+7*DAY if paused else 0,'delete_at':paused+15*DAY if paused else 0,
  'farewell_at':paused+6*DAY if paused else 0,
  'operating_history':[{'day':r['day'],'amount':str(Decimal(r['amount'])/1000000),'paid':str(Decimal(r['paid'])/1000000),'created':r['created']} for r in rows]}

def retained_allowed(db,company,user,agent,now=None):
 stamp=time.time() if now is None else now
 if not db.execute("SELECT 1 FROM sqlite_master WHERE name='operating_accounts'").fetchone():return False
 row=db.execute('''SELECT o.* FROM operating_accounts o JOIN wallets w ON w.id=o.wallet
  WHERE w.corp=? AND w.user=? AND o.agent=? AND o.phase IN ('retained','restoring')''',(company,user,agent)).fetchone()
 return bool(row and (stamp<row['paused_at']+15*DAY or row['phase']=='restoring' and row['restored_at']<row['paused_at']+15*DAY))
