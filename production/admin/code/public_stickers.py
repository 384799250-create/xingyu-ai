"""Administrator-owned public sticker catalogue; no user or financial tables."""
from contextlib import closing
import base64,hashlib,json,re,secrets,sqlite3,time,struct,zlib
from pathlib import Path

DATABASE=Path('/home/ubuntu/wecom-kf/admin/data/public-stickers.sqlite3')
MOODS = ('开心', '亲昵撒娇', '想念', '安慰鼓励', '难过委屈', '生气无语', '疑问惊讶', '害羞', '打招呼', '告别', '感谢', '抱歉', '赞同', '拒绝', '搞怪', '嘲讽')
LIMIT=500
class PoolError(ValueError):pass

def connect(write=False):
 if not write and not DATABASE.exists():return None
 db=sqlite3.connect(str(DATABASE) if write else DATABASE.resolve().as_uri()+'?mode=ro',uri=not write,timeout=5);db.row_factory=sqlite3.Row
 if write:
  db.executescript('''CREATE TABLE IF NOT EXISTS stickers(id TEXT PRIMARY KEY,revision INTEGER NOT NULL,label TEXT NOT NULL,mood TEXT NOT NULL,profile TEXT NOT NULL,mime TEXT NOT NULL,ext TEXT NOT NULL,body BLOB NOT NULL,thumbnail TEXT NOT NULL,digest TEXT NOT NULL,enabled INTEGER NOT NULL,updated REAL NOT NULL,actor TEXT NOT NULL,request_id TEXT UNIQUE);
   CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,sticker TEXT NOT NULL,action TEXT NOT NULL,actor TEXT NOT NULL,created REAL NOT NULL);''')
  DATABASE.chmod(0o600)
 return db

def ident(value):
 if not isinstance(value,str) or not re.fullmatch(r'pub_[a-f0-9]{24}',value):raise PoolError('公共表情编号无效。')
 return value

def profile(data):
 if not isinstance(data,dict) or set(data)!={'label','mood','description','text','scenes','avoid'}:raise PoolError('请填写公共表情名称、分类和画面含义。')
 result={}
 for key,limit in [('label',40),('mood',10),('description',300),('text',200),('scenes',480),('avoid',480)]:
  value=data[key]
  if not isinstance(value,str) or len(value)>limit:raise PoolError('表情说明过长，请精简后保存。')
  result[key]=value.strip()
 if result['mood']=='晚安':result['mood']='告别'
 if not result['label'] or not result['description'] or result['mood'] not in MOODS:raise PoolError('名称、固定分类和画面含义不能为空。')
 for key in ('scenes','avoid'):
  lines=[v.strip() for v in result[key].splitlines() if v.strip()]
  if len(lines)>8 or any(len(v)>60 for v in lines):raise PoolError('适用或不适用场景各最多 8 行，每行 60 字。')
 return result['label'],result['mood'],dict(description=result['description'],text=result['text'],tone='',scenes=[v.strip() for v in result['scenes'].splitlines() if v.strip()][:8],avoid=[v.strip() for v in result['avoid'].splitlines() if v.strip()][:8],keywords=[])

def image(value):
 if not isinstance(value,str) or len(value)>700000:raise PoolError('图片需小于 512 KB。')
 m=re.fullmatch(r'data:image/(png|jpeg|gif);base64,([A-Za-z0-9+/=]+)',value)
 if not m:raise PoolError('请选择 PNG、JPEG 或 GIF 图片。')
 try:raw=base64.b64decode(m[2],validate=True)
 except ValueError:raise PoolError('图片编码无效。') from None
 if not 8<=len(raw)<=512*1024:raise PoolError('图片需小于 512 KB。')
 try:
  kind=m[1]
  if kind=='png':
   if not raw.startswith(b'\x89PNG\r\n\x1a\n'):raise ValueError()
   pos=8;first=True;ended=False
   while pos<len(raw):
    size=struct.unpack('>I',raw[pos:pos+4])[0];tag=raw[pos+4:pos+8];end=pos+12+size
    if end>len(raw) or zlib.crc32(raw[pos+4:pos+8+size])&0xffffffff!=struct.unpack('>I',raw[pos+8+size:end])[0]:raise ValueError()
    if first:
     if tag!=b'IHDR' or size!=13:raise ValueError()
     width,height=struct.unpack('>II',raw[pos+8:pos+16]);first=False
    if tag==b'IEND':
     if size!=0 or end!=len(raw):raise ValueError()
     ended=True;break
    pos=end
   if not ended:raise ValueError()
  elif kind=='gif':
   if raw[:6] not in (b'GIF87a',b'GIF89a') or raw[-1:]!=b';':raise ValueError()
   width,height=struct.unpack('<HH',raw[6:10])
  else:
   if not raw.startswith(b'\xff\xd8\xff') or not raw.endswith(b'\xff\xd9'):raise ValueError()
   pos=2;width=height=0
   while pos<len(raw)-2:
    if raw[pos]!=255:raise ValueError()
    while raw[pos]==255:pos+=1
    marker=raw[pos];pos+=1
    if marker in (0xd8,0xd9):continue
    if marker==0xda:break
    size=struct.unpack('>H',raw[pos:pos+2])[0]
    if size<2 or pos+size>len(raw):raise ValueError()
    if marker in (0xc0,0xc1,0xc2,0xc3,0xc5,0xc6,0xc7,0xc9,0xca,0xcb,0xcd,0xce,0xcf):height,width=struct.unpack('>HH',raw[pos+3:pos+7]);break
    pos+=size
  if not 0<width<=4096 or not 0<height<=4096 or width*height>8_000_000:raise PoolError('图片需在 4096×4096 且 800 万像素以内。')
 except PoolError:raise
 except (ValueError,IndexError,struct.error):raise PoolError('图片损坏或无法识别，请重新导出。') from None
 return raw,'image/'+m[1],{'jpeg':'jpg','png':'png','gif':'gif'}[m[1]],''

def metadata(row,thumbnail=False):
 result={k:row[k] for k in ('id','revision','label','mood','mime','ext','digest','updated')}
 if result['mood']=='晚安':result['mood']='告别'
 result.update(size=row['size'],enabled=bool(row['enabled']),profile=json.loads(row['profile']),source='public',category_status='confirmed',understanding_status='manual')
 if thumbnail:result['thumbnail']='/ai/admin/api/public-stickers/image?id='+row['id']
 return result

COLS='id,revision,label,mood,profile,mime,ext,digest,enabled,updated,length(body) AS size'
def public(action,payload):
 if not isinstance(payload,dict):raise PoolError('公共表情请求无效。')
 if action=='list':
  if payload:raise PoolError('公共表情查询参数无效。')
 elif action in ('detail','check','thumbnail'):
  if set(payload)!={'id'}:raise PoolError('公共表情查询参数无效。')
  ident(payload['id'])
 else:raise PoolError('公共表情操作不受支持。')
 db=connect()
 if db is None:
  if action=='list':return {'ok':True,'items':[]}
  raise PoolError('此公共表情已下架，请重新选择。')
 with closing(db):
  if action=='list':return {'ok':True,'items':[metadata(r) for r in db.execute('SELECT '+COLS+' FROM stickers WHERE enabled=1 ORDER BY updated DESC,id LIMIT ?',(LIMIT,))]}
  row=db.execute('SELECT '+COLS+',body,thumbnail FROM stickers WHERE id=? AND enabled=1',(payload['id'],)).fetchone()
  if row is None:raise PoolError('此公共表情已下架，请重新选择。')
  result={'ok':True,'item':metadata(row)}
  if action=='detail':result['body']=base64.b64encode(row['body']).decode()
  if action=='thumbnail':result['image']='data:'+row['mime']+';base64,'+base64.b64encode(row['body']).decode()
  return result

def admin(actor,action,payload):
 if actor.get('role')!='superadmin':raise PoolError('只有超级管理员可以管理公共表情包。')
 if not isinstance(payload,dict):raise PoolError('请求无效。')
 if action=='image':
  if set(payload)!={'id'}:raise PoolError('图片查询无效。')
  ident(payload['id']);db=connect()
  if db is None:raise PoolError('图片不存在。')
  with closing(db):
   row=db.execute('SELECT body,mime FROM stickers WHERE id=?',(payload['id'],)).fetchone()
   if row is None:raise PoolError('图片不存在。')
   return {'body':base64.b64encode(row['body']).decode(),'mime':row['mime']}
 if action in ('list','selection'):return admin_listing(action,payload)
 if action=='detail':return admin_detail(payload)
 if action not in ('save','toggle','delete'):raise PoolError('公共表情操作无效。')
 if action=='save':
  if set(payload) not in ({'id','revision','content','image','request_id'},{'id','revision','content','image','request_id','analysis_token'}):raise PoolError('上传参数无效。')
  label,mood,desc=profile(payload['content']);request_id=payload['request_id']
  if 'analysis_token' in payload and not payload['id']:raise PoolError('识别结果只可保存到原图片。')
  if not isinstance(request_id,str) or not re.fullmatch('[a-f0-9]{32}',request_id):raise PoolError('上传请求编号无效。')
  if payload['id']:
   ident(payload['id'])
   if payload['image']:raise PoolError('修改现有表情不替换原图，请上传为新表情。')
   raw=mime=ext=thumbnail=None
  else:raw,mime,ext,thumbnail=image(payload['image'])
 else:
  if set(payload)!=({'id','revision','enabled'} if action=='toggle' else {'id','revision'}):raise PoolError('操作参数无效。')
  ident(payload['id'])
  if action=='toggle' and type(payload['enabled']) is not bool:raise PoolError('上下架状态无效。')
 if type(payload['revision']) is not int or payload['revision']<0:raise PoolError('版本无效，请重新读取。')
 with closing(connect(True)) as db,db:
  db.execute('BEGIN IMMEDIATE')
  if action=='save' and not payload['id']:
   previous=db.execute('SELECT id FROM stickers WHERE request_id=?',(request_id,)).fetchone()
   if previous:return {'ok':True,'id':previous['id'],'duplicate':True}
   if db.execute('SELECT count(*) FROM stickers').fetchone()[0]>=LIMIT:raise PoolError('公共池已满，请先删除不需要的表情。')
   key='pub_'+secrets.token_hex(12)
   db.execute('INSERT INTO stickers VALUES(?,1,?,?,?,?,?,?,?,?,1,?,?,?)',(key,label,mood,json.dumps(desc,ensure_ascii=False),mime,ext,raw,thumbnail,hashlib.sha256(raw).hexdigest(),time.time(),actor['username'],request_id))
  else:
   key=payload['id'];row=db.execute('SELECT revision,digest FROM stickers WHERE id=?',(key,)).fetchone()
   if not row or row['revision']!=payload['revision']:raise PoolError('表情已被修改或删除，请重新读取后再操作。')
   if action=='save' and 'analysis_token' in payload:
    import public_sticker_upload
    public_sticker_upload.verify_analysis(actor,payload['analysis_token'],key,row['revision'],row['digest'])
   if action=='delete':db.execute('DELETE FROM stickers WHERE id=?',(key,))
   elif action=='toggle':db.execute('UPDATE stickers SET enabled=?,revision=revision+1,updated=?,actor=? WHERE id=?',(int(payload['enabled']),time.time(),actor['username'],key))
   else:db.execute('UPDATE stickers SET label=?,mood=?,profile=?,revision=revision+1,updated=?,actor=? WHERE id=?',(label,mood,json.dumps(desc,ensure_ascii=False),time.time(),actor['username'],key))
  if action=='save' and 'analysis_token' in payload:
   db.execute('CREATE TABLE IF NOT EXISTS sticker_understanding(id TEXT PRIMARY KEY,digest TEXT NOT NULL,profile_hash TEXT NOT NULL,updated REAL NOT NULL)')
   db.execute('INSERT OR REPLACE INTO sticker_understanding VALUES(?,?,?,?)',(key,row['digest'],profile_digest(desc),time.time()))
  db.execute('INSERT INTO events(sticker,action,actor,created) VALUES(?,?,?,?)',(key,action,actor['username'],time.time()))
  return {'ok':True,'id':key}

# Inserted into public_stickers.py. Reads never create a schema or alter images.
def profile_digest(value):
 return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def analysis_marks(db):
 if not db or not db.execute("SELECT 1 FROM sqlite_master WHERE name='sticker_understanding'").fetchone():return {}
 return {r['id']:(r['digest'],r['profile_hash']) for r in db.execute('SELECT id,digest,profile_hash FROM sticker_understanding')}

def analysis_status(item,marks):
 if marks.get(item['id'])==(item['digest'],profile_digest(item['profile'])):return 'recognized'
 return 'pending' if '（按原分类导入，尚未逐图识别。）' in item['profile'].get('description','') or not item['profile'].get('description','').strip() else 'manual'

def admin_detail(payload):
 if set(payload)!={'id'}:raise PoolError('详情参数无效。')
 ident(payload['id']);db=connect()
 if db is None:raise PoolError('图片不存在。')
 with closing(db):
  row=db.execute('SELECT '+COLS+' FROM stickers WHERE id=?',(payload['id'],)).fetchone()
  if row is None:raise PoolError('图片不存在。')
  item=metadata(row,True);item['analysis_status']=analysis_status(item,analysis_marks(db));return item

def admin_listing(action,payload):
 if set(payload)-{'page','mood','understanding'}:raise PoolError('查询参数无效。')
 mood=payload.get('mood','');understanding=payload.get('understanding','')
 if not isinstance(mood,str) or mood and mood not in MOODS or understanding not in ('','pending','recognized','manual'):raise PoolError('筛选条件无效。')
 try:page=int(payload.get('page',1))
 except (ValueError,TypeError):raise PoolError('页码无效。') from None
 if not 1<=page<=100:raise PoolError('页码无效。')
 items=[];db=connect()
 if db is not None:
  with closing(db):
   marks=analysis_marks(db)
   for row in db.execute('SELECT '+COLS+' FROM stickers ORDER BY updated DESC,id LIMIT ?',(LIMIT,)):
    item=metadata(row,True);item['analysis_status']=analysis_status(item,marks);items.append(item)
 counts={name:sum(item['mood']==name for item in items) for name in MOODS}
 states={name:sum(item['analysis_status']==name for item in items) for name in ('pending','recognized','manual')}
 filtered=[v for v in items if (not mood or v['mood']==mood) and (not understanding or v['analysis_status']==understanding)]
 if action=='selection':return {'items':[{k:v[k] for k in ('id','revision','digest','label','mood','thumbnail','analysis_status')} for v in filtered],'count':len(filtered)}
 pages=max(1,(len(filtered)+11)//12);page=min(page,pages)
 return {'items':filtered[(page-1)*12:page*12],'total':len(items),'filtered_total':len(filtered),'page':page,'pages':pages,'limit':LIMIT,'categories':MOODS,'counts':counts,'understanding_counts':states}
