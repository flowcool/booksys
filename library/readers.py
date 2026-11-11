"""Read-only, bounded validation in a networkless process."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from library.primitives import LibraryError

MAX_FILE = 1024 ** 3
READER_POLICY = 'single-comic-3'


from library.reader_inputs import sha256 as sha256, fingerprint as fingerprint, ReaderUnavailable as ReaderUnavailable


def worker_failure(label: str, code: int, stderr: bytes) -> str:
    detail = {125: 'container setup failure', 126: 'worker invocation denied',
              127: 'worker executable missing', 137: 'worker killed; CPU/OOM cause unconfirmed'}.get(code, 'worker failure')
    return f'{label}: exit {code} ({detail}); ' + stderr.decode(errors='replace')[:256].strip()


def inspect_file(path: Path, *, stable_seconds: float = 2) -> dict[str, Any]:
    try:
        path = path.absolute()
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise LibraryError('Symlink in reception path')
        first = fingerprint(path)
        if first[2] > MAX_FILE or first[2] == 0:
            raise LibraryError('File size limit or empty file')
        if path.suffix.lower() not in ('.pdf', '.cbz', '.cbr'):
            return {'status': 'pending', 'reason': 'Unsupported or ambiguous suffix', 'reader_policy': READER_POLICY}
        if re.search(r'(?:^|[ ._-])(?:part|pt)[ ._-]?\d+(?:[ ._-]|$)', path.name, re.I):
            return {'status': 'pending', 'reason': 'Multipart representation', 'reader_policy': READER_POLICY}
        time.sleep(stable_seconds)
        if fingerprint(path) != first:
            raise LibraryError('Reception bytes are not stable')
        asset_hash = sha256(path)
        from library.sandbox import native_prefix, PYTHON
        command = native_prefix(path) + [PYTHON, '-m', 'library.reader_worker', '/tmp/input', path.suffix.lower()]
        image = os.environ.get('BOOKSYS_READER_IMAGE')
        if image:
            if not re.fullmatch(r'[a-z0-9./:_@-]+', image):
                raise LibraryError('Invalid reader image reference')
            if ',' in str(path) or '\n' in str(path):
                raise LibraryError('Unsupported Docker bind source syntax')
            if fingerprint(path) != first:
                raise LibraryError('Reception changed before isolated bind')
            command = ['docker', 'run', '--rm', '--network', 'none', '--read-only',
                '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                '--user', f'{os.getuid()}:{os.getgid()}', '--memory', '1536m', '--cpus', '1',
                '--pids-limit', '32', '--mount', f'type=bind,src={path},dst=/input,readonly',
                '--entrypoint', '/app/.venv/bin/python', image, '-m', 'library.reader_worker',
                '/input', path.suffix.lower()]
        from library.reader_client import endpoint, execute
        if endpoint():
            result = subprocess.CompletedProcess(command, 0, json.dumps(execute('validate', path)).encode(), b'')
        else:
            from library.decoder_ownership import prepare
            command = prepare(command)
            result = subprocess.run(command, capture_output=True, timeout=45, check=False)
        if result.returncode in (125, 126, 127):
            raise ReaderUnavailable(worker_failure('Isolated reader setup', result.returncode, result.stderr))
        if result.returncode:
            raise LibraryError(worker_failure('Isolated reader failed', result.returncode, result.stderr))
        outcome: dict[str, Any] = json.loads(result.stdout)
        if outcome.get('reader_policy') != READER_POLICY:
            raise LibraryError('Reader policy differs from controller')
        if outcome.pop('input_sha256', None) != asset_hash:
            raise LibraryError('Reader did not inspect the identified reception bytes')
        if fingerprint(path) != first or sha256(path) != asset_hash:
            raise LibraryError('Reception changed during validation')
        return {**outcome, 'sha256': asset_hash, 'size': first[2]}
    except ReaderUnavailable:
        raise
    except subprocess.TimeoutExpired:
        return {'status': 'rejected', 'reason': 'Isolated reader timed out: wall limit 45 seconds',
                'reader_policy': READER_POLICY}
    except (LibraryError, OSError, ValueError) as exc:
        return {'status': 'rejected', 'reason': str(exc),
                'reader_policy': None if str(exc) == 'Reader policy differs from controller' else READER_POLICY}
