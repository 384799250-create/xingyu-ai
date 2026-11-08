"""Daily expiring promotional starcoins. All writes join the caller's transaction.

Ordinary wallet/entry balances remain the combined, auditable total. Grants track
only the promotional portion; existing money and gifts never become expirable.
Usage consumes grants valid at settlement, then the ordinary balance. Reservations
remain estimates, never purchases: midnight retires yesterday's unspent grant even
if a request is in flight, and actual settlement uses the then-current allowance.
"""
from datetime import datetime, timedelta, timezone
import time

VERSION=1
CAMPAIGN='public-beta-20261102'
AMOUNT=1_000_000_000  # 1000 starcoins, at one million units per starcoin.
ZONE=timezone(timedelta(hours=8))

SCHEMA='''
CREATE TABLE IF NOT EXISTS daily_bonus_grants(
 id INTEGER PRIMARY KEY,wallet TEXT NOT NULL REFERENCES wallets(id),campaign TEXT NOT NULL,
 day TEXT NOT NULL,amount INTEGER NOT NULL CHECK(amount>0),remaining INTEGER NOT NULL,
 expires REAL NOT NULL,created REAL NOT NULL,entry INTEGER UNIQUE REFERENCES entries(id),
 CHECK(remaining>=0 AND remaining<=amount),UNIQUE(wallet,campaign,day));
CREATE INDEX IF NOT EXISTS daily_bonus_expiry ON daily_bonus_grants(wallet,expires,remaining);
CREATE TABLE IF NOT EXISTS daily_bonus_spending(
 entry INTEGER NOT NULL REFERENCES entries(id),grant_id INTEGER NOT NULL REFERENCES daily_bonus_grants(id),
 amount INTEGER NOT NULL CHECK(amount>0),PRIMARY KEY(entry,grant_id));
CREATE TRIGGER IF NOT EXISTS daily_bonus_identity BEFORE UPDATE ON daily_bonus_grants
 WHEN OLD.wallet IS NOT NEW.wallet OR OLD.campaign IS NOT NEW.campaign OR OLD.day IS NOT NEW.day
 OR OLD.amount IS NOT NEW.amount OR OLD.expires IS NOT NEW.expires OR OLD.created IS NOT NEW.created
 OR (OLD.entry IS NOT NULL AND OLD.entry IS NOT NEW.entry)
 BEGIN SELECT RAISE(ABORT,'immutable daily grant');END;
CREATE TRIGGER IF NOT EXISTS daily_bonus_delete BEFORE DELETE ON daily_bonus_grants
 BEGIN SELECT RAISE(ABORT,'immutable daily grant');END;
CREATE TRIGGER IF NOT EXISTS daily_bonus_spending_update BEFORE UPDATE ON daily_bonus_spending
 BEGIN SELECT RAISE(ABORT,'immutable daily allocation');END;
CREATE TRIGGER IF NOT EXISTS daily_bonus_spending_delete BEFORE DELETE ON daily_bonus_spending
 BEGIN SELECT RAISE(ABORT,'immutable daily allocation');END;
'''

def bootstrap(db):
    db.executescript(SCHEMA)

def active(db,now=None):
    stamp=time.time() if now is None else now
    meta=dict(db.execute("SELECT key,value FROM starcoin_meta WHERE key IN ('daily_beta_enabled','daily_beta_started')"))
    return meta.get('daily_beta_enabled')=='1' and stamp>=float(meta.get('daily_beta_started','inf'))

def day_info(stamp):
    dt=datetime.fromtimestamp(stamp,ZONE)
    expires=(dt.replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days=1)).timestamp()
    return dt.date().isoformat(),expires

def refresh(db,ident,now=None,*,claim=False):
    """Caller must hold BEGIN IMMEDIATE. Never backfill skipped days."""
    import star_wallet as wallet
    if not db.in_transaction:raise RuntimeError('Daily bonus requires an atomic wallet transaction')
    stamp=time.time() if now is None else now
    import wallet_closure
    current=wallet.get_wallet(db,ident)
    if wallet_closure.state(db,current['corp'],current['user']):
        if claim:raise ValueError('星币账户已注销或正在注销，不能领取。')
        return
    # Use savepoints so a caller cannot accidentally commit a partial expiration.
    db.execute('SAVEPOINT daily_bonus_refresh')
    try:
        rows=db.execute('SELECT id,remaining,day FROM daily_bonus_grants WHERE wallet=? AND remaining>0 AND expires<=? ORDER BY expires,id',(ident,stamp)).fetchall()
        for row in rows:
            wallet._ledger_post(db,ident,'daily_expire',-row['remaining'],'daily-beta',row['day']+' 活动星币到期清零',respect_holds=False)
            db.execute('UPDATE daily_bonus_grants SET remaining=0 WHERE id=?',(row['id'],))
        if claim and active(db,stamp):
            day,expires=day_info(stamp)
            prior=db.execute('SELECT id FROM daily_bonus_grants WHERE wallet=? AND campaign=? AND day=?',(ident,CAMPAIGN,day)).fetchone()
            if prior is None:
                current=wallet.get_wallet(db,ident)
                if current['balance']+AMOUNT>wallet.CAP:
                    raise ValueError('Daily bonus would exceed wallet capacity')
                cur=db.execute('INSERT INTO daily_bonus_grants(wallet,campaign,day,amount,remaining,expires,created) VALUES(?,?,?,?,?,?,?)',(ident,CAMPAIGN,day,AMOUNT,AMOUNT,expires,stamp))
                entry,_=wallet._ledger_post(db,ident,'daily_gift',AMOUNT,'daily-beta',day+' 主动领取1000活动星币（次日北京时间0点到期）')
                db.execute('UPDATE daily_bonus_grants SET entry=? WHERE id=?',(entry,cur.lastrowid))
        db.execute('RELEASE daily_bonus_refresh')
    except BaseException:
        db.execute('ROLLBACK TO daily_bonus_refresh');db.execute('RELEASE daily_bonus_refresh');raise

def post(db,ident,kind,n,actor,note,event=None,reversal=None):
    import star_wallet as wallet
    if not db.in_transaction:raise RuntimeError('Wallet writes require an atomic transaction')
    db.execute('SAVEPOINT daily_bonus_post')
    try:
        stamp=time.time()
        refresh(db,ident,stamp)
        grants=[];left=-n if n<0 and kind=='debit' else 0
        if left:
            for row in db.execute('SELECT id,remaining FROM daily_bonus_grants WHERE wallet=? AND expires>? AND remaining>0 ORDER BY expires,id',(ident,stamp)).fetchall():
                take=min(left,row['remaining']);grants.append((row['id'],take));left-=take
                if not left:break
        result=wallet._ledger_post(db,ident,kind,n,actor,note,event=event,reversal=reversal)
        for grant_id,take in grants:
            db.execute('UPDATE daily_bonus_grants SET remaining=remaining-? WHERE id=? AND remaining>=?',(take,grant_id,take))
            db.execute('INSERT INTO daily_bonus_spending VALUES(?,?,?)',(result[0],grant_id,take))
        db.execute('RELEASE daily_bonus_post');return result
    except BaseException:
        db.execute('ROLLBACK TO daily_bonus_post');db.execute('RELEASE daily_bonus_post');raise

def summary(db,ident,now=None):
    import star_wallet as wallet
    stamp=time.time() if now is None else now
    remaining=db.execute('SELECT COALESCE(SUM(remaining),0) FROM daily_bonus_grants WHERE wallet=? AND expires>?',(ident,stamp)).fetchone()[0]
    total=wallet.get_wallet(db,ident)['balance']
    assert 0<=remaining<=total,'Promotional pool exceeds combined balance'
    day,expires=day_info(stamp)
    claimed=bool(db.execute('SELECT 1 FROM daily_bonus_grants WHERE wallet=? AND campaign=? AND day=?',(ident,CAMPAIGN,day)).fetchone())
    return dict(daily_claim_required=True,daily_claimed=claimed,daily_claim_available=active(db,stamp) and not claimed,daily_bonus_version=VERSION,daily_enabled=active(db,stamp),daily_date=day,daily_timezone='Asia/Shanghai',
                daily_amount=wallet.pricing.money(AMOUNT),daily_remaining=wallet.pricing.money(remaining),
                permanent_balance=wallet.pricing.money(total-remaining),daily_expires_at=expires,
                daily_settlement_policy='settlement-date')


def claim(db,ident,expected_day,now=None):
    """Explicit authenticated intent; unique wallet/campaign/day is the dedupe key."""
    stamp=time.time() if now is None else now
    if not db.in_transaction:raise RuntimeError('Daily claim requires an atomic transaction')
    if expected_day!=day_info(stamp)[0]:raise ValueError('领取日期已变化，请刷新后重新领取。')
    if not active(db,stamp):raise ValueError('每日活动暂未开放，请稍后再试。')
    before=db.execute('SELECT id FROM daily_bonus_grants WHERE wallet=? AND campaign=? AND day=?',(ident,CAMPAIGN,expected_day)).fetchone()
    refresh(db,ident,stamp,claim=True)
    return {'ok':True,'already_claimed':bool(before)}

def enable(db,now=None):
    """Explicit operator action only; startup never enables or resets an activity."""
    stamp=time.time() if now is None else now
    if not db.in_transaction:raise RuntimeError('Activity activation requires an atomic transaction')
    db.execute("INSERT OR IGNORE INTO starcoin_meta VALUES('daily_beta_started',?)",(str(stamp),))
    db.execute("INSERT INTO starcoin_meta VALUES('daily_beta_enabled','1') ON CONFLICT(key) DO UPDATE SET value='1'")
