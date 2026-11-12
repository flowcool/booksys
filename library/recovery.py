"""Frozen common recovery points. Canonical journals remain independent evidence."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import hashlib
import os
from pathlib import Path
import shutil
from typing import Any, Iterator

from library.catalog import Catalog
from library.primitives import LibraryError, atomic_yaml, lock, sync_dir
from library.codec import decode
from library.store import Store

COMPONENTS = ('library-decisions', 'library-ledger-head.yaml', 'library-operations',
              'library-admission-operations', 'library-provisional-operations', 'library-resolution',
              'library-retirement.yaml', 'library-activation.yaml', 'task-policy.yaml',
              'stage-resources.yaml', 'stage-children', 'stage-activation.yaml',
              'reader-dependency-children.yaml', 'provider-admission.yaml', 'native-admission.yaml', 'native-effects.yaml', 'dependency-readiness.yaml')
COLLECTOR_FILES = ('state.yaml', 'monitor.yaml', 'mirror-routing.yaml', 'package-retirement.yaml', 'backfill.yaml', 'fiches.yaml')
LOCKS = ('library-cycle.lock', 'decisions.lock', 'library-copy.lock', 'library-evidence.lock', 'library.lock')


def external_media(store: Store) -> dict[str, Any]:
    """Bind retained immutable NAS media without copying it onto the VM.

    These recovery points depend on the retained NAS namespace. They are not
    independent off-host backups and never authorize deleting those media.
    """
    result = {}
    if os.environ.get('BOOKSYS_ATTACHMENT_ROOT'):
        with store._database.connection() as db:
            names = [row[0] for row in db.execute('SELECT digest FROM attachments ORDER BY digest')]
        result['attachments'] = {'root': str(store.payloads), 'files': {
            name: hashlib.sha256((store.payloads/name).read_bytes()).hexdigest() for name in names}}
    if os.environ.get('BOOKSYS_PIECES_ROOT'):
        from library.media_paths import pieces_root
        root = pieces_root(store.directory)
        result['pieces'] = {'root': str(root), 'files': {
            name+'.yaml': hashlib.sha256((root/(name+'.yaml')).read_bytes()).hexdigest()
            for name in sorted(store.retained_pieces())}}
    return result


def verify_media(value: dict[str, Any], store: Store) -> None:
    current = external_media(store)
    if current != value:
        raise LibraryError('Retained external recovery media changed or configuration is absent')


LEGACY_COMPONENTS = COMPONENTS[:8]


def inventory(root: Path, *, components: tuple[str, ...] = COMPONENTS) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in components:
        target = root / name
        if target.is_symlink():
            raise LibraryError('Symlink in canonical recovery evidence')
        if not target.exists():
            result[name] = {'absent': True}
            continue
        paths = [target] if target.is_file() else [target, *sorted(target.rglob('*'))]
        for path in paths:
            if path.is_symlink():
                raise LibraryError('Symlink in canonical recovery evidence')
            if path.is_dir():
                result[str(path.relative_to(root))] = {'directory': True}
            elif path.is_file():
                with path.open('rb') as source:
                    first = os.fstat(source.fileno())
                    digest = hashlib.file_digest(source, 'sha256').hexdigest()
                    last = os.fstat(source.fileno())
                if (first.st_ino, first.st_size, first.st_mtime_ns) != (last.st_ino, last.st_size, last.st_mtime_ns):
                    raise LibraryError('Recovery source drift')
                result[str(path.relative_to(root))] = {'bytes': first.st_size, 'sha256': digest}
    return result


def audit_ledger(store: Store, files: Path) -> tuple[int, list[str]]:
    """The database index is derived. Retained immutable files prove its prefix."""
    if (files / 'library-decisions').exists():
        records = Catalog(files,store=store).ledger()
    elif (files / 'library-ledger-head.yaml').exists():
        raise LibraryError('Decision head has no immutable evidence')
    else:
        records = []
    with store._database.connection() as db:
        revision, applied = store.check(db)
        indexed = db.execute('SELECT sequence,payload FROM ledger_records ORDER BY sequence').fetchall()
    if (len(applied) > len(records) or applied != [record['id'] for record in records[:len(applied)]]
            or len(indexed) < len(applied) or len(indexed) > len(records)):
        raise LibraryError('Recovery ledger is not a retained immutable prefix')
    for (sequence, payload), record in zip(indexed, records):
        if sequence != record['sequence'] or decode(payload) != record:
            raise LibraryError('Recovery decision index differs from immutable evidence')
    return revision, applied



def _retired(*args: Any, **kwargs: Any) -> Any:
    raise LibraryError('SQLite recovery points are historical evidence; recover current PostgreSQL state forward')


backup_activity = _retired
activity_binding = _retired
freeze = _retired
checkpoint = _retired
verify_checkpoint = _retired
restore_checkpoint = _retired
