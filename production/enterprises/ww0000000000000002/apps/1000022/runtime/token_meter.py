"""Provider-reported LLM usage. Separate durable ledger, no prompts or credentials."""
import asyncio
from contextvars import ContextVar
from functools import wraps
import inspect
import json
import logging
from pathlib import Path
import secrets
import re
import sqlite3
import time
import aiohttp
from urllib.parse import urlsplit

LOG = logging.getLogger('xiaoyi-usage')
SCOPE = ContextVar('xiaoyi_token_scope', default=None)
TOKEN_FIELDS = ('input_tokens','output_tokens','total_tokens','cached_tokens','cache_miss_tokens','reasoning_tokens')

def number(value):
    return value if type(value) is int and 0 <= value <= 10**12 else None

def normalize(usage):
    if not isinstance(usage, dict):usage = {}
    details = usage.get('prompt_tokens_details') or usage.get('input_tokens_details') or {}
    completion = usage.get('completion_tokens_details') or usage.get('output_tokens_details') or {}
    if not isinstance(details,dict):details = {}
    if not isinstance(completion,dict):completion = {}
    values = dict(input_tokens=number(usage.get('prompt_tokens',usage.get('input_tokens'))),
        output_tokens=number(usage.get('completion_tokens',usage.get('output_tokens'))),
        total_tokens=number(usage.get('total_tokens')),
        cached_tokens=number(usage.get('prompt_cache_hit_tokens',details.get('cached_tokens'))),
        cache_miss_tokens=number(usage.get('prompt_cache_miss_tokens')),
        reasoning_tokens=number(completion.get('reasoning_tokens')))
    if values['total_tokens'] is None and values['input_tokens'] is not None and values['output_tokens'] is not None:
        values['total_tokens'] = values['input_tokens'] + values['output_tokens']
    return values

def estimate_messages(messages):
    images=0
    def scrub(value):
        nonlocal images
        if isinstance(value,dict):
            if value.get('type')=='image_url':
                images+=1;return {'type':'image_url'}
            return {key:scrub(item) for key,item in value.items()}
        if isinstance(value,list):return [scrub(item) for item in value]
        return value
    cleaned=scrub(messages)
    # Inline image base64 is transport data, not an ordinary text-token count.
    # Reserve a conservative per-image budget and settle only reported usage.
    return len(json.dumps(cleaned,ensure_ascii=False).encode('utf8'))+images*16384

class TokenLedger:
    def __init__(self, store, agent):
        path = store.db.execute('PRAGMA database_list').fetchone()[2]
        self.path = Path(path).with_name('token-usage.sqlite3') if path else None
        self.store = store
        self.agent = agent
        self.db = sqlite3.connect(str(self.path) if self.path else ':memory:',timeout=5)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS token_requests(
            id TEXT PRIMARY KEY,agent INTEGER NOT NULL,user TEXT NOT NULL,purpose TEXT NOT NULL,
            model TEXT NOT NULL,provider TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,
            status TEXT NOT NULL,http_status INTEGER,source TEXT NOT NULL,
            input_tokens INTEGER,output_tokens INTEGER,total_tokens INTEGER,cached_tokens INTEGER,
            cache_miss_tokens INTEGER,reasoning_tokens INTEGER);
          CREATE INDEX IF NOT EXISTS token_requests_scope ON token_requests(agent,created,user);
          CREATE TABLE IF NOT EXISTS media_requests(
            id TEXT PRIMARY KEY,agent INTEGER NOT NULL,user TEXT NOT NULL,kind TEXT NOT NULL,
            purpose TEXT NOT NULL,model TEXT NOT NULL,provider TEXT NOT NULL,
            created REAL NOT NULL,updated REAL NOT NULL,status TEXT NOT NULL,http_status INTEGER,
            submitted_chars INTEGER NOT NULL DEFAULT 0,generated_chars INTEGER NOT NULL DEFAULT 0,
            image_count INTEGER NOT NULL DEFAULT 0,is_retry INTEGER NOT NULL DEFAULT 0);
          CREATE INDEX IF NOT EXISTS media_requests_scope ON media_requests(agent,created,user);
        ''')
        with self.db:
            self.db.execute("UPDATE token_requests SET status='interrupted',updated=? WHERE status='pending'",(time.time(),))
            self.db.execute("UPDATE media_requests SET status='interrupted',updated=? WHERE status='pending'",(time.time(),))
        # Additive migration: old usage rows are deliberately not billable.
        for table in ('token_requests','media_requests'):
            columns={r[1] for r in self.db.execute('PRAGMA table_info('+table+')')}
            for name,definition in (('billing_turn',"TEXT NOT NULL DEFAULT ''"),('bill_class',"TEXT NOT NULL DEFAULT 'internal'"),
                                    ('estimate_input','INTEGER'),('estimate_output','INTEGER')):
                if name not in columns:self.db.execute('ALTER TABLE '+table+' ADD COLUMN '+name+' '+definition)
            if 'is_retry' not in columns:self.db.execute('ALTER TABLE '+table+' ADD COLUMN is_retry INTEGER NOT NULL DEFAULT 0')
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS billing_turns(
            id TEXT PRIMARY KEY,user TEXT NOT NULL,scene TEXT NOT NULL,source_id TEXT NOT NULL,
            state TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL);
        """)
        self.db.commit()
        if self.path:self.path.chmod(0o600)

    def begin(self, model, url, payload=None):
        ident = secrets.token_hex(16)
        scope = SCOPE.get() or {}
        with self.db:
            self.db.execute('INSERT INTO token_requests(id,agent,user,purpose,model,provider,created,updated,status,source) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (ident,self.agent,str(scope.get('user') or ''),str(scope.get('purpose') or 'unattributed'),
                 str(model)[:160],(urlsplit(str(url)).hostname or '')[:255],time.time(),time.time(),'pending','missing'))
        turn=scope.get('turn')
        eligible=bool(turn and scope.get('purpose') in ('chat','proactive','greeting') and not scope.get('retry'))
        primary=eligible and not turn['primary_started']
        if eligible:turn['primary_started']=True
        # Byte length is an estimate, never substituted for provider-reported usage.
        payload=payload or {}
        estimate=estimate_messages(payload.get('messages',[]))
        output=payload.get('max_completion_tokens',payload.get('max_tokens',4096))
        output=output if type(output) is int and 0<output<=1000000 else None
        with self.db:self.db.execute('UPDATE token_requests SET billing_turn=?,bill_class=?,is_retry=?,estimate_input=?,estimate_output=? WHERE id=?',
            (turn['id'] if turn else '', 'primary' if primary else 'internal',int(scope.get('retry',False) or (eligible and not primary)),estimate,output,ident))
        return ident

    def turn_begin(self,user,scene,source_id):
        ident=scene+':'+str(source_id)
        with self.db:self.db.execute('INSERT INTO billing_turns VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET updated=excluded.updated',
            (ident,user,scene,str(source_id),'working',time.time(),time.time()))
        # Count persists across retries/restarts; resending a cached plan creates no new charge.
        started=self.db.execute("SELECT 1 FROM token_requests WHERE billing_turn=? AND bill_class='primary' LIMIT 1",(ident,)).fetchone()
        return {'id':ident,'primary_started':bool(started)}

    def turn_finish(self,turn):
        receipt=self.db.execute('SELECT scene,source_id,state FROM billing_turns WHERE id=?',(turn['id'],)).fetchone()
        if receipt[2]=='excluded':return
        state='pending'
        if receipt[0]=='chat':
            source=self.store.db.execute('SELECT status,answer FROM member_inbox WHERE msgid=?',(receipt[1],)).fetchone()
            if source:
                if source[0]=='done':
                    try:plan=json.loads(source[1] or '{}')
                    except (ValueError,TypeError):plan={}
                    state='delivered' if (plan.get('remember') or plan.get('media')) and not plan.get('tool_error') else 'excluded'
                elif source[0] in ('failed','cancelled'):state='excluded'
        elif receipt[0]=='greeting':
            source=self.store.db.execute('SELECT status FROM member_greeting_outbox WHERE id=?',(receipt[1],)).fetchone()
            if source:
                if source[0]=='done':state='delivered'
                elif source[0] in ('failed','cancelled'):state='excluded'
        else:
            source=self.store.db.execute('SELECT status,payload FROM suite_outbox WHERE id=?',(receipt[1],)).fetchone()
            if source:
                if source[0]=='done' and source[1]!='[NO_OP]':state='delivered'
                elif source[0] in ('failed','cancelled'):state='excluded'
        with self.db:self.db.execute('UPDATE billing_turns SET state=?,updated=? WHERE id=?',(state,time.time(),turn['id']))

    def exclude_turn(self):
        turn=(SCOPE.get() or {}).get('turn')
        if turn:
            with self.db:self.db.execute("UPDATE billing_turns SET state='excluded',updated=? WHERE id=?",(time.time(),turn['id']))

    def starcoin_error(self,result=None):
        from suite_app import ProviderError
        decision=(result or {}).get('decision','unavailable')
        message={
            'insufficient':'星币余额不足，暂时无法继续生成。请发送 我要设置 查看余额及充值入口。',
            'unpriced':'本次服务价格暂未确认，尚未扣费，请稍后重试。',
            'account_frozen':'账号已进入注销流程，无法继续生成。',
        }.get(decision,'星币服务暂时不可用，尚未提交生成请求，请稍后重试。')
        return ProviderError(message)

    async def require_starcoin(self,kind,request_id):
        # Retry only the private admission check. Reuse the same durable ID so
        # a committed reservation with a lost response cannot reserve twice.
        result=None
        for attempt in range(2):
            result=await self.billing('prepare',kind=kind,request_id=request_id)
            if result is not None:break
            if attempt==0:
                LOG.warning('Starcoin prepare recovery request=%s kind=%s',
                            billing_request_id(request_id),kind if kind in ('token','image','tts') else '-')
                await asyncio.sleep(.2)
        if not result or result.get('mode')!='starcoin' or result.get('enforced') is not True or result.get('allowed') is not True:
            raise self.starcoin_error(result)

    async def billing(self,operation,**data):
        if not self.path:return None
        return await billing_rpc(self.path.with_name('billing.sock'),operation,**data)

    def finish(self, ident, usage, status, http_status):
        values = normalize(usage)
        source = 'reported' if all(values[k] is not None for k in ('input_tokens','output_tokens','total_tokens')) else 'partial' if any(v is not None for v in values.values()) else 'missing'
        with self.db:
            self.db.execute('UPDATE token_requests SET status=?,http_status=?,source=?,updated=?,input_tokens=?,output_tokens=?,total_tokens=?,cached_tokens=?,cache_miss_tokens=?,reasoning_tokens=? WHERE id=?',
                (status,http_status,source,time.time(),*(values[k] for k in TOKEN_FIELDS),ident))

    def media_attempt(self,kind,model='',url='',text='',retry=False):
        return MediaAttempt(self,kind,model,url,text,retry)

def attach_models(service):
    for name in ('model','member_model'):
        model = getattr(service,name,None)
        if model is not None and hasattr(model,'http') and not isinstance(model.http,MeteredHttp):
            model.http = MeteredHttp(model.http,service.token_ledger)

def sticker_billing_batch(service,row,ident=None,batch=None):
    """Operational grouping metadata only; never sets prices or settles funds.

    Upload IDs persist through the background queue. Existing pending stickers
    without an upload ID are claimed together per owner, and keep it on retry.
    """
    if not row or not hasattr(row,'keys') or not all(k in row.keys() for k in ('agent','user')):return ''
    db=service.store.db
    with db:
        db.execute('''CREATE TABLE IF NOT EXISTS companion_sticker_billing_batches(
            id TEXT PRIMARY KEY,revision REAL NOT NULL,batch TEXT NOT NULL)''')
        target=ident or (row['id'] if 'id' in row.keys() else None)
        if not target:return ''
        current=db.execute('''SELECT p.updated,b.batch FROM suite_stickers s
            JOIN companion_sticker_profiles p ON p.id=s.id
            LEFT JOIN companion_sticker_billing_batches b ON b.id=s.id AND b.revision=p.updated
            WHERE s.id=? AND s.agent=? AND s.user=?''',(target,row['agent'],row['user'])).fetchone()
        if current is None:return ''
        if current['batch']:return current['batch']
        if isinstance(batch,str) and re.fullmatch('[a-f0-9]{32}',batch):
            db.execute('INSERT OR REPLACE INTO companion_sticker_billing_batches VALUES(?,?,?)',(target,current['updated'],batch))
            return batch
        cohort=secrets.token_hex(16)
        db.execute('''INSERT OR REPLACE INTO companion_sticker_billing_batches(id,revision,batch)
            SELECT s.id,p.updated,? FROM suite_stickers s
            JOIN companion_sticker_profiles p ON p.id=s.id
            LEFT JOIN companion_sticker_billing_batches b ON b.id=s.id AND b.revision=p.updated
            WHERE s.agent=? AND s.user=? AND p.status='pending' AND b.id IS NULL''',
            (cohort,row['agent'],row['user']))
        saved=db.execute('SELECT batch FROM companion_sticker_billing_batches WHERE id=? AND revision=?',(target,current['updated'])).fetchone()
        return saved['batch'] if saved else ''


def metered_scope(purpose):
    def decorate(fn):
        signature = inspect.signature(fn)
        @wraps(fn)
        async def wrapper(self,*args,**kwargs):
            attach_models(self)
            bound = signature.bind(self,*args,**kwargs).arguments
            row = bound.get('row',bound.get('job'))
            if row is None and 'request' in bound:
                row = self.panel_user(bound['request'])
            outer = SCOPE.get() or {}
            user = row['user'] if row is not None and hasattr(row,'keys') and 'user' in row.keys() else outer.get('user','')
            kind = purpose
            if purpose in ('chat','image','voice','voice_planning') and outer.get('purpose') in ('proactive','greeting'):kind = outer['purpose']
            retry=bool(row['attempt']) if row is not None and hasattr(row,'keys') and 'attempt' in row.keys() else outer.get('retry',False)
            turn=outer.get('turn');owner=False
            if not turn and row is not None and fn.__name__ in ('member_reply','proactive_reply','greeting_reply'):
                source_id=row['msgid'] if fn.__name__=='member_reply' else row['id']
                try:
                    turn=self.token_ledger.turn_begin(user,{'member_reply':'chat','proactive_reply':'proactive','greeting_reply':'greeting'}[fn.__name__],source_id);owner=True
                except sqlite3.Error:LOG.error('Billing receipt unavailable; simulation skipped.')
            if purpose=='sticker_analysis' and not turn:
                request=bound.get('request');batch=request.query.get('batch_id','') if request is not None else ''
                if request is None:
                    try:batch=sticker_billing_batch(self,row)
                    except sqlite3.Error:LOG.error('Sticker batch metadata unavailable; individual usage retained.')
                if isinstance(batch,str) and re.fullmatch('[a-f0-9]{32}',batch):
                    turn={'id':'sticker_batch:'+batch,'primary_started':False}
            token = SCOPE.set({'user':user,'purpose':kind,'retry':retry,'turn':turn})
            try:
                if owner and fn.__name__ in ('proactive_reply','greeting_reply'):
                    fixed=fn.__name__=='greeting_reply' and self.state(row)['options'].get('greeting_mode')=='fixed'
                    if not fixed:
                        balance=await self.token_ledger.billing('balance',user=user,page=1)
                        if not balance or balance.get('mode')!='starcoin' or float(balance.get('available','0'))<=0:
                            table='suite_outbox' if fn.__name__=='proactive_reply' else 'member_greeting_outbox'
                            with self.store.db:self.store.db.execute("UPDATE "+table+" SET status='cancelled' WHERE id=?",(row['id'],))
                            self.token_ledger.exclude_turn();self.active.discard(self.session(row));return
                return await fn(self,*args,**kwargs)
            finally:
                if owner:
                    try:self.token_ledger.turn_finish(turn)
                    except sqlite3.Error:LOG.error('Billing receipt update failed; reconciliation required.')
                    await self.token_ledger.billing('sync')
                SCOPE.reset(token)
        return wrapper
    return decorate

class UsageCapture:
    def __init__(self, streaming):
        self.streaming = streaming
        self.mode = None if streaming else False
        self.buffer = bytearray()
        self.usage = {}

    def document(self, value):
        if isinstance(value,dict) and isinstance(value.get('usage'),dict):
            # Streaming usage is cumulative; repeated final events never add twice.
            for key,item in value['usage'].items():
                if item is not None:self.usage[key] = item

    def feed(self, block):
        self.buffer.extend(block)
        if self.mode is None and self.buffer.strip():
            self.mode = self.buffer.lstrip()[:1] not in (b'{',b'[')
        if len(self.buffer) > 512*1024:
            self.buffer.clear()
            return
        if self.mode:
            while b'\n' in self.buffer:
                line,_,tail = self.buffer.partition(b'\n');self.buffer = bytearray(tail)
                self.line(line)

    def line(self,line):
        if line.startswith(b'data:'):
            try:self.document(json.loads(line[5:].strip()))
            except (ValueError,UnicodeError):pass

    def finish(self):
        if self.mode:self.line(bytes(self.buffer))
        else:
            try:self.document(json.loads(self.buffer))
            except (ValueError,UnicodeError):pass
        self.buffer.clear()

class MeteredContent:
    def __init__(self, content, capture):self.original,self.capture = content,capture
    def __getattr__(self,name):return getattr(self.original,name)
    async def iter_chunked(self,n):
        async for block in self.original.iter_chunked(n):
            self.capture.feed(block)
            yield block

class MeteredResponse:
    def __init__(self,response,capture):
        self.original,self.capture = response,capture
        self.content = MeteredContent(response.content,capture)
    def __getattr__(self,name):return getattr(self.original,name)
    async def json(self,*args,**kwargs):
        value = await self.original.json(*args,**kwargs)
        self.capture.document(value)
        return value
    async def read(self):
        value = await self.original.read();self.capture.feed(value);return value

class MeteredRequest:
    def __init__(self, context, ledger, payload, url):
        self.context,self.ledger,self.payload,self.url = context,ledger,payload,url
        self.capture = UsageCapture(bool(payload.get('stream')))
        self.ident = None
        self.status = None
    def save(self, state):
        if self.ident:
            try:
                self.capture.finish();self.ledger.finish(self.ident,self.capture.usage,state,self.status)
            except sqlite3.Error:LOG.error('Token ledger write failed; usage may be incomplete.')
    async def __aenter__(self):
        try:self.ident = self.ledger.begin(self.payload.get('model','unknown'),self.url,self.payload)
        except sqlite3.Error:LOG.error('Token ledger unavailable; usage may be incomplete.')
        if not self.ident:raise self.ledger.starcoin_error()
        try:await self.ledger.require_starcoin('token',self.ident)
        except BaseException:
            self.save('cancelled')
            # aiohttp's context owns an unstarted coroutine; close it when the
            # prepaid gate prevents __aenter__ so it cannot leak or send later.
            close=getattr(self.context,'close',None)
            if close:close()
            raise
        try:
            response = await self.context.__aenter__();self.status = response.status
            return MeteredResponse(response,self.capture)
        except BaseException as exc:
            self.save('cancelled' if isinstance(exc,asyncio.CancelledError) else 'error')
            raise
    async def __aexit__(self,typ,value,tb):
        try:return await self.context.__aexit__(typ,value,tb)
        finally:
            self.save('cancelled' if typ and issubclass(typ,asyncio.CancelledError) else 'error' if typ or self.status != 200 else 'completed')
            # Settle this actual request before the next helper reserves funds.
            await self.ledger.billing('sync')

class MeteredHttp:
    def __init__(self,http,ledger):self.original,self.ledger = http,ledger
    def __getattr__(self,name):return getattr(self.original,name)
    def post(self,url,*args,**kwargs):
        payload = kwargs.get('json')
        if isinstance(payload,dict) and isinstance(payload.get('messages'),list) and hasattr(self.ledger,'guard'):
            self.ledger.guard((SCOPE.get() or {}).get('user',''))
        if isinstance(payload,dict) and isinstance(payload.get('messages'),list):
            # Bound output before estimating/reserving, including callers that
            # previously relied on an unbounded provider default.
            payload=dict(payload)
            if not any(k in payload for k in ('max_tokens','max_completion_tokens')):payload['max_tokens']=4096
            kwargs=dict(kwargs,json=payload)
        context = self.original.post(url,*args,**kwargs)
        # Meter only chat requests on a model's session, never image/audio/WeCom APIs.
        if not isinstance(payload,dict) or not isinstance(payload.get('messages'),list):return context
        return MeteredRequest(context,self.ledger,payload,url)

class MediaAttempt:
    """One actual provider request. Never persists text, prompts, audio or credentials."""
    def __init__(self,ledger,kind,model,url,text,retry):
        self.ledger,self.kind,self.model,self.url=ledger,kind,str(model or '')[:160],url
        self.chars=len(text) if kind=='tts' and isinstance(text,str) else 0
        self.retry=bool(retry)
        self.ident=None;self.http_status=None;self.status=None;self.generated_chars=0;self.images=0

    async def __aenter__(self):
        if self.kind not in ('image','tts'):return self
        scope=SCOPE.get() or {};self.ident=secrets.token_hex(16)
        try:
            with self.ledger.db:
                self.ledger.db.execute('INSERT INTO media_requests(id,agent,user,kind,purpose,model,provider,created,updated,status,submitted_chars,is_retry) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (self.ident,self.ledger.agent,str(scope.get('user') or ''),self.kind,
                     'preview' if scope.get('purpose')=='voice_preview' else 'proactive' if scope.get('purpose')=='proactive' else 'generation' if self.kind=='image' else 'chat',
                     self.model,(urlsplit(str(self.url)).hostname or '')[:255],time.time(),time.time(),'pending',self.chars,int(self.retry or scope.get('retry',False))))
        except sqlite3.Error:
            self.ident=None;LOG.error('Media ledger unavailable; usage may be incomplete.')
        if self.ident:
            turn=scope.get('turn')
            with self.ledger.db:self.ledger.db.execute('UPDATE media_requests SET billing_turn=?,bill_class=? WHERE id=?',
                (turn['id'] if turn else '', 'media' if turn and not self.retry and not scope.get('retry') and scope.get('purpose')!='voice_preview' else 'internal',self.ident))
            try:await self.ledger.require_starcoin(self.kind,self.ident)
            except BaseException:
                with self.ledger.db:self.ledger.db.execute("UPDATE media_requests SET status='cancelled',updated=? WHERE id=?",(time.time(),self.ident))
                raise
        else:raise self.ledger.starcoin_error()
        return self

    def image_result(self,raw):
        try:
            value=json.loads(raw)
            if not isinstance(value,dict) or value.get('error'):self.status='error';return
            data=value.get('data')
            if isinstance(data,list):
                self.images=sum(1 for item in data if isinstance(item,dict) and
                    any(isinstance(item.get(k),str) and item[k].strip() for k in ('b64_json','url')))
                self.status='completed' if self.images else 'error'
            else:self.status='unknown' if any(value.get(k) for k in ('task_id','taskid','id','status')) else 'error'
        except (ValueError,TypeError,UnicodeError):self.status='error'

    def audio_result(self,raw,codec='mp3'):
        valid=isinstance(raw,bytes) and bool(raw)
        if codec=='pcm':valid=valid and len(raw)%2==0 and not raw.startswith((b'RIFF',b'ID3',b'#!AMR'))
        elif codec=='amr':valid=valid and raw.startswith(b'#!AMR\n')
        elif codec=='wav':valid=valid and raw.startswith(b'RIFF') and b'WAVE' in raw[:16]
        else:valid=valid and (raw.startswith(b'ID3') or len(raw)>2 and raw[0]==255 and raw[1]&224==224)
        self.status='completed' if valid else 'error'
        self.generated_chars=self.chars if valid else 0

    async def __aexit__(self,typ,value,tb):
        if not self.ident:return
        status=self.status or 'unknown'
        if typ:
            status='cancelled' if issubclass(typ,asyncio.CancelledError) else 'unknown' if issubclass(typ,(asyncio.TimeoutError,aiohttp.ClientError)) else 'error'
        try:
            with self.ledger.db:
                self.ledger.db.execute('UPDATE media_requests SET updated=?,status=?,http_status=?,generated_chars=?,image_count=? WHERE id=?',
                    (time.time(),status,self.http_status,self.generated_chars if status=='completed' else 0,self.images if status=='completed' else 0,self.ident))
        except sqlite3.Error:LOG.error('Media ledger write failed; usage may be incomplete.')


BILLING_RECOVERY_VERSION=1
BILLING_OPERATIONS=frozenset(('prepare','sync','balance','persona_templates','payment','wechat_payment'))
BILLING_ERROR_CODES=frozenset(('database_busy','database_error','permission_denied','invalid_request','io_error','internal_error'))

def billing_request_id(value):
    return value if isinstance(value,str) and re.fullmatch('[a-f0-9]{32}',value) else '-'

async def billing_rpc(path,operation,**data):
    """Bounded private RPC. Admission stays closed until explicitly allowed."""
    writer=None
    op=operation if isinstance(operation,str) and operation in BILLING_OPERATIONS else '-'
    ident=billing_request_id(data.get('request_id'))
    def failed(code):
        # Never log payloads, user identities, exception text or credentials.
        LOG.warning('Starcoin RPC unavailable operation=%s request=%s code=%s',op,ident,code)
    if not hasattr(asyncio,'open_unix_connection'):
        failed('unsupported_transport');return None
    try:
        async with asyncio.timeout(1.5):
            reader,writer=await asyncio.open_unix_connection(str(path),limit=262144)
            writer.write(json.dumps(dict(operation=operation,**data)).encode()+b'\n');await writer.drain()
            result=json.loads(await reader.readline())
            if isinstance(result,dict) and result.get('ok') is True:return result
            code=result.get('error_code') if isinstance(result,dict) else None
            failed(code if isinstance(code,str) and code in BILLING_ERROR_CODES else 'invalid_response')
            return None
    except asyncio.TimeoutError:failed('timeout');return None
    except PermissionError:failed('permission_denied');return None
    except OSError:failed('transport_error');return None
    except ValueError:failed('invalid_response');return None
    finally:
        if writer:
            writer.close()
            try:
                async with asyncio.timeout(.2):await writer.wait_closed()
            except (OSError,asyncio.TimeoutError):pass
