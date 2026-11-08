"""Private operations invoked only after a server-verified WeChat phone grant."""
from contextlib import closing
import hmac
import re
import sqlite3
from directory_admin import Directory, DirectoryError


def normalize(value):
    if not isinstance(value, str): raise ValueError('invalid_phone')
    if re.fullmatch(r'1[3-9][0-9]{9}', value): value = '+86' + value
    if not re.fullmatch(r'\+861[3-9][0-9]{9}', value): raise ValueError('invalid_phone')
    return value


def application(service, owner, agent=None):
    path = service.root / 'app-pool/registry.sqlite3'
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=3)) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(r) for r in db.execute("SELECT agent,corp,owner,name,state FROM apps WHERE owner=? AND state='assigned'", (owner,))]
    if agent is not None: rows = [r for r in rows if r['agent'] == agent]
    if len(rows) > 1: raise ValueError('ambiguous_application')
    return rows[0] if rows else None


def lookup(service, phone):
    phone = normalize(phone)
    proof = service.mac('phone', phone)
    row = service.db.execute('SELECT * FROM jobs WHERE phone_hash=?', (proof,)).fetchone()
    if row and not row['agent']:
        return {'state': 'pending', 'account': row['account'], 'proof': proof}
    directory = service.directory or Directory(service.home)
    try:
        result = directory.request('user/getuserid', {'access_token': directory.token()}, {'mobile': phone[3:]})
        owner = result.get('userid')
        if not isinstance(owner, str) or not re.fullmatch(r'[A-Za-z0-9_@.\-]{1,128}', owner):
            raise ValueError('directory_response_invalid')
    except DirectoryError as exc:
        if str(exc) not in ('wecom_code_60111', 'wecom_code_60121'):
            raise ValueError('directory_lookup_unavailable') from None
        if row: raise ValueError('phone_membership_mismatch') from None
        return {'state': 'none', 'proof': proof}
    if row and owner != row['owner']: raise ValueError('phone_membership_mismatch')
    app = application(service, owner, row['agent'] if row else None)
    if row and not app: raise ValueError('application_unavailable')
    return {'state': 'member', 'owner': owner, 'proof': proof,
            'application': app, 'account': row['account'] if row else None}


def enrollment(service, account, proof):
    account = service.account(account)
    row = service.row(account)
    if not row or not isinstance(proof, str) or not hmac.compare_digest(row['phone_hash'], proof):
        raise ValueError('phone_proof_mismatch')
    service.account_available(account)
    status = service.status(account)
    app = application(service, row['owner'], row['agent']) if row['agent'] else None
    if row['agent'] and not app: raise ValueError('application_unavailable')
    result = {'state': status['state'], 'stage': status.get('stage'), 'account': account,
              'application': app, 'owner': row['owner'], 'qr_ready': status['qr_ready']}
    return result


def call(service, data):
    try:
        if data.get('action') == 'mini-phone-lookup' and set(data) == {'action', 'phone'}:
            return lookup(service, data['phone'])
        if data.get('action') == 'mini-phone-progress' and set(data) == {'action', 'account', 'proof'}:
            return enrollment(service, data['account'], data['proof'])
    except (ValueError, DirectoryError) as exc:
        # The running provisioning script defines its own exception class.
        import sys
        raise sys.modules[type(service).__module__].OnboardingError(str(exc)) from None
    import sys
    raise sys.modules[type(service).__module__].OnboardingError('invalid_request')
