"""Validate work-only operator confirmation through the existing decision batch."""
from __future__ import annotations

from typing import Any

from library.primitives import LibraryError, lock
from library.review import decide


def confirm_work(service: Any, value: dict[str, Any]) -> dict[str, Any]:
    with lock(service.store.directory / 'decisions.lock'):
        with lock(service.store.directory / 'library-evidence.lock'):
            return _confirm_work(service, value)


def _confirm_work(service: Any, value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != {'asset', 'case', 'version', 'request_id', 'work', 'proof', 'reason'}:
        raise LibraryError('Invalid work confirmation fields')
    state = service.state(value['asset'])
    asset = state['assets'].get(value['asset'], {})
    if asset.get('status') != 'validated' or asset.get('identity') or asset.get('resolution'):
        raise LibraryError('Work confirmation requires a validated file without an edition decision')
    from library.review import cases_for
    backend = service.store.backend()
    case = backend.case(value['asset']) if backend is not None else cases_for(state).get(value['case'])
    if not case or case.get('asset') != value['asset']:
        raise LibraryError('Work confirmation case does not belong to this asset')
    if not isinstance(value['reason'], str) or not value['reason'].strip() or len(value['reason']) > 4000:
        raise LibraryError('Work confirmation requires a bounded justification')
    if not isinstance(value['proof'], list) or not 1 <= len(value['proof']) <= 8:
        raise LibraryError('Read and cite at least one local proof page')
    retained = []
    for proof in value['proof']:
        if not isinstance(proof, dict) or set(proof) != {'page', 'text'} or not isinstance(proof['text'], str) or not 1 <= len(proof['text']) <= 1000:
            raise LibraryError('Invalid work page concordance')
        page = service.load_piece(proof['page'])
        if page.get('asset') != value['asset'] or not page.get('source_sha256'):
            raise LibraryError('Work proof page belongs to another file')
        retained.append({**proof, 'asset': page['asset'], 'page_number': page['page'],
                         'source_sha256': page['source_sha256']})
    from library.origins import resolve_asset
    resolve_asset(service.store, service.reception, state, value['asset'])
    result = decide(service.store, [{'request_id': value['request_id'], 'case': value['case'],
        'version': value['version'], 'action': 'identify_work', 'values': {'work': value['work'],
        'evidence': {'asset': value['asset'], 'reason': value['reason'], 'pages': retained}}}], author='flowcool', locked=True)[0]
    if result['status'] != 'applied':
        raise LibraryError(result.get('reason', 'Case changed; refresh the work confirmation'))
    return result
