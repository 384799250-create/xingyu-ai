"""Private subprocess adapter: isolated enterprise imports, central closure journal.

No socket or HTTP entrypoint. Called under both enterprise operation locks.
"""
from contextlib import closing
import importlib.util,json,sys
from pathlib import Path

HOME=Path(__file__).resolve().parents[2]
CORP='ww0000000000000002'

def main():
    message=json.load(sys.stdin)
    corp=Path(message.get('root','')).name
    if corp not in (CORP,'ww0000000000000003'):raise ValueError('invalid_scope')
    root=HOME/'enterprises'/corp
    if message.get('global_root')!=str(HOME) or message.get('root')!=str(root) or root.is_symlink():raise ValueError('invalid_scope')
    sys.path[:0]=[str(root/'admin/code'),str(root/'app-pool'),str(root/'onboarding')]
    spec=importlib.util.spec_from_file_location('closure_engine',HOME/'admin/code/account_deletion.py')
    engine=importlib.util.module_from_spec(spec);spec.loader.exec_module(engine)
    from admin_ops import AdminOps
    # Never construct another live admin executor or reset jobs during a read.
    ops=AdminOps.__new__(AdminOps)
    ops.root=root;ops.directory=root/'admin/data';ops.database=HOME/'admin/data/admin.sqlite3'
    ops.portal_database=HOME/'portal/data/portal.sqlite3';ops.scoped=True
    if message.get('action')=='plan':
        actor=message['actor']
        with closing(engine.connection(ops.database)) as db:
            row=db.execute('SELECT role FROM roles WHERE account=?',(actor['id'],)).fetchone()
        if not row or row[0]!='superadmin':raise engine.ClosureError('只有超级管理员可注销账号。')
        data=engine.local_plan(ops,actor,message['account'])
    elif message.get('action')=='execute':
        ident=message['ident']
        with closing(engine.connection(ops.database)) as db:
            row=db.execute('SELECT plan,status FROM account_deletions WHERE id=?',(ident,)).fetchone()
        if not row or row['status']!='running':raise engine.ClosureError('注销记录状态不一致。')
        value=json.loads(row['plan'])
        if value.get('enterprise')!=corp:raise engine.ClosureError('企业归属不一致。')
        ctl=engine.controller(ops)
        try:engine.execute(ops,ctl,ident,value)
        finally:ctl.close()
        data={}
    else:raise ValueError('invalid_action')
    return {'ok':True,'data':data}

if __name__=='__main__':
    try:result=main()
    except Exception as exc:
        result={'ok':False,'error':str(exc) if type(exc).__name__=='ClosureError' else '企业处理未完成，请检查后继续。'}
    print(json.dumps(result,ensure_ascii=False))
