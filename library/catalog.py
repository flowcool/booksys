"""Versioned catalogue, optimistic writes, immutable decision evidence."""
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


class LibraryError(Exception):
    """A failed gate. Preserve evidence and stop dependent operations."""


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


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
            yaml.safe_dump(value, out, allow_unicode=True, sort_keys=True)
            out.flush()
            os.fsync(out.fileno())
        if exclusive:
            os.chmod(temporary, 0o444)
            os.link(temporary, path)
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
    with path.open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise LibraryError('Another library operation is active') from exc
        yield


TABLES = ('works', 'editions', 'variants', 'assets', 'artifacts', 'origins', 'cases', 'operations')


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
    requirements = {'works': ('title',), 'editions': ('work', 'language', 'publisher', 'evidence'),
                    'variants': ('edition', 'asset'), 'assets': ('sha256', 'size', 'format', 'origins'),
                    'origins': ('source', 'pid', 'fid', 'path', 'sha256')}
    for table, fields in requirements.items():
        for key, record in state[table].items():
            if any(field not in record for field in fields):
                raise LibraryError(f'Missing {table} fields: {key}')
    for edition in state['editions'].values():
        if edition['work'] not in state['works']:
            raise LibraryError('Edition references missing work')
    for variant in state['variants'].values():
        if variant['edition'] not in state['editions'] or variant['asset'] not in state['assets']:
            raise LibraryError('Variant references missing edition or asset')
    for key, asset in state['assets'].items():
        if not re.fullmatch('[0-9a-f]{64}', key) or asset['sha256'] != key:
            raise LibraryError('Invalid asset identity')
        if any(origin not in state['origins'] for origin in asset['origins']):
            raise LibraryError('Asset references missing origin')
        if type(asset['size']) is not int or asset['size'] <= 0 or asset['format'] not in ('pdf', 'cbz', 'cbr'):
            raise LibraryError('Invalid asset size or format')
        identity = asset.get('identity')
        if identity and (identity.get('work') not in state['works'] or
                         identity.get('edition') not in state['editions'] or
                         identity.get('variant') not in state['variants']):
            raise LibraryError('Asset identity references missing entity')
    for key, origin in state['origins'].items():
        if (not all(isinstance(origin[k], str) for k in ('source', 'path')) or
                type(origin['pid']) is not int or type(origin['fid']) is not int):
            raise LibraryError('Invalid origin IDs or path')
        path = Path(origin['path'])
        if path.is_absolute() or '..' in path.parts:
            raise LibraryError('Unsafe origin path')
        if origin['sha256'] is not None and not re.fullmatch('[0-9a-f]{64}', origin['sha256']):
            raise LibraryError('Invalid origin hash')
    for edition_id, edition in state['editions'].items():
        selected = edition.get('selected')
        if selected and (selected not in state['variants'] or state['variants'][selected]['edition'] != edition_id):
            raise LibraryError('Selected variant belongs to a different edition')


class Catalog:
    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / 'library.yaml'
        self.decisions = directory / 'library-decisions'

    def read(self) -> dict[str, Any]:
        value = yaml.safe_load(self.path.read_text()) if self.path.exists() else empty()
        validate(value)
        return dict(value)

    def save(self, state: dict[str, Any], *, expected: int) -> None:
        validate(state)
        with lock(self.directory / 'library.lock'):
            current = self.read()
            if current['revision'] != expected:
                raise LibraryError('Stale catalogue revision')
            result = copy.deepcopy(state)
            result['revision'] = expected + 1
            atomic_yaml(self.path, result)

    def ledger(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        self.decisions.mkdir(parents=True, exist_ok=True)
        for path in sorted(self.decisions.glob('*.yaml')):
            record = yaml.safe_load(path.read_text())
            if not isinstance(record, dict) or 'id' not in record:
                raise LibraryError('Invalid decision record')
            payload = {k: v for k, v in record.items() if k != 'id'}
            if (record['id'] != digest(payload) or record.get('schema') != 1 or
                    record.get('sequence') != len(records) + 1 or
                    path.name != f"{len(records)+1:09d}-{record['id']}.yaml" or
                    record.get('previous') != (records[-1]['id'] if records else None)):
                raise LibraryError('Decision evidence lost or altered')
            records.append(record)
        checkpoint = self.directory / 'library-ledger-head.yaml'
        if checkpoint.exists():
            head = yaml.safe_load(checkpoint.read_text())
            ids = [r['id'] for r in records]
            if head['count'] > len(ids) or (head['count'] and ids[head['count'] - 1] != head['head']):
                raise LibraryError('Decision ledger is incomplete; admission suspended')
        return records

    def append_decision(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Caller owns decision.lock. Ledger precedes effects and checkpoint."""
        records = self.ledger()
        record = {**payload, 'schema': 1, 'sequence': len(records) + 1,
                  'previous': records[-1]['id'] if records else None}
        record['id'] = digest(record)
        atomic_yaml(self.decisions / f"{record['sequence']:09d}-{record['id']}.yaml", record, exclusive=True)
        atomic_yaml(self.directory / 'library-ledger-head.yaml',
                    {'count': len(records) + 1, 'head': record['id']})
        return record

    def replay(self) -> dict[str, Any]:
        with lock(self.directory / 'decisions.lock'):
            state = self.read()
            records = self.ledger()
            available = {r['id'] for r in records}
            if any(item not in available for item in state['ledger']):
                raise LibraryError('Snapshot references missing decisions')
            # Overrides are a projection of canonical decisions, not an index
            # authority. Rebuild them even when a restored index claims replay.
            for asset in state['assets'].values():
                asset['overrides'] = {}
            for record in records:
                apply_effect(state, record, replay=True)
            state['ledger'] = [record['id'] for record in records]
            self.save(state, expected=state['revision'])
            return self.read()


def apply_effect(state: dict[str, Any], decision: dict[str, Any], *, replay: bool = False) -> None:
    action, target, values = decision['action'], decision['target'], decision['values']
    if target not in state['assets']:
        raise LibraryError('Decision target missing after restore; restore inventory first')
    asset = state['assets'][target]
    if action == 'identify':
        work, edition = values['work'], values['edition']
        wid, eid = values['work_id'], values['edition_id']
        if any(not edition.get(k) for k in ('language', 'publisher', 'evidence')) or not work.get('title'):
            raise LibraryError('Manual identity requires work and edition evidence')
        claims = {v['edition'] for v in state['variants'].values() if v['asset'] == target}
        if claims and claims != {eid}:
            raise LibraryError('Asset ownership conflict')
        if not replay and eid in state['editions'] and state['editions'][eid]['work'] != wid:
            raise LibraryError('Edition ownership conflict')
        state['works'][wid] = copy.deepcopy(work)
        managed = {k: v for k, v in state['editions'].get(eid, {}).items() if k in ('selected', 'publication')}
        state['editions'][eid] = {**copy.deepcopy(edition), 'work': wid, **managed}
        vid = digest({'asset': target, 'edition': eid})
        state['variants'][vid] = {'edition': eid, 'asset': target}
        asset['identity'] = {'work': wid, 'edition': eid, 'variant': vid, 'evidence': edition['evidence']}
        asset['disposition'] = 'pending'
        conflict_inputs = digest({'identity_result': asset.get('identity_result'),
                                  'assertions': asset.get('assertions', [])})
        if not replay or decision.get('resolution_inputs') == conflict_inputs:
            asset['contested'] = False
    elif action == 'override':
        asset.setdefault('overrides', {}).update(values)
    elif action in ('exclude', 'defer'):
        asset['disposition'] = action
    elif action == 'select':
        identity = asset.get('identity')
        if not identity:
            raise LibraryError('Unknown edition cannot be selected')
        state['editions'][identity['edition']]['selected'] = identity['variant']
    elif action == 'merge_work':
        source = values['source_work_id']
        destination = values['work_id']
        if source not in state['works'] or destination not in state['works'] or source == destination:
            raise LibraryError('Merge requires two distinct known works')
        if not values.get('evidence'):
            raise LibraryError('Merge evidence is required')
        for edition in state['editions'].values():
            if edition['work'] == source:
                edition['work'] = destination
        for entry in state['assets'].values():
            if entry.get('identity', {}).get('work') == source:
                entry['identity']['work'] = destination
        state['works'][source]['merged_into'] = destination
    elif action == 'separate_edition':
        identity = asset.get('identity')
        if not identity or not values.get('evidence') or not values.get('work', {}).get('title'):
            raise LibraryError('Separation requires an edition and explicit work evidence')
        wid = values['work_id']
        state['works'][wid] = copy.deepcopy(values['work'])
        state['editions'][identity['edition']]['work'] = wid
        for entry in state['assets'].values():
            if entry.get('identity', {}).get('edition') == identity['edition']:
                entry['identity']['work'] = wid
    else:
        raise LibraryError('Unsupported decision action')
