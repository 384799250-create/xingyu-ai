"""Read-only operations reporting. Never imports payment clients or opens writable ledgers."""
import wallet_closure
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
from decimal import Decimal
import ast,hashlib,hmac,json,re,sqlite3,time
from pathlib import Path

CORP='ww0000000000000002'
ZONE=timezone(timedelta(hours=8));PAGE=25
SCHEMA=1

def reject(message):
    from admin_ops import Rejected
    raise Rejected(message)

@contextmanager
def readonly(path):
    path=Path(path)
    if not path.is_file() or path.is_symlink():reject('资料暂不可读，请重新读取。')
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=5)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
    try:yield db
    finally:db.close()

def rows(path,sql,args=()):
    with readonly(path) as db:return [dict(r) for r in db.execute(sql,args)]

def scope(ops):
    root=Path(ops.root)
    if root.name==CORP and root.parent.name=='enterprises':return root,root.parent.parent,CORP
    return root,root,'legacy'

def money(value,unit=1_000_000):
    return None if value is None else format(Decimal(int(value))/Decimal(unit),'f')

def args(query,allowed):
    if not isinstance(query,dict) or set(query)-set(allowed):reject('查询条件无效。')
    for key,value in query.items():
        if not isinstance(value,(str,int)) or len(str(value))>200:reject('查询条件过长或格式无效。')
    try:
        if isinstance(query.get('page'),bool):raise ValueError()
        page=int(query.get('page',1));assert 1<=page<=100000
    except (ValueError,TypeError,AssertionError):reject('页码无效。')
    return page

def dates(query):
    today=datetime.now(ZONE).date()
    try:
        start=datetime.strptime(query.get('start') or (today-timedelta(days=29)).isoformat(),'%Y-%m-%d').replace(tzinfo=ZONE)
        end=datetime.strptime(query.get('end') or today.isoformat(),'%Y-%m-%d').replace(tzinfo=ZONE)+timedelta(days=1)
        assert start<end and end-start<=timedelta(days=366)
    except (ValueError,TypeError,AssertionError,OverflowError):reject('请选择有效日期范围，最长一年。')
    return start,end

def company(root):
    # Fixed, explicit roots; never accept a filesystem path or enterprise ID from the client.
    return root.name if root.parent.name=='enterprises' and root.name in (CORP,'ww0000000000000003') else 'ww0000000000000001'

def channel_status(ops):
    root,main,key=scope(ops);out=[]
    for provider in ('wechat','alipay'):
        item={'provider':provider,'enabled':None,'public_enabled':None,'amounts_fen':[],'service':'unknown','readable':False}
        p=main/'admin/private'/provider/'merchant.json'
        try:
            config=json.loads(p.read_text('utf8'))
            item.update(enabled=config.get('enabled') is True,public_enabled=config.get('public_enabled') is True,
                        amounts_fen=[n for n in config.get('allowed_fen',[]) if type(n) is int and 0<n<=1000000],readable=True)
        except (OSError,ValueError,TypeError):pass
        try:item['service']='active' if ops.run(['systemctl','is-active','xingyu-'+provider+'.service'],5).strip()=='active' else 'inactive'
        except Exception:item['service']='unavailable'
        if provider=='wechat':
            try:item['jsapi_enabled']=json.loads((main/'admin/private/wechat/jsapi.json').read_text('utf8')).get('enabled') is True
            except (OSError,ValueError):item['jsapi_enabled']=None
        out.append(item)
    return out

def version(path):
    source=path.read_bytes();tree=ast.parse(source)
    value=next((ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='VERSION' for t in n.targets)),None)
    return value,hashlib.sha256(source).hexdigest()[:12]

def inventory(ops,source_details=True):
    root,main,key=scope(ops);result=ops.overview();items=result['items']
    for item in items:
        try:
            runtime,_,_=ops.resolve(item['agent'])
            if source_details:item['source_version'],item['source_hash']=version(runtime/'suite_app.py')
        except (OSError,ValueError,SyntaxError):item['source_version']=None;item['source_hash']=None
        item['independent']=False
    pilot=root/'1000002'
    if key==CORP and (pilot/'runtime/suite_app.py').is_file():
        item={'agent':1000002,'name':'星屿 · 本人试点','independent':True,'pool_state':'independent','running':False,'healthy':False,'ai_enabled':False,'queue':{},'error_count':0,'version':None,'owner_display':'独立试点','owner_account':'','owner':''}
        try:
            info=json.loads(ops.run(['docker','inspect','xingyu-'+CORP+'-1000002'],8))[0]
            item.update(running=bool(info['State']['Running']),healthy=info['State'].get('Health',{}).get('Status')=='healthy')
            values=dict(v.split('=',1) for v in info['Config']['Env'] if '=' in v)
            item['ai_enabled']=values.get('ENABLE_AI','false').lower()=='true';item['model']=values.get('DEEPSEEK_MODEL','')
            if source_details:item['source_version'],item['source_hash']=version(pilot/'runtime/suite_app.py')
        except Exception:item['unavailable']=True
        items.append(item)
    result.update(items=items,enterprise=key,report_version=SCHEMA)
    return result

def onboarding_progress(ops,root,item,account,app,identity_key):
    """Presentation only; an inbound message does not bind a website identity."""
    item.update(progress_state='usage_unknown',progress_label='使用情况待核对',
                usage_state='unknown',usage_label='暂不可读',last_inbound=None,
                account_binding='unavailable',account_binding_label='暂不可核对')
    item['next_step']='应用已开通，使用记录暂不可读，请稍后重新读取。'
    if app is None:
        return
    if (app.get('state')!='assigned' or app.get('corp')!=company(root) or
        app.get('owner')!=item.get('owner') or app.get('request_id')!=item.get('request')):
        item.update(progress_state='assignment_changed',progress_label='应用归属待核对',usage_label='未核对',
                    next_step='当前应用归属与原申请不一致，请核对分配记录。')
        return
    if account is None:
        item.update(account_binding='account_missing',account_binding_label='网站账号已删除')
    elif not account.get('subject'):
        item.update(account_binding='pending_confirmation',account_binding_label='待确认')
    elif identity_key:
        expected=hmac.new(identity_key,json.dumps([app['corp'],app['owner']],separators=(',',':')).encode(),hashlib.sha256).hexdigest()
        confirmed=hmac.compare_digest(str(account['subject']),expected)
        item.update(account_binding='confirmed' if confirmed else 'conflict',
                    account_binding_label='已确认' if confirmed else '关联需核对')
    try:
        with readonly(ops.data_path(item['agent'])) as db:
            values=(item['agent'],item['owner'],item['created'])
            last=db.execute("SELECT MAX(created) FROM (SELECT created FROM member_inbox WHERE agent=? AND user=? AND created>=? UNION ALL SELECT created FROM suite_messages WHERE agent=? AND user=? AND created>=? AND role='user')",values+values).fetchone()[0]
        used=last is not None
        item.update(progress_state='in_use' if used else 'pending_first_chat',
                    progress_label='已在使用' if used else '已开通 · 待首次聊天',
                    usage_state='in_use' if used else 'pending_first_chat',
                    usage_label='已收到用户消息' if used else '尚无消息记录',last_inbound=last)
        item['next_step']='用户已开始在微信使用。' if used else '应用已开通，可在微信向自己的 AI 发送第一条消息。'
    except Exception:
        # Do not turn unreadable records into "never used", or expose provider/path errors.
        pass
    if item['account_binding'] in ('conflict','account_missing'):
        item['next_step']+=' 请管理员核对网站账号关联。'


def onboarding(ops,query):
    page=args(query,('q','state','page'));root,main,key=scope(ops)
    accounts={str(r['id']):r for r in rows(main/'portal/data/portal.sqlite3','SELECT id,username,subject FROM accounts')}
    data=rows(root/'onboarding/jobs.sqlite3','SELECT account,request,owner,agent,state,stage,error,created,updated,display_name FROM jobs ORDER BY updated DESC LIMIT 10001')
    if len(data)>10000:reject('申请记录过多，请联系维护人员归档。')
    known={'queued','provisioning','verifying','awaiting_scan','ready','needs_inspection','waiting_pool','failed','cancelled','reclaimed',
           'opened','in_use','pending_first_chat','usage_unknown','assignment_changed'}
    selected=query.get('state','')
    if selected and selected not in known:reject('申请状态无效。')
    term=str(query.get('q','')).strip().casefold();filtered=[];registry=None;identity_key=None
    if any(item['state'] in ('awaiting_scan','ready') for item in data):
        try:registry={r['agent']:r for r in rows(root/'app-pool/registry.sqlite3','SELECT agent,corp,state,owner,request_id FROM apps')}
        except Exception:pass
        try:identity_key=(root/'app-pool/portal-identity.key').read_bytes()
        except OSError:pass
    reasons={'application_reclaimed':'应用已回收，原申请记录保留','member_create_unknown':'成员创建结果不明，请先核对通讯录','pool_empty':'暂时没有可分配应用','runtime_hash_mismatch':'应用版本校验未通过','callback_failed':'回调检查未通过','interrupted_no_automatic_retry':'开通过程中断，需核对已经完成的步骤','slot_department_members_mismatch':'应用部门成员与预期不一致，需核对通讯录'}
    counts={};progress_counts={}
    for item in data:
        if item['stage']=='released' and item['error']=='application_reclaimed':item['state']='reclaimed'
        account=accounts.get(str(item['account']))
        item['username']=account['username'] if account else '已删除账号';item['enterprise']=key
        raw=item.pop('error') or ''
        item['issue']=reasons.get(raw,'开通未完成，需要核对原执行步骤' if raw else '')
        item['stage']=item['stage'] if re.fullmatch(r'[a-zA-Z0-9_-]{0,60}',item['stage'] or '') else 'inspection'
        item['next_step']='先确认原执行结果，再处理当前申请；不要重复创建成员。' if item['state'] in ('needs_inspection','failed') else '查看当前分配状态。'
        opened=item['state'] in ('awaiting_scan','ready')
        if opened:onboarding_progress(ops,root,item,account,None if registry is None else registry.get(item['agent'],{}),identity_key)
        counts[item['state']]=counts.get(item['state'],0)+1
        progress=item.get('progress_state',item['state']);progress_counts[progress]=progress_counts.get(progress,0)+1
        item.pop('owner');item.pop('request')
        if selected:
            if selected=='opened' and not opened:continue
            if selected!='opened' and selected not in (item['state'],progress):continue
        if term and term not in (' '.join(str(item.get(k) or '') for k in ('username','agent','display_name'))).casefold():continue
        filtered.append(item)
    return {'items':filtered[(page-1)*PAGE:page*PAGE],'count':len(filtered),'page':page,'page_size':PAGE,'counts':counts,'progress_counts':progress_counts,'enterprise':key}


ORDER_SQL='''
SELECT o.id,o.corp,o.user,o.provider,o.state,o.fen,o.checkout_mode,o.created,o.updated,o.expires,
 o.base_starcoins,o.bonus_starcoins,o.pricing_version,
 p.id AS payment_entry,p.amount AS received_amount,p.created AS received_at,p.wallet,p.entry,
 e.amount AS entry_amount,e.kind AS entry_kind,e.wallet AS entry_wallet,
 w.corp AS wallet_corp,w.user AS wallet_user,
 CASE WHEN o.state='paid' AND p.id IS NULL THEN 'missing_credit'
 WHEN o.state!='paid' AND p.id IS NOT NULL THEN 'unexpected_credit'
 WHEN p.id IS NOT NULL AND (e.id IS NULL OR w.id IS NULL OR e.kind!='credit'
   OR p.corp!=o.corp OR p.user!=o.user OR w.corp!=o.corp OR w.user!=o.user
   OR p.wallet!=e.wallet OR p.transaction_id IS NOT o.transaction_id
   OR p.amount!=e.amount OR p.amount!=(CASE WHEN o.base_starcoins=0 AND o.pricing_version='legacy-linear-v1' THEN o.fen*10 ELSE o.base_starcoins END+COALESCE(o.bonus_starcoins,0))*1000000)
 THEN 'mismatch' ELSE '' END AS issue
FROM pay_orders o LEFT JOIN pay_entries p ON p.order_id=o.id
LEFT JOIN entries e ON e.id=p.entry LEFT JOIN wallets w ON w.id=p.wallet
'''

def order_filters(query):
    start,end=dates(query);where='o.created>=? AND o.created<?';values=[start.timestamp(),end.timestamp()]
    provider=query.get('provider','');state=query.get('state','');q=str(query.get('q','')).strip()
    if provider and provider not in ('wechat','alipay'):reject('支付渠道无效。')
    if state and state not in ('paid','closed','pending','expired','attention'):reject('订单状态无效。')
    if provider:where+=' AND o.provider=?';values.append(provider)
    if state in ('paid','closed'):where+=' AND o.state=?';values.append(state)
    if state=='pending':where+=" AND o.state NOT IN ('paid','closed') AND o.expires>?";values.append(time.time())
    if state=='expired':where+=" AND o.state NOT IN ('paid','closed') AND o.expires<=?";values.append(time.time())
    if q:
        # Substring search with escaped wildcards, bound parameters only.
        escaped=q.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
        where+=" AND (o.id LIKE ? ESCAPE '\\' OR o.user LIKE ? ESCAPE '\\')";values.extend(['%'+escaped+'%']*2)
    return start,end,where,values

def payments(ops,query):
    page=args(query,('start','end','provider','state','q','user','page'));root,main,key=scope(ops)
    start,end,where,values=order_filters(query)
    where+=' AND o.corp=?';values.append(company(root))
    if query.get('user'):where+=' AND o.user=?';values.append(query['user'])
    with readonly(root/'admin/data/star-wallet.sqlite3') as db:
        sql='SELECT * FROM ('+ORDER_SQL+' WHERE '+where+')'
        if query.get('state')=='attention':sql+=" WHERE issue!=''"
        count=db.execute('SELECT COUNT(*) FROM ('+sql+')',values).fetchone()[0]
        totals=dict(db.execute("SELECT COUNT(*) AS orders,COALESCE(SUM(CASE WHEN state='paid' THEN fen ELSE 0 END),0) AS paid_fen,COALESCE(SUM(issue!=''),0) AS issues FROM ("+sql+')',values).fetchone())
        items=[]
        for row in db.execute(sql+' ORDER BY created DESC,id DESC LIMIT ? OFFSET ?',values+[PAGE,(page-1)*PAGE]):
            r=dict(row);r['received_coins']=money(r.pop('received_amount'));r['enterprise']=key
            if r['base_starcoins']==0 and r['pricing_version']=='legacy-linear-v1':r['base_starcoins']=r['fen']*10
            r['display_state']='expired' if r['state'] not in ('paid','closed') and r['expires']<=time.time() else r['state']
            r['reconciled']=r['state']=='paid' and not r['issue']
            for name in ('entry_amount','entry_kind','entry_wallet','wallet_corp','wallet_user'):r.pop(name,None)
            items.append(r)
        orphan=db.execute('SELECT COUNT(*) FROM pay_entries p LEFT JOIN pay_orders o ON o.id=p.order_id WHERE o.id IS NULL AND p.corp=?',(company(root),)).fetchone()[0]
    return {'items':items,'count':count,'page':page,'page_size':PAGE,'totals':totals,'orphan_entries':orphan,'channels':channel_status(ops),'enterprise':key,'start':start.date().isoformat(),'end':(end-timedelta(days=1)).date().isoformat(),'checked_at':time.time(),'basis':'order_created','provider_query_performed':False}


def wallet_identity_context(ops):
    # Presentation only. Never infer ownership from a similar name or a pending job.
    from admin_ops import Rejected
    root,main,enterprise=scope(ops);accounts=[];names={};manual={};pending={};identity_key=None
    try:accounts=rows(main/'portal/data/portal.sqlite3','SELECT id,username,subject FROM accounts')
    except (OSError,sqlite3.Error,ValueError,Rejected):pass
    subjects={}
    for account in accounts:
        if account['subject']:subjects.setdefault(account['subject'],[]).append(account['username'])
    try:identity_key=(root/'app-pool/portal-identity.key').read_bytes()
    except OSError:pass
    try:
        for item in ops.pool().values():
            if item.get('corp')==company(root) and item.get('owner') and isinstance(item.get('owner_name'),str) and item['owner_name'].strip():
                names.setdefault(item['owner'],set()).add(item['owner_name'].strip())
            if item.get('corp')==company(root) and item.get('owner') and item.get('state')=='assigned' and item.get('request_id') and item.get('owner_account'):
                matches=[a for a in accounts if a['username']==item['owner_account'] and not a['subject']]
                if len(matches)==1:pending.setdefault(item['owner'],set()).add(matches[0]['username'])
    except (OSError,sqlite3.Error,ValueError,Rejected):pass
    if enterprise=='legacy':
        try:
            binding=json.loads((root/'admin/data/application-bindings.json').read_text('utf8')).get('1000003',{})
            reference=binding.get('website_account') or {}
            if binding.get('agent')==1000003 and binding.get('owner'):
                matches=[a for a in accounts if str(a['id'])==str(reference.get('id')) and a['username']==reference.get('username') and not a['subject']]
                if len(matches)==1:manual[binding['owner']]=matches[0]['username']
        except (OSError,ValueError,TypeError,AttributeError):pass
    return {'corp':company(root),'key':identity_key,'subjects':subjects,'names':names,'manual':manual,'pending':pending}


def wallet_identity(context,user):
    candidates=context['names'].get(user,set());member_name=next(iter(candidates)) if len(candidates)==1 else ''
    account='';binding=''
    if context['key']:
        subject=hmac.new(context['key'],json.dumps([context['corp'],user],separators=(',',':')).encode(),hashlib.sha256).hexdigest()
        matches=context['subjects'].get(subject,[])
        if len(matches)==1:account=matches[0];binding='confirmed'
    if not account and context['manual'].get(user):account=context['manual'][user];binding='manual_dedicated'
    pending=context['pending'].get(user,set())
    if not account and len(pending)==1:account=next(iter(pending));binding='pending_confirmation'
    return {'member_name':member_name,'website_username':account,'website_binding':binding,'display_name':member_name or account or user}


def wallets(ops,query):
    page=args(query,('q','page','state'));root,main,key=scope(ops)
    status=query.get('state','active') or 'active'
    if status not in ('active','closed','all'):reject('账户状态无效。')
    term=str(query.get('q','')).strip().casefold();context=wallet_identity_context(ops)
    where='corp=?';values=(company(root),)
    with readonly(root/'admin/data/star-wallet.sqlite3') as db:
        import wallet_closure
        active=wallet_closure.active_sql(db)
        if status!='all':where+=' AND '+(active if status=='active' else 'NOT ('+active+')')
        if term:
            candidates=db.execute('SELECT id,corp,user,balance FROM wallets WHERE '+where+' ORDER BY created DESC,id',values)
            matching=[r['id'] for r in candidates if any(term in value.casefold() for value in (r['user'],r['id'],(wallet_closure.state(db,r['corp'],r['user']) or {}).get('username',''),*(v for k,v in wallet_identity(context,r['user']).items() if k!='website_binding')))]
            count=len(matching);selected=matching[(page-1)*PAGE:page*PAGE]
            if selected:result=db.execute('SELECT id,corp,user,balance,created FROM wallets WHERE corp=? AND id IN ('+','.join('?' for _ in selected)+') ORDER BY created DESC,id',values+tuple(selected))
            else:result=[]
        else:
            count=db.execute('SELECT COUNT(*) FROM wallets WHERE '+where,values).fetchone()[0]
            result=db.execute('SELECT id,corp,user,balance,created FROM wallets WHERE '+where+' ORDER BY created DESC,id LIMIT ? OFFSET ?',values+(PAGE,(page-1)*PAGE))
        items=[]
        for r in result:
            w=dict(r);w['reserved']=db.execute('SELECT COALESCE(SUM(held),0) FROM auto_requests WHERE wallet=? AND expires>?',(w['id'],time.time())).fetchone()[0]
            w['available']=w['balance']-w['reserved']
            wallet_closure.decorate(db,w)
            for k in ('balance','reserved','available'):w[k]=money(w[k])
            w.update(wallet_identity(context,w['user']));items.append(w)
    return {'items':items,'count':count,'page':page,'page_size':PAGE,'enterprise':key,'state':status}

def wallet_bill_receipt(ops,user):
    # Reuse the settings page's receipt lookup without constructing its engine:
    # Engine.__init__/balance can initialize accounts, gifts or settlement.
    import star_billing
    from types import SimpleNamespace
    source=star_billing.StarOps(ops)
    cache={}
    def path(agent):
        if agent not in cache:cache[agent]=source.data_path(agent).with_name('token-usage.sqlite3')
        return cache[agent]
    reader=SimpleNamespace(path=path)
    def receipt(agent,turn):
        from admin_ops import Rejected
        try:
            value=star_billing.BaseEngine.receipt(reader,agent,{'billing_turn':turn,'user':user})
            return value['state'] if value else None
        except (OSError,sqlite3.Error,ValueError,Rejected):return None
    return receipt


def wallet_profile(ops,query):
    page=args(query,('wallet','user','page','view','group','recharge_page'));root,main,key=scope(ops)
    view=query.get('view','ledger')
    if view not in ('ledger','records') or ('group' in query and view!='records'):reject('账务视图无效。')
    recharge_page=args({'page':query.get('recharge_page',1)},('page',))
    records={}
    ident=str(query.get('wallet',''));user=str(query.get('user',''))
    if not ident and not user:reject('请选择成员或星币账户。')
    with readonly(root/'admin/data/star-wallet.sqlite3') as db:
        w=db.execute('SELECT id,corp,user,balance,created FROM wallets WHERE '+('id=?' if ident else 'user=?')+' AND corp=?',(ident or user,company(root))).fetchone()
        if not w:reject('当前企业没有此星币账户。')
        w=dict(w);ident=w['id']
        if view=='records':
            import turn_billing,star_billing,star_wallet
            receipt=wallet_bill_receipt(ops,w['user'])
            if 'group' in query:
                try:return dict(star_wallet.public(turn_billing.detail(db,ident,query['group'],page,receipt)),wallet_id=ident,enterprise=key)
                except ValueError:reject('此账户没有这条用量记录，请重新读取。')
            records=turn_billing.overview(db,ident,page,receipt)
            records['usage_summary']=star_billing.usage_summary(db,ident)
            records=star_wallet.public(records)
            credits="wallet=? AND kind IN ('credit','gift','daily_gift') AND amount>0"
            recharge_count=db.execute('SELECT COUNT(*) FROM entries WHERE '+credits,(ident,)).fetchone()[0]
            recharges=[dict(r) for r in db.execute('SELECT id,kind,amount,created FROM entries WHERE '+credits+' ORDER BY id DESC LIMIT 20 OFFSET ?',(ident,(recharge_page-1)*20))]
            for item in recharges:item['amount']=money(item['amount'])
            records.update(recharges=recharges,recharge_count=recharge_count,recharge_page=recharge_page,recharge_page_size=20)
        totals=dict(db.execute("SELECT COALESCE(SUM(CASE WHEN kind='credit' THEN amount ELSE 0 END),0) AS recharged,COALESCE(SUM(CASE WHEN kind IN ('gift','daily_gift') THEN amount ELSE 0 END),0) AS gifted,COALESCE(-SUM(CASE WHEN kind='debit' THEN amount ELSE 0 END),0) AS consumed,COALESCE(SUM(amount),0) AS ledger_sum FROM entries WHERE wallet=?",(ident,)).fetchone())
        held=db.execute('SELECT COALESCE(SUM(held),0) FROM auto_requests WHERE wallet=? AND expires>?',(ident,time.time())).fetchone()[0]
        count=db.execute('SELECT COUNT(*) FROM entries WHERE wallet=?',(ident,)).fetchone()[0]
        items=[dict(r) for r in db.execute('SELECT e.id,e.kind,e.amount,e.balance,e.created,v.agent,v.kind AS resource,v.request_id,p.order_id FROM entries e LEFT JOIN events v ON v.id=e.event LEFT JOIN pay_entries p ON p.entry=e.id WHERE e.wallet=? ORDER BY e.id DESC LIMIT ? OFFSET ?',(ident,PAGE,(page-1)*PAGE))]
        event_counts=dict(db.execute('SELECT state,COUNT(*) FROM events WHERE wallet=? GROUP BY state',(ident,)).fetchall())
        order_count=db.execute('SELECT COUNT(*) FROM pay_orders WHERE corp=? AND user=?',(w['corp'],w['user'])).fetchone()[0]
        w.update(reserved=held,available=w['balance']-held,consistent=totals.pop('ledger_sum')==w['balance'],**totals)
        import wallet_closure
        wallet_closure.decorate(db,w)
        for name in ('balance','reserved','available','recharged','gifted','consumed'):w[name]=money(w[name])
        for r in items:
            r['amount']=money(r['amount']);r['balance']=money(r['balance'])
    w.update(wallet_identity(wallet_identity_context(ops),w['user']))
    return dict(wallet=w,items=items if view=='ledger' else [],count=count if view=='ledger' else records['bill_count'],page=page,page_size=PAGE if view=='ledger' else records['bill_page_size'],event_counts=event_counts,order_count=order_count,enterprise=key,**records)

def business(ops,query):
    args(query,('start','end'));root,main,key=scope(ops);start,end=dates(query);period=[start.timestamp(),end.timestamp()]
    with readonly(root/'admin/data/star-wallet.sqlite3') as db:
        channels=[]
        for row in db.execute("SELECT o.provider,COUNT(*) AS orders,SUM(o.fen) AS paid_fen,SUM(CASE WHEN o.base_starcoins=0 AND o.pricing_version='legacy-linear-v1' THEN o.fen*10 ELSE o.base_starcoins END) AS principal_coins,SUM(o.bonus_starcoins) AS bonus_coins,SUM(p.amount) AS credited FROM pay_entries p JOIN pay_orders o ON o.id=p.order_id WHERE p.created>=? AND p.created<? GROUP BY o.provider",period):
            r=dict(row);r['credited']=money(r['credited']);channels.append(r)
        ledger=dict(db.execute("SELECT COALESCE(-SUM(CASE WHEN kind='debit' THEN amount ELSE 0 END),0) AS consumed,COALESCE(SUM(CASE WHEN kind IN ('gift','daily_gift') THEN amount ELSE 0 END),0) AS opening_gifts,COUNT(DISTINCT CASE WHEN kind='debit' THEN wallet END) AS paying_users FROM entries WHERE created>=? AND created<?",period).fetchone())
        ledger['consumed']=money(ledger['consumed']);ledger['opening_gifts']=money(ledger['opening_gifts'])
        daily=[dict(r) for r in db.execute("SELECT date(p.created,'unixepoch','+8 hours') AS day,COUNT(*) AS orders,SUM(o.fen) AS paid_fen,SUM(p.amount) AS credited FROM pay_entries p JOIN pay_orders o ON o.id=p.order_id WHERE p.created>=? AND p.created<? GROUP BY day ORDER BY day DESC",period)]
        for r in daily:r['credited']=money(r['credited'])
    # Use existing metering rules for a separate cost estimate. It must not be
    # presented as a cash margin or subtracted from top-ups to claim profit.
    cost={'cost':None,'cost_unknown':0,'unavailable':[],'readable':False}
    try:
        import shadow_pricing,star_billing
        adapter=star_billing.StarOps(ops)
        with readonly(root/'admin/data/shadow-pricing.sqlite3') as db:
            prices=shadow_pricing.current_rows(db)
            revision=db.execute('SELECT COALESCE(MAX(id),0) FROM rate_versions').fetchone()[0]
        for price in prices:price['rates']=json.loads(price['rates']);price['enabled']=bool(price['enabled'])
        report=shadow_pricing.report(adapter,{'start':start.date().isoformat(),'end':(end-timedelta(days=1)).date().isoformat(),'group':'app'},configuration_data={'items':prices,'revision':revision})
        cost.update(cost=report['summary']['cost'],requests=report['summary']['requests'],cost_unknown=report['summary']['cost_unknown'],unavailable=report['unavailable'],missing_ledgers=report['missing_ledgers'],readable=True)
    except Exception:cost['readable']=False
    return {'channels':channels,'ledger':ledger,'daily':daily,'cost_estimate':cost,'start':start.date().isoformat(),'end':(end-timedelta(days=1)).date().isoformat(),'enterprise':key,'cash_basis':'verified_credit_time','cost_basis':'current_price_estimate','timestamp':time.time()}

def customer(ops,query):
    args(query,('account',));root,main,key=scope(ops)
    try:ident=int(query['account']);assert ident>0
    except (KeyError,ValueError,AssertionError):reject('账号无效。')
    # This action is routed to the centralized admin service like accounts.
    if key!='legacy':reject('请从成员与权限打开用户详情。')
    account=next((a for a in ops.accounts() if a['id']==ident),None)
    if not account:reject('网站账号不存在。')
    wallets=[]
    # Revalidate ownership from the account's current cryptographic subject.
    # Registry reallocation or pending display associations cannot expose another wallet.
    identity=rows(main/'portal/data/portal.sqlite3','SELECT username,subject FROM accounts WHERE id=?',(ident,))
    if not identity:reject('网站账号已变更，请重新读取。')
    subject=identity[0]['subject']
    for target,enterprise in ((root,'legacy'),(root/'enterprises'/CORP,CORP),(root/'enterprises'/'ww0000000000000003','ww0000000000000003')):
        if not (target/'admin/data/star-wallet.sqlite3').is_file():continue
        keypath=target/'app-pool/portal-identity.key'
        secret=keypath.read_bytes() if keypath.is_file() else None
        manual=None
        binding=target/'admin/data/application-bindings.json'
        if enterprise=='legacy' and binding.is_file() and not subject:
            b=json.loads(binding.read_text('utf8')).get('1000003',{});reference=b.get('website_account',{})
            if str(reference.get('id'))==str(ident) and reference.get('username')==identity[0]['username']:manual=b.get('owner')
        with readonly(target/'admin/data/star-wallet.sqlite3') as db:
            for raw in db.execute('SELECT id,corp,user,balance FROM wallets WHERE corp=?',(company(target),)):
                w=dict(raw)
                digest=hmac.new(secret,json.dumps([w['corp'],w['user']],separators=(',',':')).encode(),hashlib.sha256).hexdigest() if secret and subject else None
                if not ((subject and digest and hmac.compare_digest(subject,digest)) or (manual and manual==w['user'])):continue
                w['reserved']=db.execute('SELECT COALESCE(SUM(held),0) FROM auto_requests WHERE wallet=? AND expires>?',(w['id'],time.time())).fetchone()[0]
                w['available']=w['balance']-w['reserved']
                for k in ('balance','reserved','available'):w[k]=money(w[k])
                w['enterprise']=enterprise;wallets.append(w)
    jobs=[]
    for target,enterprise in ((root,'legacy'),(root/'enterprises'/CORP,CORP),(root/'enterprises'/'ww0000000000000003','ww0000000000000003')):
        if (target/'onboarding/jobs.sqlite3').is_file():
            for row in rows(target/'onboarding/jobs.sqlite3','SELECT agent,state,stage,created,updated FROM jobs WHERE account=? ORDER BY updated DESC LIMIT 20',(ident,)):
                row['enterprise']=enterprise
                if not re.fullmatch(r'[a-zA-Z0-9_-]{0,60}',row['stage'] or ''):row['stage']='inspection'
                jobs.append(row)
    return {'account':account,'wallets':wallets,'applications':account.get('applications',[]),'onboarding':jobs,'scope':'all_enterprises'}

def media_attention(ops):
    root,main,key=scope(ops)
    try:
        paths={agent:None for agent in ops.agents()}
        pilot=root/'1000002'
        if key==CORP and (pilot/'runtime/suite_app.py').is_file():
            paths[1000002]=pilot/'data/kf.sqlite3'
    except Exception:
        return {'open_count':None,'items':[],'readable':False}
    count=0;items=[];unreadable=[]
    for agent,path in sorted(paths.items()):
        try:
            with readonly(path if path is not None else ops.data_path(agent)) as db:
                if not db.execute("SELECT 1 FROM sqlite_master WHERE name='suite_media_alerts'").fetchone():continue
                count+=db.execute("SELECT COUNT(*) FROM suite_media_alerts WHERE agent=? AND state='open' AND updated>?",(agent,time.time()-30*86400)).fetchone()[0]
                items.extend(dict(r) for r in db.execute("SELECT agent,user_hash,msgid,kind,stage,reason,state,text_delivered,created,updated,occurrences FROM suite_media_alerts WHERE agent=? ORDER BY updated DESC LIMIT 20",(agent,)))
        except Exception:
            unreadable.append(agent)
    items.sort(key=lambda item:(item['updated'],item['agent'],item['msgid']),reverse=True)
    return {'open_count':None if unreadable else count,'items':items[:20],'readable':not unreadable,'unreadable_agents':unreadable}


def contact_attention(ops):
    """Metadata only: current gate and bounded outcomes, scoped to this enterprise."""
    root,main,key=scope(ops)
    try:
        paths={agent:None for agent in ops.agents()}
        pilot=root/'1000002'
        if key==CORP and (pilot/'runtime/suite_app.py').is_file():paths[1000002]=pilot/'data/kf.sqlite3'
    except Exception:return {'readable':False,'items':[],'events':[],'unreadable_agents':[]}
    items=[];events=[];unreadable=[]
    for agent,path in sorted(paths.items()):
        try:
            with readonly(path if path is not None else ops.data_path(agent)) as db:
                if not db.execute("SELECT 1 FROM sqlite_master WHERE name='suite_contact_status'").fetchone():continue
                items.extend(dict(r) for r in db.execute('SELECT agent,user_hash,persona,session_hash,reason,updated,next_check,job FROM suite_contact_status WHERE agent=? ORDER BY updated DESC LIMIT 20',(agent,)))
                events.extend(dict(r) for r in db.execute('SELECT agent,user_hash,persona,session_hash,reason,created,job FROM suite_contact_events WHERE agent=? ORDER BY created DESC,id DESC LIMIT 20',(agent,)))
        except Exception:unreadable.append(agent)
    items.sort(key=lambda r:(r['updated'],r['agent']),reverse=True);events.sort(key=lambda r:(r['created'],r['agent']),reverse=True)
    return {'readable':not unreadable,'items':items[:40],'events':events[:40],'unreadable_agents':unreadable}


def dashboard(ops,query):
    args(query,());root,main,key=scope(ops);apps=inventory(ops,source_details=False);opening=onboarding(ops,{'page':1})
    with readonly(root/'admin/data/star-wallet.sqlite3') as db:
        attention=db.execute("SELECT COUNT(*) FROM ("+ORDER_SQL+") WHERE issue!='' AND corp=?",(company(root),)).fetchone()[0]
        attention+=db.execute('SELECT COUNT(*) FROM pay_entries p LEFT JOIN pay_orders o ON o.id=p.order_id WHERE o.id IS NULL AND p.corp=?',(company(root),)).fetchone()[0]
        account_count=db.execute('SELECT COUNT(*) FROM wallets WHERE corp=? AND '+wallet_closure.active_sql(db),(company(root),)).fetchone()[0]
        waiting=dict(db.execute("SELECT state,COUNT(*) FROM events WHERE state IN ('waiting','insufficient') GROUP BY state").fetchall())
    import admin_queries
    failures,failure_count=admin_queries.attention(ops)
    return {'inventory':apps,'onboarding_counts':opening['counts'],'recent_applications':opening['items'][:5],'payment_attention':attention,'wallet_count':account_count,'billing_attention':waiting,'maintenance_attention':failures,'maintenance_attention_count':failure_count,'media_attention':media_attention(ops),'contact_attention':contact_attention(ops),'channels':channel_status(ops),'timestamp':time.time(),'enterprise':key}

def call(ops,action,payload):
    handlers={'ops_dashboard':dashboard,'ops_onboarding':onboarding,'ops_payments':payments,'ops_wallets':wallets,'ops_wallet':wallet_profile,'ops_business':business,'ops_customer':customer,'ops_inventory':lambda o,q: (args(q,()),inventory(o))[1]}
    if action not in handlers:reject('管理查询不存在。')
    try:return handlers[action](ops,payload)
    except (sqlite3.Error,OSError,ValueError,KeyError,TypeError):reject('资料暂不可读，请重新读取；持续失败时请联系维护人员。')
