"""Additive relocation evidence. Canonical records and old proofs stay intact."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from library.primitives import LibraryError, sync_dir
from library.readers import fingerprint, sha256

_cache: tuple[object, dict[str, Any]] | None = None


def record_relocation(files: list[tuple[Path, Path, Path]], receipt: Path) -> None:
    records = {}
    for source, target, logical in files:
        if any(not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents))
               for path in (source, target, logical)):
            raise LibraryError('Relocation paths must be absolute without symlinks')
        if str(logical) in records:
            raise LibraryError('Duplicate logical relocation path')
        before, after = fingerprint(source), fingerprint(target)
        checksum = sha256(source)
        if (sha256(target) != checksum or fingerprint(source) != before
                or fingerprint(target) != after or before[2] != after[2]):
            raise LibraryError('Relocation bytes changed or differ')
        records[str(logical)] = {'source': str(source), 'target': str(target),
            'sha256': checksum, 'original': list(before), 'relocated': list(after)}
    raw = json.dumps({'schema': 1, 'files': records}, sort_keys=True, separators=(',', ':')).encode()
    if len(raw) > 32*1024**2:
        raise LibraryError('Relocation receipt exceeds bound')
    with receipt.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
        os.fchmod(stream.fileno(), 0o400)
    sync_dir(receipt.parent)


def matches_retained(path: Path, expected: list[int], checksum: str | None = None) -> bool:
    """Compare physical identity; use relocation only for an exact old identity.

    This does not alter fingerprint(), so in-flight stability checks always use
    the current physical file. A later mutation invalidates the relocation.
    """
    actual = list(fingerprint(path))
    if actual == expected:
        return True
    configured = os.environ.get('BOOKSYS_RELOCATION_RECEIPT')
    if not configured:
        return False
    receipt = Path(configured)
    if any(p.is_symlink() for p in (receipt, *receipt.parents)):
        raise LibraryError('Symlink relocation receipt')
    info = receipt.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o222 or info.st_size > 32*1024**2:
        raise LibraryError('Relocation receipt must be owned, bounded and immutable')
    signature = (str(receipt), info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    global _cache
    if _cache is None or _cache[0] != signature:
        value = json.loads(receipt.read_bytes())
        if set(value) != {'schema', 'files'} or value['schema'] != 1 or not isinstance(value['files'], dict):
            raise LibraryError('Invalid relocation receipt')
        _cache = signature, value['files']
    record = _cache[1].get(str(path.absolute()))
    return bool(record and record['original'] == expected and record['relocated'] == actual
                and (checksum is None or record['sha256'] == checksum)
                and sha256(path) == record['sha256'] and list(fingerprint(path)) == actual)
