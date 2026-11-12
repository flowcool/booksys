"""Printed evidence stays separate from embedded metadata and manual decisions."""
from __future__ import annotations

from typing import Any

from library.primitives import LibraryError




def read_printed(path: Any, inspection: dict[str, Any]) -> dict[str, Any]:
    """Read eight validated archive pages in a stage subprocess."""
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path
    from library.primitives import LibraryError
    from library.readers import fingerprint, sha256
    path = Path(path).absolute()
    if (any(p.is_symlink() for p in (path, *path.parents)) or
            inspection.get('status') != 'validated' or inspection.get('format') not in ('cbz', 'cbr', 'pdf')):
        raise LibraryError('Printed evidence requires a validated comic input')
    before = fingerprint(path)
    expected = inspection['sha256']
    if sha256(path) != expected:
        raise LibraryError('Printed evidence source changed')
    manifest = inspection.get('page_manifest', [])
    selected = list({p['name']: p for p in [*manifest[:4], *manifest[-4:]]}.values())
    if inspection['format'] == 'pdf':
        count = inspection['pages']
        selected = [{'page': i} for i in sorted(set([*range(1, min(4, count) + 1), *range(max(1, count - 3), count + 1)]))]
    if not selected:
        raise LibraryError('Printed evidence requires a validated page manifest')
    from library.decode import run
    result = run('ocr', path, {'pages': selected})
    if result.get('status') == 'pending':
        return {**result, 'asset': expected}
    if fingerprint(path) != before or sha256(path) != expected:
        raise LibraryError('Printed evidence source changed during OCR')
    if result.pop('input_sha256', None) != expected:
        raise LibraryError('Printed reader inspected another source')
    if result.get('status') == 'ok':
        if inspection['format'] == 'pdf':
            if [p.get('page') for p in result['pages']] != [p['page'] for p in selected]:
                raise LibraryError('Printed PDF page selection changed')
        elif [(p['name'], p['sha256']) for p in result['pages']] != [(p['name'], p['sha256']) for p in selected]:
            raise LibraryError('Printed reader page evidence changed')
    result['asset'] = expected
    return result


def prepare_printed(store: Any, asset_id: str, reception: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    from library.primitives import LibraryError
    from pathlib import Path
    from library.origins import resolve_asset
    state = store.result_set('identify', {'assets': [asset_id]})
    asset = state['assets'][asset_id]
    result = read_printed(resolve_asset(store, Path(reception), state, asset_id), asset)
    asset.setdefault('printed_attempts', []).append({k: v for k, v in result.items() if k != 'pages'})
    asset['printed_attempts'] = asset['printed_attempts'][-10:]
    if result['status'] == 'ok':
        asset['printed'] = result
    return state, result


def scan_printed(store: Any, asset_id: str, reception: Any) -> dict[str, Any]:
    state, result = prepare_printed(store, asset_id, reception)
    store.save(state, expected=state['revision'])
    return result
