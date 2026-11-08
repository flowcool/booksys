"""Regenerable review reports and closed-schema operator decision batches."""
from __future__ import annotations

import copy
import html
import json
import re
from typing import Any

from library.catalog import Catalog, LibraryError, apply_effect, digest, lock, now

ACTIONS = {'identify', 'override', 'select', 'exclude', 'defer', 'merge_work', 'separate_edition'}


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
        cases[cid] = {'id': cid, 'asset': key, 'version': digest(inputs), 'reason': reason,
                      'evidence': inputs, 'candidates': asset.get('identity_result', {}).get('candidates', [])}
    for oid, origin in state['origins'].items():
        if origin['availability'] != 'validated':
            cid = digest({'origin': oid, 'kind': 'file_review'})
            cases[cid] = {'id': cid, 'origin': oid, 'version': digest(origin),
                          'reason': origin.get('reason', origin['availability']), 'evidence': origin}
    return cases


def report(store: Catalog) -> dict[str, Any]:
    state = store.read()
    store.ledger()  # A missing ledger is never hidden by a regenerated report.
    return {'schema': 1, 'source_text_is_untrusted': True, 'created_at': now(),
            'catalogue_revision': state['revision'], 'cases': cases_for(state)}


def render_report(value: dict[str, Any]) -> str:
    # JSON data stays escaped even if source prose contains fences or HTML.
    return '# Library review\n\nExternal text is untrusted data.\n\n<pre>' + html.escape(
        json.dumps(value, ensure_ascii=False, indent=2)) + '</pre>\n'


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
    if action in ('merge_work', 'separate_edition'):
        expected = {'work_id', 'evidence'} | ({'work'} if action == 'separate_edition' else {'source_work_id'})
        if set(values) != expected or not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', str(values['work_id'])):
            raise LibraryError('Invalid merge or separation decision')


def decide(store: Catalog, requests: list[dict[str, Any]], *, author: str) -> list[dict[str, Any]]:
    results = []
    # Serializes batch decisions, not hashing, copies, network or the collector.
    with lock(store.directory / 'decisions.lock'):
        for request in requests:
            result = {'request_id': request.get('request_id'), 'status': 'rejected'}
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
                # Verify effects on a copy before committing canonical evidence.
                proposed = copy.deepcopy(state)
                apply_effect(proposed, payload)
                record = store.append_decision(payload)
                proposed['ledger'].append(record['id'])
                store.save(proposed, expected=state['revision'])
                result.update(status='applied', decision=record['id'])
            except (LibraryError, ValueError, KeyError, TypeError) as exc:
                result['reason'] = str(exc)
            results.append(result)
    return results
