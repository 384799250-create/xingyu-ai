"""WeChat Native payments into the existing star wallet, isolated from Alipay transport."""
import argparse,asyncio,base64,hashlib,io,json,logging,re,socket,struct,sys,time
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).parent/'vendor_wechat'))
import aiohttp
from aiohttp import web
from alipay_errors import PaymentError
from alipay_service import Service as IdentityService
from recharge_rules import available, SUPPORTED_FEN, VERSION as PRICING_VERSION
from alipay_ledger import Ledger
from wechat_pay import Config,Client,notification,GatewayError
import star_wallet as wallet
from wechat_oauth import OAuth
import wechat_jsapi

VERSION='wechat-starcoin-v1'
LOG=logging.getLogger('xingyu-wechat')

class Service(IdentityService):
    def __init__(self,root,config,session):
        self.root=Path(root);self.metadata=json.loads(Path(config).read_text('utf8'))
        amounts=self.metadata.get('allowed_fen',[100])
        if not amounts or any(type(n) is not int or n not in SUPPORTED_FEN for n in amounts):raise ValueError('invalid_amounts')
        self.client=Client(replace(Config.load(config),allowed_fen=tuple(amounts)),session)
        self.oauth=OAuth(Path(config).with_name('jsapi.json'),session)
        self.scopes={};self.locks={}
        for scope in (self.root,self.root/'enterprises'/'ww0000000000000002',self.root/'enterprises'/'ww0000000000000003'):
            env=scope/'.env' if scope==self.root else scope/('apps/1000002/app.env' if scope.name=='ww0000000000000003' else '1000002/app.env')
            values=dict(l.split('=',1) for l in env.read_text().splitlines() if '=' in l and not l.startswith('#'))
            corp=values['WECOM_CORP_ID'];ops=SimpleNamespace(root=scope,directory=scope/'admin/data')
            self.scopes[corp]=(ops,Ledger(ops));self.locks[corp]=asyncio.Lock()

    def enabled(self,corp,user):
        c=self.client.config
        return bool(c.enabled and c.appid_confirmed and (self.metadata.get('public_enabled') is True or
            any(x.get('corp')==corp and x.get('user')==user for x in self.metadata.get('pilot_members',[]))))

    def public(self,ledger,row,qr=True):
        p=ledger.public(row);p['qr_image']='';p['checkout_url']=''
        p['checkout_mode']=row['checkout_mode'];p['pay_available']=False
        if row['checkout_mode']=='JSAPI':
            p['code_url']='';p['pay_available']=row['state']=='awaiting_payment' and not p['qr_expired'];return p
        if qr and p['state']=='awaiting_payment' and not p['qr_expired'] and p['code_url']:
            import qrcode
            stream=io.BytesIO();qrcode.make(p['code_url'],box_size=7,border=4).save(stream,format='PNG')
            p['qr_image']=base64.b64encode(stream.getvalue()).decode('ascii')
        p['code_url']=''
        return p

    def history(self,ledger,corp,user,page):
        if type(page) is not int or not 1<=page<=100000:raise PaymentError('充值记录页码无效')
        with ledger.db() as db:
            db.execute('BEGIN')
            args=(corp,user,'wechat')
            count=db.execute('SELECT count(*) FROM pay_orders WHERE corp=? AND user=? AND provider=?',args).fetchone()[0]
            rows=db.execute('SELECT * FROM pay_orders WHERE corp=? AND user=? AND provider=? ORDER BY created DESC,id DESC LIMIT 10 OFFSET ?',args+((page-1)*10,)).fetchall()
        return {'orders':[self.public(ledger,dict(r),False) for r in rows],'page':page,'page_size':10,'count':count}

    async def call(self,data):
        if not isinstance(data,dict) or set(data)!={'corp','user','agent','action','payload'}:raise PaymentError('充值请求无效')
        corp,user,agent,action,payload=(data[k] for k in ('corp','user','agent','action','payload'))
        self.check_member(corp,user,agent)
        if not isinstance(payload,dict):raise PaymentError('充值参数无效')
        ops,ledger=self.scopes[corp]
        if action=='status' and not payload:
            enabled=self.enabled(corp,user)
            return {'enabled':enabled,'provider':'wechat','exchange_rate':1000,'amounts_fen':list(self.client.config.allowed_fen) if enabled else [],
                'recharge_tiers':available(self.client.config.allowed_fen) if self.enabled(corp,user) else [],'pricing_version':PRICING_VERSION,
                'account_scope':hashlib.sha256((corp+'\0'+user).encode()).hexdigest()[:32],
                'jsapi_enabled':bool(getattr(self,'oauth',None) and self.oauth.enabled),'jsapi_kind':self.oauth.config.get('kind','') if getattr(self,'oauth',None) else '',
                'pilot':not self.metadata.get('public_enabled',False),'message':'微信支付正在配置，暂未开放。' if not enabled else ''}
        if action in ('authorize','authorized','create_jsapi','pay'):return await wechat_jsapi.call(self,corp,user,agent,action,payload)
        if action=='create' and set(payload)=={'request_id','amount_fen'}:
            if not self.enabled(corp,user):raise PaymentError('微信支付正在配置，暂未开放')
            self.client.config.ready()
            with self.allocation_lock(corp):
                self.check_member(corp,user,agent)
                with closing(wallet.connect(ops)) as db:
                    db.execute('BEGIN IMMEDIATE');wallet.account(db,corp,user);db.commit()
                row,fresh=ledger.new_order(corp,user,payload['request_id'],payload['amount_fen'],self.client.config)
                if row['appid']!=self.client.config.appid or row['mchid']!=self.client.config.mchid:raise PaymentError('订单收款信息需要核对')
            # No allocation lock is held across a network await. Never repeat an ambiguous create.
            if fresh:
                ledger.update(row['id'],'creating')
                try:
                    result=await self.client.native(row);code=result.get('code_url')
                    if not isinstance(code,str) or not re.fullmatch(r'weixin://wxpay/bizpayurl\?[^\s\x00-\x1f]{1,2000}',code):raise PaymentError('支付二维码尚未确认')
                    ledger.update(row['id'],'awaiting_payment',code_url=code)
                except PaymentError:
                    ledger.update(row['id'],'query_required',error='下单结果待确认，请查询原订单，勿重复付款')
            return self.public(ledger,ledger.get(row['id']))
        if action in ('get','query') and set(payload)=={'id'}:
            row=ledger.get(payload['id'],corp,user)
            if row['provider']!='wechat':raise PaymentError('订单支付渠道不匹配')
            if action=='query':await self.query(ledger,row)
            return self.public(ledger,ledger.get(row['id'],corp,user))
        if action=='history' and set(payload)=={'page'}:return self.history(ledger,corp,user,payload['page'])
        raise PaymentError('充值请求字段无效')

    async def query(self,ledger,row):
        if row['provider']!='wechat':raise PaymentError('订单支付渠道不匹配')
        if row['state'] in ('paid','closed'):return
        if (row['checkout_mode']=='NATIVE' and row['appid']!=self.client.config.appid) or row['mchid']!=self.client.config.mchid:raise PaymentError('订单收款信息需要核对')
        if not ledger.claim_query(row['id']):return
        try:value=await self.client.query(row['id'])
        except GatewayError as exc:
            if exc.code=='ORDER_NOT_EXIST':
                if row['expires']<time.time()-3600:ledger.update(row['id'],'closed')
                return
            raise
        ledger.validate_transaction(value,row['id'],allow_unpaid=True)
        if value.get('trade_state')=='SUCCESS':ledger.settle(value,expected=row['id'])
        elif value.get('trade_state') in ('CLOSED','REVOKED','PAYERROR'):ledger.update(row['id'],'closed')
        elif value.get('trade_state') not in ('NOTPAY','USERPAYING'):raise PaymentError('微信支付订单状态需要核对')

    async def notify(self,headers,raw):
        event,value=notification(self.client.config,headers,raw)
        corp,ledger,row=self.find(value.get('out_trade_no'))
        if row['provider']!='wechat':raise PaymentError('订单支付渠道不匹配')
        async with self.locks[corp]:ledger.settle(value,event_id='wechat:'+event)

    async def recover(self):
        while True:
            if self.client.config.enabled and self.client.config.appid_confirmed:
                for corp,(ops,ledger) in self.scopes.items():
                    for ident in ledger.pending('wechat'):
                        row=ledger.get(ident)
                        if row['created']<time.time()-172800:continue
                        try:await self.query(ledger,row)
                        except Exception:LOG.warning('wechat_reconcile_pending')
            await asyncio.sleep(30)

async def application(root,config,socket_path):
    session=aiohttp.ClientSession()
    try:service=Service(root,config,session)
    except BaseException:await session.close();raise
    app=web.Application(client_max_size=65536)
    async def notify(request):
        try:
            raw=bytearray()
            async for chunk in request.content.iter_chunked(8192):
                raw.extend(chunk)
                if len(raw)>65536:raise PaymentError('支付通知过大')
            await service.notify(request.headers,bytes(raw))
        except Exception:return web.json_response({'code':'FAIL','message':'支付通知校验失败'},status=400)
        return web.Response(status=204)
    async def health(request):
        return web.json_response({'ok':True,'version':VERSION,'checkout_enabled':bool(service.client.config.enabled and service.client.config.appid_confirmed),'jsapi_enabled':service.oauth.enabled,'public_enabled':service.metadata.get('public_enabled',False)})
    async def oauth_return(request):
        headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'}
        try:
            if set(request.query)!={'code','state'}:raise PaymentError('微信授权未完成，请返回设置页重试')
            url=service.oauth.landing([ledger for ops,ledger in service.scopes.values()],request.query['state'],request.query['code'])
            return web.Response(status=303,headers={**headers,'Location':url})
        except PaymentError as exc:return web.Response(text=str(exc),status=400,headers=headers)
    app.router.add_get('/ai/pay/wechat/return',oauth_return)
    app.router.add_post('/ai/pay/wechat/notify',notify);app.router.add_get('/health',health)
    async def rpc(reader,writer):
        try:
            peer=writer.get_extra_info('socket').getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12)
            if struct.unpack('3i',peer)[1]!=0:raise PaymentError('请求身份无效')
            data=json.loads(await asyncio.wait_for(reader.readline(),3))
            result={'ok':True,'data':await service.call(data)}
        except PaymentError as exc:result={'ok':False,'error':str(exc),'kind':'declined'}
        except Exception:result={'ok':False,'error':'微信支付暂不可用，请稍后查询原订单'}
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
    app.cleanup_ctx.append(lifecycle);return app

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True);parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--socket',default='/run/xingyu-wechat/service.sock');parser.add_argument('--port',type=int,default=8791)
    args=parser.parse_args();logging.basicConfig(level=logging.WARNING)
    web.run_app(application(args.root,args.config,args.socket),host='172.18.0.1',port=args.port,print=None,access_log=None)
