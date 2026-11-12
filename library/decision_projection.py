"""Explicit full replay for legacy compatibility and recovery."""
from typing import Any
from library.media_paths import pieces_root
from library.primitives import LibraryError, digest, validate
from library.decision_model import read, identifier

def project_state(store: Any) -> dict[str, Any]:
    state: dict[str, Any] = store.read()
    records = store.ledger()
    from library.effects import apply_effect
    # Rebuild decision-owned fields from chronological history, without replacing
    # intake, automatic identities, published references or copy operation state.
    resolution_targets = {r['target'] for r in records if r['action'] == 'resolve'}
    for variant in state['variants'].values():
        if variant['asset'] in resolution_targets:
            variant['active'] = False
    if any(item not in {r['id'] for r in records} for item in state['ledger']):
        raise LibraryError('Snapshot references missing decisions')
    for record in records:
        if record['action'] == 'identify_work':
            for proof in record['values']['evidence'].get('pages', []):
                piece = read(pieces_root(store.directory) / (identifier(proof['page']) + '.yaml'))
                if digest(piece) != proof['page'] or piece.get('asset') != record['target']:
                    raise LibraryError('Work decision proof missing or altered')
        if record['action'] == 'resolve':
            for notice in record['values'].get('notices', []):
                value = read(pieces_root(store.directory) / (identifier(notice) + '.yaml'))
                if digest(value) != notice:
                    raise LibraryError('Decision notice missing or altered')
            for proof in record['values']['proof']:
                piece = read(pieces_root(store.directory) / (identifier(proof['page']) + '.yaml'))
                if digest(piece) != proof['page']:
                    raise LibraryError('Decision evidence missing or altered')
        apply_effect(state, record, replay=True)
    state['ledger'] = [r['id'] for r in records]
    validate(state)
    return state
