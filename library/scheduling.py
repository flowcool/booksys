"""Saved-queue eligibility for the routing stage."""
from __future__ import annotations
from typing import Any


def routing_candidates(queue: list[dict[str, Any]], submissions: dict[str, Any],
                       state: dict[str, Any], journal: dict[str, Any]) -> list[str]:
    """Saved data rejects pending packages; route_package still verifies fresh ownership."""
    from mirror_routing import technical
    from mirrors import link_identity
    packages = {p['pid']: p for p in queue}
    result = []
    for source, entry in submissions.items():
        if entry.get('status') != 'queued':
            continue
        operation = journal.get('packages', {}).get(str(entry['package_id']), {})
        if operation.get('source') not in (None, source):
            operation = {}  # an entry of an earlier package that had this reused pyLoad id
        if operation:
            if operation.get('phase') not in ('active', 'restored') and not (
                    operation.get('kind') == 'park' and operation.get('phase') == 'stopped'):
                result.append(source)
            continue
        package = packages.get(entry['package_id'], {})
        primary = entry.get('primary_links') or (entry.get('links') if entry.get('primary_host') else None)
        links = package.get('links', [])
        if primary:
            selected = {link_identity(url) for url in primary}
            links = [item for item in links if link_identity(item['url']) in selected]
            if len(links) != len(selected):
                continue
        origins = {o['fid']: {**o, '_id': oid} for oid, o in state['origins'].items()
                   if o.get('source') == source and o.get('pid') == entry['package_id'] and not o.get('parent_origin')}
        if links and (any(item.get('status') == 1 or technical(origins.get(item['fid']), state) == 'failed' for item in links)
                or (all(item.get('status') == 0 and technical(origins.get(item['fid']), state) == 'valid' for item in links)
                    if primary else any(item.get('status') == 0 and technical(origins.get(item['fid']), state) == 'valid' for item in links))):
            result.append(source)
    return result
