"""Local static preview. Denies writes and never forwards requests to production."""
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json,mimetypes
from urllib.parse import urlsplit
ROOT=Path(__file__).resolve().parents[1]/'production/portal/code'
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def send(self,status,raw,content_type='application/json; charset=utf-8'):
        self.send_response(status);self.send_header('Content-Type',content_type)
        self.send_header('Cache-Control','no-store')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'self'")
        self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    def do_POST(self):self.send(403,b'{"error":"Read-only local preview"}')
    do_PUT=do_PATCH=do_DELETE=do_POST
    def do_GET(self):
        path=urlsplit(self.path).path
        if path=='/favicon.ico':self.send(204,b'','image/x-icon');return
        if path=='/ai/api/me':
            register=urlsplit(self.headers.get('Referer','')).path.startswith('/ai/register')
            body={'authenticated':True,'csrf':'preview-only','state':'onboarding','onboarding':{'id':'local-guide-demo','state':'provisioning'},'application':{'name':'示例伙伴'}} if register else {'authenticated':False}
            self.send(200,json.dumps(body,ensure_ascii=False).encode('utf8'));return
        if path=='/ai/admin/api/identity':self.send(200,b'null');return
        if '/api/' in path or path.startswith('/ai/mini/'):
            self.send(403,b'{"error":"No real account or service in local preview"}');return
        name={'/':'site/index.html','/ai/':'site/index.html','/ai':'site/index.html','/ai/help':'site/help/index.html','/ai/help/':'site/help/index.html','/ai/account':'account.html','/ai/account/':'account.html','/ai/register':'register.html','/ai/register/':'register.html','/ai/admin/':'console.html'}.get(path)
        if name is None:
            if path.startswith('/ai/site/'):name='site/'+path.removeprefix('/ai/site/')
            elif path.startswith('/ai/'):name=path.removeprefix('/ai/')
            else:self.send(404,b'{}');return
        file=(ROOT/name).resolve()
        allowed={'.html','.js','.css','.png','.webp','.jpg','.jpeg','.svg','.woff','.woff2','.txt'}
        if not file.is_relative_to(ROOT.resolve()) or not file.is_file() or file.suffix not in allowed:
            self.send(404,b'{}');return
        ctype={'.js':'application/javascript','.css':'text/css','.woff2':'font/woff2','.webp':'image/webp'}.get(file.suffix,mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
        self.send(200,file.read_bytes(),ctype)
if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=18874);args=p.parse_args()
    print(f'Local preview: http://127.0.0.1:{args.port}/ai/ ; guide: /ai/register',flush=True)
    ThreadingHTTPServer(('127.0.0.1',args.port),Handler).serve_forever()
