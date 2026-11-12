"""Qualify nested reader startup without exposing live data or Docker to it.

This is a prerequisite probe, not complete reader or migration acceptance.
The host owns Docker. The candidate receives synthetic files only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile
import uuid
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from library.process import bounded_run
from library.primitives import LibraryError


PROBE = '''
import json, os, platform, subprocess
from pathlib import Path
from library.authority import code_digest
from library.sandbox import native_prefix, PYTHON
command = native_prefix(Path('/qualification/input.cbz')) + [PYTHON, '-c',
    'from pathlib import Path; assert Path("/tmp/input").read_text() == "synthetic"; print("reader-started")']
result = subprocess.run(command, capture_output=True, text=True, timeout=10)
print(json.dumps({'uid': os.getuid(), 'gid': os.getgid(), 'kernel': platform.release(),
    'application_sha256': code_digest(), 'command': command,
    'returncode': result.returncode, 'stdout': result.stdout[:4096],
    'stderr': result.stderr[:4096]}))
'''

BOUNDARY = '''
import json, os, socket
from pathlib import Path
assert Path('/qualification/input.cbz').read_text() == 'synthetic'
for denied in ('/data/booksys/state', '/data/booksys/secrets', '/var/run/docker.sock',
               '/qualification/sibling.cbz', '/qualification/controller-secret'):
    assert not Path(denied).exists(), denied
try:
    Path('/qualification/input.cbz').write_text('changed')
except OSError:
    pass
else:
    raise RuntimeError('Input grant is writable')
with socket.socket() as channel:
    channel.settimeout(.2)
    try:
        channel.connect(('192.0.2.1', 9))
    except OSError:
        pass
    else:
        raise RuntimeError('Unexpected external network')
assert os.getuid() == 1000
print(json.dumps({'input_read_only': True, 'state_secrets_sibling_socket_denied': True,
                  'network_denied': True, 'uid': os.getuid()}))
'''

PROBES = {'nested': PROBE, 'boundary': BOUNDARY,
          'memory': 'value = bytearray(256 * 1024**2); print(len(value))',
          'timeout': 'import time; time.sleep(60)',
          'output': 'print("x" * 131072)'}


def run(argv: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)


def qualify(image: str, evidence: Path, *, mode: str = 'nested') -> dict[str, Any]:
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
        raise ValueError('Use an immutable local image ID')
    if mode not in PROBES:
        raise ValueError('Unknown fixed probe')
    evidence.mkdir(parents=True, exist_ok=False)
    inspected = run(['docker', 'image', 'inspect', image])
    if inspected.returncode:
        raise RuntimeError('Required reader image is unavailable')
    value = json.loads(inspected.stdout)[0]
    name = 'booksys-reader-probe-' + uuid.uuid4().hex
    report: dict[str, Any] = {'schema': 1, 'image': image, 'kernel': platform.release(),
        'image_user': value['Config']['User'], 'name': name,
        'mode': mode, 'result': 'NOT_QUALIFIED', 'stopped': False}
    root = Path(tempfile.mkdtemp(prefix='booksys-compose-reader-'))
    report['scratch'] = str(root)
    # Docker's non-root identity needs traversal, including on another UID.
    root.chmod(0o755)
    source = root / 'input.cbz'
    source.write_text('synthetic')
    source.chmod(0o444)
    (root/'sibling.cbz').write_text('ungranted synthetic sibling')
    (root/'controller-secret').write_text('synthetic canary only')
    memory = '64m' if mode == 'memory' else '512m'
    command = ['docker', 'create', '--name', name, '--network', 'none', '--read-only',
        '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--user', '1000:1000',
        '--memory', memory, '--memory-swap', memory, '--cpus', '0.5', '--pids-limit', '32',
        '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,size=16m,mode=1777',
        '--mount', f'type=bind,src={source},dst=/qualification/input.cbz,readonly',
        '--entrypoint', '/app/.venv/bin/python', image, '-c', PROBES[mode]]
    report['create_command'] = command
    created = run(command)
    if created.returncode:
        report.update(create_error=created.stderr[:4096])
        (evidence / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
        return report
    container = created.stdout.strip()
    if not re.fullmatch(r'[0-9a-f]{64}', container):
        raise RuntimeError('Docker did not return an owned container ID')
    report['container'] = container
    (evidence / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
    try:
        result = bounded_run(['docker', 'start', '--attach', container],
                             timeout=.5 if mode == 'timeout' else 20,
                             stdout_limit=4096 if mode == 'output' else 16384)
        report.update(returncode=result.returncode, stdout=result.stdout.decode(errors='replace'),
                      stderr=result.stderr[:4096].decode(errors='replace'))
        if mode == 'nested' and result.returncode == 0:
            report['probe'] = json.loads(result.stdout)
            report['result'] = ('STARTED_ONLY' if report['probe']['returncode'] == 0
                                else 'NESTED_SANDBOX_UNAVAILABLE')
        elif mode == 'boundary' and result.returncode == 0:
            report['result'] = 'BOUNDARY_PASS'
            report['probe'] = json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        report['result'] = 'PROBE_TIMEOUT'
    except LibraryError as exc:
        report.update(result='OUTPUT_LIMIT', reason=str(exc))
    finally:
        state = run(['docker', 'inspect', '--format', '{{json .State}}', container])
        if state.returncode:
            raise RuntimeError('Cannot prove owned container state; retain scratch and stop')
        report['state_after_client'] = json.loads(state.stdout)
        if json.loads(state.stdout)['Running']:
            killed = run(['docker', 'kill', container])
            report['kill_exit_code'] = killed.returncode
        state = run(['docker', 'inspect', '--format', '{{json .State}}', container])
        if state.returncode or json.loads(state.stdout)['Running']:
            raise RuntimeError('Owned probe stop is unproved')
        report['state'] = json.loads(state.stdout)
        if mode == 'memory' and report['state']['OOMKilled']:
            report['result'] = 'MEMORY_BOUND_PASS'
        report['stopped'] = True
        report['source_sha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
        (evidence / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
        # One-shot probes are not retained proof. Save state/output first.
        removed = run(['docker', 'rm', container])
        if removed.returncode:
            raise RuntimeError('Stopped one-shot retained; inspect evidence before cleanup')
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--mode', choices=PROBES, default='nested')
    args = parser.parse_args()
    result = qualify(args.image, args.evidence, mode=args.mode)
    print(json.dumps({key: result[key] for key in ('result', 'stopped')}))


if __name__ == '__main__':
    main()
