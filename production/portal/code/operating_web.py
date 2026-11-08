"""Retained recovery UI: website session + CSRF, no browser-provided identity."""
from pathlib import Path
from aiohttp import web
def install(portal,app):
 base=Path(__file__).resolve().parent
 async def page(request):
  portal.user(request)
  return web.Response(text=(base/'operating.html').read_text('utf8'),content_type='text/html')
 async def asset(request):
  name=request.match_info['asset']
  return web.Response(body=(base/('operating.'+name)).read_bytes(),content_type='text/css' if name=='css' else 'application/javascript')
 async def api(request):
  user=portal.user(request)
  if request.method=='GET':result=await portal.bridge.call('operating-status',subject=user['subject'])
  else:
   data=await portal.data(request,True)
   action=data.pop('action',None)
   if action not in ('operating-claim','operating-pay','operating-recover'):raise web.HTTPBadRequest()
   if 'subject' in data:raise web.HTTPBadRequest()
   result=await portal.bridge.call(action,subject=user['subject'],**data)
  return web.json_response(result)
 app.router.add_get('/ai/retained/',page)
 app.router.add_get('/ai/retained/operating.{asset:css|js}',asset)
 app.router.add_get('/ai/api/operating',api)
 app.router.add_post('/ai/api/operating',api)
