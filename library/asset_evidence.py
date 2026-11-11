"""Bind discovery labels to exact tracked files, independent of native queue state."""
from __future__ import annotations

from pathlib import PurePosixPath
import re
from typing import Any

from library.primitives import LibraryError


def bind_evidence(state: dict[str, Any], asset_id: str) -> dict[str, Any]:
    asset = state['assets'][asset_id]
    if asset.get('sha256') != asset_id:
        raise LibraryError('Evidence asset hash mismatch')
    result: dict[str, Any] = {'asset': asset_id, 'asset_bound': True, 'labels': [],
                              'discovery_queries': [], 'provenance': {}, 'identity_conflicts': {}}

    def label(value: Any, source: str) -> None:
        if not isinstance(value, str) or not value.strip():
            return
        value = value.strip()
        if re.fullmatch(r'[0-9a-fA-F]{64}', value):
            return  # Retained bundle storage names carry no bibliographic evidence.
        if value not in result['labels']:
            result['labels'].append(value)
        result['provenance'].setdefault('labels', []).append(
            {'value': value, 'source': source, 'scope': 'asset'})
        from library.names import METHOD, parse_name
        for field, parsed in parse_name(value).items():
            result['provenance'].setdefault(field, []).append(
                {'value': parsed, 'source': source, 'scope': 'asset', 'method': METHOD})

    embedded = asset.get('embedded', {})
    for xml_name, field in (('Title', 'title'), ('Series', 'series'), ('Number', 'number')):
        value = embedded.get(xml_name)
        if isinstance(value, str) and value.strip():
            result[field] = value.strip()
            result['provenance'][field] = [{'value': value.strip(),
                'source': 'embedded:' + asset_id, 'scope': 'asset'}]
    origins = []
    for oid in asset.get('origins', []):
        origin = state['origins'].get(oid)
        if not origin or origin.get('sha256') != asset_id:
            raise LibraryError('Evidence origin does not reference the exact asset')
        origins.append((oid, origin))
        name = origin.get('member') if origin.get('parent_origin') else origin.get('path')
        if isinstance(name, str):
            label(PurePosixPath(name).stem, 'origin:' + oid)
    for source, hint in asset.get('captured_hints', {}).items():
        if not isinstance(hint, dict):
            continue
        bound = any(source == origin.get('source') and not origin.get('parent_origin')
                    or source == str(origin.get('source')) + '#bundle-member=' + oid
                    and hint.get('parent_source') == origin.get('source')
                    for oid, origin in origins)
        if bound:
            label(hint.get('title'), source)
        for value in (hint.get('parent_title'), hint.get('title')):
            if isinstance(value, str) and value.strip() and value not in result['discovery_queries']:
                result['discovery_queries'].append(value)
    # A plain standalone name is qualified only when the directly captured title
    # agrees with the exact source file. Derived bundle hints cannot add this proof.
    from library.work_identity import normalized, informative
    def standalone(value: str) -> str:
        value = re.sub(r'\.(?:pdf|cbz|cbr)$', '', value, flags=re.I)
        value = re.sub(r'^Z[zZ](?=[A-ZÀ-Ý])', '', value)
        value = re.sub(r"\b([LD])_(?=[A-ZÀ-Ý])", r"\1'", value)
        value = value.replace('_', ' ')
        value = re.sub(r'\s*\((?:19|20)\d{2}\)\s*$', '', value)
        return ' '.join(value.split())
    if not any(field in result['provenance'] for field in ('title', 'number')):
        for oid, origin in origins:
            if origin.get('parent_origin'):
                continue
            hint = asset.get('captured_hints', {}).get(origin['source'], {}).get('title')
            filename = standalone(PurePosixPath(origin['path']).name)
            if (isinstance(hint, str) and informative(filename) and len(filename.split()) >= 2
                    and not re.search(r'\b(?:tomes?|volumes?|int[ée]grale|pages?|pack)\b|\d', filename, re.I)
                    and normalized(filename, 'title') == normalized(standalone(hint), 'title')):
                result['title'] = filename
                result['provenance']['title'] = [{'value': filename, 'source': 'origin:' + oid,
                    'scope': 'asset', 'method': 'standalone-file-and-capture-agreement-1'},
                    {'value': standalone(hint), 'source': origin['source'], 'scope': 'asset'}]
    return result
