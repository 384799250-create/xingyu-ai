"""Root-only directory preparation. No user creation, invitations, or public HTTP."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import re
import secrets
import time
import urllib.parse
import urllib.request


class DirectoryError(Exception):
    pass


def private_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name+'.tmp-'+secrets.token_hex(6))
    fd = os.open(temp, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as out:
        json.dump(value, out, ensure_ascii=False)
    os.replace(temp, path)


class Directory:
    def __init__(self, root, clock=time.time):
        self.root = Path(root)
        self.clock = clock
        path = self.root/'contact.json'
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise DirectoryError('unsafe_credential_permissions')
        self.config = json.loads(path.read_text())

    def request(self, endpoint, params, body=None):
        url = 'https://qyapi.weixin.qq.com/cgi-bin/'+endpoint+'?'+urllib.parse.urlencode(params)
        request = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                         headers={'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                data = json.load(response)
        except Exception:
            # An uncertain write MUST NOT be retried automatically.
            raise DirectoryError('official_result_unknown' if body is not None and endpoint=='department/create'
                                 else 'official_request_failed') from None
        if not isinstance(data, dict):
            raise DirectoryError('unexpected_response')
        if data.get('errcode', 0):
            raise DirectoryError('wecom_code_'+str(int(data['errcode'])))
        return data

    def token(self):
        path = self.root/'contact-token.json'
        if path.is_file():
            saved = json.loads(path.read_text())
            if saved.get('expires', 0)>self.clock()+120:
                return saved['token']
        value = self.request('gettoken', {'corpid':self.config['corp_id'], 'corpsecret':self.config['secret']})
        if not value.get('access_token'):
            raise DirectoryError('token_missing')
        private_json(path, {'token':value['access_token'], 'expires':self.clock()+int(value.get('expires_in',7200))})
        return value['access_token']

    def departments(self):
        result = self.request('department/simplelist', {'access_token':self.token()})
        rows = result.get('department_id')
        if not isinstance(rows, list) or any(not isinstance(x.get('id'),int) for x in rows):
            raise DirectoryError('department_response_invalid')
        return rows

    def members_in(self, department):
        cursor = ''
        members = set()
        seen = set()
        for _ in range(100):
            result = self.request('user/list_id', {'access_token':self.token()}, {'limit':10000, 'cursor':cursor})
            rows = result.get('dept_user')
            if not isinstance(rows,list):
                raise DirectoryError('member_response_invalid')
            for row in rows:
                if row.get('department')==department:
                    members.add(row['userid'])
            cursor = result.get('next_cursor','')
            if not cursor:
                return members
            if cursor in seen:
                raise DirectoryError('pagination_loop')
            seen.add(cursor)
        raise DirectoryError('pagination_limit')

    def ensure_empty_department(self, key, name, parent):
        if not re.fullmatch(r'[a-z0-9_-]{1,40}',key) or not isinstance(parent,int):
            raise DirectoryError('invalid_department_key')
        path = self.root/('department-'+key+'.json')
        if path.is_file():
            saved = json.loads(path.read_text())
            if saved.get('state')!='created':
                raise DirectoryError('prior_department_write_needs_inspection')
            if saved.get('name')!=name or saved.get('parent')!=parent:
                raise DirectoryError('department_identity_mismatch')
            department = saved['id']
        else:
            if parent not in {d['id'] for d in self.departments()}:
                raise DirectoryError('parent_missing')
            # Persist intent before mutation. A crash leaves a visible quarantine marker.
            saved = {'state':'creating','name':name,'parent':parent,'at':self.clock()}
            private_json(path,saved)
            try:
                result = self.request('department/create', {'access_token':self.token()}, {'name':name,'parentid':parent})
                department = result.get('id')
                if not isinstance(department,int) or department<=1:
                    raise DirectoryError('created_department_id_missing')
            except DirectoryError:
                saved['state']='inspection_required'
                private_json(path,saved)
                raise
            saved.update(state='created',id=department)
            private_json(path,saved)
        rows = self.departments()
        actual = next((x for x in rows if x['id']==department),None)
        if not actual or actual.get('parentid')!=parent:
            raise DirectoryError('department_moved_or_missing')
        if any(x.get('parentid')==department for x in rows) or self.members_in(department):
            raise DirectoryError('reserved_department_not_empty')
        return department

    def prepare(self):
        # Parent may contain our previously prepared child on retries.
        parent_path = self.root/'department-spaces.json'
        if parent_path.is_file():
            saved = json.loads(parent_path.read_text())
            if saved.get('state')!='created' or saved.get('name') not in ('小生艺独立空间','星屿独立空间') or saved.get('parent')!=1:
                raise DirectoryError('parent_needs_inspection')
            parent = saved['id']
            if not any(x['id']==parent and x.get('parentid')==1 for x in self.departments()) or self.members_in(parent):
                raise DirectoryError('parent_modified')
        else:
            parent = self.ensure_empty_department('spaces','星屿独立空间',1)
        department = self.ensure_empty_department('slot-003','空间-003',parent)
        return {'prepared':True,'parent_department':parent,'slot_department':department,'slot_name':'空间-003',
                'member_writes':0,'invitations_sent':0,'application_created':False}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare','status'])
    parser.add_argument('--root', default='/home/ubuntu/wecom-kf/onboarding')
    args=parser.parse_args()
    if not hasattr(os,'geteuid') or os.geteuid()!=0:
        raise SystemExit('Server root required')
    import fcntl
    root=Path(args.root)
    os.umask(0o077)
    with (root/'prepare.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        try:
            client=Directory(root)
            result=client.prepare() if args.action=='prepare' else {'ready':True,'department_count':len(client.departments())}
            print(json.dumps(result,ensure_ascii=False))
        except DirectoryError as exc:
            print(json.dumps({'prepared':False,'error':str(exc)}))
