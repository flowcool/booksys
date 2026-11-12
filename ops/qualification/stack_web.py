"""Two-container web/controller qualification with synthetic local state only."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from library.catalog import Catalog, empty
from library.primitives import atomic_yaml
from library.migration import import_catalogue, activate_catalogue, create_activity
from library.stage_tasks import StageTasks
from library.source_tasks import SourcePolicy
from library.stage_resources import Resources, Demand
from library.authority import code_digest


def run(*args: str) -> str:
    return subprocess.check_output(args, text=True).strip()


def qualify(image: str, evidence: Path) -> None:
    evidence.mkdir(parents=True, mode=0o700, exist_ok=False)
    observed = run('docker', 'run', '--rm', '--network=none', '--read-only', '--cap-drop=ALL',
        image, '-c', 'from library.authority import code_digest; print(code_digest())')
    if observed != code_digest():
        raise RuntimeError('Image and fixture application digests differ; rebuild before qualification')
    # Do not mount any live Booksys state or credentials. This fixture has no
    # sources, hoster URLs or queued tasks and cannot submit downloads.
    root = evidence/'fixture'
    for path in (root/'state/activity', root/'evidence', evidence/'ipc', evidence/'collector', evidence/'reception'):
        path.mkdir(parents=True, mode=0o700)
    atomic_yaml(root/'state/library.yaml', empty())
    store = import_catalogue(root/'state', empty(), authority='compose-web-synthetic',
        source_digest=hashlib.sha256((root/'state/library.yaml').read_bytes()).hexdigest())
    activate_catalogue(root/'state', authority=store.authority)
    StageTasks.upgrade(store)
    SourcePolicy.create(root/'state/task-policy.yaml')
    Resources.create(root/'state/stage-resources.yaml',
        capacity=Demand(memory_mib=2048, cpu_millis=2000, disk=1, browser=1, native=1))
    create_activity(root/'state/activity', authority='compose-web-synthetic', timestamp=time.time())
    credentials = evidence/'synthetic.json'
    credentials.write_text('{"api_key":"synthetic-not-a-credential"}')
    credentials.chmod(0o600)
    controllers = []
    results = {}
    prefix = 'a-booksys-lql88-web-'+evidence.name[-12:]
    common = ['--network=none', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
              '--user=1000:1000', '--memory=768m', '--memory-swap=768m', '--cpus=.5', '--pids-limit=48',
              '--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=16m']
    # This controller exercises the existing service in observation-only mode.
    # Its activation is not a claim of full queue/cgroup qualification.
    script = '''
from pathlib import Path
import time
from library.api import PublicAPI
from library.compose_transport import UnixController
api=PublicAPI(Path('/fixture'), host='127.0.0.1:8768', reception=Path('/reception'))
with UnixController(Path('/ipc/controller.sock'), api.invoke, validate=api.validate):
    while True: time.sleep(1)
'''
    controller = run('docker', 'create', '--name', prefix+'-controller', *common,
        '--mount', f'type=bind,src={root},dst=/fixture',
        '--mount', f'type=bind,src={evidence/"reception"},dst=/reception,readonly',
        '--mount', f'type=bind,src={evidence/"ipc"},dst=/ipc', image, '-c', script)
    controllers.append(controller)
    # Web network stays isolated; the probe joins its network namespace through
    # docker exec. It has only the private IPC bind, no canonical storage.
    web = run('docker', 'create', '--name', prefix+'-web', *common,
        '--mount', f'type=bind,src={evidence/"ipc"},dst=/ipc,readonly', image,
        '-m', 'library.compose_web', '--public-host', '127.0.0.1:8768', '--socket', '/ipc/controller.sock')
    controllers.append(web)
    probe = '''
import http.client,json
out={}
for route in ('/', '/api/resolution/token','/api/library','/api/activity'):
 c=http.client.HTTPConnection('127.0.0.1',8768,timeout=3);c.request('GET',route)
 r=c.getresponse();out[route]=r.status;r.read();c.close()
print(json.dumps(out))
'''
    try:
        run('docker', 'start', controller, web)
        for _ in range(40):
            if (evidence/'ipc/controller.sock').exists():
                break
            time.sleep(.1)
        for _ in range(40):
            try:
                results['connected'] = json.loads(run('docker', 'exec', web, '/app/.venv/bin/python', '-c', probe))
                break
            except subprocess.CalledProcessError:
                time.sleep(.1)
        if any(code != 200 for code in results['connected'].values()):
            raise RuntimeError('Connected public routes failed')
        run('docker', 'stop', '--time', '2', controller)
        results['controller_down'] = json.loads(run('docker', 'exec', web, '/app/.venv/bin/python', '-c', probe))
        if results['controller_down']['/'] != 200 or results['controller_down']['/api/library'] != 503:
            raise RuntimeError('Web did not preserve its failure boundary')
        run('docker', 'start', controller)
        for _ in range(40):
            result = json.loads(run('docker', 'exec', web, '/app/.venv/bin/python', '-c', probe))
            if all(code == 200 for code in result.values()):
                results['reconnected'] = result
                break
            time.sleep(.1)
        if 'reconnected' not in results:
            raise RuntimeError('Web reconnection failed')
        inspect = json.loads(run('docker', 'inspect', web))[0]
        results['web_mounts'] = inspect['Mounts']
        if len(inspect['Mounts']) != 1 or inspect['Mounts'][0]['Destination'] != '/ipc' or inspect['Mounts'][0]['RW']:
            raise RuntimeError('Unexpected web privilege')
        results.update(result='PASS', image=image, application_sha256=code_digest(),
                       limits='Two-container public API/IPC qualification only; no queues, NAS or migration activation')
    finally:
        for container in controllers:
            subprocess.run(['docker', 'stop', '--time', '2', container], capture_output=True, timeout=15)
            (evidence/(container[:12]+'.inspect.json')).write_text(run('docker', 'inspect', container))
            logs = subprocess.run(['docker', 'logs', container], capture_output=True, text=True)
            (evidence/(container[:12]+'.log')).write_text(logs.stdout+logs.stderr)
            if run('docker', 'inspect', '--format', '{{.State.Running}}', container) != 'false':
                raise RuntimeError('Owned container stop is unproved')
        (evidence/'result.json').write_text(json.dumps(results, indent=2)+'\n')
    print('PASS: typed public routes, controller loss and reconnection; owned containers stopped and retained')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--evidence', required=True, type=Path)
    args = parser.parse_args()
    qualify(args.image, args.evidence)
