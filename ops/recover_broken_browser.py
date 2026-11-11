"""Restart the exact existing broken lab browser after its clients stop."""
from __future__ import annotations

import argparse
import copy
import hashlib
import os
import re
import json
from pathlib import Path
import subprocess
import time

from switch_reader_image import command, Engine


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-browser', required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--candidate-image')
    args = parser.parse_args()
    os.umask(0o077)
    if command('docker', 'info', '--format', '{{.Name}}') != 'booksys-host':
        raise SystemExit('Wrong Docker target')
    name = 'booksys-byparr'
    before = json.loads(command('docker', 'inspect', name))[0]
    if before['Id'] != args.expected_browser or not before['State']['Running']:
        raise SystemExit('Browser target differs')
    for role in ('controller', 'discovery', 'collection'):
        if json.loads(command('docker', 'inspect', 'booksys-'+role))[0]['State']['Running']:
            raise SystemExit('Stop browser clients before recovery')
    health = command('docker', 'exec', name, 'curl', '--silent', '--max-time', '5',
                     'http://127.0.0.1:8191/health')
    if json.loads(health) != {'detail': 'Browser cleanup failed; restart service'}:
        raise SystemExit('Browser does not report the selected recoverable failure')
    candidate = args.candidate_image
    env = Path('/data/booksys/stack/.env')
    pending = env.with_name('.env.browser-runtime-fixes')
    recovery = name+'-runtime-fixes-recovery'
    updated = None
    config_hash = None
    selected_id = before['Id']
    if candidate:
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', candidate):
            raise SystemExit('Candidate image identity is invalid')
        command('docker', 'image', 'inspect', candidate, '--format', '{{.Id}}')
        expected = hashlib.sha256((Path(__file__).parent/'byparr/session_api.py').read_bytes()).hexdigest()
        observed = command('docker', 'run', '--rm', '--network=none', '--read-only',
                           '--entrypoint=sha256sum', candidate, '/app/session_api.py').split()[0]
        if expected != observed:
            raise SystemExit('Candidate browser source differs')
        raw = env.read_bytes()
        pattern = rb'(?m)^BOOKSYS_BYPARR_IMAGE=(.*)$'
        pins = re.findall(pattern, raw)
        if len(pins) != 1 or command('docker', 'image', 'inspect', pins[0].decode(), '--format', '{{.Id}}') != before['Image']:
            raise SystemExit('Installed browser pin differs')
        updated = re.sub(pattern, b'BOOKSYS_BYPARR_IMAGE='+candidate.encode(), raw)
        if pending.exists() or pending.is_symlink() or command('docker', 'ps', '-a', '--filter', 'name=^/'+recovery+'$', '--format', '{{.ID}}'):
            raise SystemExit('Browser recovery target exists')
        config_hash = subprocess.check_output(['docker', '--host', 'unix:///var/run/docker.sock', 'compose',
            '-f', '/data/booksys/stack/compose.yaml', 'config', '--hash', 'byparr'],
            text=True, env=dict(os.environ, BOOKSYS_BYPARR_IMAGE=candidate)).split()[-1]
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    (args.evidence/'before.json').write_text(json.dumps(before, indent=2))
    logs = subprocess.run(['docker', '--host', 'unix:///var/run/docker.sock', 'logs', name],
                          capture_output=True, check=True)
    (args.evidence/'stdout.log').write_bytes(logs.stdout)
    (args.evidence/'stderr.log').write_bytes(logs.stderr)
    if candidate:
        (args.evidence/'env-before').write_bytes(env.read_bytes())
        command('docker', 'stop', '--time', '30', args.expected_browser)
        if json.loads(command('docker', 'inspect', args.expected_browser))[0]['State']['Running']:
            raise SystemExit('Browser stop not proved')
        config = copy.deepcopy(before['Config'])
        config['Image'] = candidate
        config['Labels']['com.docker.compose.config-hash'] = config_hash
        networks = {key: {'Aliases': value.get('Aliases') or []}
                    for key, value in before['NetworkSettings']['Networks'].items()}
        command('docker', 'rename', name, recovery)
        engine = Engine('localhost')
        engine.request('POST', '/containers/create?name='+name,
                       json.dumps({**config, 'HostConfig': before['HostConfig'],
                                   'NetworkingConfig': {'EndpointsConfig': networks}}),
                       {'Content-Type': 'application/json'})
        response = engine.getresponse()
        result = json.loads(response.read())
        engine.close()
        (args.evidence/'create.json').write_text(json.dumps(result))
        if response.status != 201:
            raise SystemExit('Browser create failed; retain clients stopped and reconcile')
        selected_id = result['Id']
        if not isinstance(selected_id, str) or not re.fullmatch('[0-9a-f]{64}', selected_id):
            raise SystemExit('Browser create returned an invalid identity; retain clients stopped')
        assert updated is not None
        with pending.open('xb') as stream:
            os.fchmod(stream.fileno(), env.stat().st_mode & 0o777)
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        pending.replace(env)
        command('docker', 'start', selected_id)
    else:
        command('docker', 'restart', '--time', '30', args.expected_browser)
    deadline = time.monotonic()+60
    while True:
        after = json.loads(command('docker', 'inspect', selected_id))[0]
        if after['Id'] != selected_id or after['Name'] != '/'+name or after['Image'] != (candidate or before['Image']):
            raise SystemExit('Browser identity drift after restart')
        if after['State']['Health']['Status'] == 'healthy':
            break
        if not after['State']['Running'] or time.monotonic() >= deadline:
            raise SystemExit('Browser recovery not proved; retain clients stopped')
        time.sleep(2)
    (args.evidence/'after.json').write_text(json.dumps(after, indent=2))
    print('Selected browser healthy; prior image, configuration and proof retained')


if __name__ == '__main__':
    main()
