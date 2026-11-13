"""Catalogue value rules and atomic file primitives; no storage adapters."""
from __future__ import annotations
import copy
import fcntl
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
import yaml
from booksys_core.values import digest as digest

from booksys_core.errors import LibraryError as LibraryError, LibraryLockBusy as LibraryLockBusy


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_yaml(path: Path, value: Any, *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w') as out:
            yaml.dump(value, out, Dumper=getattr(yaml, 'CSafeDumper', yaml.SafeDumper), allow_unicode=True, sort_keys=True)
            out.flush()
            os.fsync(out.fileno())
        if exclusive:
            squashed = temporary.stat().st_uid != os.getuid()
            if not squashed:
                os.chmod(temporary, 0o444)
            # Linux protected_hardlinks refuses a read-only NFS file whose
            # all_squash owner differs from the local UID. Publish fully synced
            # private bytes first; finish the mode before any dependent effect.
            os.link(temporary, path)
            if squashed:
                os.chmod(path, 0o444)
            temporary.unlink()
        else:
            os.replace(temporary, path)
        sync_dir(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextmanager
def lock(path: Path, *, blocking: bool = True) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    from library.locking import acquisition_deadline
    deadline = acquisition_deadline.get()
    with path.open('a') as handle:
        if blocking and deadline is not None:
            import time
            stop = min(deadline, time.monotonic() + .5)
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= stop:
                        raise LibraryError('processing_lock_busy') from exc
                    time.sleep(min(.005, max(0, stop - time.monotonic())))
            yield
            return
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise LibraryLockBusy('Another library operation is active') from exc
        yield


TABLES = ('assets', 'origins')


def empty() -> dict[str, Any]:
    return {'schema': 1, 'revision': 0, 'ledger': [], **{key: {} for key in TABLES}}


def validate(state: Any) -> None:
    if not isinstance(state, dict) or set(state) != {'schema', 'revision', 'ledger', *TABLES}:
        raise LibraryError('Invalid catalogue keys')
    if state['schema'] != 1 or type(state['revision']) is not int or state['revision'] < 0:
        raise LibraryError('Unsupported catalogue schema or revision')
    if not isinstance(state['ledger'], list) or not all(isinstance(x, str) for x in state['ledger']):
        raise LibraryError('Invalid decision references')
    for table in TABLES:
        if not isinstance(state[table], dict):
            raise LibraryError('Catalogue tables must be keyed maps')
        for key, record in state[table].items():
            if not isinstance(key, str) or not isinstance(record, dict):
                raise LibraryError('Invalid entity')
    requirements = {'assets': ('sha256', 'size', 'format', 'origins'),
                    'origins': ('source', 'pid', 'fid', 'path', 'sha256')}
    for table, fields in requirements.items():
        for key, record in state[table].items():
            if any(field not in record for field in fields):
                raise LibraryError(f'Missing {table} fields: {key}')
    for key, asset in state['assets'].items():
        if not re.fullmatch('[0-9a-f]{64}', key) or asset['sha256'] != key:
            raise LibraryError('Invalid asset identity')
        if any(origin not in state['origins'] for origin in asset['origins']):
            raise LibraryError('Asset references missing origin')
        if type(asset['size']) is not int or asset['size'] <= 0 or asset['format'] not in ('pdf', 'cbz', 'cbr'):
            raise LibraryError('Invalid asset size or format')
        admission = asset.get('admission')
        if admission is not None:
            target = Path(str(admission.get('target', '')))
            if (admission.get('status') not in ('holding', 'classified') or target.is_absolute()
                    or '..' in target.parts or not target.parts
                    or target.parts[0] != ('holding' if admission['status'] == 'holding' else 'classified')
                    or not re.fullmatch('[0-9a-f]{32}', str(admission.get('operation', '')))
                    or not re.fullmatch('[0-9a-f]{64}', str(admission.get('signature', '')))
                    or set(admission.get('manifest', {})) != {'book.cbz', 'fiche.md'}
                    or any(not re.fullmatch('[0-9a-f]{64}', str(v)) for v in admission['manifest'].values())):
                raise LibraryError('Invalid work admission record')
    for key, origin in state['origins'].items():
        external = (origin['source'] == 'external:' + str(origin['path'])
                    and key == digest({'external': origin['path'], 'size': origin.get('size'), 'mtime_ns': origin.get('mtime_ns')}))
        if (not all(isinstance(origin[k], str) for k in ('source', 'path')) or
                not ((type(origin['pid']) is int and type(origin['fid']) is int) or
                     (external and origin['pid'] is None and origin['fid'] is None))):
            raise LibraryError('Invalid origin IDs or path')
        path = Path(origin['path'])
        if path.is_absolute() or '..' in path.parts:
            raise LibraryError('Unsafe origin path')
        if origin.get('parent_origin'):
            parent = state['origins'].get(origin['parent_origin'])
            if (not parent or origin.get('parent_sha256') != parent.get('sha256')
                    or origin.get('member_sha256') != origin['sha256']
                    or not isinstance(origin.get('member'), str)
                    or not origin['path'].startswith('bundle-inputs/' + str(parent['sha256']) + '/')):
                raise LibraryError('Invalid bundle child provenance')
        if origin['sha256'] is not None and not re.fullmatch('[0-9a-f]{64}', origin['sha256']):
            raise LibraryError('Invalid origin hash')


class WorkingState(dict[str, Any]):
    """A mutable domain closure and its original values for a targeted commit."""
    def __init__(self, state: dict[str, Any]):
        super().__init__(state)
        self.original = copy.deepcopy(state)
        self.media_omitted = False
        self.conflict: dict[str, Any] | None = None


@contextmanager
def catalogue_commit_lock(directory: Any, *, wait: bool) -> Iterator[None]:
    """Serialize only the short commit; scoped writers allow bounded contention."""
    import time
    from library.locking import bounded_acquisition, acquisition_deadline
    if not wait:
        with lock(directory/'library.lock', blocking=False):
            yield
        return
    stop = min(acquisition_deadline.get() or time.monotonic()+10, time.monotonic()+10)
    # The mutex keeps its own 500 ms bound. Its internal acquisition deadline
    # must not replace the worker's total ten-second database budget.
    with bounded_acquisition(stop), lock(directory/'library.lock'):
        yield
