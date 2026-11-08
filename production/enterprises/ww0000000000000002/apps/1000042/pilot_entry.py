# shared-runtime-bootstrap-v1:start
if __name__ == '__main__':
    import os as _shared_os
    if _shared_os.path.isfile('/data/.shared-runtime-client.py'):
        import runpy as _shared_runpy
        _shared_runpy.run_path('/data/.shared-runtime-client.py', run_name='__main__')
        raise SystemExit(0)
# shared-runtime-bootstrap-v1:end

"""Isolated WeCom pilot. No existing application's runtime or data is modified."""
import asyncio
import hashlib
import json
import logging
import os
import struct
import sys
import time
from pathlib import Path
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent / 'runtime'))
import aiohttp
from aiohttp import web
import companion_app as companion

kf = companion.base.kf
AGENT_ID = 1000042
LOG = logging.getLogger('xiaoyi-independent-app')


class IndependentService(companion.CompanionService):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.agent_id != AGENT_ID:
            raise ValueError('This pilot only accepts AgentId 1000042')
        self.bound_users = frozenset(v.strip() for v in os.environ.get('XIAOYI_APP_ADMINS', '').split(',') if v.strip())
        if not self.bound_users:
            raise ValueError('At least one verified test owner must be configured')

        self.initialize_welcome()

    def initialize_welcome(self):
        # Durable, owner-scoped state. Migration suppresses an unsolicited replay
        # to people who already used this AI before the feature was installed.
        self.store.db.executescript('''
          CREATE TABLE IF NOT EXISTS independent_welcome (
            agent INTEGER NOT NULL, user TEXT NOT NULL, source_msgid TEXT,
            status TEXT NOT NULL, created REAL NOT NULL, sent_at REAL,
            PRIMARY KEY(agent,user));
        ''')
        with self.store.db:
            # A crash in the network call has an unknown delivery result.
            self.store.db.execute("UPDATE independent_welcome SET status='delivery_unknown' WHERE status='sending'")
            for user in self.bound_users:
                previous = self.store.db.execute('''SELECT 1 FROM member_inbox
                  WHERE agent=? AND user=? AND status='done' LIMIT 1''', (self.agent_id,user)).fetchone()
                if previous is None:
                    previous = self.store.db.execute('''SELECT 1 FROM suite_activity
                      WHERE agent=? AND user=? AND last_inbound>0 LIMIT 1''', (self.agent_id,user)).fetchone()
                if previous:
                    self.store.db.execute('INSERT OR IGNORE INTO independent_welcome VALUES(?,?,NULL,?,?,NULL)',
                                          (self.agent_id,user,'existing',time.time()))

    def queue_welcome_event(self, node):
        if node.findtext('MsgType') != 'event' or node.findtext('Event') not in ('subscribe','enter_agent'):
            return False
        created = float(node.findtext('CreateTime', '0'))
        now = time.time()
        if not 0 < created <= now + 600:
            raise ValueError('Invalid event time')
        if now - created > 86400:
            return True
        user = node.findtext('FromUserName', '')
        # The callback has already checked signature, corp, agent and owner.
        lifecycle = getattr(self, 'lifecycle', None)
        if lifecycle is not None:
            value = lifecycle.status(user)
            if value['enabled'] and value['state'] != 'active':
                return True
        ident = 'welcome-event-' + hashlib.sha256((str(self.agent_id)+'|'+user+'|'+str(created)).encode()).hexdigest()[:32]
        with self.store.db:
            self.store.db.execute('INSERT OR IGNORE INTO independent_welcome VALUES(?,?,?,?,?,NULL)',
                (self.agent_id,user,ident,'queued',now))
        return True

    async def dispatch_welcome_event(self):
        if not self.enabled:
            return
        item = self.store.db.execute("SELECT * FROM independent_welcome WHERE agent=? AND status='queued' ORDER BY created LIMIT 1",
                                     (self.agent_id,)).fetchone()
        if item is None:
            return
        if item['user'] not in self.bound_users:
            return
        lifecycle = getattr(self, 'lifecycle', None)
        if lifecycle is not None:
            value = lifecycle.status(item['user'])
            if value['enabled'] and value['state'] != 'active':
                with self.store.db:
                    self.store.db.execute("UPDATE independent_welcome SET status='cancelled' WHERE agent=? AND user=? AND status='queued'",
                                          (self.agent_id,item['user']))
                return
        with self.store.db:
            changed = self.store.db.execute("UPDATE independent_welcome SET status='sending' WHERE agent=? AND user=? AND status='queued'",
                                            (self.agent_id,item['user'])).rowcount
        if not changed:
            return
        row = {'agent':self.agent_id,'user':item['user'],'msgid':item['source_msgid'],'created':item['created'],'content':''}
        try:
            await self.send_text(row,self.welcome_text(row))
        except Exception as exc:
            # Delivery may already have reached WeCom. Never replay an uncertain send.
            with self.store.db:
                self.store.db.execute("UPDATE independent_welcome SET status='delivery_unknown' WHERE agent=? AND user=? AND status='sending'",
                                      (self.agent_id,item['user']))
            LOG.warning('Welcome event delivery not confirmed (%s)',type(exc).__name__)
        else:
            with self.store.db:
                self.store.db.execute("UPDATE independent_welcome SET status='sent',sent_at=? WHERE agent=? AND user=? AND status='sending'",
                                      (time.time(),self.agent_id,item['user']))

    async def welcome_processor(self):
        while True:
            try:
                await self.dispatch_welcome_event()
            except Exception as exc:
                LOG.warning('Welcome event deferred (%s)',type(exc).__name__)
            await asyncio.sleep(5)

    async def member_processor(self):
        await asyncio.gather(super().member_processor(), self.welcome_processor())

    def claim_welcome(self, row, status):
        if not self.enabled or row['agent'] != self.agent_id or row['user'] not in self.bound_users:
            return False
        with self.store.db:
            result = self.store.db.execute('INSERT OR IGNORE INTO independent_welcome VALUES(?,?,?,?,?,NULL)',
                (row['agent'],row['user'],row['msgid'],status,time.time()))
        if result.rowcount == 1:
            return True
        # A real first message can take over a queued event before it is sent.
        with self.store.db:
            claimed = self.store.db.execute("UPDATE independent_welcome SET status=?,source_msgid=? WHERE agent=? AND user=? AND status='queued'",
                (status,row['msgid'],row['agent'],row['user'])).rowcount
        if claimed:
            return True
        # A crash before the normal settings reply is cached may leave only
        # this claim. Resume it solely for the same inbound message.
        previous = self.store.db.execute('''SELECT status,source_msgid FROM independent_welcome
          WHERE agent=? AND user=?''', (row['agent'],row['user'])).fetchone()
        return (status == 'prepared' and previous['status'] == 'prepared'
                and previous['source_msgid'] == row['msgid'])

    def welcome_text(self, row):
        link = self.issue_panel_link(row)
        return ('你好呀，欢迎来到星屿。\n'
                '从今天起，我们可以慢慢熟悉彼此。你可以先和我聊聊，也可以把我设定成你心中的那个 ta。\n\n'
                '使用说明与常见问题：\nhttps://www.example.com/ai/help\n\n'
                '你的专属设置：\n'+link+'\n\n'
                '在这里可以调整人格、导入人物卡、编辑世界书和聊天偏好。\n'
                '设置链接自生成起 15 分钟内有效，请勿转发；过期后发送 我要设置 获取新链接。\n'
                '以后想查看使用说明，发送 /帮助 就好。')

    def persona_command(self, row, text):
        if text in ('/帮助','/help'):
            return ('使用说明与常见问题：\nhttps://www.example.com/ai/help\n\n'
                    '发送 我要设置 获取你自己的设置链接，可以调整人格、导入人物卡、编辑世界书和聊天偏好。')
        if text in ('我要设置','/网页设置','/面板','/settings') and self.claim_welcome(row,'prepared'):
            # Reuse the normal command delivery/cache path: first-time 我要设置
            # receives one welcome containing one private link, not two messages.
            return self.welcome_text(row)
        return super().persona_command(row,text)

    async def member_reply(self, row):
        if not self.enabled or row['agent'] != self.agent_id or row['user'] not in self.bound_users:
            return
        settings_command = row['content'].strip() in ('我要设置','/网页设置','/面板','/settings')
        if not settings_command and self.claim_welcome(row,'sending'):
            # The welcome stays out of model input, conversation history and
            # sticker/voice selection. An uncertain send is not replayed blindly.
            try:
                await self.send_text(row,self.welcome_text(row))
            except Exception:
                with self.store.db:
                    self.store.db.execute("UPDATE independent_welcome SET status='delivery_unknown' WHERE agent=? AND user=?",
                                          (row['agent'],row['user']))
            else:
                with self.store.db:
                    self.store.db.execute("UPDATE independent_welcome SET status='sent',sent_at=? WHERE agent=? AND user=?",
                                          (time.time(),row['agent'],row['user']))
                await asyncio.sleep(1)
        try:
            await super().member_reply(row)
        finally:
            value = self.store.db.execute('SELECT status FROM member_inbox WHERE msgid=?',(row['msgid'],)).fetchone()
            if value and value['status'] in ('done','failed'):
                with self.store.db:
                    self.store.db.execute('''UPDATE independent_welcome SET status=?,sent_at=?
                      WHERE agent=? AND user=? AND source_msgid=? AND status='prepared' ''',
                      ('sent' if value['status']=='done' else 'failed',time.time() if value['status']=='done' else None,
                       row['agent'],row['user'],row['msgid']))


    def panel_user(self, request):
        row = super().panel_user(request)
        if row is None or row['agent'] != self.agent_id or row['user'] not in self.bound_users:
            return None
        return row

    def issue_panel_link(self, row):
        if row['agent'] != self.agent_id or row['user'] not in self.bound_users:
            raise ValueError('User is not bound to this application')
        return super().issue_panel_link(row)

    async def panel_session(self, request):
        if not self.panel_write_allowed(request):
            return await super().panel_session(request)
        try:
            payload = await request.json()
            raw = payload.get('token', '') if isinstance(payload, dict) else ''
            if isinstance(raw, str):
                digest = hashlib.sha256(raw.encode()).hexdigest()
                row = self.store.db.execute('SELECT agent,user FROM panel_tokens WHERE token_hash=? AND expires>?',
                    (digest, time.time())).fetchone()
                if row is not None and (row['agent'] != self.agent_id or row['user'] not in self.bound_users):
                    return web.json_response({'error': 'This user is not bound to the application'}, status=401)
        except (ValueError, TypeError, UnicodeError):
            pass
        return await super().panel_session(request)

    async def callback(self, request):
        if request.method == 'GET':
            return await super().callback(request)
        try:
            envelope = kf.xml_parse(await request.read())
            clear = self.crypto.decrypt(envelope.findtext('Encrypt', ''),
                request.query.get('msg_signature', ''), request.query.get('timestamp', ''),
                request.query.get('nonce', ''))
            node = kf.xml_parse(clear)
            if node.findtext('ToUserName') != self.crypto.corp_id or node.findtext('AgentID') != str(self.agent_id):
                raise ValueError('Mismatched callback identity')
            if node.findtext('FromUserName', '') not in self.bound_users:
                return web.Response(text='success')
        except (ValueError, ET.ParseError, UnicodeError, struct.error):
            return web.Response(status=403, text='invalid callback')
        if not self.enabled:
            return web.Response(text='success')
        try:
            if self.queue_welcome_event(node):
                return web.Response(text='success')
        except (ValueError, TypeError):
            return web.Response(status=403, text='invalid event')
        # aiohttp caches request.read(); the original verified handler remains authoritative.
        return await super().callback(request)

    async def health(self, request):
        response = await super().health(request)
        data = json.loads(response.text)
        data['first_welcome_version'] = 2
        data['independent_app'] = {'agent_id': self.agent_id, 'ai_enabled': bool(self.enabled),
            'restricted_to_test_owners': True, 'isolated_database': True, 'version': 1}
        return web.json_response(data, status=response.status)


async def main_app():
    if int(kf.required('WECOM_AGENT_ID')) != AGENT_ID:
        raise ValueError('Unexpected AgentId')
    crypto = kf.Crypto(kf.required('CALLBACK_TOKEN'), kf.required('CALLBACK_AES_KEY'), kf.required('WECOM_CORP_ID'))
    store = kf.Store(os.environ.get('DATABASE_FILE', '/data/kf.sqlite3'))
    http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30, connect=10))
    try:
        enabled = os.environ.get('ENABLE_AI', 'false').lower() == 'true'
        api = kf.WeCom(http, crypto.corp_id, kf.required('WECOM_APP_SECRET')) if enabled else None
        model = kf.Model(http) if enabled else None
        service = IndependentService(crypto, store, api, model, enabled, agent_id=AGENT_ID)
        app = companion.create_app(service)
        app['independent_service'] = service
    except BaseException:
        await http.close()
        store.db.close()
        raise

    async def cleanup(application):
        await http.close()
        store.db.close()

    app.on_cleanup.append(cleanup)
    return app


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    web.run_app(main_app(), host='0.0.0.0', port=8088, access_log=None)
