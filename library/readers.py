"""Bounded validation in a stage subprocess."""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from library.primitives import LibraryError

MAX_FILE = 1024 ** 3
READER_POLICY = 'single-comic-3'


from library.reader_inputs import sha256 as sha256, fingerprint as fingerprint


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
        from library.decode import run
        outcome = run('validate', path)
        if outcome.get('status') == 'pending':
            return {**outcome, 'reader_policy': READER_POLICY}
        if outcome.get('reader_policy') != READER_POLICY:
            raise LibraryError('Reader policy differs from controller')
        if outcome.pop('input_sha256', None) != asset_hash:
            raise LibraryError('Reader did not inspect the identified reception bytes')
        if fingerprint(path) != first or sha256(path) != asset_hash:
            raise LibraryError('Reception changed during validation')
        return {**outcome, 'sha256': asset_hash, 'size': first[2]}
    except (LibraryError, OSError, ValueError) as exc:
        return {'status': 'rejected', 'reason': str(exc),
                'reader_policy': None if str(exc) == 'Reader policy differs from controller' else READER_POLICY}
