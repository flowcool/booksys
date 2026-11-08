"""Bind discovery labels to exact tracked files, independent of native queue state."""
from __future__ import annotations

from pathlib import PurePosixPath
import re
from typing import Any

from library.catalog import LibraryError


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
    return result
