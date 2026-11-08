"""Maintain ten warm free slots across active enterprises, at most five each."""
import argparse,fcntl,importlib.util,json,re,time
from pathlib import Path
CORPS=('ww0000000000000002','ww0000000000000003')
BASE=Path('/home/ubuntu/wecom-kf')
ROOTS=(BASE,*(BASE/'enterprises'/corp for corp in CORPS))

def active_indices(roots=ROOTS):
    path=roots[0]/'onboarding/active-enterprise.json'
    if not path.exists():return (0,)
    if path.is_symlink() or path.stat().st_mode&0o022:raise ValueError('unsafe_enterprise_routing')
    value=json.loads(path.read_text())
    if value=={'corp':CORPS[0],'enabled':True}:return (1,)
    if value=={'corps':list(CORPS),'enabled':True}:return (1,2)
    raise ValueError('unknown_enterprise_routing')

def budgets(active,total=10):
    total=min(total,5*len(active))
    return {index:total//len(active)+int(position<total%len(active)) for position,index in enumerate(active)}

def sweep(roots=ROOTS,wait_seconds=45):
    result={};active=active_indices(roots);warm=budgets(active)
    # Drain inactive slots before warming the active pools. Existing assigned slots are untouched.
    for index in [i for i in range(len(roots)) if i not in active]+list(active):
        root=roots[index]
        if not root.exists():continue
        label='legacy' if index==0 else root.name
        with (root/'app-pool/operation.lock').open('a') as lock:
            deadline=time.monotonic()+wait_seconds
            while True:
                try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    if time.monotonic()>=deadline:break
                    time.sleep(1)
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:result[label]={'busy':True};continue
            spec=importlib.util.spec_from_file_location('standby_pool_'+str(index),root/'app-pool/app_pool.py')
            pool=importlib.util.module_from_spec(spec);spec.loader.exec_module(pool)
            registry=pool.Registry(root/'app-pool/registry.sqlite3')
            try:result[label]=pool.reconcile_free(registry,warm.get(index,0))
            finally:registry.db.close()
    return result

if __name__=='__main__':
    import os
    parser=argparse.ArgumentParser();parser.add_argument('--execute',action='store_true',required=True);parser.parse_args()
    if os.geteuid()!=0:raise SystemExit('server_root_required')
    try:
        result=sweep();print(json.dumps(result));raise SystemExit(1 if any(r.get('blocked') for r in result.values()) else 0)
    except Exception as exc:
        error=str(exc) if re.fullmatch('[A-Za-z0-9_]{1,100}',str(exc)) else 'standby_inspection_required'
        print(json.dumps({'error':error}));raise SystemExit(1)
