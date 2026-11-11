"""Read-only application-side binding of the Infra NFS qualification.

This never writes a NAS probe or changes mounts, exports or running services.
It does not prove that cached reads detect an outage.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ops.qualification.compose_probe import owned_probe

MOUNTS = {'files': ('/mnt/booksys-host/booksys-files', '192.0.2.10:/volume2/storage/booksys'),
          'library': ('/mnt/booksys-host/booksys-library', '192.0.2.10:/volume2/storage/comics/booksys')}
# Qualification baseline from private-work-item's delivered exports, bound by the
# initial identity receipt. Drift must fail, not become a new expected value.
IDENTITIES = {'files': '4f34ea560d283a641de4daf0a32b3ceea7ef34882e90f11ef1a6214b8fb4fc40',
              'library': '4aa71008faa5d97de5be8f542a1af9081dcb682427d839d4f84b46f0f597c75a'}

PROBE = '''
import hashlib, json
from pathlib import Path
expected = EXPECTED
lines = Path('/proc/self/mountinfo').read_text().splitlines()
result = {}
for role, value in expected.items():
    target = '/nas-' + role
    rows = [row.split() for row in lines if row.split()[4] == target]
    assert len(rows) == 1, 'Expected exactly one mounted source'
    row = rows[0]
    separator = row.index('-')
    assert row[separator+1] == 'nfs' and row[separator+2] == value['source']
    assert 'ro' in row[5].split(',')
    sentinel = hashlib.sha256((Path(target)/'.nas-identity').read_bytes()).hexdigest()
    assert sentinel == value['sentinel_sha256'], 'Unexpected export identity'
    result[role] = {'source': row[separator+2], 'fstype': row[separator+1],
                    'sentinel_sha256': sentinel, 'read_only': True}
print(json.dumps(result))
'''


def qualify(image: str, evidence: Path) -> dict[str, Any]:
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
        raise ValueError('Use an immutable local image ID')
    evidence.mkdir(parents=True, exist_ok=False)
    expected = {}
    host_mounts = [row.split() for row in Path('/proc/self/mountinfo').read_text().splitlines()]
    for role, (path, source) in MOUNTS.items():
        matches = [row for row in host_mounts if row[4] == path]
        if len(matches) != 1:
            raise ValueError('Expected exactly one host mount')
        row = matches[0]
        separator = row.index('-')
        if row[separator+1:separator+3] != ['nfs', source] or 'hard' not in row[separator+3].split(','):
            raise ValueError('Expected qualified hard NFS mount')
        expected[role] = {'source': source, 'host_mount': ' '.join(row),
            'sentinel_sha256': IDENTITIES[role]}
    (evidence/'expected.json').write_text(json.dumps(expected, indent=2)+'\n')
    command = ['docker', 'create', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges', '--user', '1000:1000', '--memory', '128m',
        '--memory-swap', '128m', '--cpus', '.25', '--pids-limit', '16']
    for role, (path, _) in MOUNTS.items():
        command += ['--mount', f'type=bind,src={path},dst=/nas-{role},readonly']
    command += ['--entrypoint', '/app/.venv/bin/python', image, '-c', PROBE.replace('EXPECTED', repr(expected))]
    report = owned_probe(command, evidence, timeout=20)
    if report['exit_code']:
        raise RuntimeError('Container mount identity failed; inspect retained result')
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    qualify(args.image, args.evidence)
    print('PASS: both qualified NFS identities are visible read-only inside the candidate')


if __name__ == '__main__':
    main()
