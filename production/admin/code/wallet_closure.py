"""Durable account closure fences. Balances, receipts and ledgers stay intact."""
import sqlite3
import time
from contextlib import closing
from pathlib import Path

VERSION = 'account-wallet-closure-20261105.1'


def exists(db):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='wallet_closures'").fetchone())


def state(db, company, user):
    if not exists(db):
        return None
    row = db.execute('SELECT * FROM wallet_closures WHERE corp=? AND user=?', (company, user)).fetchone()
    return dict(row) if row else None


def require_active(db, company, user):
    if state(db, company, user):
        raise ValueError('星币账户已注销或正在注销，不能继续使用。')


def active_sql(db, alias='wallets'):
    if alias not in ('wallets', 'w', 'events'):
        raise ValueError('Invalid wallet alias')
    if not exists(db):
        return '1'
    if alias == 'events':
        return ('NOT EXISTS (SELECT 1 FROM wallet_closures c JOIN wallets w ON '
                'w.corp=c.corp AND w.user=c.user WHERE w.id=events.wallet)')
    return f'NOT EXISTS (SELECT 1 FROM wallet_closures c WHERE c.corp={alias}.corp AND c.user={alias}.user)'


def decorate(db, w):
    record = state(db, w['corp'], w['user'])
    w.update(account_status=record['status'] if record else 'active',
             closed_at=record['updated'] if record else None,
             closed_username=record['username'] if record else '')
    if record:
        w['available'] = 0
    return w


def mark(ops, ident, value, status, *, root=None):
    if status not in ('closing', 'closed'):
        raise ValueError('Invalid wallet closure state')
    target = Path(root) if root is not None else Path(ops.root)
    company = target.name if target.parent.name == 'enterprises' else 'ww0000000000000001'
    members = {(m['corp'], m['owner']) for m in value['members']}
    if any(corp != company for corp, user in members):
        raise ValueError('Wallet closure enterprise mismatch')
    path = target / 'admin/data/star-wallet.sqlite3'
    if not members:
        return
    if not path.is_file() or path.is_symlink():
        raise ValueError('Wallet ledger unavailable')
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=rw', uri=True, timeout=10)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN IMMEDIATE')
        db.execute('''CREATE TABLE IF NOT EXISTS wallet_closures(
          corp TEXT NOT NULL,user TEXT NOT NULL,closure TEXT NOT NULL,account INTEGER NOT NULL,
          username TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('closing','closed')),
          created REAL NOT NULL,updated REAL NOT NULL,PRIMARY KEY(corp,user))''')
        now = time.time()
        for corp, user in sorted(members):
            record = state(db, corp, user)
            if record:
                if record['closure'] != ident or record['account'] != value['account']:
                    raise ValueError('Wallet closure identity changed')
                if record['status'] != 'closed' and status == 'closed':
                    db.execute("UPDATE wallet_closures SET status='closed',updated=? WHERE corp=? AND user=?", (now, corp, user))
            else:
                db.execute('INSERT INTO wallet_closures VALUES(?,?,?,?,?,?,?,?)',
                           (corp, user, ident, value['account'], value['username'], status, now, now))
