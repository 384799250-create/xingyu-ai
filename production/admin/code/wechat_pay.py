"""WeChat Pay v3 Native transport. Never logs keys or provider response bodies."""
import base64
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlencode, quote, urlsplit

import aiohttp
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


from alipay_errors import PaymentError

class GatewayError(PaymentError):
    def __init__(self,code):
        self.code=code
        super().__init__('微信支付暂未确认，请查询原订单')


@dataclass(frozen=True)
class Config:
    mchid: str
    appid: str
    serial: str
    public_key_id: str
    notify_url: str
    private_key: object = field(repr=False)
    public_key: object = field(repr=False)
    api_v3_key: bytes = field(repr=False)
    enabled: bool = False
    appid_confirmed: bool = False
    allowed_fen: tuple = (100, 1000, 3000, 5000, 10000)

    @classmethod
    def load(cls, path):
        path = Path(path).resolve()
        data = json.loads(path.read_text('utf-8-sig'))
        def asset(name):
            p = (path.parent / data[name]).resolve()
            if p.parent != path.parent:
                raise PaymentError('支付资料路径无效')
            return p.read_bytes()
        key = serialization.load_pem_private_key(asset('private_key_file'), None)
        cert = x509.load_pem_x509_certificate(asset('certificate_file'))
        public = serialization.load_pem_public_key(asset('public_key_file'))
        if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
            raise PaymentError('商户私钥格式无效')
        if not isinstance(public, rsa.RSAPublicKey) or public.key_size < 2048:
            raise PaymentError('微信支付公钥格式无效')
        if key.public_key().public_numbers() != cert.public_key().public_numbers():
            raise PaymentError('商户证书与私钥不匹配')
        now = time.time()
        if not cert.not_valid_before_utc.timestamp() <= now <= cert.not_valid_after_utc.timestamp():
            raise PaymentError('商户证书不在有效期内')
        mchid = data['mchid']
        if not isinstance(mchid, str) or not re.fullmatch(r'\d{6,20}', mchid):
            raise PaymentError('商户号格式无效')
        if not any(mchid in a.value for a in cert.subject):
            raise PaymentError('商户证书归属与商户号不匹配')
        if not re.fullmatch(r'(wx|ww)[A-Za-z0-9]{8,32}', data['appid']):
            raise PaymentError('AppID 格式无效')
        if not re.fullmatch(r'PUB_KEY_ID_[A-Za-z0-9_]+', data['public_key_id']):
            raise PaymentError('微信支付公钥 ID 格式无效')
        secret = asset('api_v3_key_file').decode('ascii').strip().encode('ascii')
        if not re.fullmatch(b'[A-Za-z0-9]{32}', secret):
            raise PaymentError('API v3 密钥格式无效')
        url = urlsplit(data['notify_url'])
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.fragment or url.query:
            raise PaymentError('支付通知地址须为 HTTPS 地址')
        if type(data.get('enabled', False)) is not bool or type(data.get('appid_confirmed', False)) is not bool:
            raise PaymentError('支付启用标记无效')
        return cls(mchid, data['appid'], format(cert.serial_number, 'X'), data['public_key_id'],
                   data['notify_url'], key, public, secret,
                   data.get('enabled', False), data.get('appid_confirmed', False))

    def ready(self):
        if not self.enabled or not self.appid_confirmed:
            raise PaymentError('充值暂未开放，等待认证及 AppID 关联完成')


def verify(config, headers, body, now=None):
    """Validate exact raw bytes, pinned public key ID, and fresh provider timestamp."""
    try:
        h = {k.lower(): v for k, v in headers.items()}
        stamp, nonce = h['wechatpay-timestamp'], h['wechatpay-nonce']
        if not re.fullmatch(r'[0-9]{10,12}', stamp) or abs((time.time() if now is None else now) - int(stamp)) > 300:
            raise ValueError('timestamp')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', nonce) or h['wechatpay-serial'] != config.public_key_id:
            raise ValueError('key identity')
        signature = base64.b64decode(h['wechatpay-signature'], validate=True)
        message = stamp.encode() + b'\n' + nonce.encode() + b'\n' + body + b'\n'
        config.public_key.verify(signature, message, padding.PKCS1v15(), hashes.SHA256())
    except Exception:
        raise PaymentError('微信支付签名验证失败') from None


def notification(config, headers, raw):
    verify(config, headers, raw)
    try:
        envelope = json.loads(raw)
        resource = envelope['resource']
        if (envelope['event_type'] != 'TRANSACTION.SUCCESS' or envelope.get('resource_type') != 'encrypt-resource'
                or resource['algorithm'] != 'AEAD_AES_256_GCM' or resource.get('original_type') != 'transaction'
                or not isinstance(envelope['id'], str) or not 1 <= len(envelope['id']) <= 128):
            raise ValueError('unsupported notification')
        plaintext = AESGCM(config.api_v3_key).decrypt(resource['nonce'].encode(),
            base64.b64decode(resource['ciphertext'], validate=True), resource.get('associated_data', '').encode())
        transaction = json.loads(plaintext)
        if not isinstance(transaction, dict):
            raise ValueError('transaction')
        return envelope['id'], transaction
    except Exception:
        raise PaymentError('微信支付通知格式或解密失败') from None


class Client:
    def __init__(self, config, session):
        self.config = config
        self.session = session

    def authorization(self, method, target, raw):
        c = self.config
        stamp, nonce = str(int(time.time())), secrets.token_hex(16)
        content = f'{method}\n{target}\n{stamp}\n{nonce}\n'.encode() + raw + b'\n'
        signature = base64.b64encode(c.private_key.sign(content, padding.PKCS1v15(), hashes.SHA256())).decode()
        return (f'WECHATPAY2-SHA256-RSA2048 mchid="{c.mchid}",nonce_str="{nonce}",'
                f'timestamp="{stamp}",serial_no="{c.serial}",signature="{signature}"')

    async def request(self, method, target, data=None):
        self.config.ready()
        raw = b'' if data is None else json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode('utf8')
        headers = {'Authorization': self.authorization(method, target, raw),
                   'Wechatpay-Serial': self.config.public_key_id, 'Accept': 'application/json',
                   'Content-Type': 'application/json', 'User-Agent': 'Xiaoyi-Pay/1'}
        try:
            async with self.session.request(method, 'https://api.mch.weixin.qq.com' + target,
                    headers=headers, data=raw or None, allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=15, connect=5)) as response:
                chunks, size = [], 0
                async for chunk in response.content.iter_chunked(8192):
                    size += len(chunk)
                    if size > 65536:
                        raise PaymentError('支付响应过大，请查询订单状态')
                    chunks.append(chunk)
                body = b''.join(chunks)
                verify(self.config, response.headers, body)
                if response.status not in (200, 201):
                    # Do not copy raw remote bodies, request headers or exceptions into logs/errors.
                    raise GatewayError(json.loads(body).get('code'))
                result = json.loads(body)
                if not isinstance(result, dict):
                    raise ValueError('body')
                return result
        except PaymentError:
            raise
        except (aiohttp.ClientError, TimeoutError, ValueError, OSError):
            raise PaymentError('支付响应未确认，请查询订单状态，勿重复付款') from None

    async def native(self, order):
        from datetime import datetime, timezone
        c = self.config
        return await self.request('POST', '/v3/pay/transactions/native', {
            'appid': c.appid, 'mchid': c.mchid, 'description': '星屿 AI 服务余额充值',
            'out_trade_no': order['id'], 'notify_url': c.notify_url,
            'time_expire': datetime.fromtimestamp(order['expires'], timezone.utc).isoformat(timespec='seconds'),
            'amount': {'total': order['fen'], 'currency': 'CNY'}})

    async def jsapi(self,order,openid):
        from datetime import datetime,timezone
        return await self.request('POST','/v3/pay/transactions/jsapi',{
            'appid':order['appid'],'mchid':self.config.mchid,'description':'星屿 AI 服务余额充值',
            'out_trade_no':order['id'],'notify_url':self.config.notify_url,
            'time_expire':datetime.fromtimestamp(order['expires'],timezone.utc).isoformat(timespec='seconds'),
            'amount':{'total':order['fen'],'currency':'CNY'},'payer':{'openid':openid}})

    def bridge(self,order):
        stamp=str(int(time.time()));nonce=secrets.token_hex(16);package='prepay_id='+order['code_url']
        raw=f"{order['appid']}\n{stamp}\n{nonce}\n{package}\n".encode()
        sig=base64.b64encode(self.config.private_key.sign(raw,padding.PKCS1v15(),hashes.SHA256())).decode()
        return {'appId':order['appid'],'timeStamp':stamp,'nonceStr':nonce,'package':package,'signType':'RSA','paySign':sig}

    async def query(self, order_id):
        return await self.request('GET', '/v3/pay/transactions/out-trade-no/' + quote(order_id, safe='')
                                  + '?' + urlencode({'mchid': self.config.mchid}))
