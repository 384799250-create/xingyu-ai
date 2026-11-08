"""Bounded administrator-only image preparation. Never writes public/user/financial DBs."""
import base64,hashlib,json,math,re,secrets,subprocess,threading,time,urllib.request,urllib.error
from pathlib import Path
import public_stickers as pool
from persona_templates import reference_model_config,_ReferenceNoRedirect

PROMPT=(Path(__file__).with_name('sticker_recognition.txt')).read_text('utf8')
ANALYSIS_SLOTS=threading.BoundedSemaphore(2)
CONVERT_SLOTS=threading.BoundedSemaphore(1)
LOCK=threading.Lock();CACHE={};ACTIVE=set()

def authorized(actor):
 if actor.get('role')!='superadmin':raise pool.PoolError('只有超级管理员可以准备公共表情。')

def normalize(result):
 if not isinstance(result,dict):raise ValueError()
 confidence=result.get('confidence');mood=result.get('mood')
 if type(confidence) not in (int,float) or not math.isfinite(confidence) or not 0<=confidence<=1 or not isinstance(mood,str):raise ValueError()
 if mood=='晚安':mood='告别'
 content={'label':'识别预览','mood':mood if mood in pool.MOODS else pool.MOODS[0]}
 for name,maximum in (('description',300),('text',200),('tone',80)):
  value=result.get(name,'')
  if not isinstance(value,str) or len(value)>maximum:raise ValueError()
  if name!='tone':content[name]=value.strip()
 for name in ('scenes','avoid'):
  values=result.get(name,[])
  if not isinstance(values,list) or len(values)>8 or any(not isinstance(v,str) or len(v)>60 for v in values):raise ValueError()
  content[name]='\n'.join(dict.fromkeys(v.strip() for v in values if v.strip()))
  if len(content[name])>480:raise ValueError()
 pool.profile(content)
 uncertain=confidence<.65 or mood not in pool.MOODS
 return {'mood':'' if uncertain else mood,'uncertain':uncertain,'content':{k:v for k,v in content.items() if k not in ('label','mood')}}

def request_analysis(url,key,model,image):
 payload={'model':model,'messages':[{'role':'system','content':PROMPT},{'role':'user','content':[{'type':'text','text':'请识别固定分类以及画面含义、可见文字、适用和不适用场景。'},{'type':'image_url','image_url':{'url':image}}]}],'stream':False,'max_tokens':1000,'thinking':{'type':'disabled'}}
 request=urllib.request.Request(url,data=json.dumps(payload,ensure_ascii=False).encode(),headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'},method='POST')
 opener=urllib.request.build_opener(_ReferenceNoRedirect())
 with opener.open(request,timeout=18) as response:raw=response.read(65537)
 if len(raw)>65536:raise ValueError()
 value=json.loads(raw)['choices'][0]['message']['content']
 if not isinstance(value,str):raise ValueError()
 return normalize(json.loads(re.sub(r'^```(?:json)?\s*|\s*```$','',value.strip(),flags=re.I)))

def analyze(ops,actor,payload):
 authorized(actor)
 if not isinstance(payload,dict) or set(payload) not in ({'image'},{'image','id','revision','digest'}):raise pool.PoolError('识别参数无效。')
 target=target_analysis(actor,payload) if 'id' in payload else None
 raw,mime,_,_=pool.image(payload['image'])
 if len(raw)>200*1024 or mime not in ('image/png','image/jpeg'):raise pool.PoolError('请使用200 KB以内的PNG或JPEG识别预览。')
 try:url,key,model=reference_model_config(ops)
 except ValueError:raise pool.PoolError('自动识别暂未配置，可先手动填写分类和画面含义。') from None
 digest=hashlib.sha256(json.dumps([url,key,model,payload['image']]).encode()).hexdigest();now=time.monotonic()
 with LOCK:
  for k in list(CACHE):
   if CACHE[k][0]<now:del CACHE[k]
  if digest in CACHE:return dict(CACHE[digest][1],cached=True,**({'analysis_token':issue_analysis(actor,target)} if target else {}))
  if digest in ACTIVE:raise pool.PoolError('这张图片正在识别，请稍后重试。')
  if not ANALYSIS_SLOTS.acquire(blocking=False):raise pool.PoolError('正在识别其他图片，请稍后重试。')
  ACTIVE.add(digest)
 try:
  result=request_analysis(url,key,model,payload['image'])
  with LOCK:
   while len(CACHE)>=32:CACHE.pop(next(iter(CACHE)))
   CACHE[digest]=(time.monotonic()+300,result)
  return dict(result,**({'analysis_token':issue_analysis(actor,target)} if target else {}))
 except (ValueError,KeyError,TypeError,IndexError,OSError,TimeoutError,urllib.error.URLError):raise pool.PoolError('自动识别未完成，图片和已填写内容已保留，可重试或手动填写。') from None
 finally:
  with LOCK:ACTIVE.discard(digest)
  ANALYSIS_SLOTS.release()

def run_converter(raw):
 image=subprocess.run(['docker','inspect','--format','{{.Image}}','wecom-kf-kf-assistant-1'],capture_output=True,text=True,timeout=3,check=True).stdout.strip()
 if not re.fullmatch(r'sha256:[a-f0-9]{64}',image):raise ValueError()
 source=Path(__file__).with_name('sticker_converter.py').read_text('utf8')
 name='xingyu-public-sticker-convert-'+secrets.token_hex(8)
 command=['docker','run','--rm','-i','--name',name,'--network','none','--memory','320m','--cpus','0.5','--pids-limit','32','--read-only','--tmpfs','/tmp:rw,noexec,size=16m','--cap-drop','ALL','--security-opt','no-new-privileges','--user','65534:65534','--entrypoint','python',image,'-I','-c',source]
 try:process=subprocess.run(command,input=raw,capture_output=True,timeout=18,check=True)
 finally:
  try:subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=3)
  except (OSError,subprocess.SubprocessError):pass
 if len(process.stdout)>750000:raise ValueError()
 result=json.loads(process.stdout)
 if result.get('error'):raise pool.PoolError(result['error'])
 if result.get('mime') not in ('image/png','image/gif') or type(result.get('animated')) is not bool:raise ValueError()
 pool.image('data:'+result['mime']+';base64,'+result['image'])
 return result

def convert(actor,payload):
 authorized(actor)
 if not isinstance(payload,dict) or set(payload)!={'image'} or not isinstance(payload['image'],str) or len(payload['image'])>1092300:raise pool.PoolError('WebP转换参数无效。')
 try:raw=base64.b64decode(payload['image'],validate=True)
 except ValueError:raise pool.PoolError('WebP编码无效。') from None
 if not 12<=len(raw)<=800*1024 or raw[:4]!=b'RIFF' or raw[8:12]!=b'WEBP':raise pool.PoolError('请选择800 KB以内的有效WebP图片。')
 if not CONVERT_SLOTS.acquire(blocking=False):raise pool.PoolError('正在转换其他动画，请稍后重试。')
 try:return run_converter(raw)
 except pool.PoolError:raise
 except (ValueError,KeyError,TypeError,OSError,subprocess.SubprocessError):raise pool.PoolError('WebP转换未完成，请缩小动画后重试；不会转成静态图。') from None
 finally:CONVERT_SLOTS.release()

# Signed proof binds a completed recognition to one image, version and admin.
# Drafts expire after four hours or an admin-service restart; never auto-saved.
TOKEN_KEY=secrets.token_bytes(32)

def issue_analysis(actor,target):
 import hmac
 claims={'id':target['id'],'revision':target['revision'],'digest':target['digest'],'actor':actor['username'],'expires':int(time.time())+14400}
 body=base64.urlsafe_b64encode(json.dumps(claims,separators=(',',':')).encode()).decode().rstrip('=')
 return body+'.'+hmac.new(TOKEN_KEY,body.encode(),hashlib.sha256).hexdigest()

def verify_analysis(actor,token,ident,revision,digest):
 import hmac
 try:
  if not isinstance(token,str) or len(token)>1200:raise ValueError()
  body,signature=token.split('.')
  if not hmac.compare_digest(signature,hmac.new(TOKEN_KEY,body.encode(),hashlib.sha256).hexdigest()):raise ValueError()
  claims=json.loads(base64.urlsafe_b64decode(body+'='*((-len(body))%4)))
  if claims!={'id':ident,'revision':revision,'digest':digest,'actor':actor['username'],'expires':claims['expires']} or type(claims['expires']) is not int or claims['expires']<time.time():raise ValueError()
 except (ValueError,KeyError,TypeError):raise pool.PoolError('识别结果已过期或图片已改变，请重新识别后保存。') from None

def target_analysis(actor,payload):
 if set(payload)!={'image','id','revision','digest'} or type(payload['revision']) is not int or payload['revision']<1:raise pool.PoolError('重新识别参数无效。')
 target=pool.admin(actor,'detail',{'id':payload['id']})
 if target['revision']!=payload['revision'] or target['digest']!=payload['digest']:raise pool.PoolError('这张表情已被修改，请刷新后重新识别。')
 return target
