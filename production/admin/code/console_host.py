"""Private Unix socket. Only root and portal UID can connect; live session+role checked per call."""
import argparse
import json
import os
from pathlib import Path
import socket
import socketserver
import struct
from admin_ops import AdminOps, Rejected

def serve(root, path, uid):
    os.umask(0o077)
    ops=AdminOps(root)
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.request.settimeout(30)
            peer=struct.unpack('3i',self.request.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))[1]
            if peer not in (0,uid):return
            try:
                raw=self.rfile.readline(1200001)
                if len(raw)>1200000 or not raw.endswith(b'\n'):raise Rejected('请求过长。')
                message=json.loads(raw)
                if len(raw)>800000 and message.get('action')!='public_sticker_convert':raise Rejected('请求过长。')
                if len(raw)>16384 and message.get('action') not in ('template_save','template_reference_analyze','public_sticker_save','public_sticker_analyze','public_sticker_convert'):raise Rejected('请求过长。')
                result={'ok':True,'data':ops.call(message)}
            except Rejected as exc:result={'ok':False,'error':str(exc)}
            except Exception:result={'ok':False,'error':'管理服务暂不可用，请稍后重试。'}
            try:self.wfile.write(json.dumps(result,ensure_ascii=False).encode()+b'\n')
            except OSError:pass
    class Server(socketserver.ThreadingUnixStreamServer):
        daemon_threads=True
        request_queue_size=16
    target=Path(path)
    if target.exists():
        if not target.is_socket():raise RuntimeError('Refusing to replace non-socket')
        target.unlink()
    with Server(str(target),Handler) as server:
        os.chown(target,0,uid);os.chmod(target,0o660)
        server.serve_forever()

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',default='/home/ubuntu/wecom-kf')
    parser.add_argument('--socket',default='/run/xiaoyi-portal/admin.sock')
    parser.add_argument('--uid',type=int,default=10001)
    args=parser.parse_args();serve(args.root,args.socket,args.uid)
