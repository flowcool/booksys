"""Versioned catalogue, optimistic writes, immutable decision evidence."""
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
from library.effects import apply_effect


class Catalog:
    def __init__(self, directory: Path, *, store: Store | None = None):
        from library.store import Store
        self.directory = directory
        self.path = directory / 'library.yaml'
        self.decisions = directory / 'library-decisions'
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
        if state['ledger'] != state.original['ledger']:
            change.ledger = list(state['ledger'])
        return PreparedResult(change, copy.deepcopy(state.conflict), int(state['revision']))

    def save(self, state: dict[str, Any], *, expected: int) -> None:
        prepared = self.prepare(state)
        self._store.commit(prepared.change, expected_revision=expected, conflict=prepared.conflict)

    def ledger(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        self.decisions.mkdir(parents=True, exist_ok=True)
        for path in sorted(self.decisions.glob('*.yaml')):
            record = yaml.safe_load(path.read_text())
            if not isinstance(record, dict) or 'id' not in record:
                raise LibraryError('Invalid decision record')
            payload = {k: v for k, v in record.items() if k != 'id'}
            if (record['id'] != digest(payload) or record.get('schema') not in (1, 2) or
                    record.get('sequence') != len(records) + 1 or
                    path.name != f"{len(records)+1:09d}-{record['id']}.yaml" or
                    record.get('previous') != (records[-1]['id'] if records else None)):
                raise LibraryError('Decision evidence lost or altered')
            records.append(record)
        indexed = self._store.decisions()
        if len(indexed) > len(records) or any(record != records[index] for index,record in enumerate(indexed)):
            raise LibraryError('Decision ledger is incomplete; admission suspended')
        return records

    def append_decision(self, payload: dict[str, Any], *, conflict: dict[str, Any] | None = None) -> dict[str, Any]:
        """Caller owns the process decision exclusion; immutable bytes precede indexing."""
        backend = self.backend()
        self.decisions.mkdir(parents=True, exist_ok=True)
        # A retained suffix is resumed with the same immutable input only.
        indexed=backend.decision_head()
        sequence=indexed['sequence'] if indexed else 0
        if indexed:
            head=self.decisions/f"{sequence:09d}-{indexed['id']}.yaml"
            if not head.is_file() or yaml.safe_load(head.read_text())!=indexed:
                raise LibraryError('Decision evidence lost or altered')
        # Startup audits the full prefix. Appends inspect only the predecessor and
        # at most one retained suffix payload, under the existing process lock.
        suffix=[]
        for path in self.decisions.glob('*.yaml'):
            try:position=int(path.name.split('-',1)[0])
            except ValueError:raise LibraryError('Invalid decision record') from None
            if position>sequence:
                suffix.append(path)
                if len(suffix)>1:
                    raise LibraryError('Reconcile interrupted decision append before confirmation')
        if suffix:
            retained: dict[str,Any]=yaml.safe_load(suffix[0].read_text())
            if not isinstance(retained,dict):
                raise LibraryError('Invalid decision record')
            actual={key:value for key,value in retained.items()
                    if key not in ('schema','sequence','previous','id')}
            expected={**actual,'schema':2 if actual.get('action') in ('resolve','resolution_result') else 1,
                      'sequence':sequence+1,'previous':indexed['id'] if indexed else None}
            if (actual!=payload or retained!={**expected,'id':digest(expected)}
                    or suffix[0].name!=f"{sequence+1:09d}-{retained['id']}.yaml"):
                raise LibraryError('Reconcile interrupted decision append before confirmation')
            backend.index_decision(retained)
            return retained
        record = backend.reserve_decision(payload,conflict=conflict)
        path = self.decisions / f"{record['sequence']:09d}-{record['id']}.yaml"
        if not path.exists():
            atomic_yaml(path, record, exclusive=True)
        elif yaml.safe_load(path.read_text()) != record:
            raise LibraryError('Decision differs from retained immutable evidence')
        backend.index_decision(record)
        return record

    def project_pending(self, *, locked: bool = False) -> None:
        backend = self.backend()
        with nullcontext() if locked else lock(self.directory / 'decisions.lock'):
            # The append may have committed its reservation before writing any
            # immutable bytes. Recover that exact payload before chain indexing.
            for record in backend.pending_decisions():
                path = self.decisions / f"{record['sequence']:09d}-{record['id']}.yaml"
                if not path.exists():
                    atomic_yaml(path,record,exclusive=True)
                elif yaml.safe_load(path.read_text()) != record:
                    raise LibraryError('Decision differs from retained immutable evidence')
            # Recovery indexes retained immutable bytes without repeating an effect.
            for record in self.ledger():
                backend.index_decision(record)
            with backend._database.connection() as db:
                _, applied = backend.check(db)
            for record in backend.decisions(after=len(applied)):
                if record.get('completion_task'):
                    break  # The owning task must project this result in its completion transaction.
                values = record.get('values', {})
                refs = {'assets': [record['target']]}
                for table, key in (('works', 'work_id'), ('editions', 'edition_id'), ('works', 'source_work_id')):
                    if values.get(key):
                        refs.setdefault(table, []).append(values[key])
                refs.setdefault('editions', []).extend(record.get('affected_editions', []))
                claims={field:[str(values['edition'][field])] for field in ('isbn','reference') if values.get('edition',{}).get(field)}
                state = self.result_set(record['action'],refs,claims=claims)
                if record['sequence'] != len(state['ledger']) + 1:
                    raise LibraryError('Non-prefix decision projection')
                from library.decision_model import verify_record_evidence
                verify_record_evidence(self, record)
                apply_effect(state, record, replay=True)
                state['ledger'].append(record['id'])
                prepared = self.prepare(state)
                from dataclasses import asdict
                from library.codec import encode
                from library.store import _projection
                capability=_projection.set((encode(record),encode(asdict(prepared.change))))
                try:
                    backend.commit(prepared.change, expected_revision=prepared.revision,
                                   conflict=prepared.conflict, commit_key='decision-project:' + record['id'])
                finally:
                    _projection.reset(capability)

    def replay(self) -> dict[str, Any]:
        """Audit immutable evidence and project the pending prefix forward."""
        from library.decision_model import verify_record_evidence
        with lock(self.directory / 'decisions.lock'):
            for record in self.ledger():
                verify_record_evidence(self, record)
            self.project_pending(locked=True)
        return self.read()


__all__ = ['Catalog', 'LibraryError', 'digest', 'now', 'sync_dir', 'atomic_yaml', 'lock',
           'TABLES', 'empty', 'validate', 'WorkingState', 'apply_effect']
