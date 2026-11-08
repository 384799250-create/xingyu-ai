"""Session-bound, single-use OAuth for WeChat Pay; credentials never leave the host."""
import base64,hashlib,json,re,secrets,sqlite3,time
from pathlib import Path
from urllib.parse import urlencode,urlsplit
import aiohttp
from alipay_errors import PaymentError

def digest(value):return hashlib.sha256(value.encode()).hexdigest()

class OAuth:
    def __init__(self,path,session):
        self.path=Path(path);self.session=session;self.config={}
        if self.path.exists():
            c=json.loads(self.path.read_text('utf8'))
            if c.get('enabled') is True:
                if c.get('kind') not in ('wechat','wecom'):raise ValueError('invalid_oauth_kind')
                if not re.fullmatch(('wx' if c['kind']=='wechat' else 'ww')+r'[A-Za-z0-9]{8,32}',c.get('appid','')):raise ValueError('invalid_oauth_app')
                secret=(self.path.parent/c['secret_file']).resolve()
                if secret.parent!=self.path.parent.resolve():raise ValueError('invalid_oauth_secret_path')
                c['secret']=secret.read_text('utf8').strip()
                if not c['secret'] or not c.get('redirects'):raise ValueError('invalid_oauth_configuration')
                for url in c['redirects']:
                    u=urlsplit(url)
                    if u.scheme!='https' or u.hostname!='www.example.com' or u.query or u.fragment or not u.path.endswith('/'):raise ValueError('invalid_oauth_redirect')
                self.config=c
        self.access=None;self.ticket=None

    @property
    def enabled(self):return bool(self.config)

    def setup(self,ledger):
        with ledger.db() as db,db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS wechat_oauth_states(
              state TEXT PRIMARY KEY,corp TEXT NOT NULL,user TEXT NOT NULL,agent INTEGER NOT NULL,
              session TEXT NOT NULL,redirect TEXT NOT NULL,expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS wechat_oauth_grants(
              session TEXT PRIMARY KEY,corp TEXT NOT NULL,user TEXT NOT NULL,agent INTEGER NOT NULL,
              appid TEXT NOT NULL,openid TEXT NOT NULL,expires REAL NOT NULL);
            ''')

    def context(self,corp,user,agent,payload):
        if not self.enabled:raise PaymentError('微信内付款尚待公众号授权配置完成；已有订单仍可查询')
        binding=payload.get('_session','');redirect=payload.get('_redirect','')
        if not isinstance(binding,str) or not re.fullmatch(r'[a-f0-9]{64}',binding) or redirect not in self.config['redirects']:raise PaymentError('付款会话无效，请从自己的设置页重试')
        return binding,redirect

    def begin(self,ledger,corp,user,agent,payload):
        binding,redirect=self.context(corp,user,agent,payload);self.setup(ledger)
        try:
            self.payer(ledger,corp,user,agent,payload)
            return {'authorized':True}
        except PaymentError:pass
        state=secrets.token_urlsafe(32);now=time.time()
        with ledger.db() as db,db:
            db.execute('DELETE FROM wechat_oauth_states WHERE expires<? OR session=?',(now,binding))
            db.execute('DELETE FROM wechat_oauth_grants WHERE expires<?',(now,))
            db.execute('INSERT INTO wechat_oauth_states VALUES(?,?,?,?,?,?,?)',(digest(state),corp,user,agent,binding,redirect,now+300))
        args={'appid':self.config['appid'],'redirect_uri':'https://www.example.com/ai/pay/wechat/return','response_type':'code','scope':'snsapi_base','state':state}
        if self.config['kind']=='wecom':args['agentid']=str(agent)
        return {'authorize_url':'https://open.weixin.qq.com/connect/oauth2/authorize?'+urlencode(args)+'#wechat_redirect'}

    def landing(self,ledgers,state,code):
        if not self.enabled or not isinstance(state,str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}',state) or not isinstance(code,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}',code):raise PaymentError('微信授权回调无效')
        found=[]
        for ledger in ledgers:
            self.setup(ledger)
            with ledger.db() as db:r=db.execute('SELECT redirect FROM wechat_oauth_states WHERE state=? AND expires>?',(digest(state),time.time())).fetchone()
            if r and r['redirect'] in self.config['redirects']:found.append(r['redirect'])
        if len(found)!=1:raise PaymentError('微信授权已失效，请返回设置页重试')
        return found[0]+'#wechat-oauth='+urlencode({'code':code,'state':state})

    async def get(self,url,params,method='GET',body=None):
        try:
            async with self.session.request(method,url,params=params,json=body,allow_redirects=False,timeout=aiohttp.ClientTimeout(total=10)) as r:
                raw=await r.content.read(65537)
                if len(raw)>65536 or r.status!=200:raise ValueError()
                value=json.loads(raw)
                if not isinstance(value,dict) or value.get('errcode',0)!=0:raise ValueError()
                return value
        except (aiohttp.ClientError,OSError,TimeoutError,ValueError):raise PaymentError('微信身份授权暂未完成，请重试授权；不会重复创建订单') from None

    async def token(self):
        if self.access and self.access[1]>time.time()+60:return self.access[0]
        c=self.config
        v=await self.get('https://qyapi.weixin.qq.com/cgi-bin/gettoken',{'corpid':c['appid'],'corpsecret':c['secret']})
        token=v.get('access_token')
        if not isinstance(token,str) or not token:raise PaymentError('企业微信授权未完成')
        self.access=(token,time.time()+min(int(v.get('expires_in',7200)),7200));return token

    async def complete(self,ledger,corp,user,agent,payload):
        binding,redirect=self.context(corp,user,agent,payload);self.setup(ledger)
        state,code=payload.get('state'),payload.get('code')
        if not isinstance(state,str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}',state) or not isinstance(code,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}',code):raise PaymentError('微信授权回调无效')
        with ledger.db() as db,db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM wechat_oauth_states WHERE state=?',(digest(state),)).fetchone()
            if not row or (row['corp'],row['user'],row['agent'],row['session'],row['redirect'])!=(corp,user,agent,binding,redirect) or row['expires']<time.time():raise PaymentError('微信授权已失效，请重新授权')
            db.execute('DELETE FROM wechat_oauth_states WHERE state=?',(digest(state),))
        c=self.config
        if c['kind']=='wechat':
            v=await self.get('https://api.weixin.qq.com/sns/oauth2/access_token',{'appid':c['appid'],'secret':c['secret'],'code':code,'grant_type':'authorization_code'})
            if v.get('scope')!='snsapi_base':raise PaymentError('微信授权范围不匹配')
            openid=v.get('openid')
        else:
            if corp!=c['appid']:raise PaymentError('当前企业尚未关联微信内付款')
            token=await self.token()
            v=await self.get('https://qyapi.weixin.qq.com/cgi-bin/auth/getuserinfo',{'access_token':token,'code':code})
            if v.get('UserId')!=user:raise PaymentError('付款身份与当前设置账号不匹配')
            v=await self.get('https://qyapi.weixin.qq.com/cgi-bin/user/convert_to_openid',{'access_token':token},'POST',{'userid':user})
            openid=v.get('openid')
        if not isinstance(openid,str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}',openid):raise PaymentError('微信付款身份未确认')
        with ledger.db() as db,db:
            db.execute('INSERT OR REPLACE INTO wechat_oauth_grants VALUES(?,?,?,?,?,?,?)',(binding,corp,user,agent,c['appid'],openid,time.time()+900))
        return {'authorized':True}

    def payer(self,ledger,corp,user,agent,payload):
        binding,_=self.context(corp,user,agent,payload);self.setup(ledger)
        with ledger.db() as db:r=db.execute('SELECT * FROM wechat_oauth_grants WHERE session=?',(binding,)).fetchone()
        if not r or (r['corp'],r['user'],r['agent'],r['appid'])!=(corp,user,agent,self.config['appid']) or r['expires']<time.time():raise PaymentError('请先完成微信身份授权，再继续原订单')
        return r['openid']

    async def sdk(self,payload):
        if self.config['kind']!='wecom':return None
        page=payload.get('page_url','');_,redirect=self.context(None,None,None,payload)
        u=urlsplit(page)
        if not isinstance(page,str) or len(page)>2048 or (u.scheme,u.netloc,u.path)!=(urlsplit(redirect).scheme,urlsplit(redirect).netloc,urlsplit(redirect).path) or u.fragment:raise PaymentError('付款页面地址无效')
        if not self.ticket or self.ticket[1]<time.time()+60:
            v=await self.get('https://qyapi.weixin.qq.com/cgi-bin/get_jsapi_ticket',{'access_token':await self.token()})
            ticket=v.get('ticket')
            if not isinstance(ticket,str) or not ticket:raise PaymentError('企业微信网页签名未就绪')
            self.ticket=(ticket,time.time()+min(int(v.get('expires_in',7200)),7200))
        stamp=int(time.time());nonce=secrets.token_hex(16)
        raw='jsapi_ticket='+self.ticket[0]+'&noncestr='+nonce+'&timestamp='+str(stamp)+'&url='+page
        return {'appId':self.config['appid'],'timestamp':stamp,'nonceStr':nonce,'signature':hashlib.sha1(raw.encode()).hexdigest()}
