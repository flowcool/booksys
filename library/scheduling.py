"""Sequential work turns: drain intake, then two conversions per OCR opportunity."""
from __future__ import annotations
from typing import Any


def next_delay(outcome: dict[str, Any]) -> int:
    if outcome.get('status') == 'lab' and any(outcome.get(k, 0) > 0 for k in
            ('intake_backlog', 'ready_backlog', 'provisional_ready_backlog')):
        return 0
    return 300


def heavy_stage(intake_backlog: int, turn: int) -> str | None:
    return None if intake_backlog else 'metadata' if turn % 3 == 2 else 'provisional'


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


class RoutingClient:
    """Record write attempts so only pre-write HTTP read failures are optional."""
    def __init__(self, client: Any):
        self.client = client
        self.wrote = False
        self.endpoint = ''

    def call(self, method: str, *, write: bool = False, **parameters: Any) -> Any:
        self.endpoint = '/api/' + method
        self.wrote = self.wrote or write
        return self.client.call(method, write=write, **parameters)
