"""微信客服 / 企业应用加密回调 + DeepSeek。仅单实例运行。"""
import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import math
import logging
import os
import re
import sqlite3
import struct
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import aiohttp
from aiohttp import web
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

LOG = logging.getLogger("wecom-kf")


def required(name):
    value = os.environ.get(name, "").strip()
    if not value or not value.isascii() or any(c.isspace() for c in value):
        raise ValueError("缺少或无效的配置：" + name)
    return value


def xml_parse(data):
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("不支持 XML DTD")
    return ET.fromstring(data)


class Crypto:
    def __init__(self, token, encoded_key, corp_id):
        if not re.fullmatch(r"[A-Za-z0-9]{1,32}", token):
            raise ValueError("回调 Token 应为 1–32 位英文或数字")
        if len(encoded_key) != 43:
            raise ValueError("EncodingAESKey 应为 43 位")
        self.key = base64.b64decode(encoded_key + "=", validate=True)
        if len(self.key) != 32:
            raise ValueError("EncodingAESKey 格式错误")
        self.token, self.corp_id = token, corp_id

    def decrypt(self, encrypted, signature, timestamp, nonce):
        if not re.fullmatch(r"[0-9a-f]{40}",signature):
            raise ValueError("签名格式错误")
        if not timestamp or not nonce or abs(time.time()-int(timestamp)) > 600:
            raise ValueError("时间戳过期")
        digest = hashlib.sha1("".join(sorted([self.token,timestamp,nonce,encrypted])).encode()).hexdigest()
        if not hmac.compare_digest(digest,signature):
            raise ValueError("签名错误")
        cipher = Cipher(algorithms.AES(self.key), modes.CBC(self.key[:16])).decryptor()
        raw = cipher.update(base64.b64decode(encrypted,validate=True)) + cipher.finalize()
        pad = raw[-1] if raw else 0
        if not 1 <= pad <= 32 or raw[-pad:] != bytes([pad])*pad:
            raise ValueError("填充错误")
        raw = raw[:-pad]
        if len(raw) < 20:
            raise ValueError("长度错误")
        length = struct.unpack("!I",raw[16:20])[0]
        if length > len(raw)-20:
            raise ValueError("消息长度错误")
        if not hmac.compare_digest(raw[20+length:],self.corp_id.encode()):
            raise ValueError("CorpID 不匹配")
        return raw[20:20+length]


class Store:
    def __init__(self,path):
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value TEXT);
          CREATE TABLE IF NOT EXISTS accounts (id TEXT PRIMARY KEY,cursor TEXT DEFAULT '',token TEXT DEFAULT '',token_at REAL DEFAULT 0,due REAL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS inbox (msgid TEXT PRIMARY KEY,account TEXT,user TEXT,content TEXT,created REAL,status TEXT DEFAULT 'pending',answer TEXT,attempt INTEGER DEFAULT 0,due REAL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY,session TEXT,role TEXT,content TEXT);
        """)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES('started',?)", (str(time.time()),))
            self.db.execute("UPDATE inbox SET status='pending' WHERE status='working'")
            self.db.execute("DELETE FROM inbox WHERE created < ? AND status='done'", (time.time()-7*86400,))
        if 'created' not in {v[1] for v in self.db.execute('PRAGMA table_info(history)')}:
            with self.db:
                self.db.execute('ALTER TABLE history ADD COLUMN created REAL')
        self.started = float(self.db.execute("SELECT value FROM metadata WHERE key='started'").fetchone()[0])

    def notice(self,account,token=""):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO accounts(id) VALUES(?)",(account,))
            if token:
                self.db.execute("UPDATE accounts SET token=?,token_at=?,due=0 WHERE id=?",(token,time.time(),account))

    def ingest(self,account,page):
        with self.db:
            for item in page.get("msg_list",[]):
                if item.get("origin") == 4 and item.get("event",{}).get("event_type") == "msg_send_fail":
                    LOG.warning("企微报告消息最终发送失败，请检查客服接待状态及发送限制。")
                if item.get("origin") != 3 or not item.get("msgid") or not item.get("external_userid"):
                    continue
                created = float(item.get("send_time",0))
                if created < self.started or time.time()-created > 47*3600:
                    continue
                content = item.get("text",{}).get("content","") if item.get("msgtype") == "text" else ""
                self.db.execute("INSERT OR IGNORE INTO inbox(msgid,account,user,content,created) VALUES(?,?,?,?,?)",(item["msgid"],account,item["external_userid"],content,created))
            cursor = page.get("next_cursor")
            if isinstance(cursor,str):
                self.db.execute("UPDATE accounts SET cursor=? WHERE id=?",(cursor,account))

    def trim_history(self, session, limit=48):
        overflow=self.db.execute('SELECT id,role,content FROM history WHERE session=? AND id NOT IN (SELECT id FROM history WHERE session=? ORDER BY id DESC LIMIT ?) ORDER BY id',(session,session,limit)).fetchall()
        callback=getattr(self,'history_trim_callback',None)
        if callback and overflow:callback(session,overflow)
        self.db.executemany('DELETE FROM history WHERE id=?',[(v['id'],) for v in overflow])

    def history_records(self,session):
        rows = self.db.execute("SELECT id,role,content,created FROM history WHERE session=? ORDER BY id DESC LIMIT 48",(session,)).fetchall()[::-1]
        while rows and sum(len(r["content"]) for r in rows)>32000:
            rows=rows[2:]
        return [dict(r) for r in rows]

    def history(self,session):
        return [{"role": r["role"], "content": r["content"]} for r in self.history_records(session)]

    def append_history(self, session, entries):
        values = []
        for role, content, created in entries:
            if isinstance(created, bool) or not isinstance(created, (int, float)) or not math.isfinite(created) or created <= 0:
                created = None
            values.append((session, role, content, created))
        self.db.executemany('INSERT INTO history(session,role,content,created) VALUES(?,?,?,?)', values)

    def finish(self,row,answer,remember=False):
        session = row["account"]+":"+row["user"]
        with self.db:
            if remember:
                self.append_history(session,[("user",row["content"],dict(row).get("created")),("assistant",answer,time.time())])
                self.trim_history(session)
            self.db.execute("UPDATE inbox SET status='done' WHERE msgid=?",(row["msgid"],))


class APIError(Exception):
    def __init__(self,code):
        self.code=code
        super().__init__(f"WeCom errcode={code}")


class WeCom:
    def __init__(self,http,corp,secret):
        self.http,self.corp,self.secret=http,corp,secret
        self.token=""
        self.expiry=0
        self.lock=asyncio.Lock()

    async def access_token(self):
        async with self.lock:
            if self.token and time.time()<self.expiry:
                return self.token
            async with self.http.get("https://qyapi.weixin.qq.com/cgi-bin/gettoken",params={"corpid":self.corp,"corpsecret":self.secret}) as response:
                response.raise_for_status()
                data=await response.json()
            if data.get("errcode",0)!=0:
                raise APIError(data.get("errcode"))
            self.token=data["access_token"]
            self.expiry=time.time()+max(0,int(data.get("expires_in",7200))-120)
            return self.token

    async def call(self,method,body):
        for attempt in range(2):
            token=await self.access_token()
            async with self.http.post("https://qyapi.weixin.qq.com/cgi-bin/"+method,params={"access_token":token},json=body) as response:
                response.raise_for_status()
                data=await response.json()
            code=data.get("errcode",0)
            if code in (40014,42001) and attempt==0:
                self.expiry=0
                continue
            if code!=0:
                raise APIError(code)
            return data


def shorten(text,limit=2000):
    if len(text.encode())<=limit:
        return text
    note="\n（回复较长，已截断；可让我继续。）"
    return text.encode()[:limit-len(note.encode())].decode(errors="ignore")+note


class Model:
    def __init__(self,http):
        self.http=http
        self.key=required("DEEPSEEK_API_KEY")
        self.name=os.environ.get("DEEPSEEK_MODEL","deepseek-flash")
        self.url=os.environ.get("DEEPSEEK_BASE_URL","https://api.deepseek.com").rstrip("/")+"/chat/completions"
        self.prompt=os.environ.get("SYSTEM_PROMPT","你是小小艺，通过微信客服提供服务，后端模型是 DeepSeek。用中文简洁回答，每次尽量不超过500个汉字。你只能文字对话，不能直接操作服务器、读取文件或联网，不要声称已经执行这些操作。")

    async def answer(self,history,text):
        try:
            async with self.http.post(self.url,headers={"Authorization":"Bearer "+self.key},json={"model":self.name,"messages":[{"role":"system","content":self.prompt}]+history+[{"role":"user","content":text}],"stream":False,"max_tokens":800,"thinking":{"type":"disabled"}},timeout=aiohttp.ClientTimeout(total=120,connect=15)) as response:
                if response.status!=200:
                    LOG.warning("模型接口失败 HTTP %s",response.status)
                    return {401:"模型密钥无效，请联系管理员。",402:"模型余额不足，请联系管理员。",429:"请求较多，请稍后再试。"}.get(response.status,"模型服务暂时不可用，请稍后再试。"),False
                data=await response.json()
            answer=data["choices"][0]["message"]["content"]
            if not isinstance(answer,str) or not answer.strip():
                raise ValueError()
            return shorten(answer.strip()),True
        except (asyncio.TimeoutError,aiohttp.ClientError,ValueError,KeyError,IndexError,TypeError):
            LOG.warning("模型请求异常；未记录正文或密钥。")
            return "暂时未能获取模型回复，请稍后再试。",False


class Service:
    def __init__(self,crypto,store,api=None,model=None,enabled=False):
        self.crypto,self.store,self.api,self.model,self.enabled=crypto,store,api,model,enabled
        self.last_check=0
        self.last_ok=0
        self.active=set()
        self.tasks=set()
        self.last_accounts=0
        self.allowed={v.strip() for v in os.environ.get("KF_ACCOUNT_IDS","").split(",") if v.strip()}

    async def callback(self,request):
        try:
            query=request.query
            if request.method=="GET":
                encrypted=query.get("echostr","")
            else:
                encrypted=xml_parse(await request.read()).findtext("Encrypt","")
            clear=self.crypto.decrypt(encrypted,query.get("msg_signature",""),query.get("timestamp",""),query.get("nonce",""))
            if request.method=="GET":
                LOG.info("企微 URL 验证通过。")
                return web.Response(body=clear,content_type="text/plain")
            node=xml_parse(clear)
            if node.findtext("ToUserName")!=self.crypto.corp_id:
                raise ValueError()
            if node.findtext("Event")=="kf_msg_or_event":
                account=node.findtext("OpenKfId","")
                token=node.findtext("Token","")
                if account and (not self.allowed or account in self.allowed):
                    self.store.notice(account,token)
            return web.Response(text="success")
        except (ValueError,ET.ParseError,UnicodeError,struct.error):
            return web.Response(status=403,text="invalid callback")

    async def health(self,request):
        return web.json_response({"ok":True,"mode":"ai" if self.enabled else "verify-only","last_sync_ok":int(self.last_ok)})

    async def sync(self,account):
        # Cursor and inbox commit together, before processing model requests.
        row=self.store.db.execute("SELECT * FROM accounts WHERE id=?",(account,)).fetchone()
        for _ in range(20):
            payload={"open_kfid":account,"limit":1000}
            if row["cursor"]:
                payload["cursor"]=row["cursor"]
            if row["token"] and time.time()-row["token_at"]<540:
                payload["token"]=row["token"]
            page=await self.api.call("kf/sync_msg",payload)
            self.store.ingest(account,page)
            self.last_ok=time.time()
            if not page.get("has_more"):
                break
            new=self.store.db.execute("SELECT * FROM accounts WHERE id=?",(account,)).fetchone()
            if new["cursor"]==row["cursor"]:
                raise ValueError("cursor did not advance")
            row=new
        with self.store.db:
            # Do not postpone a notification that arrived while sync was running.
            self.store.db.execute("UPDATE accounts SET due=? WHERE id=? AND token_at=?",(time.time()+60,account,row["token_at"]))

    async def collector(self):
        while True:
            try:
                if time.time()-self.last_accounts>300:
                    self.last_accounts=time.time()
                    data=await self.api.call("kf/account/list",{"offset":0,"limit":100})
                    for item in data.get("account_list",[]):
                        account=item.get("open_kfid")
                        if account and (not self.allowed or account in self.allowed):
                            self.store.notice(account)
                for row in self.store.db.execute("SELECT id FROM accounts WHERE due<=?",(time.time(),)).fetchall():
                    try:
                        await self.sync(row["id"])
                    except Exception as exc:
                        self.log_failure("拉取客服消息",exc)
                        with self.store.db:
                            self.store.db.execute("UPDATE accounts SET due=? WHERE id=?",(time.time()+60,row["id"]))
            except Exception as exc:
                self.log_failure("检查客服权限",exc)
            await asyncio.sleep(2)

    @staticmethod
    def log_failure(action,exc):
        # Never log exception messages for HTTP clients: URLs can contain tokens.
        if isinstance(exc,APIError):
            LOG.error("%s失败，企微 errcode=%s",action,exc.code)
        else:
            LOG.error("%s失败 (%s)",action,type(exc).__name__)

    async def reply_one(self,row):
        session=row["account"]+":"+row["user"]
        try:
            with self.store.db:
                self.store.db.execute("UPDATE inbox SET status='working' WHERE msgid=?",(row["msgid"],))
            state=await self.api.call("kf/service_state/get",{"open_kfid":row["account"],"external_userid":row["user"]})
            if state.get("service_state") not in (0,1):
                LOG.info("跳过已由人工接待、排队或结束的会话。")
                self.store.finish(row,"",False)
                return
            text=row["content"].strip()
            answer=row["answer"]
            remember=False
            if answer is None:
                if text in ("/new","/reset","清空对话"):
                    with self.store.db:
                        self.store.db.execute("DELETE FROM history WHERE session=?",(session,))
                    answer="已清空当前会话记录，可以开始新话题。"
                elif text=="/status":
                    answer="微信客服接入正常，当前模型："+self.model.name+"。"
                elif text=="/help":
                    answer="我是小小艺，可以和你文字聊天。发送 /new 清空聊天记录。暂不支持图片、文件、语音识别、联网或执行命令。"
                elif not text:
                    answer="目前支持文字聊天，请用文字告诉我你的问题。"
                elif len(text)>8000:
                    answer="内容较长，请分成每条不超过 8000 字符后发送。"
                else:
                    answer,remember=await self.model.answer(self.store.history(session),text)
                # Persist answer before sending; replay uses the same deterministic msgid.
                cached=json.dumps({"text":shorten(answer),"remember":remember},ensure_ascii=False)
                with self.store.db:
                    self.store.db.execute("UPDATE inbox SET answer=? WHERE msgid=?",(cached,row["msgid"]))
            else:
                cached=answer
            response=json.loads(cached)
            # Re-check before send so a human takeover during inference is respected.
            state=await self.api.call("kf/service_state/get",{"open_kfid":row["account"],"external_userid":row["user"]})
            if state.get("service_state") not in (0,1):
                self.store.finish(row,"",False)
                return
            msgid="ai_"+hashlib.sha256(row["msgid"].encode()).hexdigest()[:28]
            await self.api.call("kf/send_msg",{"touser":row["user"],"open_kfid":row["account"],"msgid":msgid,"msgtype":"text","text":{"content":response["text"]}})
            self.store.finish(row,response["text"],response["remember"])
            LOG.info("客服回复已提交。")
        except Exception as exc:
            self.log_failure("回复客服消息",exc)
            attempt=row["attempt"]+1
            # Explicit permanent errors should not loop or consume additional quota.
            permanent=isinstance(exc,APIError) and exc.code not in (-1,45009,45033)
            status="failed" if permanent or attempt>=3 else "pending"
            with self.store.db:
                self.store.db.execute("UPDATE inbox SET status=?,attempt=?,due=? WHERE msgid=?",(status,attempt,time.time()+30*attempt,row["msgid"]))
        finally:
            self.active.discard(session)

    async def processor(self):
        while True:
            rows=self.store.db.execute("SELECT * FROM inbox WHERE status='pending' AND due<=? ORDER BY created,msgid LIMIT 100",(time.time(),)).fetchall()
            for row in rows:
                session=row["account"]+":"+row["user"]
                if session in self.active:
                    continue
                # Prevent a later message overtaking an earlier one waiting for retry.
                first=self.store.db.execute("SELECT msgid FROM inbox WHERE account=? AND user=? AND status IN ('pending','working') ORDER BY created,msgid LIMIT 1",(row["account"],row["user"])).fetchone()
                if first["msgid"]!=row["msgid"]:
                    continue
                if time.time()-row["created"]>47*3600:
                    self.store.finish(row,"",False)
                    continue
                if len(self.active)>=2:
                    break
                self.active.add(session)
                task=asyncio.create_task(self.reply_one(row))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
            await asyncio.sleep(0.3)


def create_app(service):
    application=web.Application(client_max_size=256*1024)
    application.router.add_get("/health",service.health)
    application.router.add_route("GET","/wecom/enterprises/ww0000000000000002/apps/1000042",service.callback)
    application.router.add_route("POST","/wecom/enterprises/ww0000000000000002/apps/1000042",service.callback)

    async def lifecycle(application):
        loops=[]
        if service.enabled:
            loops=[asyncio.create_task(service.collector()),asyncio.create_task(service.processor())]
        LOG.info("微信客服接收服务启动：%s", "AI 回复已启用" if service.enabled else "仅验证 URL；AI 回复未启用")
        yield
        for task in loops+list(service.tasks):
            task.cancel()
        await asyncio.gather(*loops,*service.tasks,return_exceptions=True)
    application.cleanup_ctx.append(lifecycle)
    return application


async def main_app():
    crypto=Crypto(required("CALLBACK_TOKEN"),required("CALLBACK_AES_KEY"),required("WECOM_CORP_ID"))
    store=Store(os.environ.get("DATABASE_FILE","/data/kf.sqlite3"))
    http=aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30,connect=10))
    enabled=os.environ.get("ENABLE_AI","false").lower()=="true"
    api=model=None
    if enabled:
        api=WeCom(http,crypto.corp_id,required("WECOM_APP_SECRET"))
        model=Model(http)
    service=Service(crypto,store,api,model,enabled)
    application=create_app(service)
    async def cleanup(application):
        await http.close()
        store.db.close()
    application.on_cleanup.append(cleanup)
    return application


if __name__=="__main__":
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    try:
        # Default access logs would expose encrypted callback query parameters.
        web.run_app(main_app(),host="0.0.0.0",port=8088,access_log=None,print=None)
    except ValueError as exc:
        LOG.error("%s",exc)
        raise SystemExit(1)
