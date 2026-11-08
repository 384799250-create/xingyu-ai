"""Return the pilot to standalone operation. Financial/chat DBs are never restored."""
import argparse
from contextlib import ExitStack, closing
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import time

ROOT = Path('/home/ubuntu/wecom-kf')
SCOPE = ROOT / 'enterprises/ww0000000000000002'
SHARED = SCOPE / 'shared-runtime-v1'
AGENTS = (1000005, 1000006)
SHA = lambda raw: hashlib.sha256(raw).hexdigest()


def command(args):
    r = subprocess.run(args, capture_output=True, text=True, timeout=40)
    if r.returncode:
        raise RuntimeError('Command failed: ' + args[0])
    return r.stdout


def main(execute):
    record = json.loads((SHARED / 'deployment.json').read_text())
    plan = json.loads((SHARED / 'plan.json').read_text())
    with ExitStack() as locks:
        for root in (ROOT, SCOPE):
            lock = locks.enter_context((root / 'app-pool/operation.lock').open('a'))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for agent in AGENTS:
            root = SCOPE / 'apps' / str(agent)
            assert SHA((root / 'pilot_entry.py').read_bytes()) == plan['patches'][str(agent)]['new'], 'source_drift'
            with closing(sqlite3.connect((root / 'data/kf.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
                for table in ('member_inbox', 'suite_outbox', 'member_greeting_outbox'):
                    assert not db.execute('SELECT 1 FROM ' + table + " WHERE status IN ('pending','working','sending','processing') LIMIT 1").fetchone(), 'application_busy'
            with closing(sqlite3.connect((root / 'data/token-usage.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
                for table in ('token_requests', 'media_requests'):
                    assert not db.execute('SELECT 1 FROM ' + table + " WHERE status NOT IN ('completed','failed','cancelled','unknown','rejected') LIMIT 1").fetchone(), 'provider_request_unsettled'
        if not execute:
            print(json.dumps({'ready': True, 'execute': False, 'database_restore': False}))
            return
        for agent in AGENTS:
            command(['docker', 'stop', '-t', '30', 'xingyu-ww0000000000000002-' + str(agent)])
        command(['docker', 'stop', '-t', '30', 'xingyu-shared-runtime-pilot'])
        for agent in AGENTS:
            root = SCOPE / 'apps' / str(agent)
            original = (Path(record['backup']) / str(agent) / 'pilot_entry.py').read_bytes()
            assert SHA(original) == plan['patches'][str(agent)]['old']
            with (root / 'pilot_entry.py').open('wb') as stream:
                stream.write(original)
            manifest = json.loads((root / 'manifest.json').read_text())
            manifest['runtime']['pilot_entry.py'] = plan['patches'][str(agent)]['old']
            manifest.pop('shared_runtime_version', None)
            (root / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
            (root / 'data/.shared-runtime-client.py').unlink(missing_ok=True)
            command(['docker', 'start', 'xingyu-ww0000000000000002-' + str(agent)])
        for agent in AGENTS:
            probe = "import json,urllib.request;h=json.load(urllib.request.urlopen('http://127.0.0.1:8088/health',timeout=3));assert h['ok'] and h['agent_id']==" + str(agent) + " and 'shared_runtime_version' not in h"
            for attempt in range(30):
                try:
                    command(['docker', 'exec', 'xingyu-ww0000000000000002-' + str(agent), 'python', '-B', '-c', probe])
                    break
                except RuntimeError:
                    if attempt == 29:
                        raise
                    time.sleep(.5)
        (SHARED / 'rollback-result.json').write_text(json.dumps({'at': time.time(), 'standalone': True, 'database_restore': False}))
        print(json.dumps({'standalone': True, 'database_restore': False}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true')
    main(parser.parse_args().execute)
