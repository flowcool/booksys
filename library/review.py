"""Regenerable review reports and closed-schema operator decision batches."""
from __future__ import annotations

import copy
from contextlib import nullcontext
import html
import json
import re
from typing import Any

from library.catalog import Catalog
from library.primitives import LibraryError, digest, lock, now, validate, WorkingState
from library.effects import apply_effect

ACTIONS = {'identify', 'identify_work', 'override', 'select', 'exclude', 'defer', 'merge_work', 'separate_edition'}


def cases_for(state: dict[str, Any]) -> dict[str, Any]:
    cases = {}
    for key, asset in state['assets'].items():
        identity = asset.get('identity')
        reason = ('identity_contested' if asset.get('contested') else
                  'identity_or_edition_unknown' if not identity else 'metadata_or_variant_review')
        cid = digest({'asset': key, 'kind': 'asset_review'})
        inputs = {'asset': asset, 'origins': {oid: {k: v for k, v in state['origins'][oid].items()
                  if k != 'observed_at'} for oid in asset['origins']},
                  'edition': state['editions'].get(identity['edition']) if identity else None}
        inputs['works'] = state['works']
        inputs['edition_ownership'] = {key: value['work'] for key, value in state['editions'].items()}
        candidates = [*asset.get('work_result', {}).get('candidates', []),
                      *asset.get('identity_result', {}).get('candidates', [])]
        candidates = list({digest(candidate): candidate for candidate in candidates}.values())
        cases[cid] = {'id': cid, 'asset': key, 'version': digest(inputs), 'reason': reason,
                      'evidence': inputs, 'candidates': candidates}
    for oid, origin in state['origins'].items():
        if origin['availability'] != 'validated':
            cid = digest({'origin': oid, 'kind': 'file_review'})
            cases[cid] = {'id': cid, 'origin': oid, 'version': digest(origin),
                          'reason': origin.get('reason', origin['availability']), 'evidence': origin}
    return cases


def report(store: Catalog) -> dict[str, Any]:
    state = store.read()
    store.ledger()  # A missing ledger is never hidden by a regenerated report.
    cases = cases_for(state)
    backend = store.backend()
    if backend is not None:
        for case in cases.values():
            if case.get('asset'):
                indexed = backend.case(case['asset'])
                case.update(version=indexed['version'], evidence=indexed['evidence'])
    return {'schema': 1, 'source_text_is_untrusted': True, 'created_at': now(),
            'catalogue_revision': state['revision'], 'cases': cases}


def render_report(value: dict[str, Any]) -> str:
    lines = ['# Library review', '', 'External text is untrusted data.', '']
    fields = ('work_ref', 'edition_ref', 'title', 'author', 'number', 'language', 'publisher', 'date', 'revision', 'url')
    labels = ('Work ID', 'Edition ID', 'Title', 'Author', 'Volume', 'Language', 'Publisher', 'Date', 'Revision', 'Source URL')
    for case in value['cases'].values():
        lines.extend(['## Case ' + case['id'], '', 'Reason: ' + html.escape(case['reason']), ''])
        if case.get('candidates'):
            lines.append('<table><thead><tr>' + ''.join('<th>' + label + '</th>' for label in labels) + '</tr></thead><tbody>')
            for candidate in case['candidates']:
                lines.append('<tr>' + ''.join('<td>' + html.escape(str(candidate.get(field) or 'unknown')) + '</td>'
                                               for field in fields) + '</tr>')
            lines.extend(['</tbody></table>', ''])
        else:
            lines.extend(['No resolved edition candidate. Use a qualified explicit source ID or retain pending.', ''])
        lines.extend(['<details><summary>Evidence and decision version</summary><pre>',
                      html.escape(json.dumps(case, ensure_ascii=False, indent=2)), '</pre></details>', ''])
    return '\n'.join(lines)


def validate_request(request: Any, author: str) -> None:
    if author != 'flowcool':
        raise LibraryError('Decision author must be the operator flowcool')
    if not isinstance(request, dict) or set(request) != {'request_id', 'case', 'version', 'action', 'values'}:
        raise LibraryError('Invalid decision keys')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', str(request['request_id'])):
        raise LibraryError('Invalid request ID')
    if request['action'] not in ACTIONS or not isinstance(request['values'], dict):
        raise LibraryError('Unsupported action or values')
    action, values = request['action'], request['values']
    if action == 'identify_work':
        if (set(values) != {'work', 'evidence'} or not isinstance(values['work'], dict)
                or not isinstance(values['evidence'], dict) or not values['evidence']
                or not isinstance(values['work'].get('title'), str) or not values['work']['title'].strip()
                or set(values['work']) - {'title', 'series', 'number', 'author', 'summary', 'family'}
                or any(not isinstance(v, str) or len(v) > 4000 for v in values['work'].values())
                or len(json.dumps(values)) > 16384):
            raise LibraryError('Work confirmation requires bounded work fields and explicit evidence')
        if values['work'].get('family') not in (None, 'BD', 'Manga', 'Comics'):
            raise LibraryError('Unsupported work family')
    if action in ('select', 'exclude', 'defer') and values:
        raise LibraryError('Action takes no values')
    if action == 'override':
        if not values or set(values) - {'title', 'summary', 'genres', 'public', 'author'}:
            raise LibraryError('Unsupported override field')
        for field, value in values.items():
            if field == 'genres':
                if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                    raise LibraryError('Genres must be explicit strings')
            elif value is not None and not isinstance(value, str):
                raise LibraryError('Override must be text or explicit unknown')
    if action == 'identify':
        if set(values) != {'work_id', 'edition_id', 'work', 'edition'}:
            raise LibraryError('Invalid identity decision')
        if not all(re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', str(values[k])) for k in ('work_id', 'edition_id')):
            raise LibraryError('Invalid stable identity')
        if not isinstance(values['work'], dict) or not isinstance(values['edition'], dict):
            raise LibraryError('Invalid identity records')
        if (set(values['work']) - {'title', 'series', 'author', 'number', 'summary', 'genres', 'public'} or
                set(values['edition']) - {'language', 'publisher', 'date', 'revision', 'evidence'}):
            raise LibraryError('Unsupported identity record fields')
        for field, value in values['work'].items():
            if field == 'genres':
                if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                    raise LibraryError('Identity genres must be text values')
            elif not isinstance(value, str):
                raise LibraryError('Identity work fields must be text')
        for field, value in values['edition'].items():
            if field == 'evidence':
                if not isinstance(value, dict) or not value:
                    raise LibraryError('Identity evidence must be a nonempty record')
            elif not isinstance(value, str):
                raise LibraryError('Identity edition fields must be text')
    if action in ('merge_work', 'separate_edition'):
        expected = {'work_id', 'evidence'} | ({'work'} if action == 'separate_edition' else {'source_work_id'})
        if set(values) != expected or not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', str(values['work_id'])):
            raise LibraryError('Invalid merge or separation decision')


def decide(store: Catalog, requests: list[dict[str, Any]], *, author: str, locked: bool = False) -> list[dict[str, Any]]:
    if store.backend() is not None:
        return decide_indexed(store, requests, author=author, locked=locked)
    results = []
    # Serializes batch decisions, not hashing, copies, network or the collector.
    with nullcontext() if locked else lock(store.directory / 'decisions.lock'):
        for request in requests:
            result = {'request_id': request.get('request_id') if isinstance(request, dict) else None, 'status': 'rejected'}
            try:
                validate_request(request, author)
                records = store.ledger()
                previous = next((r for r in records if r.get('request_id') == request['request_id']), None)
                if previous:
                    if previous['request'] != request or previous['author'] != author:
                        raise LibraryError('Request ID reused with different content')
                    state = store.read()
                    if previous['id'] not in state['ledger']:
                        for record in records:
                            if record['id'] not in state['ledger']:
                                apply_effect(state, record, replay=True)
                                state['ledger'].append(record['id'])
                        store.save(state, expected=state['revision'])
                    result.update(status='applied', decision=previous['id'], replay=True)
                    results.append(result)
                    continue
                state = store.read()
                cases = cases_for(state)
                case = cases.get(request['case'])
                if case is None or case['version'] != request['version']:
                    result.update(status='stale', reason='Case inputs changed or case missing')
                    results.append(result)
                    continue
                if 'asset' not in case:
                    raise LibraryError('Unvalidated file cannot receive bibliographic admission')
                payload = {'request_id': request['request_id'], 'request': copy.deepcopy(request),
                    'target': case['asset'], 'action': request['action'], 'values': request['values'],
                    'author': author, 'time': now(), 'case_version': case['version']}
                if request['action'] == 'identify':
                    payload['resolution_inputs'] = digest({'identity_result': state['assets'][case['asset']].get('identity_result'),
                                                          'assertions': state['assets'][case['asset']].get('assertions', [])})
                # Verify effects on a copy before committing canonical evidence.
                proposed = copy.deepcopy(state)
                apply_effect(proposed, payload)
                validate(proposed)
                record = store.append_decision(payload)
                proposed['ledger'].append(record['id'])
                store.save(proposed, expected=state['revision'])
                result.update(status='applied', decision=record['id'])
            except (LibraryError, ValueError, KeyError, TypeError) as exc:
                result['reason'] = str(exc)
            results.append(result)
    return results


def decide_indexed(store: Catalog, requests: list[dict[str, Any]], *, author: str,
                  locked: bool = False) -> list[dict[str, Any]]:
    """Same closed decision interface with indexed cases and declared closures."""
    from library.decision_model import verify_record_evidence
    backend = store.backend()
    if backend is None:
        raise LibraryError('PostgreSQL authority required')
    results = []
    with nullcontext() if locked else lock(store.directory / 'decisions.lock'):
        for request in requests:
            result = {'request_id': request.get('request_id') if isinstance(request, dict) else None, 'status': 'rejected'}
            try:
                validate_request(request, author)
                store.project_pending(locked=True)
                previous = backend.decisions(request_id=request['request_id'])
                if previous:
                    record = previous[0]
                    if record.get('request') != request or record.get('author') != author:
                        raise LibraryError('Request ID reused with different content')
                    verify_record_evidence(store, record)
                    store.project_pending(locked=True)
                    results.append({**result, 'status': 'applied', 'decision': record['id'], 'replay': True})
                    continue
                store.project_pending(locked=True)
                with backend._database.connection() as db:
                    binding = db.execute('SELECT kind,target FROM case_bindings WHERE id=%s', (request['case'],)).fetchone()
                if not binding or binding[0] != 'assets':
                    raise LibraryError('Unvalidated file cannot receive bibliographic admission')
                asset = binding[1]
                case = backend.case(asset)
                if request['version'] != case['version']:
                    results.append({**result, 'status': 'stale', 'reason': 'Case inputs changed'})
                    continue
                values = request['values']
                refs = {'assets': [asset]}
                for kind, field in (('works', 'work_id'), ('works', 'source_work_id'), ('editions', 'edition_id')):
                    if values.get(field):
                        refs.setdefault(kind, []).append(values[field])
                state = store.working_set(request['action'], refs)
                if not isinstance(state,WorkingState) or state.conflict is None:
                    raise LibraryError('Decision requires original scoped inputs')
                payload = {'request_id': request['request_id'], 'request': copy.deepcopy(request),
                    'target': asset, 'action': request['action'], 'values': values, 'author': author,
                    'time': now(), 'case_version': case['version']}
                if request['action'] == 'identify':
                    payload['resolution_inputs'] = digest({'identity_result': state['assets'][asset].get('identity_result'),
                                                          'assertions': state['assets'][asset].get('assertions', [])})
                proposed = copy.deepcopy(state)
                apply_effect(proposed, payload)
                validate(proposed)
                record = store.append_decision(payload,conflict=state.conflict)
                store.project_pending(locked=True)
                result.update(status='applied', decision=record['id'])
            except (LibraryError, ValueError, KeyError, TypeError) as exc:
                result['reason'] = str(exc)
            results.append(result)
    return results
