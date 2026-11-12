"""Start/stop exact retained PostgreSQL Booksys containers. No create or remove.

Installed under the operation directory and invoked by systemd as flow. This
stdlib-only launcher needs no temporary Python environment or database secret.
"""
from __future__ import annotations
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time

OP = Path(os.environ.get('BOOKSYS_OPERATION_ROOT', '/data/booksys/runtime'))


def run(*args: str) -> str:
    return subprocess.check_output(args, text=True, stderr=subprocess.PIPE).strip()



def wait_ready(role: str, container: str, startup_deadline: float) -> None:
    """Serialize startup audits; socket presence alone is not readiness."""
    deadline = min(startup_deadline, time.monotonic() + 120)
    while time.monotonic() < deadline:
        row = json.loads(run('docker', 'inspect', container))[0]
        test = row['Config'].get('Healthcheck',{}).get('Test',[])
        if not test or test[0] != 'CMD':
            raise SystemExit('Expected explicit readiness command missing: '+role)
        argv = ['docker','exec',container,*test[1:]]
        try:
            result = subprocess.run(argv,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10)
            if result.returncode == 0:
                return
        except subprocess.TimeoutExpired:
            pass
        if not row['State']['Running']:
            raise SystemExit('Selected role stopped before readiness: '+role)
        time.sleep(1)
    raise SystemExit('Selected role readiness deadline: '+role)


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in ('start', 'stop', 'status'):
        raise SystemExit('Select start, stop or status')
    runtime = json.loads((OP / 'runtime.json').read_text())
    all_rows = runtime['containers']
    network = json.loads(run('docker', 'network', 'inspect', os.environ.get('BOOKSYS_NETWORK', 'booksys_default')))[0]
    if network['Id'] != runtime['network_id']:
        raise SystemExit('Retained network differs')
    observed = json.loads(run('docker', 'inspect', *[x['id'] for x in all_rows]))
    for expected, actual in zip(all_rows, observed):
        if (actual['Id'], actual['Image'], actual['Name'].lstrip('/'), actual['Mounts']) != (
                expected['id'], expected['image'], expected['name'], expected['mounts']):
            raise SystemExit('Retained runtime identity differs')
    roles = {x['role']: x['id'] for x in all_rows}
    order = ('postgres', 'byparr', 'pyload', 'reader-launcher', 'controller',
             'discovery', 'collection', 'intake', 'processing', 'copies', 'routing', 'retirement', 'web')
    if set(roles) != set(order):
        raise SystemExit('Runtime roles differ')
    if sys.argv[1] == 'start':
        authority = runtime['authority']
        if hashlib.sha256(Path('/data/booksys/library-lab/state/storage-authority.yaml').read_bytes()).hexdigest() != authority['storage_selector_sha256']:
            raise SystemExit('Selected authority changed')
        selector = json.loads(Path('/data/booksys/library-lab/state/postgresql.json').read_text())
        if selector != {'schema': 'booksys', 'dsn_file': str(OP / 'secrets/app.dsn')}:
            raise SystemExit('Selected PostgreSQL configuration differs')
        startup_deadline = time.monotonic() + 285
        for role in order:
            run('docker', 'start', roles[role])
            if role == 'postgres':
                deadline = min(startup_deadline, time.monotonic() + 60)
                while time.monotonic() < deadline:
                    row = json.loads(run('docker', 'inspect', roles[role]))[0]
                    if row['State'].get('Health', {}).get('Status') == 'healthy':
                        observed_identity = run('docker', 'exec', roles[role], 'sh', '-c',
                            'export PGPASSWORD="$(cat /secrets/postgres-password)"; '
                            'psql -X --no-password -U booksys_migration -d booksys -At '
                            '-c "SELECT system_identifier::text || chr(58) || '
                            '(SELECT oid::text FROM pg_database WHERE datname=current_database()) '
                            'FROM pg_control_system()"')
                        if observed_identity != authority['target']['system_identifier'] + ':' + str(authority['target']['database_oid']):
                            raise SystemExit('PostgreSQL system identity changed')
                        break
                    time.sleep(1)
                else:
                    raise SystemExit('PostgreSQL authenticated readiness missing')
            else:
                wait_ready(role, roles[role], startup_deadline)
    elif sys.argv[1] == 'stop':
        for role in reversed(order):
            run('docker', 'stop', '--time', '60', roles[role])
        remaining = run('docker', 'ps', '-q')
        if remaining:
            for row in json.loads(run('docker', 'inspect', *remaining.splitlines())):
                if row['Name'].startswith('/booksys-reader-'):
                    raise SystemExit('Owned reader remains; retain and reconcile before state operations')
    print(json.dumps({'action': sys.argv[1], 'retained_ids': len(roles), 'project': runtime['project']}))


if __name__ == '__main__': main()
