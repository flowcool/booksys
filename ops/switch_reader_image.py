"""Switch the lab reader pin while retaining the stopped prior launcher."""
from __future__ import annotations

import argparse
import http.client
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from library.postgres import Database


def command(*args: str) -> str:
    if args[0] == 'docker':
        args = ('docker', '--host', 'unix:///var/run/docker.sock', *args[1:])
    return subprocess.check_output(args, text=True).strip()


class Engine(http.client.HTTPConnection):
    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(30)
        self.sock.connect('/var/run/docker.sock')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-launcher', required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    if command('docker', 'info', '--format', '{{.Name}}') != 'booksys-host':
        raise SystemExit('Wrong Docker target')
    name = 'booksys-reader-launcher'
    previous = json.loads(command('docker', 'inspect', name))[0]
    if previous['Id'] != args.expected_launcher or not re.fullmatch('sha256:[0-9a-f]{64}', args.image):
        raise SystemExit('Launcher or image identity differs')
    if command('docker', 'image', 'inspect', args.image, '--format', '{{.Id}}') != args.image:
        raise SystemExit('Reader image is unavailable')
    for client in ('intake', 'processing', 'copies'):
        if json.loads(command('docker', 'inspect', 'booksys-'+client))[0]['State']['Running']:
            raise SystemExit('Stop reader clients before switching')
    with Database.configured(Path('/data/booksys/library-lab/state')).connection() as db:
        if db.execute("SELECT 1 FROM stage_tasks WHERE status='active' AND stage IN ('intake','ocr','copies') LIMIT 1").fetchone():
            raise SystemExit('Reader task is still active; reconcile before switching')
    running = command('docker', 'ps', '--format', '{{.Names}}').splitlines()
    if any(re.fullmatch('booksys-reader-[0-9a-f]{32}', value) for value in running):
        raise SystemExit('Reader job is still running')
    config = previous['Config']
    index = config['Cmd'].index('--image') + 1
    old = config['Cmd'][index]
    env = Path('/data/booksys/stack/.env')
    before = env.read_bytes()
    old_line = ('BOOKSYS_READER_IMAGE='+old).encode()
    if before.splitlines().count(old_line) != 1:
        raise SystemExit('Installed reader pin differs')
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    (args.evidence/'launcher-before.json').write_text(json.dumps(previous, indent=2))
    (args.evidence/'env-before').write_bytes(before)
    log = subprocess.run(['docker', '--host', 'unix:///var/run/docker.sock', 'logs', name], capture_output=True, check=True)
    (args.evidence/'launcher.stdout').write_bytes(log.stdout)
    (args.evidence/'launcher.stderr').write_bytes(log.stderr)
    recovery = name+'-recovery-2v9ed'
    command('docker', 'stop', '--time', '30', name)
    if json.loads(command('docker', 'inspect', name))[0]['State']['Running']:
        raise SystemExit('Previous launcher stop is unproved')
    if any(re.fullmatch('booksys-reader-[0-9a-f]{32}', value) for value in
           command('docker', 'ps', '--format', '{{.Names}}').splitlines()):
        raise SystemExit('Reader appeared during stop; retain launcher and reconcile')
    command('docker', 'rename', name, recovery)
    after = before.replace(old_line, ('BOOKSYS_READER_IMAGE='+args.image).encode(), 1)
    pending = env.with_name('.env.reader-2v9ed')
    with pending.open('xb') as stream:
        os.fchmod(stream.fileno(), env.stat().st_mode & 0o777)
        stream.write(after)
        stream.flush()
        os.fsync(stream.fileno())
    pending.replace(env)
    config['Cmd'][index] = args.image
    config['Labels']['com.docker.compose.config-hash'] = command(
        'docker', 'compose', '-f', '/data/booksys/stack/compose.yaml', 'config', '--hash', 'reader-launcher').split()[-1]
    body = json.dumps({**config, 'HostConfig': previous['HostConfig']})
    engine = Engine('localhost')
    engine.request('POST', '/containers/create?name='+name, body, {'Content-Type': 'application/json'})
    response = engine.getresponse()
    result = json.loads(response.read())
    engine.close()
    (args.evidence/'create-result.json').write_text(json.dumps(result))
    if response.status != 201:
        raise SystemExit('Create failed; retain recovery and reconcile before resuming clients')
    command('docker', 'start', name)
    observed = json.loads(command('docker', 'inspect', name))[0]
    if not observed['State']['Running'] or observed['Config']['Cmd'][index] != args.image:
        raise SystemExit('New launcher readback failed; keep clients stopped')
    (args.evidence/'launcher-after.json').write_text(json.dumps(observed, indent=2))
    print(json.dumps({'launcher': observed['Id'], 'reader': args.image, 'retained': recovery}))


if __name__ == '__main__':
    main()
