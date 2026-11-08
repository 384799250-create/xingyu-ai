"""Fixed-socket phone lookup; no HTTP or client-selected enterprise paths."""
import re
import sys
from enterprise_router import forward, config, CORP, CORPS

LEGACY = 'ww0000000000000001'


def worker(bridge, corp, data):
    path = '/run/xiaoyi-portal/onboarding' + ('' if corp == LEGACY else '-' + corp) + '.sock'
    result = forward(path, data)
    if not result.get('ok'):
        raise sys.modules[type(bridge).__module__].Denied('service_unavailable')
    return result['data']


def candidate(bridge, corp, value):
    from mini_bridge import legacy_rows, subject as legacy_subject
    from bridge import Bridge
    if value.get('application'):
        app = value['application']
        if app['corp'] != corp or app['owner'] != value['owner'] or app['state'] != 'assigned':
            raise sys.modules[type(bridge).__module__].Denied('application_unavailable')
        scoped = bridge if corp == LEGACY else Bridge(bridge.root/'enterprises'/corp,
            (bridge.root/'enterprises'/corp/'app-pool/portal-identity.key').read_bytes(), origin=bridge.origin)
        if corp == LEGACY:
            scoped.app(app)
        else:
            checked = forward('/run/xiaoyi-portal/bridge-' + corp + '.sock',
                              {'action': 'status', 'subject': scoped.subject(app)})
            if not checked.get('ok') or checked['data']['agent'] != app['agent']:
                raise sys.modules[type(bridge).__module__].Denied('application_unavailable')
        return dict(state='existing', subject=scoped.subject(app), account=value.get('account'),
                    application={'agent': app['agent'], 'name': app['name']})
    if corp == LEGACY and value.get('owner'):
        row = next((r for r in legacy_rows(bridge) if r['owner'] == value['owner']), None)
        if row:
            return dict(state='existing', subject=legacy_subject(bridge, row), account=None,
                        application={'agent': 1000003, 'name': row['name']})
    return None


def resolve(bridge, data):
    Denied = sys.modules[type(bridge).__module__].Denied
    if data.get('action') == 'mini-phone' and set(data) == {'action', 'phone'}:
        phone = data['phone']
        if not isinstance(phone, str) or not re.fullmatch(r'\+861[3-9][0-9]{9}', phone):
            raise Denied('invalid_phone')
        scopes = [LEGACY] + list(config(bridge.root))
        matches, pending, proofs, unmapped = [], [], {}, False
        for corp in scopes:
            value = worker(bridge, corp, {'action': 'mini-phone-lookup', 'phone': phone})
            proofs[corp] = value['proof']
            found = candidate(bridge, corp, value)
            if found: matches.append(found)
            elif value['state'] == 'pending': pending.append({'account': value['account'], 'corp': corp})
            elif value['state'] == 'member': unmapped = True
        if len(matches) + len(pending) > 1: raise Denied('application_unavailable')
        if matches: return dict(matches[0], proofs=proofs)
        if pending: return dict(state='pending', proofs=proofs, **pending[0])
        if unmapped: raise Denied('application_unavailable')
        return {'state': 'new', 'proofs': proofs}
    if data.get('action') == 'mini-phone-progress' and set(data) == {'action', 'account', 'proofs'}:
        account, proofs = data['account'], data['proofs']
        if (not isinstance(account, str) or not re.fullmatch(r'[1-9][0-9]{0,15}', account)
                or not isinstance(proofs, dict) or not proofs or set(proofs) - {LEGACY, *CORPS}
                or any(not isinstance(p, str) or not re.fullmatch(r'[a-f0-9]{64}', p) for p in proofs.values())):
            raise Denied('invalid_request')
        # Account IDs share the original portal namespace. Select the scope from
        # existing job records, never the active enterprise for a fresh signup.
        import sqlite3
        from contextlib import closing
        results = []
        for corp in proofs:
            root = bridge.root if corp == LEGACY else bridge.root/'enterprises'/corp
            with closing(sqlite3.connect((root/'onboarding/jobs.sqlite3').as_uri()+'?mode=ro', uri=True)) as db:
                exists = db.execute('SELECT 1 FROM jobs WHERE account=?', (account,)).fetchone()
            if exists:
                value = worker(bridge, corp, {'action': 'mini-phone-progress', 'account': account, 'proof': proofs[corp]})
                found = candidate(bridge, corp, value)
                results.append(dict(found or value, corp=corp))
        if len(results) != 1: raise Denied('application_unavailable')
        return results[0]
    raise Denied('invalid_request')
