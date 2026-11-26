"""Evidence for choosing a reader copy of a confirmed work."""
from __future__ import annotations

import re
from typing import Any

from library.work_identity import IDENTITY_FIELDS, accepted_work, creator_names, informative, normalized


def identity(asset: dict[str, Any]) -> dict[str, Any]:
    work = accepted_work(asset) or {}
    conflicts = work.get('field_conflicts', {})
    if not work or set(conflicts) & {*IDENTITY_FIELDS, 'author', 'contributors'}:
        return {}
    refs: dict[str, set[str]] = {}
    for row in asset['work_result'].get('concordant_candidates', []):
        provider = row.get('source')
        ref = (row.get('provenance', {}).get('canonical_album_ref') or row.get('edition_ref')
               if provider == 'bedetheque' else row.get('work_ref'))
        if provider and ref:
            refs.setdefault(provider, set()).add(str(ref))
    fields = {field: normalized(work[field], field) for field in IDENTITY_FIELDS
              if work.get(field) not in (None, '')}
    if not informative(work.get('title')):
        fields.pop('title', None)
    return {'fields': fields, 'refs': refs, 'creators': creator_names(work),
            'edition': asset.get('edition') or (asset.get('identity') or {}).get('edition')}


def work_comparison(left: dict[str, Any], right: dict[str, Any]) -> tuple[bool, bool]:
    """Return whether there is a review lead and whether work agreement is strong."""
    if not left or not right:
        return False, False
    a, b = left['fields'], right['fields']
    shared = left['refs'].keys() & right['refs'].keys()
    reference = any(left['refs'][p] & right['refs'][p] for p in shared)
    numbered = a.get('series') and a.get('number') and all(a[k] == b.get(k) for k in ('series', 'number'))
    titled = a.get('title') and a['title'] == b.get('title')
    if not (reference or numbered or titled):
        return False, False
    if (any(a[k] != b[k] for k in a.keys() & b.keys())
            or any(not left['refs'][p] & right['refs'][p] for p in shared)):
        return True, False
    primary = [(left['creators'][role], right['creators'][role]) for role in left['creators']]
    if any(x and y and not x & y for x, y in primary):
        return True, False
    return True, bool((reference and (numbered or titled)) or
                      (titled and (numbered or any(x & y for x, y in primary))))


def keeps_edition(incoming: dict[str, Any], default: dict[str, Any]) -> bool:
    return incoming['edition'] is None or incoming['edition'] == default['edition']


def work_leads(work: dict[str, Any]) -> set[tuple[str, ...]]:
    if not work:
        return set()
    fields = work['fields']
    keys: set[tuple[str, ...]] = {
        ('reference', source, ref) for source, refs in work['refs'].items() for ref in refs}
    if fields.get('series') and fields.get('number'):
        keys.add(('number', fields['series'], fields['number']))
    if fields.get('title'):
        keys.add(('title', fields['title']))
    return keys


def output_pages(asset: str, record: dict[str, Any]) -> tuple[str, ...]:
    """Extract the ordered page digests from verified publication evidence."""
    result = record.get('transformation', {})
    pages = result.get('page_manifest')
    if (result.get('status') != 'validated' or result.get('source_asset_sha256') != asset
            or result.get('artifact_sha256') != record.get('manifest', {}).get('book.cbz')
            or not isinstance(pages, list) or not pages or len(pages) != result.get('pages')):
        return ()
    digests = []
    for page in pages:
        if not isinstance(page, dict) or not re.fullmatch('[0-9a-f]{64}', str(page.get('sha256', ''))):
            return ()
        digests.append(page['sha256'])
    return tuple(digests)


def copies_signature(asset: dict[str, Any]) -> str | None:
    return (asset.get('reader_selection') or asset.get('admission') or {}).get('signature')
