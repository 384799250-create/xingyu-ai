"""Tencent SMS adapter and single-use, account-bound phone verification. No CLI sends."""
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time
import urllib.request


class SmsError(Exception):
    pass


class TencentSms:
    def __init__(self, config, transport=None, clock=time.time):
        self.config = config
        self.transport = transport or self._transport
        self.clock = clock

    def ready(self):
        c=self.config
        return (c.get('enabled') is True and all(isinstance(c.get(k),str) and c[k].strip()
                for k in ['secret_id','secret_key','sdk_app_id','sign_name','template_id'])
                and c.get('parameter_order') in (['code'],['code','minutes']))

    @staticmethod
    def _transport(headers, payload):
        request=urllib.request.Request('https://sms.tencentcloudapi.com/',data=payload,headers=headers,method='POST')
        with urllib.request.urlopen(request,timeout=20) as response:
            return json.load(response)

    def send(self, phone, code):
        if not self.ready():
            raise SmsError('sms_not_configured')
        if not re.fullmatch(r'\+861[3-9][0-9]{9}',phone) or not re.fullmatch(r'[0-9]{6}',code):
            raise SmsError('invalid_sms_input')
        c=self.config
        timestamp=int(self.clock());date=time.strftime('%Y-%m-%d',time.gmtime(timestamp))
        params=[code] if c['parameter_order']==['code'] else [code,'5']
        payload=json.dumps({'PhoneNumberSet':[phone],'SmsSdkAppId':c['sdk_app_id'],
                            'SignName':c['sign_name'],'TemplateId':c['template_id'],
                            'TemplateParamSet':params},ensure_ascii=False,separators=(',',':')).encode('utf-8')
        canonical_headers='content-type:application/json; charset=utf-8\nhost:sms.tencentcloudapi.com\n'
        signed_headers='content-type;host'
        canonical='POST\n/\n\n'+canonical_headers+'\n'+signed_headers+'\n'+hashlib.sha256(payload).hexdigest()
        scope=date+'/sms/tc3_request'
        string_to_sign='TC3-HMAC-SHA256\n'+str(timestamp)+'\n'+scope+'\n'+hashlib.sha256(canonical.encode()).hexdigest()
        sign=lambda key,value:hmac.new(key,value.encode(),hashlib.sha256).digest()
        signing=sign(sign(sign(('TC3'+c['secret_key']).encode(),date),'sms'),'tc3_request')
        signature=hmac.new(signing,string_to_sign.encode(),hashlib.sha256).hexdigest()
        headers={'Content-Type':'application/json; charset=utf-8','Host':'sms.tencentcloudapi.com',
                 'X-TC-Action':'SendSms','X-TC-Version':'2021-01-11','X-TC-Timestamp':str(timestamp),
                 'X-TC-Region':c.get('region','ap-guangzhou'),
                 'Authorization':'TC3-HMAC-SHA256 Credential='+c['secret_id']+'/'+scope+', SignedHeaders='+signed_headers+', Signature='+signature}
        try:
            result=self.transport(headers,payload)
        except Exception:
            # Billing/delivery may have happened; never automatically resubmit.
            raise SmsError('delivery_unknown') from None
        if not isinstance(result,dict) or not isinstance(result.get('Response'),dict):
            raise SmsError('delivery_unknown')
        response=result['Response']
        if response.get('Error'):
            code=str(response['Error'].get('Code','Rejected'))
            safe=code if re.fullmatch(r'[A-Za-z0-9_.]{1,100}',code) else 'Rejected'
            raise SmsError('provider_'+safe)
        statuses=response.get('SendStatusSet')
        if not isinstance(statuses,list) or len(statuses)!=1:
            raise SmsError('delivery_unknown')
        status=statuses[0]
        if status.get('PhoneNumber')!=phone:
            raise SmsError('delivery_unknown')
        if status.get('Code')!='Ok':
            code=str(status.get('Code','Rejected'))
            safe=code if re.fullmatch(r'[A-Za-z0-9_.]{1,100}',code) else 'Rejected'
            raise SmsError('provider_'+safe)
        return {'request_id':str(response.get('RequestId',''))[:128],
                'serial_no':str(status.get('SerialNo',''))[:128]}


class Verification:
    def __init__(self,path,key,sender,clock=time.time,daily_budget=100):
        if len(key)<32:raise ValueError('A persistent 32-byte private HMAC key is required')
        self.key,self.sender,self.clock=key,sender,clock
        self.daily_budget=daily_budget
        self.db=sqlite3.connect(str(path),timeout=10,isolation_level=None)
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS otp (
              id TEXT PRIMARY KEY, account TEXT NOT NULL, phone_hash TEXT NOT NULL, ip_hash TEXT NOT NULL,
              code_hash TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL,
              request_id TEXT, serial_no TEXT);
            CREATE INDEX IF NOT EXISTS otp_created ON otp(created);
            CREATE INDEX IF NOT EXISTS otp_phone ON otp(phone_hash,created);
            CREATE TABLE IF NOT EXISTS proofs (
              hash TEXT PRIMARY KEY, account TEXT NOT NULL, phone_hash TEXT NOT NULL,
              created REAL NOT NULL, expires REAL NOT NULL, consumed INTEGER NOT NULL DEFAULT 0);
        ''')

    def close(self):self.db.close()

    def mac(self,*values):
        return hmac.new(self.key,json.dumps(values,separators=(',',':')).encode(),hashlib.sha256).hexdigest()

    @staticmethod
    def phone(value):
        if not isinstance(value,str):raise SmsError('invalid_phone')
        value=value.strip()
        if re.fullmatch(r'1[3-9][0-9]{9}',value):value='+86'+value
        if not re.fullmatch(r'\+861[3-9][0-9]{9}',value):raise SmsError('invalid_phone')
        return value

    @staticmethod
    def account(value):
        if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',value):raise SmsError('invalid_account')
        return value

    def send(self,account,phone,ip):
        account=self.account(account);phone=self.phone(phone)
        if not self.sender.ready():raise SmsError('sms_not_configured')
        if not isinstance(ip,str) or len(ip)>100:raise SmsError('invalid_ip')
        now=self.clock();ph=self.mac('phone',phone);ih=self.mac('ip',ip)
        ident=secrets.token_urlsafe(24);code=f'{secrets.randbelow(1000000):06d}'
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('DELETE FROM otp WHERE created<?',(now-86400,))
            self.db.execute('DELETE FROM proofs WHERE expires<=?',(now,))
            # All attempts count, including unknown deliveries and explicit provider rejects.
            if self.db.execute('SELECT 1 FROM otp WHERE (phone_hash=? OR account=?) AND created>? LIMIT 1',(ph,account,now-60)).fetchone():
                raise SmsError('wait_60_seconds')
            for column,value,limit,window in [('phone_hash',ph,5,3600),('phone_hash',ph,10,86400),
                                            ('account',account,5,3600),('ip_hash',ih,20,3600)]:
                count=self.db.execute('SELECT COUNT(*) FROM otp WHERE '+column+'=? AND created>?',(value,now-window)).fetchone()[0]
                if count>=limit:raise SmsError('verification_rate_limit')
            if self.db.execute('SELECT COUNT(*) FROM otp WHERE created>?',(now-86400,)).fetchone()[0]>=self.daily_budget:
                raise SmsError('daily_budget_reached')
            self.db.execute("UPDATE otp SET state='superseded' WHERE account=? AND state IN ('sent','sending','unknown')",(account,))
            self.db.execute('INSERT INTO otp(id,account,phone_hash,ip_hash,code_hash,created,expires,state) VALUES(?,?,?,?,?,?,?,?)',
                            (ident,account,ph,ih,self.mac('otp',ident,account,ph,code),now,now+300,'sending'))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK');raise
        try:
            result=self.sender.send(phone,code)
        except Exception as exc:
            reason=str(exc) if isinstance(exc,SmsError) else 'delivery_unknown'
            self.db.execute("UPDATE otp SET state=? WHERE id=? AND state='sending'",('unknown' if reason=='delivery_unknown' else 'failed',ident))
            raise SmsError(reason) from None
        self.db.execute("UPDATE otp SET state='sent',request_id=?,serial_no=? WHERE id=? AND state='sending'",
                        (result.get('request_id',''),result.get('serial_no',''),ident))
        return {'challenge':ident,'expires_in':300,'resend_after':60}

    def verify(self,account,phone,challenge,code):
        account=self.account(account);phone=self.phone(phone)
        if not isinstance(challenge,str) or not re.fullmatch(r'[A-Za-z0-9_-]{32}',challenge):raise SmsError('invalid_code')
        if not isinstance(code,str) or not re.fullmatch(r'[0-9]{6}',code):raise SmsError('invalid_code')
        ph=self.mac('phone',phone);now=self.clock()
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row=self.db.execute('SELECT * FROM otp WHERE id=? AND account=? AND phone_hash=?',(challenge,account,ph)).fetchone()
            if not row or row['state']!='sent' or row['expires']<=now or row['attempts']>=5:
                raise SmsError('invalid_code')
            if not hmac.compare_digest(row['code_hash'],self.mac('otp',challenge,account,ph,code)):
                self.db.execute('UPDATE otp SET attempts=attempts+1 WHERE id=?',(challenge,))
                self.db.execute('COMMIT')
                raise SmsError('invalid_code')
            self.db.execute("UPDATE otp SET state='verified' WHERE id=?",(challenge,))
            raw=secrets.token_urlsafe(32)
            self.db.execute('INSERT INTO proofs VALUES(?,?,?,?,?,0)',(self.mac('proof',raw),account,ph,now,now+900))
            self.db.execute('COMMIT')
            return {'proof':raw,'expires_in':900}
        except BaseException:
            if self.db.in_transaction:self.db.execute('ROLLBACK')
            raise

    def consume(self,account,phone,proof):
        account=self.account(account);phone=self.phone(phone)
        if not isinstance(proof,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',proof):raise SmsError('invalid_proof')
        # Provisioning must persist its request before consuming this proof; never reuse it for another phone/account.
        result=self.db.execute('UPDATE proofs SET consumed=1 WHERE hash=? AND account=? AND phone_hash=? AND expires>? AND consumed=0',
                               (self.mac('proof',proof),account,self.mac('phone',phone),self.clock()))
        if result.rowcount!=1:raise SmsError('invalid_proof')
        return {'phone_verified':True}
