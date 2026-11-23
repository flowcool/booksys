"""Bind discovery labels to exact tracked files, independent of native queue state."""
from __future__ import annotations

from pathlib import PurePosixPath
import re
from typing import Any

from library.names import is_volume_title
from library.bdnex_protocol import bedetheque_album_id
from library.primitives import LibraryError


def bind_evidence(state: dict[str, Any], asset_id: str, *, include_capture: bool = True) -> dict[str, Any]:
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
        parsed = parse_name(value)
        suffix = parsed.pop('title_suffix_hint', None)
        raw_title = parsed.pop('raw_title', None)
        for field, field_value in parsed.items():
            entry = {'value': field_value, 'source': source, 'scope': 'asset', 'method': METHOD}
            if field == 'title' and suffix:
                entry.update(value=raw_title, title_suffix_hint=suffix, title_without_suffix=field_value)
            result['provenance'].setdefault(field, []).append(entry)

    embedded = asset.get('embedded', {})
    web = embedded.get('Web')
    if bedetheque_album_id(web):
        result['web'] = web
        result['provenance']['web'] = [{'value': web, 'scope': 'asset',
                                      'source': 'embedded:' + asset_id}]
    # Only explicit primary roles are eligible for a creator exemption.
    for xml_name, role in (('Writer', 'writer'), ('Penciller', 'penciller')):
        value = embedded.get(xml_name)
        if isinstance(value, str) and value.strip():
            result['provenance'][role] = [{'value': name.strip(), 'role': role,
                'source': 'embedded:' + asset_id, 'scope': 'asset'}
                for name in value.split(',') if name.strip()]
    for xml_name, field in (('Title', 'title'), ('Series', 'series'), ('Number', 'number')):
        value = embedded.get(xml_name)
        if isinstance(value, str) and value.strip():
            if field == 'title' and is_volume_title(value):
                continue
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
    has_file_number = bool(result['provenance'].get('number'))
    captured_hints = asset.get('captured_hints', {}) if include_capture else {}
    for source, hint in captured_hints.items():
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
    from library.work_identity import normalized, informative
    embedded_title = result.get('title', '')
    numbered_title = re.fullmatch(r'Tome\s+([0-9]{1,3})\s*[-–—:]\s*(.+)', embedded_title, re.I)
    if numbered_title and has_file_number:
        title = numbered_title[2].strip()
        numbers = {normalized(entry['value'], 'number') for entry in result['provenance'].get('number', [])}
        if (numbers == {normalized(numbered_title[1], 'number')}
                and informative(title) and not is_volume_title(title)):
            result['title'] = title
            # The embedded assertion is recorded before any origin or capture titles.
            result['provenance']['title'][0].update(
                value=title, raw_value=embedded_title, method='embedded-numbered-title-1')
    if not include_capture:
        return result
    # A plain standalone name is qualified only when the directly captured title
    # agrees with the exact source file. Derived bundle hints cannot add this proof.
    def standalone(value: str) -> str:
        value = re.sub(r'\.(?:pdf|cbz|cbr)$', '', value, flags=re.I)
        value = re.sub(r'^zz(?=[a-zÀ-ÿ])', '', value, flags=re.I)
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
            if (isinstance(hint, str) and informative(filename)
                    and not re.search(r'\b(?:tomes?|volumes?|int[ée]grale|pages?|pack)\b|\d', filename, re.I)
                    and normalized(filename, 'title') == normalized(standalone(hint), 'title')):
                result['title'] = filename
                result['provenance']['title'] = [{'value': filename, 'source': 'origin:' + oid,
                    'scope': 'asset', 'method': 'standalone-file-and-capture-agreement-1'},
                    {'value': standalone(hint), 'source': origin['source'], 'scope': 'asset'}]
    return result
