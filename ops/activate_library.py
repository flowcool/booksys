"""Activate a committed library candidate after coordinated graceful shutdown.

Restore the retained previous unit to roll back code/image selection. Catalogue,
journals, recovery and originals are never rolled back or removed by this tool.
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
UNIT = Path.home()/'.config/systemd/user/booksys-library.service'


def run(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--expected-unit-sha256', required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    if run('hostname') != 'booksys-host' or ROOT.is_symlink() or UNIT.is_symlink():
        raise RuntimeError('Unexpected activation host/path')
    previous = UNIT.read_bytes()
    if hashlib.sha256(previous).hexdigest() != args.expected_unit_sha256:
        raise RuntimeError('Library unit drift; inspect before retry')
    previous_text = previous.decode()
    old_images = [line.split('=', 2)[-1] for line in previous_text.splitlines()
                  if line.startswith('Environment=BOOKSYS_READER_IMAGE=')]
    if len(old_images) != 1:
        raise RuntimeError('Expected one existing reader image')
    run('docker', 'image', 'inspect', old_images[0])
    image = json.loads(run('docker', 'image', 'inspect', args.image))[0]['Id']
    if run('git', '-C', str(ROOT), 'status', '--porcelain'):
        raise RuntimeError('Commit the complete candidate before activation')
    commit = run('git', '-C', str(ROOT), 'rev-parse', 'HEAD')
    containers: list[dict[str, Any]] = json.loads(run('docker', 'inspect', 'booksys-pyload', 'booksys-worker-1', 'booksys-byparr-1'))
    proof = {'commit': commit, 'worktree': str(ROOT), 'image': image,
             'previous_unit_sha256': args.expected_unit_sha256,
             'containers': {c['Name']: c['Id'] for c in containers}}
    print(json.dumps(proof), flush=True)
    if not args.apply:
        return
    active = subprocess.run(['systemctl', '--user', 'is-active', 'booksys-library.service'],
                            capture_output=True, text=True)
    if active.stdout.strip() not in ('inactive', 'failed'):
        raise RuntimeError('Coordinate graceful library shutdown before activation')
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    (args.evidence/'previous.service').write_bytes(previous)
    (args.evidence/'activation.json').write_text(json.dumps(proof, indent=2))
    template = (ROOT/'ops/booksys-library.service').read_text()
    template = template.replace('/opt/booksys', str(ROOT))
    template = '\n'.join('Environment=BOOKSYS_READER_IMAGE='+image
                         if line.startswith('Environment=BOOKSYS_READER_IMAGE=') else line
                         for line in template.splitlines())+'\n'
    candidate = args.evidence/'candidate.service'
    candidate.write_text(template)
    # Atomic unit replacement; do not touch any catalogue or downloader state.
    pending = UNIT.with_suffix('.service.pending')
    pending.write_text(template)
    pending.chmod(0o644)
    pending.replace(UNIT)
    run('systemctl', '--user', 'daemon-reload')
    run('systemctl', '--user', 'start', 'booksys-library.service')
    after = json.loads(run('docker', 'inspect', *[c['Id'] for c in containers]))
    if [c['Id'] for c in after] != [c['Id'] for c in containers]:
        raise RuntimeError('Shared container drift; preserve evidence')
    if run('systemctl', '--user', 'is-active', 'booksys-library.service') != 'active':
        raise RuntimeError('Library did not start; inspect retained unit and journal')
    proof['active'] = True
    (args.evidence/'activation.json').write_text(json.dumps(proof, indent=2))


if __name__ == '__main__':
    main()
