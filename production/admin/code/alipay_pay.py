"""Alipay RSA2 computer website payment. No live calls until explicit configuration is ready."""
import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import re
from urllib.parse import parse_qsl, urlencode, urlsplit

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from alipay_errors import PaymentError

class GatewayError(PaymentError):
    def __init__(self,code,subcode):
        self.code=code;self.subcode=subcode
        super().__init__('支付宝尚未确认交易，请稍后查询原订单')

GATEWAY = 'https://openapi.alipay.com/gateway.do'
CHINA = timezone(timedelta(hours=8))


def https_url(value):
    if not isinstance(value,str):return False
    u=urlsplit(value)
    return u.scheme=='https' and bool(u.hostname) and not (u.username or u.password or u.query or u.fragment)


@dataclass(frozen=True)
class Config:
    appid: str
    seller_id: str | None
    notify_url: str | None
    private_key: object = field(repr=False)
    public_key: object = field(repr=False)
    enabled: bool = False
    product_confirmed: bool = False
    allowed_fen: tuple = (100,1000,3000,5000,10000)
    provider: str = field(default='alipay',init=False)

    @property
    def mchid(self):return self.seller_id or ''

    @property
    def checkout_enabled(self):
        return self.enabled and self.product_confirmed and bool(re.fullmatch(r'2088\d{12}',self.seller_id or '')) and https_url(self.notify_url)

    @classmethod
    def load(cls,path):
        try:
            path=Path(path).resolve();data=json.loads(path.read_text('utf-8-sig'))
            def asset(name):
                file=(path.parent/data[name]).resolve()
                if file.parent!=path.parent:raise ValueError('path')
                return file.read_bytes()
            private=serialization.load_pem_private_key(asset('private_key_file'),None)
            application=serialization.load_pem_public_key(asset('application_public_key_file'))
            public=serialization.load_pem_public_key(asset('alipay_public_key_file'))
            if not isinstance(private,rsa.RSAPrivateKey) or private.key_size<2048:raise ValueError('key')
            if not all(isinstance(k,rsa.RSAPublicKey) and k.key_size>=2048 for k in (public,application)):raise ValueError('key')
            if private.public_key().public_numbers()!=application.public_numbers() or public.public_numbers()==application.public_numbers():raise ValueError('pair')
            if not re.fullmatch(r'\d{16}',data['appid']):raise ValueError('appid')
            if data.get('gateway',GATEWAY)!=GATEWAY or data.get('sign_type')!='RSA2':raise ValueError('protocol')
            if data.get('product_code')!='FAST_INSTANT_TRADE_PAY':raise ValueError('product')
            if any(type(data.get(k,False)) is not bool for k in ('enabled','product_confirmed')):raise ValueError('flags')
            if data.get('seller_id') is not None and not re.fullmatch(r'2088\d{12}',data['seller_id']):raise ValueError('seller')
            if data.get('notify_url') is not None and not https_url(data['notify_url']):raise ValueError('notify')
            config=cls(data['appid'],data.get('seller_id'),data.get('notify_url'),private,public,data.get('enabled',False),data.get('product_confirmed',False))
            if config.enabled:config.ready()
            return config
        except (OSError,ValueError,KeyError,TypeError):
            raise PaymentError('支付宝配置或密钥资料无效') from None

    def ready(self):
        if not self.checkout_enabled:
            raise PaymentError('支付宝充值暂未开放，等待产品审核及收款配置完成')


def canonical(params):
    return '&'.join(k+'='+v for k,v in sorted(params.items()) if v!='')


def verify(public,message,signature):
    try:
        public.verify(base64.b64decode(signature,validate=True),message,padding.PKCS1v15(),hashes.SHA256())
    except Exception:
        raise PaymentError('支付宝签名验证失败') from None


def unique_pairs(pairs):
    value={}
    for k,v in pairs:
        if k in value:raise ValueError('duplicate field')
        value[k]=v
    return value


def signed_response(config,raw,method):
    """Verify the exact JSON object slice, never a reserialized object."""
    try:
        text=raw.decode('utf8');decoder=json.JSONDecoder(object_pairs_hook=unique_pairs)
        envelope=json.loads(text,object_pairs_hook=unique_pairs)
        key=method.replace('.','_')+'_response'
        if not isinstance(envelope,dict) or key not in envelope or not isinstance(envelope[key],dict):raise ValueError('envelope')
        pos=text.index('{')+1;segment=None
        while True:
            while text[pos].isspace():pos+=1
            name,pos=decoder.raw_decode(text,pos)
            while text[pos].isspace():pos+=1
            if text[pos]!=':':raise ValueError('separator')
            pos+=1
            while text[pos].isspace():pos+=1
            start=pos;_,pos=decoder.raw_decode(text,pos)
            if name==key:segment=text[start:pos].encode('utf8')
            while text[pos].isspace():pos+=1
            if text[pos]=='}':break
            if text[pos]!=',':raise ValueError('separator')
            pos+=1
        if segment is None:raise ValueError('payload')
        verify(config.public_key,segment,envelope.get('sign',''))
        result=envelope[key]
        if result.get('code')!='10000':raise GatewayError(result.get('code'),result.get('sub_code'))
        return result
    except PaymentError:raise
    except (ValueError,KeyError,TypeError,IndexError,UnicodeError):
        raise PaymentError('支付宝响应格式无效') from None


def fen(value):
    if not isinstance(value,str) or not re.fullmatch(r'\d{1,10}(?:\.\d{1,2})?',value):
        raise PaymentError('支付宝交易金额无效')
    whole,_,fraction=value.partition('.')
    return int(whole)*100+int(fraction.ljust(2,'0') or '0')


def transaction(config,value,query=False):
    """Query identity is bound to the signed request; notification identity must be explicit."""
    if not isinstance(value,dict):raise PaymentError('支付宝交易数据无效')
    if value.get('trans_currency','CNY')!='CNY':raise PaymentError('支付宝交易币种不匹配')
    if query:
        if value.get('app_id',config.appid)!=config.appid or value.get('seller_id',config.seller_id)!=config.seller_id:
            raise PaymentError('支付宝交易归属不匹配')
    elif value.get('app_id')!=config.appid or value.get('seller_id')!=config.seller_id:
        raise PaymentError('支付宝交易归属不匹配')
    state={'TRADE_SUCCESS':'SUCCESS','TRADE_FINISHED':'SUCCESS','TRADE_CLOSED':'CLOSED','WAIT_BUYER_PAY':'NOTPAY'}.get(value.get('trade_status'))
    if state is None:raise PaymentError('支付宝订单状态需要人工核对')
    result={'provider':'alipay','out_trade_no':value.get('out_trade_no'),'mchid':config.seller_id,'appid':config.appid,
        'trade_type':'PAGE','trade_state':state,'transaction_id':value.get('trade_no'),
        'amount':{'total':fen(value.get('total_amount')),'currency':'CNY'}}
    if state=='SUCCESS':
        try:
            stamp=value.get('send_pay_date') if query else value.get('gmt_payment')
            result['success_time']=datetime.strptime(stamp,'%Y-%m-%d %H:%M:%S').replace(tzinfo=CHINA).isoformat()
        except (ValueError,TypeError):raise PaymentError('支付宝支付成功时间无效') from None
    return result


def notification(config,raw):
    try:
        text=raw.decode('utf8')
        if re.search(r'%(?![0-9a-fA-F]{2})',text):raise ValueError('percent encoding')
        value=unique_pairs(parse_qsl(text,keep_blank_values=True,strict_parsing=True,encoding='utf8',errors='strict',max_num_fields=100))
        if value.get('sign_type')!='RSA2' or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',value.get('notify_id','')):raise ValueError('metadata')
        params={k:v for k,v in value.items() if k not in ('sign','sign_type')}
        verify(config.public_key,canonical(params).encode('utf8'),value.get('sign',''))
        return 'alipay:'+value['notify_id'],transaction(config,value)
    except PaymentError:raise
    except (ValueError,TypeError,UnicodeError):raise PaymentError('支付宝通知格式无效') from None


class Client:
    def __init__(self,config,session):self.config,self.session=config,session

    def params(self,method,biz,extra=None):
        self.config.ready()
        params={'app_id':self.config.appid,'method':method,'format':'JSON','charset':'utf-8','sign_type':'RSA2',
            'timestamp':datetime.now(CHINA).strftime('%Y-%m-%d %H:%M:%S'),'version':'1.0',
            'biz_content':json.dumps(biz,ensure_ascii=False,separators=(',',':'))}
        params.update(extra or {})
        params['sign']=base64.b64encode(self.config.private_key.sign(canonical(params).encode('utf8'),padding.PKCS1v15(),hashes.SHA256())).decode('ascii')
        return params

    def page(self,order):
        biz={'out_trade_no':order['id'],'product_code':'FAST_INSTANT_TRADE_PAY',
            'total_amount':f"{order['fen']//100}.{order['fen']%100:02d}",'subject':'星屿 AI 服务余额充值',
            'time_expire':datetime.fromtimestamp(order['expires'],CHINA).strftime('%Y-%m-%d %H:%M:%S')}
        # No return URL carries settings tokens; a browser redirect can never credit the wallet.
        return GATEWAY+'?'+urlencode(self.params('alipay.trade.page.pay',biz,{'notify_url':self.config.notify_url}))

    async def query(self,ident):
        params=self.params('alipay.trade.query',{'out_trade_no':ident})
        try:
            async with self.session.post(GATEWAY,data=urlencode(params).encode('ascii'),
                    headers={'Content-Type':'application/x-www-form-urlencoded; charset=utf-8'},
                    allow_redirects=False,timeout=aiohttp.ClientTimeout(total=15,connect=5)) as response:
                chunks=[];size=0
                async for chunk in response.content.iter_chunked(8192):
                    size+=len(chunk)
                    if size>65536:raise PaymentError('支付宝响应过大，请稍后查询')
                    chunks.append(chunk)
                if response.status!=200:raise PaymentError('支付宝响应未确认，请稍后查询原订单')
                return transaction(self.config,signed_response(self.config,b''.join(chunks),'alipay.trade.query'),query=True)
        except PaymentError:raise
        except (aiohttp.ClientError,TimeoutError,OSError,ValueError):raise PaymentError('支付宝响应未确认，请稍后查询原订单') from None
