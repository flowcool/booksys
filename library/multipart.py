"""Select exact tracked RAR volume sets without scanning reception."""
from __future__ import annotations
import re
from typing import Any
from urllib.parse import urlsplit
from library.catalog import LibraryError

PART = re.compile(r"(.+)\.part([0-9]{1,3})\.rar", re.I)

def is_multipart(name: str) -> bool:
    return bool(re.search(r'\.part[0-9]+\.rar$', name, re.I) or re.search(r"(?:\.r[0-9]{2,3}|\.z[0-9]{2,3}|\.(?:zip|7z)\.[0-9]+)$", name, re.I))

def select_parts(links: list[dict[str, Any]], item: dict[str, Any]) -> list[dict[str, Any]]:
    match = PART.fullmatch(item['name'])
    if not match:
        raise LibraryError('Unsupported multipart type; only numbered RAR volumes are accepted')
    host = urlsplit(item.get('url', '')).hostname
    if any(ord(char) < 32 or char in ',/\\' for char in item['name']):
        raise LibraryError('Unsupported multipart unsafe filename')
    if not host:
        raise LibraryError('Unsupported multipart without tracked host provenance')
    members = []
    for candidate in links:
        other = PART.fullmatch(candidate['name'])
        if (other and other[1] == match[1] and candidate.get('input_folder') == item.get('input_folder')
                and urlsplit(candidate.get('url', '')).hostname == host):
            members.append((int(other[2]), candidate))
    members.sort(key=lambda pair: pair[0])
    if not 2 <= len(members) <= 64 or [n for n,_ in members] != list(range(1,len(members)+1)):
        raise LibraryError('Missing part or duplicate multipart number')
    numbers = [PART.fullmatch(candidate['name'])[2] for _,candidate in members]  # type: ignore[index]
    if len({len(number) for number in numbers}) != 1 and any(number != str(int(number)) for number in numbers):
        raise LibraryError('Unsupported multipart naming mismatch')
    if any(type(candidate.get('status')) is not int or candidate['status'] != 0 for _,candidate in members):
        raise LibraryError('Awaiting part: every tracked volume must be finished')
    return [candidate for _,candidate in members]


def receive_parts(state: dict[str, Any], directory: Any, root: Any, source: str,
                  package: dict[str, Any], hint: dict[str, Any], budget: int,
                  stable_seconds: float) -> int:
    from pathlib import Path
    from library.intake import reception_path
    from library.bundles import receive_bundle
    from library.bundle_worker import POLICY, MAX_SOURCE
    from library.catalog import digest, now
    from library.readers import fingerprint, sha256
    import os
    used = 0
    preparations = 0
    seen: set[int] = set()
    for item in package.get('links', []):
        if not is_multipart(item['name']) or item['fid'] in seen:
            continue
        oid = digest({'source': source, 'pid': package['pid'], 'fid': item['fid']})
        origin = {'source': source, 'pid': package['pid'], 'fid': item['fid'],
                  'path': str(Path(item.get('input_folder', package['folder'])) / item['name']),
                  'sha256': None, 'availability': 'pending', 'observed_at': now(),
                  'transfer_status': item.get('status'), 'transfer_error': bool(item.get('error'))}
        try:
            members = select_parts(package['links'], item)
            seen.update(member['fid'] for member in members)
            first = members[0]
            oid = digest({'source': source, 'pid': package['pid'], 'fid': first['fid']})
            previous = state['origins'].get(oid, {})
            paths = [reception_path(root, member.get('input_folder', package['folder']), member['name']) for member in members]
            if any(not path.is_file() for path in paths):
                raise LibraryError('Missing part: finished tracked volume is absent')
            stamps = [list(fingerprint(path)) for path in paths]
            if any(stamp[2] <= 0 for stamp in stamps) or sum(stamp[2] for stamp in stamps) > MAX_SOURCE:
                raise LibraryError('Bundle source limit: 4 GiB source set maximum')
            records = [{'path': str(path.relative_to(root)), 'fid': member['fid'],
                        'sha256': sha256(path), 'fingerprint': stamp,
                        'url': member['url']} for member,path,stamp in zip(members,paths,stamps)]
            if [list(fingerprint(path)) for path in paths] != stamps:
                raise LibraryError('Multipart source is unstable')
            origin.update(fid=first['fid'], path=records[0]['path'], source_parts=records,
                          sha256=records[0]['sha256'], fingerprint=records[0]['fingerprint'],
                          local_size=records[0]['fingerprint'][2], reported_size=first.get('size'),
                          source_set_size=sum(stamp[2] for stamp in stamps),
                          source_set_sha256=digest([{'name': p.name, 'sha256': r['sha256'],
                              'size': r['fingerprint'][2]} for p,r in zip(paths,records)]))
            policy = digest({'policy': POLICY, 'image': os.environ.get('BOOKSYS_READER_IMAGE', 'native')})
            unchanged = previous.get('source_parts') == records and previous.get('multipart_policy') == policy
            if unchanged and previous.get('availability') == 'rejected':
                state['origins'][oid] = {**previous, 'observed_at': now()}
                continue
            if not previous.get('bundle') and (used >= budget or preparations >= 1):
                origin['reason'] = 'Intake budget deferred'
            else:
                preparations += not bool(previous.get('bundle'))
                origin['multipart_policy'] = policy
                used += receive_bundle(state, directory, paths[0], oid, origin, hint,
                    budget=max(0, budget-used), stable_seconds=stable_seconds, parts=paths)
            state['origins'][oid] = origin
            for member,record in zip(members[1:],records[1:]):
                mid = digest({'source': source, 'pid': package['pid'], 'fid': member['fid']})
                state['origins'][mid] = {'source': source, 'pid': package['pid'], **record,
                    'sha256': record['sha256'], 'availability': 'multipart_member',
                    'multipart_parent': oid, 'observed_at': now(), 'transfer_status': 0,
                    'reason': 'Volume retained as part of tracked RAR source set'}
        except (LibraryError, OSError) as exc:
            canonical = state['origins'].get(oid, {})
            if canonical.get('bundle'):
                origin = {**canonical, 'observed_at': now()}
            origin.update(reason=str(exc), availability='pending' if str(exc).startswith(('Awaiting part', 'Missing part')) else 'rejected')
            state['origins'][oid] = origin
    return used
