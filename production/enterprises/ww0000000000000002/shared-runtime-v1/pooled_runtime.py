"""Two-application pilot: shared dependencies, separate module globals and stores.

Application credentials never enter the process environment or logs. Each existing
container retains its network identity and supplies its environment over a private
Unix lease. Closing that lease stops its application before another can start.
"""
import asyncio
import builtins
from contextvars import ContextVar
import gc
import hashlib
import json
import logging
import os
from pathlib import Path
import signal
import socket
import struct
import sys
import types
from types import MappingProxyType

from aiohttp import web

LOG = logging.getLogger('shared-runtime')
OWNER = ContextVar('shared_runtime_owner', default=None)
CODE = {}
VERSION = 'shared-runtime-v1-20261103.1'


class ScopedOS(types.ModuleType):
    def __init__(self, environment):
        super().__init__('os')
        self.environ = MappingProxyType(dict(environment))

    def getenv(self, key, default=None):
        return self.environ.get(key, default)

    def __getattr__(self, name):
        if name in ('putenv', 'unsetenv', 'environb', 'getenvb'):
            raise RuntimeError('Process environment mutation is unavailable')
        return getattr(os, name)


class Modules:
    """Use original source with private globals and shared dependency imports."""
    def __init__(self, root, agent, environment):
        self.root = Path(root)
        self.agent = int(agent)
        self.os = ScopedOS(environment)
        self.loaded = {}
        self.prefix = '_xingyu_pool_' + str(agent) + '_'
        self.sys = types.ModuleType('sys')
        self.sys.__dict__.update(sys.__dict__)
        self.sys.path = list(sys.path)

    def importer(self, name, globals=None, locals=None, fromlist=(), level=0):
        if level:
            raise ImportError('Relative application imports are unsupported')
        if name == 'os':
            return self.os
        if name == 'sys':
            return self.sys
        if name == 'pilot_entry' or (self.root / 'runtime' / (name + '.py')).is_file():
            return self.load(name)
        return builtins.__import__(name, globals, locals, fromlist, level)

    def load(self, name):
        if name in self.loaded:
            return self.loaded[name]
        path = self.root / ('pilot_entry.py' if name == 'pilot_entry' else 'runtime/' + name + '.py')
        source = path.read_text('utf8')
        # The only fixed writable path besides DATABASE_FILE is the preview cache.
        source = source.replace("Path('/voice-preview-cache')", 'Path(' + repr(str(self.root / 'voice-preview-cache')) + ')')
        key = hashlib.sha256(source.encode()).hexdigest()
        if key not in CODE:
            if len(CODE) >= 64:
                CODE.clear()
            CODE[key] = compile(source, name + '.py', 'exec')
        module = types.ModuleType(self.prefix + name)
        module.__file__ = str(path)
        module.__dict__['__builtins__'] = dict(vars(builtins), __import__=self.importer)
        self.loaded[name] = module
        sys.modules[module.__name__] = module
        try:
            exec(CODE[key], module.__dict__)
        except BaseException:
            self.close()
            raise
        return module

    def close(self):
        for module in self.loaded.values():
            sys.modules.pop(module.__name__, None)
        self.loaded.clear()


class Tenant:
    def __init__(self, root, agent, corp):
        self.root, self.agent, self.corp = Path(root), int(agent), corp
        self.lock = asyncio.Lock()
        self.tasks = set()
        self.runner = None
        self.modules = None
        self.server = None
        self.ready = False
        self.leases = set()

    @property
    def control_path(self):
        return self.root / 'data/.shared-control.sock'

    @property
    def http_path(self):
        return self.root / 'data/.shared-http.sock'

    def validate(self, environment):
        if not isinstance(environment, dict) or len(environment) > 250:
            raise ValueError('Invalid environment')
        if any(not isinstance(k, str) or not isinstance(v, str) or len(k) > 150 or len(v) > 20000
               for k, v in environment.items()):
            raise ValueError('Invalid environment values')
        if environment.get('WECOM_AGENT_ID') != str(self.agent) or environment.get('WECOM_CORP_ID') != self.corp:
            raise ValueError('Application identity mismatch')
        environment = dict(environment)
        if environment.get('DATABASE_FILE', '/data/kf.sqlite3') != '/data/kf.sqlite3':
            raise ValueError('Unexpected database path')
        environment['DATABASE_FILE'] = str(self.root / 'data/kf.sqlite3')
        return environment

    async def start(self, environment):
        environment = self.validate(environment)
        self.modules = Modules(self.root, self.agent, environment)
        token = OWNER.set(self)
        app = None
        try:
            entry = self.modules.load('pilot_entry')
            app = await entry.main_app()
            service = app['independent_service']
            if service.agent_id != self.agent or service.crypto.corp_id != self.corp:
                raise ValueError('Constructed identity mismatch')

            @web.middleware
            async def scope(request, handler):
                token = OWNER.set(self)
                try:
                    response = await handler(request)
                    if request.path == '/health' and response.status == 200:
                        data = json.loads(response.text)
                        data.update(shared_runtime_version=VERSION, shared_runtime_pid=os.getpid())
                        response = web.json_response(data)
                    return response
                finally:
                    OWNER.reset(token)

            app.middlewares.insert(0, scope)
            self.runner = web.AppRunner(app, access_log=None, shutdown_timeout=12)
            await self.runner.setup()
            self.http_path.unlink(missing_ok=True)
            await web.UnixSite(self.runner, str(self.http_path)).start()
            self.http_path.chmod(0o600)
            self.ready = True
        except BaseException:
            if self.runner is None and app is not None:
                app.freeze()
                await app.cleanup()
            await self.stop()
            raise
        finally:
            OWNER.reset(token)

    async def stop(self):
        self.ready = False
        # Cancel every owned task, including nested gather/upload jobs, before
        # original cleanup closes SQLite connections. Other tenants keep running.
        current = asyncio.current_task()
        pending = [t for t in self.tasks if t is not current and not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self.runner is not None:
            runner, self.runner = self.runner, None
            await runner.cleanup()
        self.http_path.unlink(missing_ok=True)
        if self.modules is not None:
            self.modules.close()
            self.modules = None
        gc.collect()

    async def lease(self, reader, writer):
        current = asyncio.current_task()
        self.leases.add(current)
        acquired = False
        try:
            peer = writer.get_extra_info('socket').getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            if struct.unpack('3i', peer)[1] != os.getuid():
                raise PermissionError('Unexpected peer')
            raw = await asyncio.wait_for(reader.readline(), 10)
            env = self.validate(json.loads(raw))
            # A new instance never replaces an existing live lease.
            if self.lock.locked():
                raise RuntimeError('Application already active')
            await self.lock.acquire()
            acquired = True
            # validate() is intentionally applied only once to the input path.
            env['DATABASE_FILE'] = '/data/kf.sqlite3'
            await self.start(env)
            writer.write(json.dumps({'ready': True, 'agent': self.agent, 'version': VERSION}).encode() + b'\n')
            await writer.drain()
            while True:
                line = await asyncio.wait_for(reader.readline(), 10)
                if line == b'ping\n':
                    writer.write(b'pong\n')
                    await writer.drain()
                elif line in (b'stop\n', b''):
                    break
                else:
                    raise ValueError('Invalid lease command')
        except Exception as exc:
            LOG.warning('Tenant %s lease ended: %s', self.agent, type(exc).__name__)
        finally:
            if acquired:
                try:
                    await self.stop()
                    writer.write(b'stopped\n')
                    await writer.drain()
                except (ConnectionError, BrokenPipeError):
                    pass
                finally:
                    self.lock.release()
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            self.leases.discard(current)

    async def listen(self):
        self.control_path.unlink(missing_ok=True)
        self.server = await asyncio.start_unix_server(self.lease, str(self.control_path), limit=262144)
        self.control_path.chmod(0o600)


def install_task_tracking(loop):
    previous = loop.get_task_factory()

    def factory(loop, coro, context=None):
        owner = context.get(OWNER) if context is not None else OWNER.get()
        task = (previous(loop, coro, context=context) if previous else
                asyncio.Task(coro, loop=loop, context=context))
        if owner is not None:
            owner.tasks.add(task)
            task.add_done_callback(owner.tasks.discard)
        return task

    loop.set_task_factory(factory)
    return previous


async def main():
    plan = json.loads(Path('/shared/plan.json').read_text())
    tenants = [Tenant('/tenants/' + str(a), a, plan['corp']) for a in plan['agents']]
    loop = asyncio.get_running_loop()
    install_task_tracking(loop)
    done = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, done.set)
    for tenant in tenants:
        await tenant.listen()
    await done.wait()
    for tenant in tenants:
        tenant.server.close()
        await tenant.server.wait_closed()
    # Let each lease perform its own serialized cleanup before process exit.
    for tenant in tenants:
        pending = list(tenant.leases)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    asyncio.run(main())
