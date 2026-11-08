"""Root-only retained-account RPC, authorized by the website's opaque subject."""
from pathlib import Path
from contextlib import closing
from types import SimpleNamespace
import hashlib,hmac,json,re,socket,sqlite3,sys,time
BASE=Path('/home/ubuntu/wecom-kf')
sys.path.insert(0,str(BASE/'admin/code'))
import operating_fee as fee,star_wallet as wallet,daily_bonus,wallet_closure

def roots(root):
 return (root,root/'enterprises/ww0000000000000002',root/'enterprises/ww0000000000000003')
def lookup(root,subject):
 found=[]
 for r in roots(Path(root)):
  with closing(sqlite3.connect((r/'admin/data/star-wallet.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
   db.row_factory=sqlite3.Row
   if not db.execute("SELECT 1 FROM sqlite_master WHERE name='operating_accounts'").fetchone():continue
   rows=db.execute("SELECT o.*,w.corp,w.user FROM operating_accounts o JOIN wallets w ON w.id=o.wallet WHERE o.phase IN ('retained','restoring')").fetchall()
  key=(r/'app-pool/portal-identity.key').read_bytes()
  for s in rows:
   proof=hmac.new(key,json.dumps([s['corp'],s['user']],separators=(',',':')).encode(),hashlib.sha256).hexdigest()
   if hmac.compare_digest(proof,subject):found.append((r,dict(s)))
 if len(found)>1:raise ValueError('ambiguous_retained_identity')
 return found[0] if found else None
def payment(path,data):
 with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
  client.settimeout(25);client.connect(path);client.sendall(json.dumps(data).encode()+b'\n')
  with client.makefile('rb') as stream:raw=stream.readline(950000)
  result=json.loads(raw)
  return result
def route(root,data):
 if not isinstance(data,dict):return None
 action=data.get('action');special=isinstance(action,str) and action.startswith('operating-')
 if action not in ('status','mint') and not special:return None
 subject=data.get('subject')
 if not isinstance(subject,str) or not re.fullmatch('[a-f0-9]{64}',subject):return {'ok':False,'error':'invalid_request'} if special else None
 allowed={'status':set(),'mint':set(),'operating-status':set(),'operating-claim':{'day'},'operating-pay':{'provider','payment_action','payload'},'operating-recover':set()}
 if action not in allowed or set(data)!={'action','subject'}|allowed[action]:return {'ok':False,'error':'invalid_request'}
 match=lookup(root,subject)
 if not match:
  if special:return {'ok':False,'error':'application_unavailable'}
  return None
 r,s=match;now=time.time()
 if not (now<s['paused_at']+15*86400 or s['phase']=='restoring' and s['restored_at']<s['paused_at']+15*86400):return {'ok':False,'error':'application_unavailable'}
 ops=SimpleNamespace(root=r,directory=r/'admin/data')
 import fcntl
 with (r/'app-pool/operation.lock').open('a') as lock:
  try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:return {'ok':False,'error':'service_unavailable'}
  with closing(wallet.connect(ops)) as db:
   if wallet_closure.state(db,s['corp'],s['user']):return {'ok':False,'error':'application_unavailable'}
   if not fee.retained_allowed(db,s['corp'],s['user'],s['agent']):return {'ok':False,'error':'application_unavailable'}
   result={'subject':subject,'agent':s['agent'],'name':'资料保留与恢复','state':'retained'}
   if action in ('status','mint'):
    if action=='mint':result['url']='https://www.example.com/ai/retained/'
    return {'ok':True,'data':result}
   if action=='operating-pay':
    provider=data['provider'];pa=data['payment_action'];payload=data['payload']
    if provider not in ('alipay','wechat') or pa not in ('status','create','get','query','history') or not isinstance(payload,dict):return {'ok':False,'error':'invalid_request'}
    # The payment service reacquires this same lock and repeats owner validation.
    fcntl.flock(lock,fcntl.LOCK_UN)
    return payment('/run/xingyu-'+provider+'/service.sock',{'corp':s['corp'],'user':s['user'],'agent':s['agent'],'action':pa,'payload':payload})
   db.execute('BEGIN IMMEDIATE')
   if action=='operating-claim':
    if not isinstance(data['day'],str) or data['day']!=fee.day_info(now)[0]:raise ValueError('claim_date_changed')
    daily_bonus.claim(db,s['wallet'],data['day'])
   fee.reconcile(db,s['wallet']);db.commit()
   result.update(fee.summary(db,s['wallet']))
   result.update(wallet.public(daily_bonus.summary(db,s['wallet'],now)))
   return {'ok':True,'data':result}
