"""Verified payment receipts and spendable starcoin credits commit atomically."""
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time

from alipay_errors import PaymentError
import star_wallet as wallet
from recharge_rules import quote, SUPPORTED_FEN
from contextlib import closing

NANO_PER_FEN = 10_000_000


def owner(corp, user):
    if not isinstance(corp, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', corp):
        raise PaymentError('企业身份无效')
    if not isinstance(user, str) or not re.fullmatch(r'[^\s\x00-\x1f\x7f]{1,200}', user):
        raise PaymentError('用户身份无效')
    return corp, user


class Ledger:
    def __init__(self, ops):
        self.ops=ops
        with closing(wallet.connect(ops)):pass
        self.path = (ops.directory/'star-wallet.sqlite3').resolve()
        if self.path.name == 'sim-wallet.sqlite3':
            raise PaymentError('真实账本不能使用模拟账本路径')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS pay_orders(
                id TEXT PRIMARY KEY,corp TEXT NOT NULL,user TEXT NOT NULL,request_id TEXT NOT NULL,
                fen INTEGER NOT NULL CHECK(fen>0),mchid TEXT NOT NULL,appid TEXT NOT NULL,
                state TEXT NOT NULL,code_url TEXT NOT NULL DEFAULT '',transaction_id TEXT UNIQUE,
                created REAL NOT NULL,updated REAL NOT NULL,expires REAL NOT NULL,
                queried REAL NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',
                UNIQUE(corp,user,request_id));
              CREATE TABLE IF NOT EXISTS pay_entries(
                id INTEGER PRIMARY KEY,corp TEXT NOT NULL,user TEXT NOT NULL,
                order_id TEXT NOT NULL UNIQUE REFERENCES pay_orders(id),transaction_id TEXT NOT NULL UNIQUE,
                amount INTEGER NOT NULL CHECK(amount>0),created REAL NOT NULL,
                wallet TEXT NOT NULL REFERENCES wallets(id),entry INTEGER NOT NULL UNIQUE REFERENCES entries(id));
              CREATE TABLE IF NOT EXISTS pay_notifications(
                id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,order_id TEXT NOT NULL,created REAL NOT NULL);
              CREATE INDEX IF NOT EXISTS order_scope ON pay_orders(corp,user,created);
              CREATE INDEX IF NOT EXISTS order_pending ON pay_orders(state,queried);
              CREATE TRIGGER IF NOT EXISTS pay_entry_update BEFORE UPDATE ON pay_entries
                BEGIN SELECT RAISE(ABORT,'immutable real-money entry'); END;
              CREATE TRIGGER IF NOT EXISTS pay_entry_delete BEFORE DELETE ON pay_entries
                BEGIN SELECT RAISE(ABORT,'immutable real-money entry'); END;
              CREATE TRIGGER IF NOT EXISTS pay_order_identity BEFORE UPDATE ON pay_orders
                WHEN OLD.id IS NOT NEW.id OR OLD.corp IS NOT NEW.corp OR OLD.user IS NOT NEW.user
                  OR OLD.request_id IS NOT NEW.request_id OR OLD.fen IS NOT NEW.fen
                  OR OLD.mchid IS NOT NEW.mchid OR OLD.appid IS NOT NEW.appid
                BEGIN SELECT RAISE(ABORT,'immutable order identity'); END;
            ''')
            # Additive migration: existing orders and their immutable entries remain WeChat.
            if 'provider' not in {r[1] for r in db.execute('PRAGMA table_info(pay_orders)')}:
                db.execute("ALTER TABLE pay_orders ADD COLUMN provider TEXT NOT NULL DEFAULT 'wechat'")
            db.executescript('''CREATE TRIGGER IF NOT EXISTS pay_order_provider BEFORE UPDATE ON pay_orders
                WHEN OLD.provider IS NOT NEW.provider
                BEGIN SELECT RAISE(ABORT,'immutable order provider'); END;''')
            db.execute('BEGIN IMMEDIATE')
            columns={r[1] for r in db.execute('PRAGMA table_info(pay_orders)')}
            if 'checkout_mode' not in columns:
                db.execute("ALTER TABLE pay_orders ADD COLUMN checkout_mode TEXT NOT NULL DEFAULT 'NATIVE'")
                db.execute("UPDATE pay_orders SET checkout_mode='PAGE' WHERE provider='alipay'")
            if 'payer_hash' not in columns:db.execute("ALTER TABLE pay_orders ADD COLUMN payer_hash TEXT NOT NULL DEFAULT ''")
            db.execute("""CREATE TRIGGER IF NOT EXISTS pay_order_checkout BEFORE UPDATE ON pay_orders
                WHEN OLD.checkout_mode IS NOT NEW.checkout_mode OR OLD.payer_hash IS NOT NEW.payer_hash
                BEGIN SELECT RAISE(ABORT,'immutable order checkout identity'); END;""")
            # Existing orders retain their original linear quote and no bonus.
            if 'base_starcoins' not in columns:
                db.execute("ALTER TABLE pay_orders ADD COLUMN base_starcoins INTEGER NOT NULL DEFAULT 0 CHECK(base_starcoins>=0)")
            if 'bonus_starcoins' not in columns:
                db.execute("ALTER TABLE pay_orders ADD COLUMN bonus_starcoins INTEGER NOT NULL DEFAULT 0 CHECK(bonus_starcoins>=0)")
            if 'pricing_version' not in columns:
                db.execute("ALTER TABLE pay_orders ADD COLUMN pricing_version TEXT NOT NULL DEFAULT 'legacy-linear-v1'")
            db.execute("UPDATE pay_orders SET base_starcoins=fen*10 WHERE base_starcoins=0 AND pricing_version='legacy-linear-v1'")
            db.execute("""CREATE TRIGGER IF NOT EXISTS pay_order_pricing BEFORE UPDATE ON pay_orders
                WHEN OLD.base_starcoins IS NOT NEW.base_starcoins OR OLD.bonus_starcoins IS NOT NEW.bonus_starcoins
                  OR OLD.pricing_version IS NOT NEW.pricing_version
                BEGIN SELECT RAISE(ABORT,'immutable order quote'); END;""")
            db.commit()
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            yield db
        finally:
            db.close()

    def new_order(self, corp, user, request_id, fen, config, *, trade_type=None, payer_hash=''):
        owner(corp, user)
        provider = getattr(config, 'provider', 'wechat')
        mode=trade_type or ('PAGE' if provider=='alipay' else 'NATIVE')
        if (provider=='alipay' and mode!='PAGE' or provider=='wechat' and mode not in ('NATIVE','JSAPI')
                or mode=='JSAPI' and not re.fullmatch(r'[a-f0-9]{64}',payer_hash)
                or mode!='JSAPI' and payer_hash):raise PaymentError('付款类型或身份无效')
        if provider not in ('wechat', 'alipay'):
            raise PaymentError('支付渠道无效')
        if not isinstance(request_id, str) or not re.fullmatch(r'[a-f0-9]{32}', request_id):
            raise PaymentError('充值操作编号无效')
        if type(fen) is not int or fen not in config.allowed_fen or fen not in SUPPORTED_FEN:
            raise PaymentError('请选择有效的充值金额')
        with self.db() as db, db:
            db.execute('BEGIN IMMEDIATE')
            import wallet_closure
            try:wallet_closure.require_active(db,corp,user)
            except ValueError as exc:raise PaymentError(str(exc)) from None
            prior = db.execute('SELECT * FROM pay_orders WHERE corp=? AND user=? AND request_id=?',
                               (corp, user, request_id)).fetchone()
            if prior:
                if prior['fen'] != fen or prior['provider'] != provider or prior['checkout_mode']!=mode or prior['payer_hash']!=payer_hash or prior['appid']!=config.appid:
                    raise PaymentError('同一操作编号的充值金额或渠道发生变化')
                return dict(prior), False
            pending = db.execute("SELECT COUNT(*) FROM pay_orders WHERE corp=? AND user=? AND state NOT IN ('paid','closed') AND expires>?",
                                 (corp, user, time.time())).fetchone()[0]
            if pending >= 3:
                raise PaymentError('未完成订单较多，请先查看已有订单')
            terms=quote(fen)
            now, ident = time.time(), 'XY' + secrets.token_hex(15)
            db.execute('INSERT INTO pay_orders(id,corp,user,request_id,fen,mchid,appid,state,created,updated,expires,provider,checkout_mode,payer_hash,base_starcoins,bonus_starcoins,pricing_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (ident, corp, user, request_id, fen, config.mchid, config.appid, 'prepared', now, now, now+900, provider, mode, payer_hash, terms['base_starcoins'], terms['bonus_starcoins'], terms['pricing_version']))
            return dict(db.execute('SELECT * FROM pay_orders WHERE id=?', (ident,)).fetchone()), True

    def get(self, ident, corp=None, user=None):
        if not isinstance(ident, str) or not re.fullmatch(r'XY[a-f0-9]{30}', ident):
            raise PaymentError('订单不存在')
        with self.db() as db:
            row = db.execute('SELECT * FROM pay_orders WHERE id=?', (ident,)).fetchone()
        if not row or (corp is not None and (row['corp'], row['user']) != owner(corp, user)):
            raise PaymentError('订单不存在')
        return dict(row)

    def update(self, ident, state, code_url='', error=''):
        with self.db() as db, db:
            # A callback can complete the order while the create request is still returning.
            db.execute("UPDATE pay_orders SET state=?,code_url=CASE WHEN ?='' THEN code_url ELSE ? END,error=?,updated=? WHERE id=? AND state NOT IN ('paid','closed')",
                       (state, code_url, code_url, error, time.time(), ident))

    def claim_query(self, ident):
        with self.db() as db, db:
            cur = db.execute("UPDATE pay_orders SET queried=? WHERE id=? AND state NOT IN ('paid','closed') AND queried<?",
                             (time.time(), ident, time.time()-5))
            return cur.rowcount == 1

    def validate_transaction(self, value, expected=None, allow_unpaid=False):
        if not isinstance(value, dict):
            raise PaymentError('支付交易数据无效')
        order = self.get(value.get('out_trade_no'))
        if expected is not None and order['id'] != expected:
            raise PaymentError('查询结果订单不匹配')
        amount = value.get('amount')
        if (value.get('provider', 'wechat') != order['provider']
                or value.get('mchid') != order['mchid'] or value.get('appid') != order['appid']
                or not isinstance(amount, dict) or type(amount.get('total')) is not int
                or amount['total'] != order['fen'] or amount.get('currency') != 'CNY'
                or (value.get('trade_type') != order['checkout_mode']
                    and not (allow_unpaid and order['provider']=='wechat' and value.get('trade_state')=='NOTPAY' and value.get('trade_type') is None))):
            raise PaymentError('支付交易归属或金额不匹配')
        if order['checkout_mode']=='JSAPI' and value.get('trade_state')=='SUCCESS':
            payer=value.get('payer',{}).get('openid') if isinstance(value.get('payer'),dict) else None
            if not isinstance(payer,str) or hashlib.sha256(payer.encode()).hexdigest()!=order['payer_hash']:raise PaymentError('微信付款身份与订单不匹配')
        return order

    def settle(self, value, event_id=None, expected=None):
        order = self.validate_transaction(value, expected)
        if value.get('trade_state') != 'SUCCESS':
            raise PaymentError('尚未确认支付成功')
        transaction = value.get('transaction_id')
        if not isinstance(transaction, str) or not re.fullmatch(r'[0-9A-Za-z_-]{6,80}', transaction):
            raise PaymentError('支付交易号无效')
        # Existing WeChat IDs stay intact; Alipay occupies a disjoint namespace.
        if order['provider'] == 'alipay':
            transaction = 'alipay:' + transaction
        try:
            stamp = datetime.fromisoformat(value['success_time'].replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                raise ValueError('timezone')
        except (KeyError, ValueError, TypeError, AttributeError):
            raise PaymentError('支付成功时间无效') from None
        # Only the transaction, not randomized notification ciphertext, defines replay identity.
        fingerprint = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        with self.db() as db, db:
            db.execute('BEGIN IMMEDIATE')
            if event_id:
                old = db.execute('SELECT * FROM pay_notifications WHERE id=?', (event_id,)).fetchone()
                if old and old['fingerprint'] != fingerprint:
                    raise PaymentError('通知编号内容冲突')
            entry = db.execute('SELECT * FROM pay_entries WHERE order_id=? OR transaction_id=?',
                               (order['id'], transaction)).fetchone()
            if entry:
                if entry['order_id'] != order['id'] or entry['transaction_id'] != transaction:
                    raise PaymentError('支付交易号与订单冲突')
                if event_id:
                    db.execute('INSERT OR IGNORE INTO pay_notifications VALUES(?,?,?,?)',
                               (event_id, fingerprint, order['id'], time.time()))
                return False
            current=db.execute('SELECT * FROM wallets WHERE corp=? AND user=?',(order['corp'],order['user'])).fetchone()
            if not current:raise PaymentError('充值账户需要人工核对，款项尚未入账')
            amount=(order['base_starcoins']+order['bonus_starcoins'])*1_000_000
            if current['balance']+amount>wallet.CAP:raise PaymentError('账户余额超出上限，需要人工核对')
            note=('微信支付充值 ' if order['provider']=='wechat' else '支付宝充值 ')+order['id']
            if order['bonus_starcoins']:note+='（含赠送 '+str(order['bonus_starcoins'])+' 星币）'
            entry,balance=wallet.post(db,current['id'],'credit',amount,order['provider']+'-verified',note)
            db.execute('INSERT INTO pay_entries(corp,user,order_id,transaction_id,amount,created,wallet,entry) VALUES(?,?,?,?,?,?,?,?)',
                       (order['corp'],order['user'],order['id'],transaction,amount,time.time(),current['id'],entry))
            db.execute("UPDATE pay_orders SET state='paid',transaction_id=?,error='',updated=? WHERE id=?",
                       (transaction, time.time(), order['id']))
            if event_id:
                db.execute('INSERT INTO pay_notifications VALUES(?,?,?,?)',
                           (event_id, fingerprint, order['id'], time.time()))
            return True

    def balance(self, corp, user):
        owner(corp, user)
        with self.db() as db:
            row = db.execute('SELECT balance FROM wallets WHERE corp=? AND user=?', (corp, user)).fetchone()
            return row[0] if row else 0

    def history(self, corp, user, page=1, provider=None):
        owner(corp, user)
        if type(page) is not int or not 1 <= page <= 100000:
            raise PaymentError('充值记录页码无效')
        with self.db() as db:
            db.execute('BEGIN')
            count = db.execute('SELECT COUNT(*) FROM pay_orders WHERE corp=? AND user=? AND (? IS NULL OR provider=?)', (corp,user,provider,provider)).fetchone()[0]
            orders = [self.public(dict(r)) for r in db.execute(
                'SELECT * FROM pay_orders WHERE corp=? AND user=? AND (? IS NULL OR provider=?) ORDER BY created DESC,id DESC LIMIT 10 OFFSET ?',
                (corp,user,provider,provider,(page-1)*10))]
        return {'orders':orders, 'count':count, 'page':page, 'page_size':10}

    def pending(self, provider=None):
        with self.db() as db:
            # Old ambiguous orders remain queryable after QR expiry. Never equate local expiry with closure.
            return [r['id'] for r in db.execute("SELECT id FROM pay_orders WHERE state NOT IN ('paid','closed') AND queried<? AND (? IS NULL OR provider=?) ORDER BY queried,created LIMIT 50", (time.time()-30,provider,provider))]

    def public(self, order):
        result = {k: order[k] for k in ('id', 'fen', 'state', 'code_url', 'created', 'expires', 'error', 'provider')}
        result['qr_expired'] = order['expires'] <= time.time()
        if result['qr_expired'] or order['state'] in ('paid', 'closed'):
            result['code_url'] = ''
        result['checkout_url'] = result['code_url'] if order['provider']=='alipay' else ''
        result['base_starcoins']=order['base_starcoins'];result['bonus_starcoins']=order['bonus_starcoins']
        result['pricing_version']=order['pricing_version']
        result['starcoins']=order['base_starcoins']+order['bonus_starcoins']
        if order['provider']=='alipay':result['code_url']=''
        return result
