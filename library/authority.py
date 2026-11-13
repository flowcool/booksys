"""Read-only interlock shared by supported catalogue entry points.

Migration tooling selects authority. Runtime code only verifies it. This protects
against accidental old launches, not deliberate changes by the owning operator.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys
from typing import Any

import yaml

from library.primitives import LibraryError

ROOT = Path(__file__).resolve().parents[1]
TOP_LEVEL = ('booksys.py', 'pipeline.py', 'fiches.py', 'mirrors.py',
             'mirror_routing.py', 'retirement_history.py',
             'ops/library_service.py', 'ops/library_cycle.py', 'ops/retire_packages.py',
             'ops/route_mirrors.py')
FIELDS = {'schema', 'backend', 'authority_id', 'catalogue_path', 'source_digest',
          'source_semantic_sha256', 'code_sha256', 'runtime', 'compatible_legacy_commit'}
_cache: tuple[Any, str] | None = None


def code_digest(root: Path = ROOT) -> str:
    """Hash the exact application bundle shared by the controller and images."""
    global _cache
    paths = [root / name for name in TOP_LEVEL]
    for directory in ('library', 'booksys_core'):
        paths.extend(sorted((root / directory).rglob('*.py')))
    if any(path.is_symlink() or not path.is_file() for path in paths):
        raise LibraryError('Application code bundle is incomplete')
    signature = tuple((str(path), path.stat().st_size, path.stat().st_mtime_ns) for path in paths)
    if _cache is not None and _cache[0] == signature:
        return _cache[1]
    result = hashlib.sha256()
    for path in paths:
        name = str(path.relative_to(root)).encode()
        raw = path.read_bytes()
        result.update(len(name).to_bytes(4, 'big')); result.update(name)
        result.update(len(raw).to_bytes(8, 'big')); result.update(raw)
    value = result.hexdigest()
    _cache = signature, value
    return value


def read_manifest(directory: Path, *, expected_code: str | None = None) -> dict[str, Any]:
    path = directory / 'storage-authority.yaml'
    if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o222:
        raise LibraryError('Catalogue authority manifest must be owned and read-only')
    value = yaml.safe_load(path.read_text())
    if (not isinstance(value, dict) or set(value) != FIELDS or value['schema'] != 2
            or value['backend'] != 'postgresql'
            or value['catalogue_path'] != 'postgresql.json'
            or not isinstance(value['authority_id'], str)
            or value['runtime'] != {'python_minor': [3, 14], 'postgresql_min': [18, 6]}
            or value['compatible_legacy_commit'] is not None):
        raise LibraryError('Invalid catalogue authority manifest')
    if sys.version_info[:2] != (3, 14):
        raise LibraryError('Selected authority requires the qualified runtime')
    if value['code_sha256'] != (code_digest() if expected_code is None else expected_code):
        raise LibraryError('Application code differs from selected authority')
    from library.store import Store
    store = Store(directory, authority=value['authority_id'])
    with store._database.connection() as db:
        if db.info.server_version < 180006:
            raise LibraryError('Selected authority requires the qualified PostgreSQL runtime')
        store.verify(db)
        source = db.execute('SELECT imported_source_digest,source_semantic FROM meta').fetchone()
        if source != (value['source_digest'], value['source_semantic_sha256']):
            raise LibraryError('Catalogue source differs from selected authority')
    return value
