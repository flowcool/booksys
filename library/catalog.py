from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, TYPE_CHECKING

if TYPE_CHECKING:
    from library.store import Store, PreparedResult

import yaml


from library.primitives import (LibraryError, digest, now, sync_dir, atomic_yaml, lock,
                                TABLES, empty, validate, WorkingState)



class Catalog:
    def __init__(self, directory: Path, *, store: Store | None = None):
        from library.store import Store
        self.directory = directory
        self.path = directory / 'library.yaml'
        if store is None:
            from library.authority import read_manifest
            manifest = read_manifest(directory)
            store = Store(directory, authority=manifest['authority_id'])
        self._store = store

    def backend(self) -> Store:
        return self._store

    @property
    def revision(self) -> int:
        return self._store.revision

    def get(self, kind: str, ident: str) -> dict[str, Any] | None:
        return self._store.get(kind, ident)

    def records(self, kind: str, *, media: bool = False) -> Iterator[tuple[str, dict[str, Any]]]:
        for ident in self._store.ids(kind):
            record = self._store.get(kind, ident, media=media)
            if record is not None:
                yield ident, record

    def working_set(self, action: str, refs: dict[str, list[str]], *, media: bool = True) -> dict[str, Any]:
        return self.result_set(action, refs, media=media)

    def result_set(self, action: str, refs: dict[str, list[str]], *,
                   claims: dict[str, list[str]] | None = None, media: bool = True) -> dict[str, Any]:
        """Prepare remote/expensive work without holding a transaction open."""
        backend = self.backend()
        from library.conflicts import snapshot
        with backend._database.connection() as db:
            state, token = snapshot(backend, db, action, refs, claims or {}, media=media)
        if media:
            from library.codec import encode
            for kind, records in token['covers'].items():
                for ident, cover in records.items():
                    state[kind][ident] = backend.restore(encode({'record':state[kind][ident],'cover':cover}))
        result = WorkingState(state)
        result.media_omitted = not media
        result.conflict = token
        return result

    def extend_result(self, token: dict[str, Any], refs: dict[str, list[str]]) -> dict[str, Any]:
        from library.conflicts import snapshot, verify
        backend = self.backend()
        merged = {kind: sorted(set(ids) | set(refs.get(kind, []))) for kind, ids in token['refs'].items()}
        for kind, ids in refs.items():
            merged.setdefault(kind, ids)
        with backend._database.connection() as db:
            verify(backend, db, token)
            _, result = snapshot(backend, db, token['action'], merged, token['claims'], media=token['media'])
        return result

    def verify_result(self, token: dict[str, Any]) -> None:
        from library.conflicts import verify
        backend = self.backend()
        with backend._database.connection() as db:
            verify(backend, db, token)

    def read(self) -> dict[str, Any]:
        return WorkingState(self._store.export())

    def prepare(self, state: dict[str, Any]) -> PreparedResult:
        """Detach the result before the short durable completion transaction."""
        from library.codec import encode
        from library.store import Change, PreparedResult
        if not isinstance(state, WorkingState):
            raise LibraryError('PostgreSQL mutation requires a declared working set')
        change = Change()
        for kind in TABLES:
            before, after = state.original[kind], state[kind]
            updates = {ident: copy.deepcopy(record) for ident, record in after.items()
                       if ident not in before or encode(record) != encode(before[ident])}
            if state.media_omitted:
                for ident, record in updates.items():
                    if 'cover' not in record and 'cover' not in before.get(ident, {}):
                        retained = self._store.get(kind, ident)
                        if retained is not None and 'cover' in retained:
                            record['cover'] = retained['cover']
            if updates:
                change.upserts[kind] = updates
            removed = sorted(set(before) - set(after))
            if removed:
                change.deletes[kind] = removed
        return PreparedResult(change, copy.deepcopy(state.conflict), int(state['revision']))

    def save(self, state: dict[str, Any], *, expected: int) -> None:
        prepared = self.prepare(state)
        self._store.commit(prepared.change, expected_revision=expected, conflict=prepared.conflict)


__all__ = ['Catalog', 'LibraryError', 'digest', 'now', 'sync_dir', 'atomic_yaml', 'lock',
           'TABLES', 'empty', 'validate', 'WorkingState']
