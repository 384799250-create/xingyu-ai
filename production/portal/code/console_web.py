"""Admin HTTP adapter using the existing portal's sessions, CSRF and origin validation."""
import asyncio
import json
import time
from pathlib import Path
from aiohttp import web

OVERVIEW_SCOPE_TIMEOUT=25

class Console:
    def __init__(self,portal):self.portal=portal

    async def call(self,request,action,payload=None):
        user=self.portal.user(request)
        enterprise=request.headers.get('X-Xingyu-Enterprise','legacy')
        if enterprise=='all':
            if action=='ops_dashboard':return await self.all_overview(user,payload)
            if action not in ('identity','accounts','role','ops_customer') and not action.startswith(('template_','account_deletion_','public_sticker_')):
                return web.json_response({'error':'请先选择一个企业，再查看或管理这项资料。'},status=400)
            enterprise='legacy'
        return await self.enterprise_call(user,enterprise,action,payload)

    async def enterprise_call(self,user,enterprise,action,payload=None):
        writer=None
        try:
            async with asyncio.timeout(28):
                if enterprise not in ('legacy','ww0000000000000002','ww0000000000000003'):
                    return web.json_response({'error':'企业选择无效，请重新选择。'},status=400)
                # Global website roles remain centralized; application data uses isolated sockets.
                path='/bridge/admin.sock' if enterprise=='legacy' or action in ('role','accounts','ops_customer') or action.startswith(('account_deletion_','template_','public_sticker_')) else '/bridge/admin-'+enterprise+'.sock'
                reader,writer=await asyncio.open_unix_connection(path,limit=(2 if action.startswith('template_') else 1)*1024*1024)
                message={'action':action,'session':user['session_hash'],'payload':payload or {}}
                writer.write(json.dumps(message,ensure_ascii=False).encode()+b'\n');await writer.drain()
                result=json.loads(await reader.readline())
                if not result.get('ok'):
                    return web.json_response({'error':result.get('error','管理请求失败。')},status=403 if action=='identity' else 409)
                return web.json_response(result['data'])
        except (OSError,asyncio.TimeoutError,ValueError):
            return web.json_response({'error':'管理服务暂不可用，请稍后重试。'},status=503)
        finally:
            if writer:
                writer.close()
                try:await writer.wait_closed()
                except OSError:pass

    async def all_overview(self,user,payload=None):
        if payload:
            return web.json_response({'error':'总览不接受筛选参数。'},status=400)
        async def read(key):
            try:
                async with asyncio.timeout(OVERVIEW_SCOPE_TIMEOUT):
                    identity=await self.enterprise_call(user,key,'identity')
                    if identity.status!=200:return key,identity
                    return key,await self.enterprise_call(user,key,'ops_dashboard')
            except asyncio.TimeoutError:
                return key,web.json_response({'error':'企业资料读取超时。'},status=503)
        try:
            async with asyncio.timeout(28):
                results=await asyncio.gather(*(read(key) for key in ('legacy','ww0000000000000002','ww0000000000000003')))
        except asyncio.TimeoutError:
            return web.json_response({'error':'企业资料读取超时，请重新读取总览。'},status=503)
        # Never turn denied access into a partially authorized overview.
        if any(response.status==403 for _,response in results):
            return web.json_response({'error':'当前账号没有查看全部企业总览的权限。'},status=403)
        items=[]
        for key,response in results:
            item={'key':key,'available':response.status==200}
            if item['available']:item['data']=json.loads(response.body)
            else:item['error']='企业资料暂不可读，请重新读取。'
            items.append(item)
        if not any(item['available'] for item in items):
            return web.json_response({'error':'企业资料暂不可读，请重新读取总览。'},status=503)
        return web.json_response({'enterprise':'all','enterprises':items,'partial':not all(item['available'] for item in items),'timestamp':time.time()})

    async def operations(self,request):
        return await self.call(request,'ops_'+request.match_info['report'],dict(request.query))

    async def public_sticker_analyze(self,request):
        data=await self.portal.data(request,True)
        self.portal.limit('console-pool-analysis:'+str(self.portal.user(request)['id']),600,600)
        return await self.call(request,'public_sticker_analyze',data)

    async def public_sticker_convert(self,request):
        import base64,hmac
        user=self.portal.user(request)
        if request.headers.get('Origin')!=self.portal.origin or request.headers.get('X-Requested-With')!='xiaoyi-portal':raise web.HTTPForbidden()
        if not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),user['csrf']):raise web.HTTPForbidden()
        if request.content_type!='image/webp':raise web.HTTPUnsupportedMediaType()
        raw=await request.read()
        if not 12<=len(raw)<=800*1024:raise web.HTTPBadRequest(text='WebP需在800 KB以内')
        self.portal.limit('console-pool-convert:'+str(user['id']),240,600)
        return await self.call(request,'public_sticker_convert',{'image':base64.b64encode(raw).decode()})

    async def public_sticker_image(self,request):
        import base64
        response=await self.call(request,'public_sticker_image',dict(request.query))
        if response.status!=200:return response
        data=json.loads(response.body)
        return web.Response(body=base64.b64decode(data['body']),content_type=data['mime'],headers={'Cache-Control':'private, no-store'})

    async def public_sticker_catalog(self,request):
        return await self.call(request,'public_sticker_'+request.match_info['kind'],dict(request.query))

    async def public_stickers(self,request):
        return await self.call(request,'public_sticker_list',dict(request.query))
    async def public_sticker_write(self,request):
        data=await self.portal.data(request,True)
        self.portal.limit('console-public-sticker:'+request.match_info['operation']+':'+str(self.portal.user(request)['id']),600 if request.match_info['operation']=='save' else 30,600 if request.match_info['operation']=='save' else 60)
        return await self.call(request,'public_sticker_'+request.match_info['operation'],data)

    async def templates(self,request):
        return await self.call(request,'template_list')
    async def template_detail(self,request):
        return await self.call(request,'template_detail',dict(request.query))
    async def template_reference_analyze(self,request):
        data=await self.portal.data(request,True)
        self.portal.limit('console-template-reference:'+str(self.portal.user(request)['id']),8,60)
        return await self.call(request,'template_reference_analyze',data)

    async def template_card_parse(self,request):
        data=await self.portal.data(request,True)
        self.portal.limit('console-template-card:'+str(self.portal.user(request)['id']),6,60)
        return await self.call(request,'template_card_parse',data)

    async def template_voice_preview(self,request):
        data=await self.portal.data(request,True)
        self.portal.limit('console-template-voice:'+str(self.portal.user(request)['id']),12,60)
        response=await self.call(request,'template_voice_preview',data)
        response.headers['Cache-Control']='no-store'
        return response

    async def template_write(self,request):
        data=await self.portal.data(request,True)
        self.portal.limit('console-template:'+str(self.portal.user(request)['id']),20,60)
        return await self.call(request,'template_'+request.match_info['operation'],data)

    async def identity(self,request):return await self.call(request,'identity')
    async def overview(self,request):return await self.call(request,'overview')
    async def detail(self,request):
        try:agent=int(request.match_info['agent'])
        except ValueError:return web.json_response({'error':'应用编号无效。'},status=400)
        return await self.call(request,'detail',{'agent':agent})
    async def global_defaults(self,request):
        if set(request.query)-{'refresh_voices'} or request.query.get('refresh_voices','') not in ('','1'):
            return web.json_response({'error':'音色读取参数无效。'},status=400)
        return await self.call(request,'global_defaults',{'refresh_voices':True} if request.query.get('refresh_voices')=='1' else {})
    async def save_global_defaults(self,request):
        data=await self.portal.data(request,True)
        self.portal.limit('console-defaults:'+str(self.portal.user(request)['id']),10,60)
        return await self.call(request,'global_defaults_save',data)

    async def accounts(self,request):return await self.call(request,'accounts',dict(request.query))
    async def account_deletion_list(self,request):return await self.call(request,'account_deletion_list')
    async def account_deletion_write(self,request):
        data=await self.portal.data(request,True)
        self.portal.limit('console-delete:'+str(self.portal.user(request)['id']),20,60)
        operation=request.match_info['operation']
        return await self.call(request,'account_deletion_'+operation,data)

    async def application_deletions(self,request):return await self.call(request,'application_deletions')
    async def jobs(self,request):return await self.call(request,'jobs',dict(request.query))
    async def audit(self,request):return await self.call(request,'audit',dict(request.query))
    async def token_usage(self,request):return await self.call(request,'token_usage',dict(request.query))
    async def shadow_report(self,request):return await self.call(request,'shadow_report',dict(request.query))
    async def shadow_prices(self,request):return await self.call(request,'shadow_prices')
    async def shadow_price_save(self,request):
        data=await self.portal.data(request,True)
        self.portal.limit('console-price:'+str(self.portal.user(request)['id']),20,60)
        return await self.call(request,'shadow_price_save',data)
    async def starcoin_overview(self,request):return await self.call(request,'starcoin_overview')
    async def starcoin_detail(self,request):return await self.call(request,'starcoin_detail',dict(request.query))
    async def wallet_overview(self,request):return await self.call(request,'wallet_overview')
    async def wallet_detail(self,request):return await self.call(request,'wallet_detail',dict(request.query))
    async def wallet_write(self,request):
        data=await self.portal.data(request,True)
        self.portal.limit('console-wallet:'+str(self.portal.user(request)['id']),20,60)
        operation=request.match_info['operation']
        return await self.call(request,'wallet_'+operation,data)
    async def task_attention(self,request):
        data=await self.portal.data(request,True)
        self.portal.limit('console-task-attention:'+str(self.portal.user(request)['id']),30,60)
        return await self.call(request,'task_attention',data)
    async def submit(self,request):
        data=await self.portal.data(request,True)
        self.portal.limit('console-write:'+str(self.portal.user(request)['id']),20,60)
        return await self.call(request,'submit',data)
    async def role(self,request):
        data=await self.portal.data(request,True)
        self.portal.limit('console-role:'+str(self.portal.user(request)['id']),10,60)
        return await self.call(request,'role',data)

def install_console(portal,app):
    console=Console(portal)
    base=Path(__file__).resolve().parent
    async def page(request):return web.Response(body=(base/'console.html').read_bytes(),content_type='text/html',charset='utf8')
    async def asset(request):
        name=request.match_info['name']
        return web.Response(body=(base/name).read_bytes(),content_type='text/css' if name.endswith('.css') else 'application/javascript')
    app.router.add_post('/ai/admin/api/templates/analyze-reference',console.template_reference_analyze)
    app.router.add_post('/ai/admin/api/templates/parse-card',console.template_card_parse)
    app.router.add_post('/ai/admin/api/templates/voice-preview',console.template_voice_preview)
    app.router.add_get('/ai/admin/api/templates',console.templates)
    app.router.add_get('/ai/admin/api/public-stickers',console.public_stickers)
    app.router.add_get('/ai/admin/api/public-stickers/{kind:detail|selection}',console.public_sticker_catalog)
    app.router.add_post('/ai/admin/api/public-stickers/analyze',console.public_sticker_analyze)
    app.router.add_post('/ai/admin/api/public-stickers/convert',console.public_sticker_convert)
    app.router.add_get('/ai/admin/api/public-stickers/image',console.public_sticker_image)
    app.router.add_post('/ai/admin/api/public-stickers/{operation:save|toggle|delete}',console.public_sticker_write)
    app.router.add_get('/ai/admin/api/templates/detail',console.template_detail)
    app.router.add_post('/ai/admin/api/templates/{operation:save|publish|withdraw|default}',console.template_write)
    app.router.add_get('/ai/admin',page)
    app.router.add_get('/ai/admin/',page)
    app.router.add_get('/ai/admin/{name:console.css|console.js}',asset)
    for route,handler in [('application-deletions',console.application_deletions),('global-defaults',console.global_defaults),('identity',console.identity),('apps',console.overview),('accounts',console.accounts),('jobs',console.jobs),('audit',console.audit),('token-usage',console.token_usage),('shadow-report',console.shadow_report),('shadow-prices',console.shadow_prices),('starcoins',console.starcoin_overview),('starcoin-detail',console.starcoin_detail),('wallets',console.wallet_overview),('wallet-detail',console.wallet_detail)]:
        app.router.add_get('/ai/admin/api/'+route,handler)
    app.router.add_get('/ai/admin/api/operations/{report:dashboard|inventory|onboarding|payments|wallets|wallet|business|customer}',console.operations)
    app.router.add_get('/ai/admin/api/apps/{agent:[0-9]+}',console.detail)
    app.router.add_post('/ai/admin/api/global-defaults',console.save_global_defaults)
    app.router.add_post('/ai/admin/api/jobs',console.submit)
    app.router.add_post('/ai/admin/api/task-attention',console.task_attention)
    app.router.add_post('/ai/admin/api/roles',console.role)
    app.router.add_post('/ai/admin/api/shadow-prices',console.shadow_price_save)

    app.router.add_post('/ai/admin/api/wallet/{operation:create|credit|reverse|settle}',console.wallet_write)

    app.router.add_get('/ai/admin/api/account-deletions',console.account_deletion_list)
    app.router.add_post('/ai/admin/api/account-deletions/{operation:preview|submit|retry}',console.account_deletion_write)
