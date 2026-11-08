"""Journaled native package parking and one-set fallback. No global queue actions."""
from __future__ import annotations

from collections import Counter
import hashlib
from pathlib import Path
import time
from urllib.parse import urlsplit
from typing import Any

import yaml

from booksys import CollectionError
from library.catalog import atomic_yaml, lock, now
from library.intake import reception_path
from library.readers import fingerprint, sha256
from mirrors import host_sets, link_identity


def package_signature(package: dict[str, Any]) -> list[list[Any]]:
    return sorted([[item['fid'], item['url']] for item in package['links']])


def owned_package(client: Any, entry: dict[str, Any]) -> dict[str, Any]:
    package = client.call('get_package_data', package_id=entry['package_id'])
    if package['name'] != entry['name'] or not package['name'].startswith('Booksys-'):
        raise CollectionError('Mirror routing package ownership changed')
    return dict(package)


def stopped(client: Any, pid: int) -> None:
    ids = [item['fid'] for item in client.call('status_downloads') if item['package_id'] == pid]
    if ids:
        client.call('stop_downloads', write=True, file_ids=ids)
    for _ in range(20):
        if not any(item['package_id'] == pid for item in client.call('status_downloads')):
            return
        time.sleep(.1)
    raise CollectionError('Owned package transfers did not stop; preserve routing intent')


def technical(origin: dict[str, Any] | None, state: dict[str, Any]) -> str:
    if not origin:
        return 'pending'
    if origin.get('availability') == 'validated':
        return 'valid'
    if origin.get('availability') == 'bundle':
        children = [item for item in state['origins'].values() if item.get('parent_origin') == origin.get('_id')]
        outcomes = [technical(child, state) for child in children]
        return 'failed' if 'failed' in outcomes else 'valid' if outcomes and all(item == 'valid' for item in outcomes) else 'pending'
    reason = origin.get('reason', '').casefold()
    # Only intrinsic complete-input failure permits another acquisition. Local
    # resources, source drift, missing files and policy errors need recovery.
    intrinsic = ('crc', 'encrypted', 'cannot be validated', 'image decoding', 'broken data',
                 'not a pdf', 'trailer dictionary', 'invalid pdf', 'invalid image', 'badzipfile',
                 'bad archive', 'password', 'syntax error')
    local = ('limit', 'timeout', 'timed out', 'isolated', 'changed', 'policy', 'permission', 'reserve')
    if origin.get('availability') == 'rejected' and not any(word in reason for word in local) and any(word in reason for word in intrinsic):
        return 'failed'
    return 'pending'


def route_package(client: Any, source: str, entry: dict[str, Any], record: dict[str, Any],
                  state: dict[str, Any], reception: Path, path: Path, *, apply: bool = False,
                  availability: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read-only plan unless apply=True; caller owns operation authorization."""
    if path.resolve().is_relative_to(reception.resolve()):
        raise CollectionError('Private routing journal must stay outside reception')
    host = urlsplit(source).hostname or ''
    if not (host == 'bookys-ebooks.com' or host.endswith('.bookys-ebooks.com')):
        raise CollectionError('Mirror routing requires a tracked Bookys source')
    with lock(path.with_suffix('.lock')):
        journal = yaml.safe_load(path.read_text()) if path.exists() else {'schema': 1, 'packages': {}}
        if journal.get('schema') != 1 or not isinstance(journal.get('packages'), dict):
            raise CollectionError('Invalid mirror routing journal')
        from retirement_history import history_path, read_history
        retired = read_history(history_path(path))['acquisitions'].get(source)
        if retired and retired.get('phase') in ('deleting', 'retired'):
            if (retired['submission']['package_id'] != entry['package_id']
                    or retired['submission']['name'] != entry['name']):
                raise CollectionError('Retirement submission ownership changed')
            if retired['phase'] != 'retired':
                raise CollectionError('Removal intent needs reconciliation before routing')
            return {'outcome': 'retired', 'pid': entry['package_id']}
        package = owned_package(client, entry)
        pid = package['pid']; key = str(pid)
        operation = journal['packages'].get(key)
        if operation and operation.get('phase') in ('stopped', 'active', 'restored') and (operation.get('kind') == 'park' or operation.get('phase') in ('active', 'restored')):
            return {'outcome': 'unchanged', 'pid': pid}
        sets = host_sets(record) if record.get('downloads') else {}
        primary = entry.get('primary_links') or (entry.get('links') if entry.get('primary_host') else None)
        if not primary:
            from mirrors import select_primary
            if availability is None:
                return {'outcome': 'pending', 'reason': 'Current AllDebrid host availability required'}
            try:
                _, primary = select_primary(record, availability)
            except CollectionError as exc:
                return {'outcome': 'pending', 'reason': str(exc)}
        by_url = {link_identity(item['url']): item for item in package['links']}
        primary = [link_identity(url) for url in primary]
        if len(by_url) != len(package['links']):
            raise CollectionError('Duplicate URLs in tracked package')
        if any(url not in by_url for url in primary):
            raise CollectionError('Primary URL set is incomplete in pyLoad')
        origins = {item['fid']: {**item, '_id': oid} for oid, item in state['origins'].items()
                   if item.get('source') == source and item.get('pid') == pid and not item.get('parent_origin')}
        outcomes = [technical(origins.get(by_url[url]['fid']), state) for url in primary]
        complete = all(by_url[url]['status'] == 0 and outcome == 'valid' for url, outcome in zip(primary, outcomes))
        # Exact pinned pyLoad status map: offline=1. Temporary failures are not offline.
        failed = any(outcome == 'failed' or by_url[url]['status'] == 1 for url, outcome in zip(primary, outcomes))
        if not operation and not (complete or failed):
            return {'outcome': 'pending', 'reason': 'Every primary member needs technical proof; identity is irrelevant'}
        if operation and operation.get('source') != source:
            raise CollectionError('Routing operation source changed')
        if not operation:
            candidate = next(((host, urls) for host, urls in sets.items() if {link_identity(url) for url in urls} != set(primary)), None) if failed else None
            if failed and not candidate:
                return {'outcome': 'pending', 'reason': 'No retained alternative complete host set'}
            operation = {'source': source, 'pid': pid, 'name': package['name'], 'phase': 'intent',
                'kind': 'fallback' if failed else 'park', 'primary_links': primary,
                'original_destination': package['dest'], 'original_order': package.get('order', 0),
                'original_folder': package['folder'], 'original_files': package_signature(package),
                'input_folders': {str(item['fid']): package['folder'] for item in package['links']},
                'fallback_host': candidate[0] if candidate else None, 'fallback_links': candidate[1] if candidate else [],
                'created_at': now(), 'files': {}}
            for item in package['links']:
                if item['status'] == 0:
                    file = reception_path(reception, package['folder'], item['name'])
                    known = origins.get(item['fid'], {})
                    if known.get('sha256') and sha256(file) != known['sha256']:
                        raise CollectionError('Completed file differs from canonical technical proof')
                    operation['files'][str(item['fid'])] = {'path': str(file.relative_to(reception)),
                        'fingerprint': list(fingerprint(file)), 'sha256': sha256(file)}
        if not apply:
            return {'outcome': 'planned', 'kind': operation['kind'], 'pid': pid, 'stop_active_only': True}
        journal['packages'][key] = operation
        def save(phase: str) -> None:
            operation['phase'] = phase
            atomic_yaml(path, journal)
        if operation['phase'] == 'intent':
            save('intent')
            client.call('move_package', write=True, destination=0, package_id=pid)
            if owned_package(client, entry)['dest'] != 0:
                raise CollectionError('Collector destination was not confirmed')
            save('parked')
        if operation['phase'] == 'parked':
            stopped(client, pid)
            current = owned_package(client, entry)
            if package_signature(current) != operation['original_files']:
                raise CollectionError('Package IDs or URLs changed during parking')
            for proof in operation['files'].values():
                file = reception / proof['path']
                if list(fingerprint(file)) != proof['fingerprint'] or sha256(file) != proof['sha256']:
                    raise CollectionError('Completed source changed during routing')
            save('stopped')
        if operation['kind'] == 'park':
            return {'outcome': 'parked', 'pid': pid}
        if operation['phase'] == 'active':
            return {'outcome': 'fallback_active', 'pid': pid}
        attempt = 'Booksys-fallback-' + hashlib.sha256((source + operation['fallback_host']).encode()).hexdigest()[:24]
        if operation['phase'] == 'stopped':
            operation['attempt_name'] = attempt
            save('create_intent')
            first_create = True
        else:
            first_create = False
        if operation['phase'] == 'create_intent':
            packages = client.call('get_queue') + client.call('get_collector')
            matches = [item for item in packages if item['name'] == attempt]
            if len(matches) > 1:
                raise CollectionError('Several fallback attempt packages; preserve intent')
            if matches:
                attempt_pid = matches[0]['pid']
            elif first_create:
                attempt_pid = client.call('add_package', write=True, name=attempt,
                                          links=operation['fallback_links'], dest=0)
                if type(attempt_pid) is not int:
                    raise CollectionError('Invalid fallback package ID')
            else:
                raise CollectionError('Uncertain fallback create; no automatic resend')
            operation['attempt_pid'] = attempt_pid
            save('attempt_created')
        attempt_pid = operation['attempt_pid']
        attempt_package = client.call('get_package_data', package_id=attempt_pid)
        if (attempt_package['name'] != attempt or Counter(link_identity(item['url']) for item in attempt_package['links']) !=
                Counter(link_identity(url) for url in operation['fallback_links'])):
            raise CollectionError('Fallback complete-set readback differs')
        if any(item['fid'] in {fid for fid, _ in operation['original_files']} for item in attempt_package['links']):
            raise CollectionError('Fallback reused original file IDs')
        if attempt_package['folder'] == operation['original_folder']:
            raise CollectionError('Fallback folder overlaps retained originals')
        operation['attempt_folder'] = attempt_package['folder']
        operation['attempt_files'] = package_signature(attempt_package)
        save('attempt_verified')
        client.call('move_package', write=True, destination=1, package_id=attempt_pid)
        if client.call('get_package_data', package_id=attempt_pid)['dest'] != 1:
            raise CollectionError('Fallback queue destination not confirmed')
        save('active')
        return {'outcome': 'fallback_active', 'pid': pid, 'attempt_pid': attempt_pid}


def restore_package(client: Any, entry: dict[str, Any], operation: dict[str, Any]) -> None:
    """Explicit rollback only; no automatic restart of old mirrors."""
    original = owned_package(client, entry)
    if package_signature(original) != operation['original_files']:
        raise CollectionError('Rollback source package changed')
    for pid in (entry['package_id'], operation.get('attempt_pid')):
        if pid is not None and any(item['package_id'] == pid for item in client.call('status_downloads')):
            raise CollectionError('Rollback requires stopped owned transfers')
    if operation.get('attempt_pid'):
        attempt = client.call('get_package_data', package_id=operation['attempt_pid'])
        if attempt['name'] != operation['attempt_name'] or package_signature(attempt) != operation['attempt_files']:
            raise CollectionError('Rollback fallback ownership changed')
        client.call('move_package', write=True, destination=0, package_id=operation['attempt_pid'])
        if client.call('get_package_data', package_id=operation['attempt_pid'])['dest'] != 0:
            raise CollectionError('Rollback fallback collector destination not confirmed')
    client.call('move_package', write=True, destination=operation['original_destination'], package_id=entry['package_id'])
    client.call('order_package', write=True, package_id=entry['package_id'], position=operation['original_order'])
    current = owned_package(client, entry)
    if current['dest'] != operation['original_destination'] or current['order'] != operation['original_order']:
        raise CollectionError('Rollback destination/order readback failed')
