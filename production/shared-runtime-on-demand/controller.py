"""Host-only controller; no application starts, credentials or user-data writes."""
import argparse
from contextlib import contextmanager, ExitStack
import fcntl
import http.client
import json
import logging
import os
from pathlib import Path
import shlex
import signal
import socket
import subprocess
import time

LOG = logging.getLogger('shared-on-demand')

class UnixHTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect('/var/run/docker.sock')

class Docker:
    def request(self, method, path, missing=False, timeout=40):
        connection = UnixHTTP('localhost', timeout=timeout)
        try:
            connection.request(method, path)
            response = connection.getresponse(); raw = response.read()
            if response.status == 404 and missing:
                return None
            if not 200 <= response.status < 300:
                raise RuntimeError('docker_http_' + str(response.status))
            return json.loads(raw) if raw else {}
        finally:
            connection.close()

    def inventory(self):
        records = self.request('GET', '/containers/json?all=1')
        return {name.lstrip('/'): d for d in records for name in d['Names']}

    def inspect(self, name):
        return self.request('GET', '/containers/' + name + '/json', missing=True)

    def start(self, name):
        self.request('POST', '/containers/' + name + '/start')

    def stop(self, name):
        self.request('POST', '/containers/' + name + '/stop?t=30')

    def remove(self, name):
        self.request('DELETE', '/containers/' + name)

    def create(self, args):
        p = subprocess.run(['docker', *args], capture_output=True, timeout=40)
        if p.returncode:
            raise RuntimeError('worker_create_failed')


@contextmanager
def locked(path, mode='rb'):
    with Path(path).open(mode) as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield stream
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def atomic(path, value):
    path = Path(path); temporary = path.with_suffix(path.suffix + '.ondemand-new')
    temporary.write_text(json.dumps(value, indent=2))
    temporary.chmod(0o600 if path.name == 'container-run.json' else 0o644)
    os.replace(temporary, path)


def busy(record):
    return record is not None and record.get('State') not in ('exited', 'created', 'dead', 'removing')


class Controller:
    def __init__(self, config, docker=None, clock=time.monotonic):
        self.config = config; self.docker = docker or Docker(); self.clock = clock
        self.empty_since = {}; self.retry_at = {}

    def active(self, group, inventory):
        return any(busy(inventory.get(a['name'])) for a in group['apps'])

    def desired(self, group, inventory):
        members = [a for a in group['apps'] if a['name'] in inventory]
        if not members:
            raise RuntimeError('no_remaining_members')
        for app in members:
            d = self.docker.inspect(app['name'])
            if d is None:
                raise RuntimeError('member_changed_during_wake')
            env = dict(s.split('=', 1) for s in d['Config']['Env'] if '=' in s)
            mounts = {m['Destination']: m['Source'] for m in d['Mounts']}
            if (env.get('WECOM_CORP_ID') != group['plan']['corp'] or
                env.get('WECOM_AGENT_ID') != str(app['agent']) or mounts.get('/data') != app['data']):
                raise RuntimeError('member_identity_changed')
        agents = {str(a['agent']) for a in members}
        plan = dict(group['plan'], agents=[a['agent'] for a in members])
        plan['entry_modules'] = {k:v for k,v in plan['entry_modules'].items() if k in agents}
        args = []; source_args = group['args']; i = 0
        health = "from pathlib import Path;assert all((Path('/tenants')/str(a)/'data/.shared-control.sock').is_socket() for a in " + repr(plan['agents']) + ')'
        mounts = {}
        while i < len(source_args):
            key = source_args[i]
            if key == '-v':
                src, target, mode = source_args[i+1].split(':'); i += 2
                if target.startswith('/tenants/') and target.split('/')[2] not in agents:
                    continue
                p = Path(src)
                if not p.exists() or p.is_symlink():
                    raise RuntimeError('missing_or_unsafe_mount_source')
                args += ['--mount', 'type=bind,src=' + src + ',dst=' + target + (',readonly' if mode == 'ro' else '')]
                mounts[target] = src
            elif key == '--health-cmd':
                args += [key, 'python -c ' + shlex.quote(health)]; i += 2
            else:
                args.append(key); i += 1
        return plan, args, mounts

    def wake(self, group, inventory):
        worker = self.docker.inspect(group['name'])
        if worker and (worker['State']['Running'] or worker['State'].get('Restarting')):
            return False
        plan, args, mounts = self.desired(group, inventory)
        if worker:
            if worker['Config'].get('Labels', {}).get('xingyu.shared-runtime') != 'fleet10':
                raise RuntimeError('unexpected_worker_identity')
            current = {m['Destination']:m['Source'] for m in worker['Mounts']}
            if current != mounts or group.get('rebuild_on_wake', False):
                self.docker.remove(group['name']); worker = None
        atomic(Path(group['folder'])/'plan.json', plan)
        atomic(Path(group['folder'])/'container-run.json', args)
        if worker:
            self.docker.start(group['name'])
        else:
            self.docker.create(args)
        LOG.info('worker_started name=%s members=%s', group['name'], len(plan['agents']))
        return True

    def reconcile(self):
        inventory = self.docker.inventory()
        for group in self.config['groups']:
            name = group['name']; now = self.clock()
            try:
                if self.active(group, inventory):
                    self.empty_since.pop(name, None)
                    if now >= self.retry_at.get(name, 0):
                        self.wake(group, inventory)
                        self.retry_at.pop(name, None)
                    continue
                start = self.empty_since.setdefault(name, now)
                if now - start < self.config.get('idle_seconds', 30) or not busy(inventory.get(name)):
                    continue
                # All controlled starts hold operation.lock. Client script inode is
                # also held shared for its lifetime, covering raw Docker starts.
                with ExitStack() as stack:
                    stack.enter_context(locked(group['operation_lock'], 'ab'))
                    for app in group['apps']:
                        p = Path(app['data'])/'.shared-runtime-client.py'
                        if p.exists():
                            stack.enter_context(locked(p))
                    fresh = self.docker.inventory()
                    if self.active(group, fresh):
                        self.empty_since.pop(name, None)
                        continue
                    self.docker.stop(name)
                    LOG.info('worker_stopped name=%s reason=all_entries_closed', name)
                fresh = self.docker.inventory()
                if self.active(group, fresh):
                    self.wake(group, fresh)
            except BlockingIOError:
                pass
            except Exception as exc:
                self.retry_at[name] = now + 5
                LOG.error('reconcile_failed name=%s error=%s', name, type(exc).__name__)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='/home/ubuntu/wecom-kf/shared-runtime-on-demand/config.json')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args(); config = json.loads(Path(args.config).read_text())
    with locked(config['controller_lock'], 'ab'):
        controller = Controller(config)
        import threading
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        while not stop.is_set():
            try:
                controller.reconcile()
            except Exception as exc:
                LOG.error('inventory_failed error=%s', type(exc).__name__)
            if args.once:
                break
            stop.wait(config.get('poll_seconds', 2))

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    main()
