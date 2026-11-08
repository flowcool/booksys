"""Operator previews and ledger-only confirmation; cycle-owned application."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any
import yaml

from library.catalog import Catalog, LibraryError, atomic_yaml, digest, lock, now, validate

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


def project_state(store: Catalog) -> dict[str, Any]:
    state = store.read()
    records = store.ledger()
    from library.catalog import apply_effect
    # Rebuild decision-owned fields from chronological history, without replacing
    # intake, automatic identities, published references or copy operation state.
    resolution_targets = {r['target'] for r in records if r['action'] == 'resolve'}
    for variant in state['variants'].values():
        if variant['asset'] in resolution_targets:
            variant['active'] = False
    if any(item not in {r['id'] for r in records} for item in state['ledger']):
        raise LibraryError('Snapshot references missing decisions')
    for record in records:
        if record['action'] == 'resolve':
            for notice in record['values'].get('notices', []):
                value = read(store.directory / 'library-resolution/pieces' / (identifier(notice) + '.yaml'))
                if digest(value) != notice:
                    raise LibraryError('Decision notice missing or altered')
            for proof in record['values']['proof']:
                piece = read(store.directory / 'library-resolution/pieces' / (identifier(proof['page']) + '.yaml'))
                if digest(piece) != proof['page']:
                    raise LibraryError('Decision evidence missing or altered')
        apply_effect(state, record, replay=True)
    state['ledger'] = [r['id'] for r in records]
    validate(state)
    return state


def dependencies(state: dict[str, Any], key: str, values: dict[str, Any]) -> str:
    asset = state['assets'][key]
    editions = {values['edition_id']}
    if asset.get('identity'):
        editions.add(asset['identity']['edition'])
    fields = ('sha256', 'size', 'format', 'status', 'pages', 'page_manifest', 'identity',
              'assertions', 'identity_result', 'contested', 'disposition', 'overrides', 'cover')
    edition_values = {eid: state['editions'].get(eid) for eid in editions}
    works = {e['work']: state['works'][e['work']] for e in edition_values.values() if e}
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


class Resolution:
    def __init__(self, store: Catalog, reception: Path):
        self.store, self.reception = store, reception
        self.directory = store.directory / 'library-resolution'
        self.pieces = self.directory / 'pieces'
        self.previews = self.directory / 'previews'

    def state(self) -> dict[str, Any]:
        return project_state(self.store)

    def piece(self, value: dict[str, Any]) -> str:
        # All admission/eviction and confirmation share this order.
        with lock(self.store.directory / 'decisions.lock'):
            with lock(self.store.directory / 'library-evidence.lock'):
                return self._piece(value)

    def _piece(self, value: dict[str, Any]) -> str:
        encoded = json.dumps(value, ensure_ascii=False).encode()
        if len(encoded) > 3 * 1024**2:
            raise LibraryError('Evidence piece exceeds limit')
        key = digest(value)
        path = self.pieces / (key + '.yaml')
        created = not path.exists()
        if created:
            atomic_yaml(path, value, exclusive=True)
        try:
            self.trim_previews()
        except LibraryError:
            if created and path.exists():
                path.unlink()  # Never admitted/referenced while decisions.lock is held.
            raise
        if not path.exists():
            raise LibraryError('Temporary evidence budget exhausted; reopen the case')
        return key

    def load_piece(self, key: str) -> dict[str, Any]:
        identifier(key)
        value = read(self.pieces / (key + '.yaml'))
        if digest(value) != key:
            raise LibraryError('Evidence piece missing or altered')
        return value

    def page(self, asset_id: str, index: int) -> dict[str, Any]:
        with lock(self.store.directory / 'library-proof.lock', blocking=False):
            return self._page(asset_id, index)

    def _page(self, asset_id: str, index: int) -> dict[str, Any]:
        from library.origins import resolve_asset
        from library.process import bounded_run
        from library.sandbox import native_prefix, PYTHON
        state = self.state()
        asset = state['assets'][asset_id]
        if asset.get('status') != 'validated' or type(index) is not int or not 1 <= index <= asset['pages']:
            raise LibraryError('Proof requires a validated page')
        source = resolve_asset(self.store, self.reception, state, asset_id)
        command = native_prefix(source, ocr=True) + [PYTHON, '-m', 'library.proof_worker',
                                                    '/tmp/input', source.suffix.lower(), str(index)]
        result = bounded_run(command, timeout=180, stdout_limit=3 * 1024**2)
        if result.returncode:
            raise LibraryError('Isolated proof page reader failed')
        value = json.loads(result.stdout)
        from library.readers import sha256
        if value.get('asset') != asset_id or sha256(source) != asset_id:
            raise LibraryError('Proof source changed')
        if asset['format'] != 'pdf' and value['source_sha256'] != asset['page_manifest'][index - 1]['sha256']:
            raise LibraryError('Proof page hash differs from catalogue')
        key = self.piece(value)
        return {'id': key, **{k: v for k, v in value.items() if k != 'png'}}

    def validate_values(self, state: dict[str, Any], asset_id: str, value: Any) -> dict[str, Any]:
        keys = {'edition_id', 'work_id', 'work', 'edition', 'select', 'proof', 'reason', 'supersedes', 'restore'}
        notices = []
        edit_shared = False
        if isinstance(value, dict) and 'edit_shared' in value:
            value = dict(value)
            edit_shared = value.pop('edit_shared')
            if type(edit_shared) is not bool:
                raise LibraryError('Invalid shared correction flag')
        if isinstance(value, dict) and 'candidate' in value:
            value = dict(value)
            candidate_id = value.pop('candidate')
            if candidate_id:
                captured = self.load_piece(identifier(candidate_id))['candidate']
                notices.append(candidate_id)
                value['work'] = {k: captured[k] for k in WORK_FIELDS if captured.get(k)}
                value['edition'] = {k: captured[k] for k in EDITION_FIELDS if captured.get(k)}
                value['edition']['reference'] = str(captured.get('edition_ref') or captured.get('url'))
        if not isinstance(value, dict) or set(value) != keys or type(value['select']) is not bool:
            raise LibraryError('Invalid resolution fields')
        value = copy.deepcopy(value)
        for part, allowed in (('work', WORK_FIELDS), ('edition', EDITION_FIELDS)):
            if not isinstance(value[part], dict) or set(value[part]) - set(allowed):
                raise LibraryError('Invalid bibliography fields')
            value[part] = {k: text(v, 4000 if k == 'summary' else 500) for k, v in value[part].items() if v}
        eid = value['edition_id']
        if eid:
            identifier(eid)
            if eid not in state['editions']:
                raise LibraryError('Unknown edition')
            edition = state['editions'][eid]
            if value['work_id'] != edition['work']:
                raise LibraryError('Edition ownership conflict')
            supplied_work, supplied_edition = value['work'], value['edition']
            value['work'], value['edition'] = (copy.deepcopy(state['works'][edition['work']]),
                                              {k: edition[k] for k in EDITION_FIELDS if k in edition})
            if edit_shared:
                publications = [e for e in state['editions'].values() if e['work'] == edition['work'] and e.get('publication')]
                if len(publications) > 1:
                    raise LibraryError('Shared correction affects multiple published editions; retain pending')
                value['work'].update(supplied_work)
                value['edition'].update(supplied_edition)
        else:
            if not value['work'].get('title') or any(not value['edition'].get(k) for k in ('publisher', 'language')):
                raise LibraryError('Title, publisher and language are required')
            reference = value['edition'].get('reference') or digest({'work': value['work_id'] or value['work'], 'edition': value['edition']})
            eid = digest({'manual_edition': reference})
            value['edition_id'] = eid
            if eid in state['editions']:
                raise LibraryError('Edition already exists; choose its local identity')
            if value['work_id']:
                identifier(value['work_id'])
                if value['work_id'] not in state['works']:
                    raise LibraryError('Unknown work')
                value['work'] = copy.deepcopy(state['works'][value['work_id']])
            else:
                value['work_id'] = digest({'manual_work': value['work']})
        if state['works'].get(value['work_id'], {}).get('merged_into'):
            raise LibraryError('Choose the current merged work')
        for other in state['editions'].values():
            for field in ('reference', 'isbn'):
                if (value['edition'].get(field) and other.get(field) == value['edition'][field]
                        and other is not state['editions'].get(eid)):
                    raise LibraryError('Edition reference already owned')
        if value['edition'].get('isbn'):
            from library.metadata import isbn13
            if not isbn13(value['edition']['isbn']):
                raise LibraryError('Invalid ISBN')
        value['reason'] = text(value['reason'], 4000)
        if not value['reason'] or not isinstance(value['proof'], list) or not 1 <= len(value['proof']) <= 20:
            raise LibraryError('Readable proof and reason are mandatory')
        pages = set()
        for proof in value['proof']:
            if not isinstance(proof, dict) or set(proof) != {'page', 'field', 'text'}:
                raise LibraryError('Invalid proof fields')
            if proof['field'] not in FIELDS or not text(proof['text'], 1000):
                raise LibraryError('Proof needs a distinguishing field and transcription')
            proof['text'] = text(proof['text'], 1000)
            page = self.load_piece(proof['page'])
            if page.get('asset') != asset_id:
                raise LibraryError('Proof belongs to another asset')
            pages.add(proof['page'])
        required_fields = {'title', 'publisher', 'language'}
        required_fields.update(k for k in ('author', 'series', 'number') if value['work'].get(k))
        required_fields.update(k for k in ('date', 'revision', 'isbn') if value['edition'].get(k))
        if required_fields - {p['field'] for p in value['proof']}:
            raise LibraryError('Each distinguishing field needs a page transcription: ' + ', '.join(sorted(required_fields)))
        if len(pages) > 4:
            raise LibraryError('At most four proof pages')
        for field in ('supersedes', 'restore'):
            if value[field] is not None:
                identifier(value[field])
        value['notices'] = notices
        value['edit_shared'] = edit_shared
        return dict(value)

    def preview(self, asset_id: str, values: dict[str, Any]) -> dict[str, Any]:
        identifier(asset_id)
        with lock(self.store.directory / 'decisions.lock'):
            state = self.state()
            asset = state['assets'][asset_id]
            if asset.get('status') != 'validated':
                raise LibraryError('Unvalidated asset')
            values = self.validate_values(state, asset_id, values)
            eid = values['edition_id']
            if values['restore']:
                operation = read(self.store.directory / 'library-operations' / (values['restore'] + '.yaml'))
                if (operation.get('phase') != 'committed' or not operation.get('previous')
                        or operation['previous']['asset'] != asset_id or operation.get('edition') != eid):
                    raise LibraryError('Recovery revision does not match the selected asset and edition')
            old = asset.get('identity', {}).get('edition')
            affected = sorted({eid, *([old] if old else [])})
            self.check_pending(state, asset_id, affected, values['supersedes'])
            published = [(key, e['publication']) for key, e in state['editions'].items()
                         if e.get('publication', {}).get('asset') == asset_id]
            target_publication = state['editions'].get(eid, {}).get('publication')
            if target_publication and target_publication['asset'] != asset_id and (
                    (old and old != eid) or any(previous_eid != eid for previous_eid, _ in published)):
                raise LibraryError('Target edition already published')
            if published and published[0][0] != eid and not values['select']:
                raise LibraryError('Published transfer must select this asset')
            effect = 'pending'
            roles: list[str] = []
            if values['select'] and asset.get('disposition') not in ('exclude', 'defer'):
                if values['restore']:
                    effect, roles = 'restore', ['withdraw', 'restore'] if published and published[0][0] != eid else ['restore']
                elif published and published[0][0] != eid:
                    effect, roles = 'transfer', ['withdraw', 'create']
                elif target_publication:
                    effect, roles = 'replace', ['replace']
                else:
                    effect, roles = 'create', ['create']
            proposed = copy.deepcopy(state)
            apply_resolution(proposed, {'action': 'resolve', 'id': 'preview', 'target': asset_id, 'values': values,
                'resolution_inputs': digest({'identity_result': asset.get('identity_result'), 'assertions': asset.get('assertions', [])}),
                'affected_editions': affected})
            from library.publish import fiche_for
            from library.xmlmetadata import comicinfo
            from library.transform import VERSION
            expected_export = digest({'asset': asset_id,
                'xml': hashlib.sha256(comicinfo(proposed, asset_id)).hexdigest(),
                'fiche': hashlib.sha256(fiche_for(proposed, asset_id)).hexdigest(),
                'cover': asset.get('cover'), 'tool': VERSION})
            same_export = target_publication and target_publication.get('export_version') == expected_export
            if effect == 'replace' and same_export:
                effect, roles = 'unchanged', []
            import re
            slug = re.sub(r'[^a-z0-9]+', '-', str(proposed['works'][values['work_id']]['title']).lower()).strip('-')[:48] or 'work'
            target = target_publication['target'] if target_publication else slug + '--' + eid
            result = {'target': target, 'before_identity': asset.get('identity'),
                      'after_identity': proposed['assets'][asset_id]['identity'],
                      'selection_before': state['editions'].get(eid, {}).get('selected'),
                      'selection_after': proposed['editions'][eid].get('selected'),
                      'files': ['book.cbz', 'fiche.md', 'couverture.png'] if asset.get('cover') else ['book.cbz', 'fiche.md'],
                      'artifact_hash': 'Computed only after conversion and verification',
                      'recovery': [p['operation'] for _, p in published],
                      'absence_warning': 'Withdrawal precedes creation; failure can leave this book absent until recovery' if effect == 'transfer' else '',
                      'asset': asset_id, 'values': values, 'before': dependencies(state, asset_id, values),
                      'old_publications': published, 'effect': effect, 'roles': roles,
                      'affected_editions': affected, 'created_at': now(),
                      'resolution_inputs': digest({'identity_result': asset.get('identity_result'),
                                                   'assertions': asset.get('assertions', [])})}
            result['id'] = digest(result)
            self.previews.mkdir(parents=True, exist_ok=True)
            # Replace the active preview for this case. Durable pieces are never deleted.
            atomic_yaml(self.previews / (asset_id + '.yaml'), result)
            with lock(self.store.directory / 'library-evidence.lock'):
                self.trim_previews()
            return result

    def trim_previews(self) -> None:
        durable = {key for r in self.store.ledger() if r['action'] == 'resolve'
                   for key in [*[p['page'] for p in r['values']['proof']], *r['values'].get('notices', [])]}
        previews = sorted(self.previews.glob('*.yaml'), key=lambda p: p.stat().st_mtime_ns)
        def temporary_size() -> int:
            return sum(p.stat().st_size for p in self.pieces.glob('*.yaml') if p.stem not in durable)
        while temporary_size() > 64 * 1024**2 and previews:
            previews.pop(0).unlink()
            retained = durable | {key for p in previews for key in
                [*[proof['page'] for proof in read(p)['values']['proof']], *read(p)['values'].get('notices', [])]}
            for piece in self.pieces.glob('*.yaml'):
                if piece.stem not in retained:
                    piece.unlink()
        if temporary_size() > 64 * 1024**2:
            raise LibraryError('Temporary evidence budget exhausted')

    def check_pending(self, state: dict[str, Any], asset: str, editions: list[str], supersedes: str | None) -> None:
        for key, entry in state['assets'].items():
            resolution = entry.get('resolution', {})
            if resolution.get('status') in ('pending', 'blocked') and (key == asset or
                    set(resolution.get('editions', [])) & set(editions)):
                if resolution.get('decision') != supersedes or key != asset:
                    raise LibraryError('A correction is pending; explicitly supersede it')

    def confirm(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, dict) or set(request) != {'request_id', 'preview'}:
            raise LibraryError('Invalid confirmation fields')
        request_id, preview_id = identifier(request['request_id']), identifier(request['preview'])
        with lock(self.store.directory / 'decisions.lock'):
            records = self.store.ledger()
            for record in records:
                if record.get('request_id') == request_id:
                    if record.get('preview') != preview_id:
                        raise LibraryError('Request ID reused with different content')
                    return {'status': 'recorded', 'decision': record['id']}
            candidates = [read(p) for p in self.previews.glob('*.yaml')]
            preview = next((p for p in candidates if p['id'] == preview_id), None)
            if preview is None:
                raise LibraryError('Stale preview')
            state = self.state()
            asset_id, values = preview['asset'], preview['values']
            if dependencies(state, asset_id, values) != preview['before']:
                raise LibraryError('Stale preview; inputs changed')
            # Copy lock also covers hashing and the post-rename commit window.
            with lock(self.store.directory / 'library-copy.lock', blocking=False):
                self.check_pending(state, asset_id, preview['affected_editions'], values['supersedes'])
                for proof in values['proof']:
                    self.load_piece(proof['page'])
                for operation in (self.store.directory / 'library-operations').glob('*.yaml'):
                    if read(operation).get('phase') not in ('committed', 'interrupted', 'capacity_aborted'):
                        raise LibraryError('Reconcile existing operation before confirmation')
                record = {'request_id': request_id, 'preview': preview_id, 'target': asset_id,
                          'action': 'resolve', 'values': values, 'author': 'flowcool', 'time': now(),
                          **{k: preview[k] for k in ('effect', 'roles', 'old_publications', 'affected_editions', 'resolution_inputs')}}
                total = sum(len(json.dumps(self.load_piece(k)).encode()) for k in
                            {*[p['page'] for p in values['proof']], *values.get('notices', [])})
                if total + len(json.dumps(values).encode()) > 13 * 1024**2:
                    raise LibraryError('Decision evidence exceeds 13 MiB')
                proposed = copy.deepcopy(state)
                apply_resolution(proposed, {**record, 'id': 'proposed'})
                record['expected_after'] = dependencies(proposed, asset_id, values)
                validate(proposed)
                record = self.store.append_decision(record)
                return {'status': 'recorded', 'decision': record['id']}

    def project(self) -> dict[str, Any]:
        with lock(self.store.directory / 'decisions.lock'):
            return self._project()

    def _project(self) -> dict[str, Any]:
        state = self.state()
        before = self.store.read()
        if state != before:
            self.store.save(state, expected=state['revision'])
        return self.store.read()

    def search(self, query: str) -> dict[str, Any]:
        with lock(self.store.directory / 'library-search.lock', blocking=False):
            return self._search(query)

    def _search(self, query: str) -> dict[str, Any]:
        from library.metadata import Providers, isbn13
        query = text(query, 200)
        if not query:
            raise LibraryError('Empty search')
        providers = Providers(self.store.directory / 'library-provider-cache', budget=12,
                              shared=self.store.directory)
        attempts = []
        if isbn13(query):
            attempts = [providers.source('bnf', query), providers.edition(query)]
        elif re.fullmatch('OL[0-9]+M', query):
            attempts = [providers.edition(query)]
        else:
            attempts = providers.resolve_discovery(providers.discover(query))
        candidates = []
        for attempt in attempts:
            candidate = attempt.get('candidate')
            if not candidate:
                continue
            piece = self.piece({'candidate': candidate})
            candidates.append({'label': str(candidate.get('title', 'unknown')) + ' / ' + str(candidate.get('publisher', 'unknown')),
                'candidate': piece, 'notice': {k: candidate.get(k) for k in ('source', 'url', 'edition_ref', 'retrieved_at')}, 'work': {k: candidate[k] for k in WORK_FIELDS if candidate.get(k)},
                'edition': {k: candidate[k] for k in EDITION_FIELDS if candidate.get(k)}})
        return {'candidates': candidates, 'remaining': providers.budget,
                'reason': 'No exact edition returned; retain pending or provide manual proof' if not candidates else ''}

    def outcome(self, record: dict[str, Any], status: str, reason: str) -> None:
        with lock(self.store.directory / 'decisions.lock'):
            current = self.state()['assets'][record['target']].get('resolution', {})
            if current.get('status') == status and current.get('reason') == reason:
                return
            self.store.append_decision({'action': 'resolution_result', 'target': record['target'],
                'values': {'decision': record['id'], 'status': status, 'reason': reason},
                'author': 'cycle', 'time': now()})
        self.project()

    def apply(self, publisher: Any, *, limit: int = 8) -> int:
        """Caller owns the cycle lock. Reconcile before any catalogue projection."""
        publisher.reconcile()
        self.project()
        count = 0
        for record in self.store.ledger():
            if record['action'] != 'resolve':
                continue
            state = self.store.read()
            current = state['assets'][record['target']].get('resolution', {})
            if current.get('decision') != record['id'] or current.get('status') != 'pending':
                continue
            if count >= limit:
                break
            count += 1
            try:
                key, eid = record['target'], record['values']['edition_id']
                if record['effect'] in ('pending', 'unchanged'):
                    self.outcome(record, 'applied', 'Identity recorded; publication ' + record['effect'])
                    continue
                completed = {op.get('role'): op for op in state['operations'].values()
                             if op.get('decision') == record['id'] and op['status'] == 'committed'}
                comparison = copy.deepcopy(state)
                if 'withdraw' in completed:
                    # Normalize only this decision's own committed removal. All
                    # other preview dependencies still have to match after restart.
                    for old_eid, publication in record['old_publications']:
                        comparison['editions'][old_eid]['publication'] = publication
                final_role_done = any(r in completed for r in ('create', 'replace', 'restore'))
                if not final_role_done and dependencies(comparison, key, record['values']) != record['expected_after']:
                    raise LibraryError('Decision dependencies changed; preview again')
                if 'withdraw' in record['roles'] and 'withdraw' not in completed:
                    # Deterministic conversion proof before removing the readable old copy.
                    if record['effect'] == 'restore':
                        recovery = publisher.load_operation(record['values']['restore'])
                        from library.publish import verify
                        verify(publisher.recovery / recovery['id'], recovery['previous']['manifest'])
                        publisher.resolve_origin(state, key)
                    else:
                        publisher.preflight(key)
                    old_eid = record['old_publications'][0][0]
                    publisher.withdraw(old_eid, expected=self.store.read()['revision'],
                                       decision=record['id'], role='withdraw')
                if record['effect'] == 'restore':
                    if 'restore' not in completed:
                        publisher.restore(record['values']['restore'], expected=self.store.read()['revision'],
                                          decision=record['id'], role='restore')
                elif not any(role in completed for role in ('create', 'replace')):
                    publisher.publish(key, replace=record['effect'] == 'replace', decision=record['id'],
                                      role='replace' if record['effect'] == 'replace' else 'create')
                self.outcome(record, 'applied', 'Decision applied; publication verified')
            except (LibraryError, OSError) as exc:
                # Reconcile an uncertain copy before any outcome changes the
                # catalogue revision expected by its unfinished commit.
                publisher.reconcile()
                done = {op.get('role') for op in self.store.read()['operations'].values()
                        if op.get('decision') == record['id'] and op['status'] == 'committed'}
                if set(record['roles']) <= done:
                    self.outcome(record, 'applied', 'Decision applied; interrupted response reconciled')
                else:
                    self.outcome(record, 'blocked', str(exc))
        return count


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


def case_view(service: Resolution, key: str) -> dict[str, Any]:
    state = service.state()
    asset = state['assets'][key]
    if asset.get('status') != 'validated':
        raise LibraryError('Only validated assets can be resolved')
    candidates = []
    for candidate in asset.get('identity_result', {}).get('candidates', [])[:10]:
        candidates.append({'label': 'Candidat : ' + str(candidate.get('title', 'unknown')) + ' / ' + str(candidate.get('publisher', 'unknown')),
            'candidate': service.piece({'candidate': candidate}),
            'notice': {k: candidate.get(k) for k in ('source', 'url', 'edition_ref', 'retrieved_at')},
            'work': {k: candidate[k] for k in WORK_FIELDS if candidate.get(k)},
            'edition': {k: candidate[k] for k in EDITION_FIELDS if candidate.get(k)}})
    editions = [{'label': 'Édition locale : ' + state['works'][e['work']]['title'] + ' / ' + e['publisher'],
                 'edition_id': eid, 'work_id': e['work'], 'work': state['works'][e['work']],
                 'edition': {k: e[k] for k in EDITION_FIELDS if k in e}}
                for eid, e in state['editions'].items() if not state['works'][e['work']].get('merged_into')][:100]
    recovery = []
    for path in (service.store.directory / 'library-operations').glob('*.yaml'):
        operation = read(path)
        if operation.get('asset') == key and operation.get('previous') and operation.get('phase') == 'committed':
            recovery.append({'id': operation['id'], 'label': operation['id'] + ' / ' + operation['kind']})
    return {'asset': key, 'identity': {k: asset['identity'][k] for k in ('work', 'edition', 'variant')} if asset.get('identity') else None, 'resolution': asset.get('resolution'),
            'publications': {eid: e['publication'] for eid, e in state['editions'].items()
                             if e.get('publication', {}).get('asset') == key},
            'candidates': candidates, 'editions': editions, 'recovery': recovery,
            'works': [{'id': wid, 'label': str(w['title'])[:500],
                       'fields': {k: str(w[k])[:4000] for k in WORK_FIELDS if k in w}}
                      for wid, w in state['works'].items() if not w.get('merged_into')][:100],
            'variants': {vid: {'edition': v['edition'], 'asset': v['asset'],
                'format': state['assets'][v['asset']]['format'], 'size': state['assets'][v['asset']]['size'],
                'pages': state['assets'][v['asset']].get('pages'),
                'selected': state['editions'][v['edition']].get('selected') == vid,
                'origins': [{'pid': state['origins'][oid]['pid'], 'fid': state['origins'][oid]['fid'],
                             'name': Path(state['origins'][oid]['path']).name}
                            for oid in state['assets'][v['asset']]['origins']]}
                         for vid, v in state['variants'].items() if v.get('active', True)},
            'unknowns': 'Missing fields stay unknown. Local pages are evidence; candidates are unconfirmed.'}
