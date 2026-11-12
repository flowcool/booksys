"""Decision values, proof checks and pure ownership projection."""
from __future__ import annotations
import copy
import re
from pathlib import Path
from typing import Any
import yaml
from library.media_paths import pieces_root
from library.primitives import LibraryError, digest

WORK_FIELDS = ('title', 'author', 'series', 'number', 'summary')
EDITION_FIELDS = ('publisher', 'language', 'date', 'revision', 'isbn', 'reference')
FIELDS = (*WORK_FIELDS, *EDITION_FIELDS)


def read(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise LibraryError('Symlink in resolution evidence')
    value = yaml.safe_load(path.read_text()) if path.exists() else {}
    if not isinstance(value, dict):
        raise LibraryError('Invalid resolution evidence')
    return value


def text(value: Any, limit: int = 500) -> str:
    if not isinstance(value, str) or len(value) > limit or '\x00' in value:
        raise LibraryError('Invalid bounded text')
    return value.strip()


def identifier(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch('[A-Za-z0-9_-]{1,100}', value):
        raise LibraryError('Invalid identifier')
    return value


def active_claims(state: dict[str, Any], asset: str) -> set[str]:
    return {v['edition'] for v in state['variants'].values()
            if v['asset'] == asset and v.get('active', True)}


def apply_resolution(state: dict[str, Any], record: dict[str, Any]) -> None:
    """Pure chronological projection; publication belongs to copy journals."""
    if record['action'] == 'resolution_result':
        target = state['assets'][record['target']]
        if target.get('resolution', {}).get('decision') == record['values']['decision']:
            target['resolution'].update(record['values'])
        return
    values, key = record['values'], record['target']
    asset = state['assets'][key]
    wid, eid = values['work_id'], values['edition_id']
    old = asset.get('identity')
    if old and old['edition'] != eid:
        state['variants'][old['variant']]['active'] = False
        previous = state['editions'][old['edition']]
        if previous.get('selected') == old['variant']:
            previous.pop('selected', None)
    if values.get('edit_shared'):
        state['works'][wid].update(copy.deepcopy(values['work']))
        state['editions'][eid].update(copy.deepcopy(values['edition']))
    state['works'].setdefault(wid, copy.deepcopy(values['work']))
    state['editions'].setdefault(eid, {**copy.deepcopy(values['edition']), 'work': wid,
                                     'evidence': {'basis': 'asset claims'}})
    variant = digest({'asset': key, 'edition': eid})
    state['variants'][variant] = {'asset': key, 'edition': eid, 'active': True,
                                  'evidence': values['proof']}
    asset['identity'] = {'work': wid, 'edition': eid, 'variant': variant,
                         'evidence': {'decision': record['id']}}
    if values['select']:
        state['editions'][eid]['selected'] = variant
    elif values.get('previous_selected') is not None:
        state['editions'][eid]['selected'] = values['previous_selected']
    if record['resolution_inputs'] == digest({'identity_result': asset.get('identity_result'),
                                            'assertions': asset.get('assertions', [])}):
        asset['contested'] = False
    asset['resolution'] = {'decision': record['id'], 'status': 'pending',
                           'editions': record['affected_editions']}


def verify_record_evidence(store: Any, record: dict[str, Any]) -> None:
    """Immutable decision proof is mandatory for both replay and pending overlays."""
    pages = record['values'].get('evidence', {}).get('pages', []) if record['action'] == 'identify_work' else (
        record['values'].get('proof', []) if record['action'] == 'resolve' else [])
    for proof in pages:
        piece = read(pieces_root(store.directory) / (identifier(proof['page']) + '.yaml'))
        if digest(piece) != proof['page'] or piece.get('asset') != record['target']:
            raise LibraryError('Decision proof missing or altered')
    if record['action'] == 'resolve':
        for notice in record['values'].get('notices', []):
            piece = read(pieces_root(store.directory) / (identifier(notice) + '.yaml'))
            if digest(piece) != notice:
                raise LibraryError('Decision notice missing or altered')


def dependencies(state: dict[str, Any], key: str, values: dict[str, Any]) -> str:
    asset = state['assets'][key]
    editions = {values['edition_id']}
    if asset.get('identity'):
        editions.add(asset['identity']['edition'])
    fields = ('sha256', 'size', 'format', 'status', 'pages', 'page_manifest', 'identity',
              'assertions', 'identity_result', 'contested', 'disposition', 'overrides', 'cover',
              'work_result', 'printed')
    edition_values = {eid: state['editions'].get(eid) for eid in editions}
    works = {wid: state['works'].get(wid) for wid in
             {values['work_id'], *(e['work'] for e in edition_values.values() if e)}}
    mapping = {eid: {k: e.get(k) for k in ('isbn', 'reference')} for eid, e in state['editions'].items()
               if (values['edition'].get('reference') and e.get('reference') == values['edition']['reference'])
               or (values['edition'].get('isbn') and e.get('isbn') == values['edition']['isbn'])}
    origins = {oid: {k: state['origins'][oid].get(k) for k in
               ('pid', 'fid', 'path', 'sha256', 'availability', 'parent_origin', 'parent_sha256', 'member')}
               for oid in asset['origins']}
    asset_fields = {k: asset.get(k) for k in fields}
    if asset_fields.get('identity'):
        asset_fields['identity'] = {k: asset_fields['identity'][k] for k in ('work', 'edition', 'variant')}
    return digest({'asset': asset_fields, 'origins': origins,
                   'editions': edition_values, 'works': works, 'mapping': mapping,
                   'claims': sorted(active_claims(state, key))})


def gate(state: dict[str, Any], asset_id: str, decision: str | None, role: str | None) -> None:
    identity = state['assets'][asset_id].get('identity', {})
    eid = identity.get('edition')
    for key, asset in state['assets'].items():
        resolution = asset.get('resolution', {})
        if resolution.get('status') in ('pending', 'blocked') and (key == asset_id or eid in resolution.get('editions', [])):
            if resolution.get('decision') != decision or resolution['status'] == 'blocked':
                raise LibraryError('Pending or blocked operator decision gates publication')
    if decision:
        if role not in ('create', 'replace', 'withdraw', 'restore'):
            raise LibraryError('Invalid decision operation role')
