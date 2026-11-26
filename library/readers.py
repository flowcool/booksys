"""Bounded validation in a stage subprocess."""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from library.primitives import LibraryError
from library.reader_inputs import SOURCE_UNAVAILABLE_ERRNOS

MAX_FILE = 1024 ** 3
READER_POLICY = 'single-comic-3'
GENERIC_ARCHIVE_SUFFIXES = {'.zip': '.cbz', '.rar': '.cbr'}
COMIC_SUFFIXES = frozenset(('.pdf', '.cbz', '.cbr', *GENERIC_ARCHIVE_SUFFIXES))


from library.reader_inputs import sha256 as sha256, fingerprint as fingerprint


def inspect_file(path: Path, *, stable_seconds: float = 2, expected_sha256: str | None = None) -> dict[str, Any]:
    try:
        path = path.absolute()
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise LibraryError('Symlink in reception path')
        first = fingerprint(path)
        suffix = path.suffix.lower()
        generic_archive = suffix in GENERIC_ARCHIVE_SUFFIXES
        if first[2] == 0 or (first[2] > MAX_FILE and not generic_archive):
            raise LibraryError('File size limit or empty file')
        if suffix not in COMIC_SUFFIXES:
            return {'status': 'pending', 'reason': 'Unsupported or ambiguous suffix', 'reader_policy': READER_POLICY}
        if re.search(r'(?:^|[ ._-])(?:part|pt)[ ._-]?\d+(?:[ ._-]|$)', path.name, re.I):
            return {'status': 'pending', 'reason': 'Multipart representation', 'reader_policy': READER_POLICY}
        time.sleep(stable_seconds)
        if fingerprint(path) != first:
            raise LibraryError('Reception bytes are not stable')
        asset_hash = sha256(path) if not generic_archive else None
        if expected_sha256 is not None and asset_hash != expected_sha256:
            raise LibraryError('Telegram file SHA-256 differs from the receipt')
        from library.decode import run
        outcome = run('validate_generic' if generic_archive else 'validate', path)
        if outcome.get('status') == 'pending':
            return {**outcome, 'reader_policy': READER_POLICY}
        if outcome.get('reader_policy') != READER_POLICY:
            raise LibraryError('Reader policy differs from controller')
        if outcome.get('status') == 'bundle':
            return outcome
        if asset_hash is None and outcome.get('status') == 'rejected':
            return outcome
        if asset_hash is None:
            asset_hash = sha256(path)
        if outcome.pop('input_sha256', None) != asset_hash:
            raise LibraryError('Reader did not inspect the identified reception bytes')
        if fingerprint(path) != first or sha256(path) != asset_hash:
            raise LibraryError('Reception changed during validation')
        return {**outcome, 'sha256': asset_hash, 'size': first[2]}
    except (LibraryError, OSError, ValueError) as exc:
        if (expected_sha256 is not None and isinstance(exc, OSError)
                and exc.errno in SOURCE_UNAVAILABLE_ERRNOS):
            raise
        return {'status': 'rejected', 'reason': str(exc),
                'reader_policy': None if str(exc) == 'Reader policy differs from controller' else READER_POLICY}
