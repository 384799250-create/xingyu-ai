"""Read-only, cross-enterprise account allocation presentation.

This does not confirm website ownership or change any assignment.
"""
from contextlib import closing
import hashlib
import hmac
import json
import sqlite3
import application_names
from pathlib import Path

CORP = 'ww0000000000000002'


def read(path, sql):
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True, timeout=10)) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(sql)]


def assignment_name(ops,root,row):
    database=None
    if root==ops.root and row['agent']==1000003:
        try:database=ops.data_path(row['agent'])
        except Exception:return row['name']
    return application_names.assigned_name(root,row['agent'],row['owner'],row['name'],database)


def listing(ops):
    roles = {r['account']: r['role'] for r in read(ops.database, 'SELECT account,role FROM roles')}
    accounts = read(ops.portal_database, 'SELECT id,username,created,subject,waiting FROM accounts ORDER BY id')
    by_id = {str(a['id']): a for a in accounts}
    by_subject = {a['subject']: a for a in accounts if a['subject']}
    allocations = {a['id']: [] for a in accounts}
    states = {a['id']: [] for a in accounts}
    scopes = [(ops.root, 'legacy', '原企业')]
    for corp, label in ((CORP,'星屿 01'),('ww0000000000000003','星屿 02')):
        other = ops.root/'enterprises'/corp
        if other.exists():
            if other.is_symlink():raise ValueError('unsafe_enterprise_path')
            scopes.append((other, corp, label))
    for root, enterprise, label in scopes:
        jobs_path = root/'onboarding/jobs.sqlite3'
        jobs = read(jobs_path, 'SELECT account,agent,owner,request,state,stage,error FROM jobs') if jobs_path.is_file() else []
        current_jobs = {}
        for job in jobs:
            account = by_id.get(str(job['account']))
            if account:
                states[account['id']].append('reclaimed' if job['stage']=='released' and job['error']=='application_reclaimed' else job['state'])
                if job['state'] in ('awaiting_scan', 'ready'):
                    current_jobs[(job['agent'], job['owner'], job['request'])] = account
        registry = root/'app-pool/registry.sqlite3'
        rows = read(registry, 'SELECT agent,corp,name,state,owner,request_id FROM apps') if registry.is_file() else []
        # Original manually configured main app is outside the allocation registry.
        bindings = root/'admin/data/application-bindings.json'
        if enterprise == 'legacy' and bindings.is_file():
            config = dict(line.split('=', 1) for line in (root/'.env').read_text('utf8').splitlines()
                          if line and not line.startswith('#') and '=' in line)
            for key, value in json.loads(bindings.read_text('utf8')).items():
                if key != '1000003' or value.get('agent') != 1000003 or not value.get('owner'):
                    raise ValueError('invalid_manual_binding')
                rows.append(dict(agent=1000003, corp=config.get('WECOM_CORP_ID', ''), name=value.get('name', ''),
                                 state='assigned', owner=value['owner'], request_id=None, website_account=value.get('website_account')))
        key_path = root/'app-pool/portal-identity.key'
        key = key_path.read_bytes() if key_path.is_file() else None
        for row in rows:
            if not row['owner'] or row['state'] not in ('assigned', 'disabled', 'failed'):
                continue
            subject = hmac.new(key, json.dumps([row['corp'], row['owner']], separators=(',', ':')).encode(),
                               hashlib.sha256).hexdigest() if key else None
            account = by_subject.get(subject)
            confirmed = account is not None
            if not account and row['state'] == 'assigned' and row['request_id']:
                candidate = current_jobs.get((row['agent'], row['owner'], row['request_id']))
                # A pending allocation is a display association only. A conflicting
                # confirmed website identity must never be inferred or overwritten.
                if candidate and not candidate['subject']:
                    account = candidate
            manual = False
            if not account and row.get('website_account'):
                reference = row['website_account']
                candidate = by_id.get(str(reference.get('id')))
                if candidate and candidate['username'] == reference.get('username') and not candidate['subject']:
                    account = candidate
                    manual = True
            if not account:
                continue
            allocations[account['id']].append(dict(agent=row['agent'], name=assignment_name(ops,root,row), corp_id=row['corp'],
                enterprise=enterprise, enterprise_label=label, status='confirmed' if confirmed else 'manual_dedicated' if manual else 'pending_confirmation'))
    result = []
    for account in accounts:
        apps = sorted(allocations[account['id']], key=lambda a: (a['enterprise'], a['agent']))
        job_states = states[account['id']]
        if apps:
            status = 'confirmed' if all(a['status'] == 'confirmed' for a in apps) else 'manual_dedicated' if all(a['status'] == 'manual_dedicated' for a in apps) else 'pending_confirmation'
        elif 'needs_inspection' in job_states:
            status = 'needs_inspection'
        elif any(s in ('queued', 'provisioning', 'verifying') for s in job_states):
            status = 'opening'
        elif 'waiting_pool' in job_states:
            status = 'waiting_pool'
        elif 'failed' in job_states:
            status = 'failed'
        elif 'reclaimed' in job_states:
            status = 'reclaimed'
        else:
            status = 'unallocated'
        result.append(dict(id=account['id'], username=account['username'], created=account['created'],
            role=roles.get(account['id'], 'member'), waiting=bool(account['waiting']),
            agent=apps[0]['agent'] if len(apps) == 1 else None, applications=apps, binding_status=status))
    return result
