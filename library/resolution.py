"""Operator previews and ledger-only confirmation; cycle-owned application."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any
import yaml

from library.catalog import Catalog
from library.primitives import LibraryError, atomic_yaml, digest, lock, now, validate

from library.decision_model import (WORK_FIELDS, EDITION_FIELDS, FIELDS, read, text,
    identifier, active_claims, apply_resolution, verify_record_evidence, dependencies, gate)
from library.decision_projection import project_state


class Resolution:
    def __init__(self, store: Catalog, reception: Path):
        self.store, self.reception = store, reception
        self.directory = store.directory / 'library-resolution'
        from library.media_paths import pieces_root
        self.pieces = pieces_root(store.directory)
        self.previews = self.directory / 'previews'

    def state(self, asset: str | None = None, *, values: dict[str, Any] | None = None, options: bool = False) -> dict[str, Any]:
        backend = self.store.backend()
        if backend is not None:
            if asset is None:
                raise LibraryError('A targeted resolution asset is required')
            return backend.resolution_state(asset, values=values, options=options)
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
        key = identifier(key)
        base = os.path.realpath(self.pieces)
        candidate = os.path.join(base, key + '.yaml')
        normalized = os.path.realpath(candidate)
        if not normalized.startswith(base + os.sep):
            raise LibraryError('Evidence path escapes its directory')
        if normalized != candidate:
            raise LibraryError('Symlink in resolution evidence')
        value = read(Path(normalized))
        if digest(value) != key:
            raise LibraryError('Evidence piece missing or altered')
        return value

    def page(self, asset_id: str, index: int) -> dict[str, Any]:
        with lock(self.store.directory / 'library-proof.lock', blocking=False):
            from library.reader_client import endpoint
            if endpoint():
                from library.manual_reader import submit_page
                return submit_page(self.store, asset_id, index)
            return self._page(asset_id, index)

    def _page(self, asset_id: str, index: int) -> dict[str, Any]:
        from library.origins import resolve_asset
        from library.process import bounded_run
        from library.sandbox import native_prefix, PYTHON
        state = self.state(asset_id)
        asset = state['assets'][asset_id]
        if asset.get('status') != 'validated' or type(index) is not int or not 1 <= index <= asset['pages']:
            raise LibraryError('Proof requires a validated page')
        source = resolve_asset(self.store, self.reception, state, asset_id)
        command = native_prefix(source, ocr=True) + [PYTHON, '-m', 'library.proof_worker',
                                                    '/tmp/input', source.suffix.lower(), str(index)]
        from library.reader_client import endpoint, execute
        if endpoint():
            import subprocess
            result = subprocess.CompletedProcess(command, 0, json.dumps(execute('proof', source, {'page': index})).encode(), b'')
        else:
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
            from library.bibliographic import isbn13
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
            state = self.state(asset_id, values=values)
            asset = state['assets'][asset_id]
            if asset.get('status') != 'validated':
                raise LibraryError('Unvalidated asset')
            values = self.validate_values(state, asset_id, values)
            eid = values['edition_id']
            if values['restore']:
                from library.operational_state import CopyOperations
                operation = CopyOperations(self.store.backend()._database,'publication').get(values['restore'])
                if operation is None:
                    raise LibraryError('Publication restore operation unavailable')
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
                      'old_publications': [[eid,publication] for eid,publication in published], 'effect': effect, 'roles': roles,
                      'affected_editions': affected, 'created_at': now(),
                      'resolution_inputs': digest({'identity_result': asset.get('identity_result'),
                                                   'assertions': asset.get('assertions', [])})}
            result['id'] = digest(result)
            self.previews.mkdir(parents=True, exist_ok=True)
            # Replace the active preview for this case. Durable pieces are never deleted.
            from library.operational_state import ResolutionPreviews
            ResolutionPreviews(self.store.backend()._database).retain(result)
            backend = self.store.backend()
            if backend is not None:
                backend.bind_preview(result['id'], asset_id)
            with lock(self.store.directory / 'library-evidence.lock'):
                self.trim_previews()
            return result

    def trim_previews(self) -> None:
        backend = self.store.backend()
        if backend is not None:
            durable = backend.retained_pieces()
        else:
            records = self.store.ledger()
            durable = {key for r in records if r['action'] == 'resolve'
                   for key in [*[p['page'] for p in r['values']['proof']], *r['values'].get('notices', [])]}
            durable.update(p['page'] for r in records if r['action'] == 'identify_work'
                       for p in r['values']['evidence'].get('pages', []))
        if sum(p.stat().st_size for p in self.pieces.glob('*.yaml') if p.stem not in durable)>64*1024**2:
            raise LibraryError('Temporary evidence budget exhausted; retained evidence requires inspection')

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
            backend = self.store.backend()
            records = backend.decisions(request_id=request_id) if backend is not None else self.store.ledger()
            for record in records:
                if record.get('request_id') == request_id:
                    if record.get('preview') != preview_id:
                        raise LibraryError('Request ID reused with different content')
                    return {'status': 'recorded', 'decision': record['id']}
            from library.operational_state import ResolutionPreviews
            preview=ResolutionPreviews(self.store.backend()._database).get(preview_id)
            if preview is None:
                raise LibraryError('Stale preview')
            asset_id, values = preview['asset'], preview['values']
            state = self.state(asset_id, values=values)
            if dependencies(state, asset_id, values) != preview['before']:
                raise LibraryError('Stale preview; inputs changed')
            # Copy lock also covers hashing and the post-rename commit window.
            with lock(self.store.directory / 'library-copy.lock', blocking=False):
                self.check_pending(state, asset_id, preview['affected_editions'], values['supersedes'])
                for proof in values['proof']:
                    self.load_piece(proof['page'])
                from library.operational_state import CopyOperations
                for operation in CopyOperations(self.store.backend()._database,'publication').records():
                    if operation.get('phase') not in ('committed', 'interrupted', 'capacity_aborted'):
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
                record = self.store.append_decision(record, conflict=getattr(state,'conflict',None))
                return {'status': 'recorded', 'decision': record['id']}

    def project(self) -> dict[str, Any]:
        if self.store.backend() is not None:
            self.store.project_pending()
            return {'revision': self.store.revision}
        with lock(self.store.directory / 'decisions.lock'):
            return self._project()

    def _project(self) -> dict[str, Any]:
        if self.store.backend() is not None:
            self.store.project_pending()
            return {'revision': self.store.revision}
        state = self.state()
        before = self.store.read()
        if state != before:
            self.store.save(state, expected=state['revision'])
        return self.store.read()

    def search(self, query: str) -> dict[str, Any]:
        with lock(self.store.directory / 'library-search.lock', blocking=False):
            return self._search(query)

    def _search(self, query: str) -> dict[str, Any]:
        from library.metadata import Providers
        from library.bibliographic import isbn13
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

    def outcome(self, record: dict[str, Any], status: str, reason: str, *, prepared: list[Any] | None=None, task: Any=None) -> None:
        with lock(self.store.directory / 'decisions.lock'):
            state=self.state(record['target'])
            current=state['assets'][record['target']].get('resolution',{})
            if current.get('status')==status and current.get('reason')==reason:return
            result=prepared.pop() if prepared else None
            if prepared:
                raise LibraryError('Multiple terminal decision results require reconciliation')
            if result is not None:
                for kind,values in result.change.upserts.items():state[kind].update(copy.deepcopy(values))
                for kind,keys in result.change.deletes.items():
                    for key in keys:state[kind].pop(key,None)
            payload={'action':'resolution_result','target':record['target'],
                'values':{'decision':record['id'],'status':status,'reason':reason},'author':'cycle','time':now()}
            if task is not None:
                payload['completion_task']={'key':task.key,'generation':task.generation}
                payload['completion_conflict']=result.conflict if result is not None else getattr(state,'conflict',None)
                payload['completion_parent']={name:record.get(name) for name in ('id','expected_after','affected_editions')}
                payload['affected_editions']=record.get('affected_editions',[])
            backend=self.store.backend()
            retained=next((entry for entry in reversed(backend.decisions(action='resolution_result',target=record['target']))
                if entry.get('values')==payload['values'] and entry.get('completion_task')==payload.get('completion_task')),None)
            completion=retained or self.store.append_decision(payload,conflict=result.conflict if result is not None else getattr(state,'conflict',None))
            if prepared is not None:
                from library.effects import apply_effect
                from dataclasses import replace
                apply_effect(state,completion,replay=True)
                state['ledger'].append(completion['id'])
                terminal=self.store.prepare(state)
                if result is not None:
                    terminal=replace(terminal,conflict=result.conflict,completions=result.completions)
                prepared.append(terminal)
        if prepared is None:self.project()

    def apply(self, publisher: Any, *, limit: int = 8, asset_only: str | None = None, task: Any=None, reconcile_only: bool=False) -> int:
        """Caller owns the cycle lock. Reconcile before any catalogue projection."""
        publisher.completion_task=task
        publisher.reconcile(**({'asset': asset_only} if asset_only is not None else {}))
        self.project()
        count = 0
        backend = self.store.backend()
        for record in backend.decisions(action='resolve', target=asset_only) if backend is not None else self.store.ledger():
            if record['action'] != 'resolve' or (asset_only is not None and record['target'] != asset_only):
                continue
            state = self.state(record['target'], values=record['values'])
            current = state['assets'][record['target']].get('resolution', {})
            if current.get('decision') != record['id'] or current.get('status') != 'pending':
                continue
            if count >= limit:
                break
            count += 1
            try:
                key, eid = record['target'], record['values']['edition_id']
                if record['effect'] in ('pending', 'unchanged'):
                    self.outcome(record,'applied','Identity recorded; publication '+record['effect'],prepared=publisher.prepared,task=task)
                    continue
                completed = {op.get('role'): op for op in state['operations'].values()
                             if op.get('decision') == record['id'] and op['status'] == 'committed'}
                for terminal in publisher.prepared or []:
                    completed.update({op.get('role'):op for op in terminal.change.upserts.get('operations',{}).values() if op.get('decision')==record['id'] and op.get('status')=='committed'})
                comparison = copy.deepcopy(state)
                if 'withdraw' in completed:
                    # Normalize only this decision's own committed removal. All
                    # other preview dependencies still have to match after restart.
                    for old_eid, publication in record['old_publications']:
                        comparison['editions'][old_eid]['publication'] = publication
                final_role_done = any(r in completed for r in ('create', 'replace', 'restore'))
                if reconcile_only and not final_role_done:
                    continue  # Exact absence never authorizes a new filesystem effect in readback.
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
                    terminal_results=publisher.prepared
                    publisher.prepared=None
                    try:
                        publisher.withdraw(old_eid,expected=self.store.revision,decision=record['id'],role='withdraw')
                    finally:publisher.prepared=terminal_results
                if record['effect'] == 'restore':
                    if 'restore' not in completed:
                        publisher.restore(record['values']['restore'], expected=self.store.revision,
                                          decision=record['id'], role='restore')
                elif not any(role in completed for role in ('create', 'replace')):
                    publisher.publish(key, replace=record['effect'] == 'replace', decision=record['id'],
                                      role='replace' if record['effect'] == 'replace' else 'create')
                self.outcome(record,'applied','Decision applied; publication verified',prepared=publisher.prepared,task=task)
            except (LibraryError, OSError) as exc:
                # Reconcile an uncertain copy before any outcome changes the
                # catalogue revision expected by its unfinished commit.
                publisher.reconcile(**({'asset': asset_only} if asset_only is not None else {}))
                done = {op.get('role') for op in self.state(record['target'], values=record['values'])['operations'].values()
                        if op.get('decision') == record['id'] and op['status'] == 'committed'}
                if set(record['roles']) <= done:
                    self.outcome(record,'applied','Decision applied; interrupted response reconciled',prepared=publisher.prepared,task=task)
                else:
                    self.outcome(record,'blocked',str(exc),prepared=publisher.prepared,task=task)
        return count


def case_view(service: Resolution, key: str) -> dict[str, Any]:
    state = service.state(key, options=True)
    asset = state['assets'][key]
    if asset.get('status') != 'validated':
        raise LibraryError('Only validated assets can be resolved')
    candidates = []
    for candidate in asset.get('work_result', asset.get('identity_result', {})).get('candidates', [])[:10]:
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
    from library.operational_state import CopyOperations
    for operation in CopyOperations(service.store.backend()._database,'publication').records():
        if operation.get('asset') == key and operation.get('previous') and operation.get('phase') == 'committed':
            recovery.append({'id': operation['id'], 'label': operation['id'] + ' / ' + operation['kind']})
    from library.review import cases_for
    backend = service.store.backend()
    work_case = backend.case(key) if backend is not None else next(c for c in cases_for(state).values() if c.get('asset') == key)
    return {'asset': key, 'work_case': {'case': work_case['id'], 'version': work_case['version']},
            'identity': {k: asset['identity'][k] for k in ('work', 'edition', 'variant')} if asset.get('identity') else None, 'resolution': asset.get('resolution'),
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
