"""Private, allowlisted routing. Existing requests always keep their original tenant."""
import hashlib,hmac,json,re,socket,sqlite3,time
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

CORPS=('ww0000000000000002','ww0000000000000003')
CORP=CORPS[0]  # Compatibility with private callers during the routing update.

def forward(path,data):
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
        client.settimeout(25);client.connect(str(path))
        client.sendall(json.dumps(data).encode()+b'\n')
        with client.makefile('rb') as stream:raw=stream.readline(950000)
        if len(raw)>949999 or not raw.endswith(b'\n'):raise ValueError('Invalid response')
        result=json.loads(raw)
        if not isinstance(result,dict) or type(result.get('ok')) is not bool:raise ValueError('Invalid response')
        return result

def config(root):
    path=Path(root)/'onboarding/active-enterprise.json'
    if not path.exists():return ()
    if path.is_symlink() or path.stat().st_mode&0o022:raise ValueError('Unsafe routing config')
    value=json.loads(path.read_text())
    if value=={'corp':CORPS[0],'enabled':True}:return (CORPS[0],)
    if value=={'corps':list(CORPS),'enabled':True}:return CORPS
    raise ValueError('Unknown enterprise config')

def read(path,sql,args=()):
    path=Path(path)
    if path.is_symlink() or not path.is_file():raise ValueError('Unavailable enterprise')
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
        return db.execute(sql,args).fetchall()

def roots(root):
    root=Path(root)
    return (root,*(root/'enterprises'/corp for corp in CORPS if (root/'enterprises'/corp).is_dir()))

def dispatch(root,target,data,kind):
    if target==Path(root):return None
    try:return forward('/run/xiaoyi-portal/'+kind+'-'+target.name+'.sock',data)
    except (OSError,ValueError):return {'ok':False,'error':'service_unavailable'}

def onboarding_route(root,data):
    active=config(root)
    if not active:return None
    if not isinstance(data,dict) or not isinstance(data.get('account'),str) or not re.fullmatch(r'[1-9][0-9]{0,15}',data['account']):
        return {'ok':False,'error':'invalid_request'}
    # Serialize selection and submission until a worker has persisted its request.
    import fcntl
    with (Path(root)/'onboarding/enterprise-routing.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        matched=[r for r in roots(root) if read(r/'onboarding/jobs.sqlite3','SELECT 1 FROM jobs WHERE account=?',(data['account'],))]
        if len(matched)>1:return {'ok':False,'error':'request_identity_mismatch'}
        if matched:return dispatch(root,matched[0],data,'onboarding')
        # A phone already used in another tenant must not receive another slot.
        if data.get('action')=='submit' and isinstance(data.get('phone'),str):
            phone=data['phone'].strip()
            if re.fullmatch(r'1[3-9][0-9]{9}',phone):phone='+86'+phone
            for r in roots(root):
                key=(r/'app-pool/portal-identity.key').read_bytes()
                digest=hmac.new(key,json.dumps(('phone',phone),separators=(',',':')).encode(),hashlib.sha256).hexdigest()
                if read(r/'onboarding/jobs.sqlite3','SELECT 1 FROM jobs WHERE phone_hash=?',(digest,)):
                    return {'ok':False,'error':'phone_already_requested'}
        candidates=[]
        for corp in active:
            target=Path(root)/'enterprises'/corp
            used=read(target/'onboarding/jobs.sqlite3','SELECT COUNT(*) FROM jobs WHERE created>?',(time.time()-86400,))[0][0]
            free=read(target/'app-pool/registry.sqlite3',"SELECT COUNT(*) FROM apps WHERE state='free'")[0][0]
            candidates.append((used>=20,free<1,used,target))
        candidates.sort(key=lambda x:x[:3])
        if not candidates or candidates[0][0]:return {'ok':False,'error':'onboarding_daily_limit'}
        # Forward just once. An uncertain write must never fall through to another tenant.
        return dispatch(root,candidates[0][3],data,'onboarding')

def bridge_route(root,data,legacy):
    from operating_bridge import route
    operating=route(root,data)
    if operating is not None:return operating
    if not config(root) or not isinstance(data,dict):return None
    targets=[]
    if data.get('action')=='verify':
        value=data.get('link')
        if not isinstance(value,str):return None
        path=urlsplit(value).path
        targets=[r for r in roots(root)[1:] if path.startswith('/ai/enterprises/'+r.name+'/')]
        if not targets and not path.startswith('/ai/enterprises/'):return None
    elif data.get('action') in ('status','mint'):
        subject=data.get('subject')
        if not isinstance(subject,str) or not re.fullmatch('[a-f0-9]{64}',subject):return None
        if any(hmac.compare_digest(legacy.subject(row),subject) for row in legacy.rows()):return None
        for r in roots(root)[1:]:
            key=(r/'app-pool/portal-identity.key').read_bytes()
            for corp,owner in read(r/'app-pool/registry.sqlite3','SELECT corp,owner FROM apps WHERE owner IS NOT NULL'):
                digest=hmac.new(key,json.dumps([corp,owner],separators=(',',':')).encode(),hashlib.sha256).hexdigest()
                if hmac.compare_digest(digest,subject):targets.append(r);break
    else:return None
    if len(targets)!=1:return {'ok':False,'error':'application_unavailable'}
    return dispatch(root,targets[0],data,'bridge')
