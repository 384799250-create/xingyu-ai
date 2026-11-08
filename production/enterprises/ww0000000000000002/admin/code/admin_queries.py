"""Admin list queries and explicit task acknowledgements; no financial writes."""
import json,re,sqlite3,time
from contextlib import closing
from operations_report import args,readonly,dates
PAGE=25
def reject(message):
 from admin_ops import Rejected
 raise Rejected(message)
def page(items,page):return {'items':items[(page-1)*PAGE:page*PAGE],'count':len(items),'page':page,'page_size':PAGE}
def accounts(ops,query):
 current=args(query,('q','role','enterprise','page'));term=str(query.get('q','')).strip().casefold();role=query.get('role','');enterprise=query.get('enterprise','')
 if role and role not in ('superadmin','operator','viewer','member'):reject('权限筛选无效。')
 if enterprise and enterprise not in ('legacy','ww0000000000000002'):reject('企业筛选无效。')
 items=[]
 for account in ops.accounts():
  apps=account.get('applications',[])
  if role and account['role']!=role:continue
  if enterprise and not any(app['enterprise']==enterprise for app in apps):continue
  hay=' '.join([account['username'],str(account['id']),*(str(app['agent'])+' '+app.get('name','') for app in apps)]).casefold()
  if term and term not in hay:continue
  items.append(account)
 return page(items,current)
def exists(db):return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='task_acknowledgements'").fetchone())
def records(ops,kind,query):
 if kind not in ('jobs','audit'):reject('记录类型无效。')
 current=args(query,('q','agent','status','start','end','page'));where=[];values=[]
 term=str(query.get('q','')).strip()
 if term:
  escaped=term.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
  where.append("(r.actor LIKE ? ESCAPE '\\' OR r.operation LIKE ? ESCAPE '\\')");values.extend(['%'+escaped+'%']*2)
 if query.get('agent'):
  try:agent=int(query['agent']);assert 1<=agent<=1999999
  except (ValueError,TypeError,AssertionError):reject('应用 ID 无效。')
  where.append('EXISTS(SELECT 1 FROM json_each(r.agents) a WHERE a.value=?)');values.append(agent)
 status=query.get('status','')
 if status:
  if kind!='jobs' or status not in ('pending','running','done','failed','interrupted'):reject('状态筛选无效。')
  where.append('r.status=?');values.append(status)
 if query.get('start') or query.get('end'):
  start,end=dates(query);where.append('r.created>=? AND r.created<?');values.extend([start.timestamp(),end.timestamp()])
 clause=' WHERE '+' AND '.join(where) if where else ''
 with readonly(ops.database) as db:
  count=db.execute('SELECT COUNT(*) FROM '+kind+' r'+clause,values).fetchone()[0]
  records=[dict(row) for row in db.execute('SELECT r.* FROM '+kind+' r'+clause+' ORDER BY r.created DESC,r.id DESC LIMIT ? OFFSET ?',values+[PAGE,(current-1)*PAGE])]
  known=exists(db)
  for row in records:
   row['agents']=json.loads(row['agents'])
   if kind=='jobs':
    row.pop('request_id',None);row['result']=json.loads(row['result']) if row.get('result') else []
    ack=db.execute('SELECT actor,note,updated FROM task_acknowledgements WHERE job=? AND acknowledged=1',(row['id'],)).fetchone() if known else None
    row['acknowledgement']=dict(ack) if ack else None
 return {'items':records,'count':count,'page':current,'page_size':PAGE}
def attention(ops):
 with readonly(ops.database) as db:
  condition="status IN ('failed','interrupted')"
  if exists(db):condition+=' AND NOT EXISTS(SELECT 1 FROM task_acknowledgements a WHERE a.job=jobs.id AND a.acknowledged=1)'
  count=db.execute('SELECT COUNT(*) FROM jobs WHERE '+condition).fetchone()[0]
  items=[dict(row) for row in db.execute('SELECT id,operation,status,created FROM jobs WHERE '+condition+' ORDER BY created DESC LIMIT 8')]
 return items,count
def acknowledge(ops,actor,payload):
 if actor['role'] not in ('superadmin','operator'):reject('只读管理员不能处理维护提醒。')
 if not isinstance(payload,dict) or set(payload)!={'job','acknowledged','note','expected_updated'}:reject('核对参数无效。')
 job=payload['job'];note=payload['note']
 if not isinstance(job,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',job):reject('任务编号无效。')
 if type(payload['acknowledged']) is not bool or not isinstance(note,str) or len(note)>300 or any(ord(c)<32 and c not in '\n\t' for c in note):reject('请输入 300 字以内的核对备注。')
 if payload['acknowledged'] and not note.strip():reject('请填写核对结果，再标记已核对。')
 from admin_ops import connect
 with ops.lock,closing(connect(ops.database)) as db,db:
  row=db.execute('SELECT status,updated,agents FROM jobs WHERE id=?',(job,)).fetchone()
  if not row or row['status'] not in ('failed','interrupted'):reject('仅失败或中断任务需要人工核对。')
  if row['updated']!=payload['expected_updated']:reject('任务结果已变化，请重新读取后核对。')
  db.execute('CREATE TABLE IF NOT EXISTS task_acknowledgements(job TEXT PRIMARY KEY,acknowledged INTEGER NOT NULL,actor TEXT NOT NULL,note TEXT NOT NULL,updated REAL NOT NULL)')
  now=time.time();db.execute('INSERT OR REPLACE INTO task_acknowledgements VALUES(?,?,?,?,?)',(job,int(payload['acknowledged']),actor['username'],note.strip(),now))
  result='标记已核对' if payload['acknowledged'] else '恢复待核对'
  db.execute('INSERT INTO audit(actor,operation,agents,result,created) VALUES(?,?,?,?,?)',(actor['username'],'task_attention',row['agents'],result+' · 任务 '+job+' · '+note.strip(),now))
 return {'ok':True}
