"""Native AllDebrid host-set selection and retained queue/collector snapshots."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from booksys_core.errors import CollectionError


def link_identity(url: str) -> str:
    """Match pyLoad's observed 1Fichier affiliate removal; preserve other semantics."""
    parts = urlsplit(url)
    if parts.hostname == '1fichier.com':
        from urllib.parse import urlunsplit
        query = '&'.join(token for token in parts.query.split('&') if token.split('=', 1)[0] != 'af')
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))
    return url


def public_hosts() -> dict[str, Any]:
    request = Request('https://api.alldebrid.com/v4/hosts?agent=booksys', headers={'User-Agent': 'Booksys/0.1'})
    with urlopen(request, timeout=15) as response:
        data = response.read(2*1024**2+1)
    if len(data) > 2*1024**2:
        raise CollectionError('AllDebrid host response exceeds limit')
    value = json.loads(data)
    if not isinstance(value, dict) or value.get('status') != 'success' or not isinstance(value.get('data'), dict):
        raise CollectionError('AllDebrid availability is unknown')
    if not isinstance(value['data'].get('hosts'), dict):
        raise CollectionError('Malformed AllDebrid host response')
    return dict(value['data'])


def host_sets(record: dict[str, Any]) -> dict[str, list[str]]:
    result: dict[str, set[str]] = {}
    for download in record['downloads'].values():
        host = download['hoster'].strip().casefold()
        for url in download['urls']:
            parts = urlsplit(url)
            if parts.scheme not in ('https', 'http') or not parts.hostname or parts.username or parts.password:
                raise CollectionError('Invalid captured download URL')
            result.setdefault(host, set()).add(url)
    return {host: sorted(urls) for host, urls in sorted(result.items()) if urls}


def select_primary(record: dict[str, Any], availability: dict[str, Any]) -> tuple[str, list[str]]:
    domains = {domain.casefold() for host in availability.get('hosts', {}).values()
               if isinstance(host, dict) and host.get('status') is True
               for domain in host.get('domains', []) if isinstance(domain, str)}
    eligible = []
    for host, urls in host_sets(record).items():
        if all(any(urlsplit(url).hostname == domain or str(urlsplit(url).hostname).endswith('.'+domain)
                   for domain in domains) for url in urls):
            eligible.append((host, urls))
    if not eligible:
        raise CollectionError('No complete currently available AllDebrid host set; acquisition pending')
    return min(eligible, key=lambda item: (not any(urlsplit(url).hostname == '1fichier.com' for url in item[1]), item[0]))


def logical_snapshot(client: Any, routing: Path | None = None) -> list[dict[str, Any]]:
    """Keep original IDs and explicit old input folders after a fallback attempt."""
    result = client.call('get_queue_data') + client.call('get_collector_data')
    if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
        raise CollectionError('Unexpected pyLoad package snapshot')
    ids = [item['pid'] for item in result]
    if len(ids) != len(set(ids)):
        raise CollectionError('Package appears in both pyLoad destinations')
    from library.operational_state import NativeHistory
    value = NativeHistory(routing.parent).routing() if routing else {'packages':{}}
    for package in result:
        operation = value.get('packages', {}).get(str(package['pid']), {})
        if operation.get('name') not in (None, package.get('name')):
            operation = {}  # an entry of an earlier package that had this reused pyLoad id
        folders = operation.get('input_folders', {})
        for link in package['links']:
            if str(link.get('fid')) in folders:
                link['input_folder'] = folders[str(link['fid'])]
        for original_id, op in value.get('packages', {}).items():
            if op.get('attempt_pid') == package['pid']:
                if package['name'] != op['attempt_name']:
                    raise CollectionError('Fallback package ownership changed')
                package['mirror_source'] = op['source']
                package['mirror_original_pid'] = int(original_id)
        package['mirror_routing'] = operation.get('phase', 'unmanaged')
    if routing is not None:
        from retirement_history import history_path, retained_snapshot
        return retained_snapshot(result, history_path(routing))
    return list(result)
