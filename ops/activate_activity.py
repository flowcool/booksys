"""Activate Activity after the runtime owner releases the exact lab slot.

Graceful library shutdown is required before --apply. On any failed write stop
and inspect evidence. Rollback restores code/unit/images, never business state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
UNIT = Path.home() / '.config/systemd/user/booksys-library.service'


def run(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True, check=True).stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker-image', required=True)
    parser.add_argument('--reader-image', required=True)
    parser.add_argument('--expected-worker', required=True)
    parser.add_argument('--expected-unit-sha256', required=True)
    parser.add_argument('--evidence', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    if run('hostname') != 'booksys-host' or run('docker', 'context', 'show') != 'default':
        raise RuntimeError('Unexpected host/control plane')
    if run('git', '-C', str(ROOT), 'status', '--porcelain'):
        raise RuntimeError('Commit the complete candidate first')
    if hashlib.sha256(UNIT.read_bytes()).hexdigest() != args.expected_unit_sha256:
        raise RuntimeError('Installed unit drift')
    containers: list[dict[str, Any]] = json.loads(run('docker', 'inspect', 'booksys-worker-1', 'booksys-byparr-1', 'booksys-pyload'))
    previous = {c['Name']: {'id': c['Id'], 'image': c['Image']} for c in containers}
    if previous['/booksys-worker-1']['id'] != args.expected_worker:
        raise RuntimeError('Worker target drift')
    worker_image = json.loads(run('docker', 'image', 'inspect', args.worker_image))[0]['Id']
    reader_image = json.loads(run('docker', 'image', 'inspect', args.reader_image))[0]['Id']
    from activity import read_yaml
    history = read_yaml(Path('/data/booksys/state/package-retirement.yaml'))
    if not isinstance(history.get('acquisitions'), dict):
        raise RuntimeError('Retirement history unavailable')
    if any(v.get('phase') != 'retired' for v in history['acquisitions'].values()):
        raise RuntimeError('Retirement intent is not terminal; coordinate owning session')
    proof = {'commit': run('git', '-C', str(ROOT), 'rev-parse', 'HEAD'),
             'worktree': str(ROOT), 'before': previous, 'worker_image': worker_image, 'reader_image': reader_image}
    print(json.dumps(proof), flush=True)
    if not args.apply:
        return
    active = subprocess.run(['systemctl', '--user', 'is-active', 'booksys-library.service'], capture_output=True, text=True)
    if active.stdout.strip() not in ('inactive', 'failed'):
        raise RuntimeError('Wait for coordinated graceful library shutdown')
    args.evidence.mkdir(parents=True, mode=0o700, exist_ok=False)
    (args.evidence / 'before.json').write_text(json.dumps(proof, indent=2))
    (args.evidence / 'previous.service').write_bytes(UNIT.read_bytes())
    rollback = json.loads(run('docker', 'compose', '-f', str(ROOT / 'compose.runtime.yaml'), 'config', '--format', 'json'))
    rollback['services']['worker']['image'] = previous['/booksys-worker-1']['image']
    (args.evidence / 'rollback-compose.json').write_text(json.dumps(rollback, indent=2))
    run('docker', 'image', 'tag', previous['/booksys-worker-1']['image'], 'booksys-worker:before-activity')
    run('docker', 'image', 'tag', worker_image, 'booksys-worker:local')
    run('docker', 'compose', '-p', 'booksys', '-f', str(ROOT / 'compose.runtime.yaml'),
        'up', '-d', '--no-deps', '--no-build', '--pull', 'never', 'worker')
    current = json.loads(run('docker', 'inspect', 'booksys-worker-1', 'booksys-byparr-1', 'booksys-pyload'))
    for c in current:
        if c['Name'] != '/booksys-worker-1' and c['Id'] != previous[c['Name']]['id']:
            raise RuntimeError('Unowned container drift; stop dependent activation')
        if c['Name'] == '/booksys-worker-1' and (c['Image'] != worker_image or not c['State']['Running']):
            raise RuntimeError('Worker candidate not running')
    run(str(ROOT / '.venv/bin/python'), str(ROOT / 'ops/activate_library.py'),
        '--image', reader_image, '--expected-unit-sha256', args.expected_unit_sha256,
        '--evidence', str(args.evidence / 'library'), '--apply')
    proof['active'] = True
    (args.evidence / 'after.json').write_text(json.dumps(proof, indent=2))


if __name__ == '__main__':
    main()
