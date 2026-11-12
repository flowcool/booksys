"""Journaled independent copies. Linux atomic directory publication only."""
from __future__ import annotations

import ctypes
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator
import os
import re
import shutil
import stat
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


from library.media_paths import writer_uid
from library.catalog import Catalog
from library.primitives import LibraryError, atomic_yaml, lock, sync_dir
from library.readers import sha256


_copy_root: ContextVar[Path | None] = ContextVar('publication_copy_root', default=None)


@contextmanager
def publication_lock(directory: Path, root: Path) -> Iterator[None]:
    """Bind the already required copy lock to its private publication tree."""
    with lock(directory/'library-copy.lock', blocking=False):
        token = _copy_root.set(root.absolute())
        try:
            yield
        finally:
            _copy_root.reset(token)


def rename_atomic(source: Path, target: Path, *, exchange: bool = False,
                  private_root: Path | None = None) -> None:
    """Publish without replacement; callers hold the shared library-copy lock.

    NFSv3 rejects renameat2 flags. Files can use an exclusive hard link. A new
    directory can use POSIX rename only inside the explicitly private, single
    writer tree. Existing-directory exchange still requires kernel support.
    """
    import errno
    from library.locking import check_effect
    check_effect()
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, 'renameat2', None)
    if function is None:
        raise LibraryError('renameat2 is required; no unsafe rename fallback')
    result = function(-100, os.fsencode(source), -100, os.fsencode(target), 2 if exchange else 1)
    if result:
        code = ctypes.get_errno()
        if exchange or code not in (errno.EINVAL, errno.ENOTSUP, errno.ENOSYS):
            raise LibraryError(f'Atomic directory rename failed: {os.strerror(code)}')
        try:
            if source.is_symlink():
                raise LibraryError('Symlink in atomic publication')
            if source.is_file():
                # link is an atomic no-replace operation even on NFSv3. Sync the
                # new name before releasing the old name; a crash retains bytes.
                os.link(source, target)
                sync_dir(target.parent)
                source.unlink()
            elif private_root is not None and source.is_dir():
                root = private_root.absolute()
                if _copy_root.get() != root:
                    raise LibraryError('Atomic directory publication requires the held library-copy lock')
                paths = (root, source.absolute(), target.absolute())
                if (any('..' in path.parts or path.resolve() != path for path in paths)
                        or not paths[1].is_relative_to(root) or not paths[2].is_relative_to(root)):
                    raise LibraryError('Atomic directory publication is outside the private library')
                private_tree(root)
                # No other authorized writer can create this name: the caller
                # owns library-copy.lock and the complete tree is owner-only.
                if target.exists() or target.is_symlink():
                    raise LibraryError('Atomic directory publication target already exists')
                check_effect()
                os.rename(source, target)
            else:
                raise LibraryError('Atomic directory publication requires exclusive private ownership')
        except OSError as exc:
            raise LibraryError(f'Atomic publication failed: {exc.strerror}') from exc
    sync_dir(source.parent)
    if target.parent != source.parent:
        sync_dir(target.parent)


def manifest(path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_dir():
        raise LibraryError('Managed directory missing or symlink')
    output = {}
    for child in path.iterdir():
        if child.is_symlink() or not child.is_file():
            raise LibraryError('Unmanaged directory content')
        output[child.name] = sha256(child)
    return output


def verify(path: Path, expected: dict[str, str]) -> None:
    if manifest(path) != expected:
        raise LibraryError('Managed file drift; preserve folder and investigate')


def private_tree(path: Path) -> None:
    """Only the dedicated writer can mutate this tree; readers require RO access."""
    for entry in (path, *path.rglob('*')):
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != writer_uid(entry) or info.st_mode & 0o022:
            raise LibraryError('Exclusive library ownership is not established')


class Capacity:
    def __init__(self, *, reserve: int = 0,
                 free: Callable[[Path], int] = lambda path: shutil.disk_usage(path).free,
                 external_active: Callable[[], bool] = lambda: False,
                 max_external_bps: int | None = None, qualified: bool = False,
                 max_interval: float = .25):
        self.reserve, self.free, self.external_active = reserve, free, external_active
        self.max_external_bps, self.qualified, self.max_interval = max_external_bps, qualified, max_interval
        self.last_check = time.monotonic()

    def check(self, path: Path, remaining: int) -> None:
        active = self.external_active()
        margin = 0
        if active:
            if not self.qualified or self.max_external_bps is None:
                raise LibraryError('Concurrent writer capacity envelope is not qualified; promotion suspended')
            elapsed = time.monotonic() - self.last_check
            if elapsed > self.max_interval:
                raise LibraryError('Capacity probe interval exceeded qualified envelope')
            margin = int(self.max_external_bps * self.max_interval)
        self.last_check = time.monotonic()
        if self.free(path) < self.reserve + remaining + margin:
            raise LibraryError('Insufficient capacity for remaining copy and configured reserve')


class Publisher:
    def __init__(self, store: Catalog, reception: Path, root: Path, *, isolated: bool = False, lab: bool = False,
                 capacity: Capacity | None = None,
                 fault: Callable[[str, dict[str, Any]], None] = lambda *_: None,
                 exclusive: bool = False, local_recovery: bool = False, prepared: list[Any] | None = None):
        self.store, self.reception, self.root = store, reception.resolve(), root.resolve()
        self.isolated, self.capacity, self.fault = isolated, capacity or Capacity(), fault
        self.local_recovery = local_recovery
        self.prepared=prepared
        self.completion_task: Any=None
        if local_recovery and store.backend() is None:
            raise LibraryError('Local recovery requires scoped PostgreSQL authority')
        if self.root.is_relative_to(self.reception) or self.reception.is_relative_to(self.root):
            raise LibraryError('Library and reception must be disjoint')
        if isolated and (self.root.is_relative_to(Path('/data/booksys')) or
                         self.reception.is_relative_to(Path('/data/booksys'))):
            raise LibraryError('Isolated mode refuses live Booksys data')
        if lab:
            from library.runtime import lab_preflight
            if isolated:
                raise LibraryError('Lab and isolated modes are mutually exclusive')
            lab_preflight(reception, root, store.directory)
        if not (isolated or lab) and not exclusive:
            raise LibraryError('Runtime preflight must establish dedicated writer exclusivity')
        self.staging, self.recovery = (self.root / name for name in ('.staging', '.recovery'))
        self.journals = store.directory / 'library-admission-operations'
        from library.operational_state import CopyOperations
        self.operations=CopyOperations(store.backend()._database,'admission')
        for directory in (self.root, self.staging, self.recovery, self.journals):
            if directory.is_symlink():
                raise LibraryError('Symlink in library layout')
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if len({d.stat().st_dev for d in (self.root, self.staging, self.recovery)}) != 1:
            raise LibraryError('Library staging and recovery must share the exposure filesystem')
        private_tree(self.root)

    def _phase_evidence(self, operation: dict[str,Any]) -> None:
        from library.codec import encode
        import hashlib
        evidence=self.journals/'evidence'/operation['id']/(hashlib.sha256(encode(operation).encode()).hexdigest()+'.yaml')
        if not evidence.exists():
            atomic_yaml(evidence,operation,exclusive=True)

    def _terminal(self, operation: dict[str,Any], result: Any) -> None:
        from dataclasses import replace
        operation['phase']='committed'
        self._phase_evidence(operation)
        current=self.operations.get(operation['id'])
        if current is not None and current.get('phase')=='committed':
            self.fault('committed',operation)
            return
        result=replace(result,completions=[*result.completions,self.operations.prepare(dict(operation))])
        if self.prepared is not None:
            self.prepared.append(result)
        else:
            backend=self.store.backend()
            request=dict(expected_revision=result.revision,conflict=result.conflict,completions=result.completions)
            backend.commit(result.change,**request)
        self.fault('committed',operation)

    def transition(self, operation: dict[str, Any], phase: str) -> None:
        if phase=='committed':
            from library.store import PreparedResult,Change
            self._terminal(operation,PreparedResult(Change(),None,0))
            return
        operation['phase']=phase
        self._phase_evidence(operation)
        self.operations.phase(operation)
        self.fault(phase,operation)

    def check_capacity(self, operation: dict[str, Any], remaining: int) -> None:
        try:
            self.capacity.check(self.root, remaining)
        except LibraryError:
            for stage, allowed in ((self.staging / operation['id'], set(operation['manifest'])),
                                   (self.staging / (operation['id'] + '-transform'), {'book.cbz'})):
                if stage.exists():
                    contents = manifest(stage)
                    if set(contents) - allowed:
                        raise LibraryError('Unknown temporary content; capacity cleanup blocked')
                    operation.setdefault('aborted_partial_manifests', {})[stage.name] = contents
                    for name in contents:
                        (stage / name).unlink()
                    stage.rmdir()
                    sync_dir(self.staging)
            self.transition(operation, 'capacity_aborted')
            raise

    def resolve_origin(self, state: dict[str, Any], asset_id: str) -> Path:
        from library.origins import resolve_asset
        return resolve_asset(self.store, self.reception, state, asset_id)

    def operation_applies(self, operation: dict[str, Any], asset: str) -> bool:
        """Validate ownership even when skipping a known unrelated operation."""
        if not re.fullmatch('[0-9a-f]{64}', str(operation.get('asset', ''))):
            raise LibraryError('Copy journal has unknown conflict scope')
        target = str(operation.get('target', ''))
        if operation.get('kind') == 'admission':
            # Each admission leaf owns an immutable asset hash and signature.
            if not re.search('--' + operation['asset'] + '--[0-9a-f]{12}$', target):
                raise LibraryError('Copy journal has unknown destination scope')
        return bool(operation['asset'] == asset)
