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

from library.catalog import LibraryError

MAX_FILE = 1024 ** 3
READER_POLICY = 'single-comic-3'


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as handle:
        while data := handle.read(1024 * 1024):
            digest.update(data)
    return digest.hexdigest()


def fingerprint(path: Path) -> tuple[int, int, int, int, int]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise LibraryError('Not a regular file')
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


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
        result = subprocess.run(command, capture_output=True, timeout=45, check=False)
        if result.returncode:
            raise LibraryError('Isolated reader failed or resource limit exceeded')
        outcome: dict[str, Any] = json.loads(result.stdout)
        if outcome.get('reader_policy') != READER_POLICY:
            raise LibraryError('Reader policy differs from controller')
        if outcome.pop('input_sha256', None) != asset_hash:
            raise LibraryError('Reader did not inspect the identified reception bytes')
        if fingerprint(path) != first or sha256(path) != asset_hash:
            raise LibraryError('Reception changed during validation')
        return {**outcome, 'sha256': asset_hash, 'size': first[2]}
    except (LibraryError, OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return {'status': 'rejected', 'reason': str(exc),
                'reader_policy': None if str(exc) == 'Reader policy differs from controller' else READER_POLICY}
