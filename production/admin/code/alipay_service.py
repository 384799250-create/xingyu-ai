"""Alipay host service: root-only RPC, signed notifications and atomic starcoin credit."""
import argparse,asyncio,hashlib,json,logging,os,re,socket,sqlite3,struct,time
from contextlib import closing,contextmanager,AsyncExitStack
from pathlib import Path
from types import SimpleNamespace
import aiohttp
from aiohttp import web
from alipay_errors import PaymentError
from alipay_pay import Config,Client,GatewayError,notification
from recharge_rules import available, SUPPORTED_FEN, VERSION as PRICING_VERSION
from alipay_ledger import Ledger,owner
import star_wallet as wallet
from dataclasses import replace

VERSION='alipay-starcoin-v1'
LOG=logging.getLogger('xingyu-alipay')

def ro(path):
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=5)
    db.row_factory=sqlite3.Row
    return closing(db)

class Service:
    def __init__(self,root,config,session):
        self.root=Path(root);self.config_path=Path(config)
        self.metadata=json.loads(self.config_path.read_text('utf8'))
        self.client=Client(Config.load(config),session)
        amounts=self.metadata.get('allowed_fen',[100])
        if not amounts or any(type(n) is not int or n not in SUPPORTED_FEN for n in amounts):raise ValueError('invalid_amounts')
        self.client.config=replace(self.client.config,allowed_fen=tuple(amounts))
        self.scopes={};self.locks={}
        for scope in (self.root,self.root/'enterprises'/'ww0000000000000002',self.root/'enterprises'/'ww0000000000000003'):
            env=scope/'.env' if scope==self.root else scope/('apps/1000002/app.env' if scope.name=='ww0000000000000003' else '1000002/app.env')
            values=dict(l.split('=',1) for l in env.read_text().splitlines() if '=' in l and not l.startswith('#'))
            corp=values['WECOM_CORP_ID']
            ops=SimpleNamespace(root=scope,directory=scope/'admin/data')
            self.scopes[corp]=(ops,Ledger(ops));self.locks[corp]=asyncio.Lock()

    def enabled(self,corp,user):
        return self.client.config.checkout_enabled and (self.metadata.get('public_enabled') is True or
            any(x.get('corp')==corp and x.get('user')==user for x in self.metadata.get('pilot_members',[])))

    def frozen(self,corp,user):
        import wallet_closure
        if corp in self.scopes:
            with ro(self.scopes[corp][0].directory/'star-wallet.sqlite3') as db:
                if wallet_closure.state(db,corp,user):return True
        with ro(self.root/'admin/data/admin.sqlite3') as db:
            for row in db.execute('SELECT plan FROM account_deletions'):
                p=json.loads(row[0])
                if any(x.get('corp')==corp and x.get('owner')==user for x in p.get('members',[])+p.get('apps',[])):
                    return True
        return False

    def check_member(self,corp,user,agent):
        owner(corp,user)
        if corp not in self.scopes or type(agent) is not int:raise PaymentError('充值身份无效')
        ops,_=self.scopes[corp]
        if self.frozen(corp,user):raise PaymentError('账号已注销或正在注销，不能充值')
        import operating_fee
        with ro(ops.directory/'star-wallet.sqlite3') as db:
            if operating_fee.retained_allowed(db,corp,user,agent):return
        if ops.root==self.root and agent==1000003:
            binding=json.loads((self.root/'admin/data/application-bindings.json').read_text('utf8'))
            if binding.get(str(agent),{}).get('owner')!=user:raise PaymentError('应用归属已变化，请重新获取设置链接')
        elif agent==1000002 and ops.root!=self.root:
            if {'corp':corp,'user':user,'agent':agent} not in self.metadata.get('dedicated_members',[]):
                raise PaymentError('应用归属已变化，请重新获取设置链接')
        else:
            with ro(ops.root/'app-pool/registry.sqlite3') as db:
                r=db.execute('SELECT corp,owner,state FROM apps WHERE agent=?',(agent,)).fetchone()
            if not r or r['corp']!=corp or r['owner']!=user or r['state']!='assigned':raise PaymentError('应用归属已变化，请重新获取设置链接')

    @contextmanager
    def allocation_lock(self,corp):
        with (self.scopes[corp][0].root/'app-pool/operation.lock').open('a') as stream:
            if os.name!='nt':
                import fcntl
                try:fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:raise PaymentError('应用正在维护，请稍后重试') from None
            yield

    def find(self,ident):
        found=[]
        for corp,(ops,ledger) in self.scopes.items():
            try:found.append((corp,ledger,ledger.get(ident)))
            except PaymentError:pass
        if len(found)!=1:raise PaymentError('订单不存在或需要人工核对')
        return found[0]

    async def call(self,data):
        if not isinstance(data,dict) or set(data)!={'corp','user','agent','action','payload'}:raise PaymentError('充值请求无效')
        corp,user,agent,action,payload=(data[k] for k in ('corp','user','agent','action','payload'))
        self.check_member(corp,user,agent)
        if not isinstance(payload,dict):raise PaymentError('充值参数无效')
        ops,ledger=self.scopes[corp]
        if action=='status' and not payload:
            return {'enabled':self.enabled(corp,user),'provider':'alipay','exchange_rate':1000,
                'amounts_fen':list(self.client.config.allowed_fen) if self.enabled(corp,user) else [],
                'recharge_tiers':available(self.client.config.allowed_fen) if self.enabled(corp,user) else [],'pricing_version':PRICING_VERSION,
                'account_scope':hashlib.sha256((corp+'\0'+user).encode()).hexdigest()[:32],
                'pilot':not self.metadata.get('public_enabled',False)}
        if action=='create' and set(payload)=={'request_id','amount_fen'}:
            if not self.enabled(corp,user):raise PaymentError('支付宝充值维护中，暂未开放')
            self.client.config.ready()
            with self.allocation_lock(corp):
                self.check_member(corp,user,agent)
                with closing(wallet.connect(ops)) as db:
                    db.execute('BEGIN IMMEDIATE');wallet.account(db,corp,user);db.commit()
                row,fresh=ledger.new_order(corp,user,payload['request_id'],payload['amount_fen'],self.client.config)
                if fresh or (row['state']=='prepared' and not row['code_url']):
                    ledger.update(row['id'],'awaiting_payment',code_url=self.client.page(row))
                return ledger.public(ledger.get(row['id']))
        if action in ('get','query') and set(payload)=={'id'}:
            row=ledger.get(payload['id'],corp,user)
            if row['provider']!='alipay':raise PaymentError('订单支付渠道不匹配')
            if action=='query':await self.query(ledger,row)
            return ledger.public(ledger.get(row['id'],corp,user))
        if action=='history' and set(payload)=={'page'}:
            r=ledger.history(corp,user,payload['page'],'alipay')
            for item in r['orders']:item['checkout_url']=''
            return r
        raise PaymentError('充值请求字段无效')

    async def query(self,ledger,row):
        if row['state'] in ('paid','closed'):return
        if row['appid']!=self.client.config.appid or row['mchid']!=self.client.config.mchid:raise PaymentError('订单收款信息需要核对')
        if not ledger.claim_query(row['id']):return
        try:value=await self.client.query(row['id'])
        except GatewayError as exc:
            if exc.subcode=='ACQ.TRADE_NOT_EXIST':
                if row['expires']<time.time()-3600:ledger.update(row['id'],'closed')
                return
            raise
        ledger.validate_transaction(value,row['id'])
        if value['trade_state']=='SUCCESS':ledger.settle(value,expected=row['id'])
        elif value['trade_state']=='CLOSED':ledger.update(row['id'],'closed')

    async def notify(self,raw):
        event,value=notification(self.client.config,raw)
        corp,ledger,row=self.find(value['out_trade_no'])
        async with self.locks[corp]:
            if value['trade_state']=='SUCCESS':ledger.settle(value,event_id=event)
            else:
                ledger.validate_transaction(value)
                if value['trade_state']=='CLOSED':ledger.update(row['id'],'closed')

    async def recover(self):
        while True:
            for corp,(ops,ledger) in self.scopes.items():
                for ident in ledger.pending('alipay'):
                    row=ledger.get(ident)
                    if row['created']<time.time()-172800:continue
                    try:await self.query(ledger,row)
                    except Exception:LOG.warning('payment_reconcile_pending')
            await asyncio.sleep(30)

async def application(root,config,socket_path):
    session=aiohttp.ClientSession();service=Service(root,config,session)
    app=web.Application(client_max_size=65536)
    async def notify(request):
        if request.content_type!='application/x-www-form-urlencoded':return web.Response(text='failure',status=400)
        try:await service.notify(await request.read())
        except Exception:return web.Response(text='failure',status=400)
        return web.Response(text='success')
    async def health(request):
        return web.json_response({'ok':True,'version':VERSION,'public_enabled':service.metadata.get('public_enabled',False)})
    app.router.add_post('/ai/pay/alipay/notify',notify)
    app.router.add_get('/health',health)
    async def rpc(reader,writer):
        try:
            peer=writer.get_extra_info('socket').getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12)
            if struct.unpack('3i',peer)[1]!=0:raise PaymentError('请求身份无效')
            data=json.loads(await asyncio.wait_for(reader.readline(),3))
            result={'ok':True,'data':await service.call(data)}
        except PaymentError as exc:result={'ok':False,'error':str(exc),'kind':'declined'}
        except Exception:result={'ok':False,'error':'充值服务暂不可用，请稍后查询原订单'}
        try:writer.write(json.dumps(result).encode()+b'\n');await writer.drain()
        except (OSError,ConnectionError):pass
        finally:writer.close();await writer.wait_closed()
    async def lifecycle(app):
        p=Path(socket_path)
        if p.is_symlink() or (p.exists() and not p.is_socket()):raise RuntimeError('unsafe_payment_socket')
        if p.exists():p.unlink()
        server=await asyncio.start_unix_server(rpc,str(p),limit=8193);p.chmod(0o600)
        task=asyncio.create_task(service.recover())
        try:yield
        finally:
            task.cancel()
            try:await task
            except asyncio.CancelledError:pass
            server.close();await server.wait_closed();p.unlink(missing_ok=True);await session.close()
    app.cleanup_ctx.append(lifecycle)
    return app

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True);parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--socket',default='/run/xingyu-alipay/service.sock');parser.add_argument('--port',type=int,default=8790)
    args=parser.parse_args();logging.basicConfig(level=logging.WARNING)
    web.run_app(application(args.root,args.config,args.socket),host='172.18.0.1',port=args.port,print=None,access_log=None)
