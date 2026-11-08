"""Select only known Booksys submission origins; never scan shared reception."""
from __future__ import annotations

from pathlib import Path
import time
import os
from typing import Any
from urllib.parse import urlsplit

from library.catalog import Catalog, LibraryError, digest, now
from library.readers import inspect_file, fingerprint, sha256, READER_POLICY


def reception_path(root: Path, folder: str, name: str) -> Path:
    relative = Path(folder) / name
    if Path(folder).is_absolute() or Path(name).name != name or '..' in relative.parts:
        raise LibraryError('Unsafe pyLoad reception path')
    path = root / relative
    if any(p.is_symlink() for p in (path, *path.parents)) or not path.resolve().is_relative_to(root.resolve()):
        raise LibraryError('Reception escape or symlink')
    return path


def receive(store: Catalog, root: Path, captured: dict[str, Any], journal: dict[str, Any],
            queue: list[dict[str, Any]], *, stable_seconds: float = 2,
            incremental: bool = False, inspection_limit: int | None = None) -> dict[str, Any]:
    # The caller obtains immutable snapshots without holding a collector lock.
    # Parsing and hashing happen before the short catalogue compare-and-save.
    state = store.read()
    packages = {int(p['pid']): p for p in queue}
    inspected = 0
    bundle_preparations = 0
    submissions = list(journal.get('submissions', {}).items())
    for attempt_package in queue:
        attempt_source = attempt_package.get('mirror_source')
        owner = journal.get('submissions', {}).get(attempt_source, {})
        if isinstance(attempt_source, str) and owner.get('package_id') == attempt_package.get('mirror_original_pid'):
            submissions.append((attempt_source, {'status': 'queued', 'package_id': attempt_package['pid'], 'name': attempt_package['name']}))
    if incremental:
        def priority(entry: tuple[str, Any]) -> tuple[bool, int]:
            source, submission = entry
            files = packages.get(submission.get('package_id'), {}).get('links', [])
            fresh = [item['fid'] for item in files if item.get('status') == 0 and
                     not state['origins'].get(digest({'source': source, 'pid': submission.get('package_id'),
                                                     'fid': item['fid']}), {}).get('sha256')]
            return bool(fresh), max(fresh, default=0)
        submissions.sort(key=priority, reverse=True)
    for source, submission in submissions:
        host = urlsplit(source).hostname or ''
        if (source not in captured.get('items', {}) or submission.get('status') != 'queued'
                or not (host == 'bookys-ebooks.com' or host.endswith('.bookys-ebooks.com'))):
            continue
        pid = submission['package_id']
        package = packages.get(pid)
        if package is None:
            for origin in state['origins'].values():
                if origin['pid'] == pid and origin['source'] == source:
                    origin['availability'] = 'package_missing'
            continue
        if package['name'] != submission['name']:
            raise LibraryError('Tracked pyLoad package identity changed')
        for item in package.get('links', []):
            fid = item['fid']
            oid = digest({'source': source, 'pid': pid, 'fid': fid})
            name = item['name']
            if name == 'fiche.md' or name.startswith('couverture.'):
                continue
            origin = {'source': source, 'pid': pid, 'fid': fid,
                      'path': str(Path(item.get('input_folder', package['folder'])) / name),
                      'sha256': state['origins'].get(oid, {}).get('sha256'),
                      'observed_at': now(), 'availability': 'pending',
                      'transfer_status': item.get('status'),
                      'transfer_error': bool(item.get('error')),
                      'reported_size': item.get('size'), 'local_size': None, 'size_warning': ''}
            try:
                path = reception_path(root, item.get('input_folder', package['folder']), name)
                # Exact pinned pyLoad status: core/datatypes/pyfile.py: finished = 0.
                if type(item.get('status')) is not int or item['status'] != 0:
                    origin['reason'] = 'Transfer is not confirmed finished'
                elif not path.exists():
                    origin.update(availability='missing', reason='Finished origin is missing or renamed')
                else:
                    stamp = list(fingerprint(path))
                    # pyLoad arrived includes chunk overlaps removed during assembly.
                    # Only full reader validation establishes local file integrity.
                    size_fields = {'reported_size': item.get('size'), 'local_size': stamp[2],
                        'size_warning': 'Downloader-reported size differs from local file; reader validation required'
                                        if item.get('size') != stamp[2] else ''}
                    origin.update(size_fields)
                    previous = state['origins'].get(oid, {})
                    # Recover only the unique canonical reverse link lost by an older budget deferral.
                    if (incremental and not previous.get('sha256')
                            and previous.get('path') == origin['path']
                            and previous.get('reason') in ('Intake budget deferred', 'Ambiguous canonical origin references')):
                        inverse = [key for key, asset in state['assets'].items()
                                   if oid in asset.get('origins', [])]
                        if len(inverse) > 1:
                            origin['reason'] = 'Ambiguous canonical origin references'
                            state['origins'][oid] = origin
                            continue
                        if len(inverse) == 1:
                            previous = {**previous, 'sha256': inverse[0]}
                            origin['sha256'] = inverse[0]
                    known = state['assets'].get(previous.get('sha256'), {})
                    if (incremental and 'fingerprint' not in previous
                            and (previous.get('availability') == 'validated' or
                                 previous.get('reason') == 'Intake budget deferred')
                            and previous.get('path') == origin['path']
                            and known.get('status') == 'validated' and known.get('size') == stamp[2]
                            and known.get('format') == path.suffix.lower().lstrip('.')
                            and sha256(path) == previous['sha256']
                            and sha256(path) == previous['sha256'] and list(fingerprint(path)) == stamp):
                        state['origins'][oid] = {**previous, **size_fields, 'fingerprint': stamp,
                            'availability': 'validated', 'reason': '',
                            'observed_at': origin['observed_at'], 'transfer_status': 0,
                            'transfer_error': bool(item.get('error'))}
                        continue
                    if (path.suffix.lower() in ('.zip', '.rar') or previous.get('bundle') or
                            (previous.get('availability') == 'pending' and 'Nested container' in previous.get('reason', ''))):
                        from library.bundles import receive_bundle
                        from library.bundle_worker import POLICY
                        engine_policy = digest({'bundle': POLICY, 'reader': READER_POLICY,
                                                'image': os.environ.get('BOOKSYS_READER_IMAGE', 'native')})
                        temporary_bundle = any(text in previous.get('reason', '').lower() for text in
                            ('isolated bundle worker failed', 'cannot create isolated bundle engine', 'wall limit'))
                        same_attempt = (previous.get('fingerprint') == stamp and
                                        previous.get('bundle_engine_policy') == engine_policy)
                        retry_bundle = (temporary_bundle and int(previous.get('bundle_attempts', 1)) < 3 and
                                        previous.get('bundle_next_attempt', 0) <= time.time())
                        if (incremental and not previous.get('bundle') and same_attempt and
                                previous.get('availability') == 'rejected' and not retry_bundle):
                            state['origins'][oid] = {**previous, **size_fields, 'observed_at': origin['observed_at']}
                            continue
                        if not previous.get('bundle') and (bundle_preparations >= 1 or
                                (inspection_limit is not None and inspected >= inspection_limit)):
                            origin['reason'] = 'Intake budget deferred'
                            state['origins'][oid] = origin
                            continue
                        bundle_preparations += not bool(previous.get('bundle'))
                        attempts = int(previous.get('bundle_attempts', 0)) + 1 if same_attempt else 1
                        origin.update(fingerprint=stamp, bundle_policy=POLICY, bundle_engine_policy=engine_policy,
                                      bundle_attempts=attempts, bundle_next_attempt=time.time() + (300 if attempts == 1 else 900))
                        inspected += receive_bundle(state, store.directory, path, oid, origin,
                            captured['items'][source], budget=max(0, (inspection_limit if inspection_limit is not None else 8) - inspected),
                            stable_seconds=stable_seconds)
                        continue
                    temporary = any(text in previous.get('reason', '').lower() for text in
                        ('isolated reader failed', 'resource limit exceeded', 'timed out', 'reader policy differs'))
                    retry_due = (temporary and int(previous.get('intake_attempts', 1)) < 3
                                 and previous.get('intake_next_attempt', 0) <= time.time())
                    policy_retry = (previous.get('reader_policy') != READER_POLICY and
                        previous.get('reason') in ('Multipart representation',
                            'LibraryError: Active PDF content or attachment',
                            'LibraryError: Nested container or unauthorized archive content',
                            'Multiple image groups: possible multi-work archive',
                            'Reader policy differs from controller'))
                    if (incremental and not retry_due and not policy_retry and previous.get('fingerprint') == stamp
                            and previous.get('path') == origin['path']
                            and previous.get('availability') in ('validated', 'rejected', 'pending')):
                        if previous.get('availability') == 'validated' and previous.get('sha256') not in state['assets']:
                            raise LibraryError('Cached origin references missing asset')
                        state['origins'][oid] = {**previous, **size_fields, 'observed_at': origin['observed_at'], 'transfer_status': 0, 'transfer_error': bool(item.get('error'))}
                        continue
                    if inspection_limit is not None and inspected >= inspection_limit:
                        origin['reason'] = 'Intake budget deferred'
                        state['origins'][oid] = origin
                        continue
                    inspected += 1
                    result = inspect_file(path, stable_seconds=stable_seconds)
                    if result['status'] == 'pending' and 'Nested container' in result.get('reason', ''):
                        from library.bundles import receive_bundle
                        inspected -= 1
                        if bundle_preparations >= 1:
                            origin['reason'] = 'Intake budget deferred'
                            state['origins'][oid] = origin
                            continue
                        bundle_preparations += 1
                        inspected += receive_bundle(state, store.directory, path, oid, origin,
                            captured['items'][source], budget=max(0, (inspection_limit if inspection_limit is not None else 8) - inspected),
                            stable_seconds=stable_seconds)
                        continue
                    count = (int(previous.get('intake_attempts', 0)) + 1
                             if previous.get('fingerprint') == stamp and previous.get('reader_policy') == READER_POLICY else 1)
                    origin.update(intake_attempts=count, intake_next_attempt=time.time() + (300 if count == 1 else 900))
                    if list(fingerprint(path)) == stamp:
                        origin['fingerprint'] = stamp
                    origin.update(availability=result['status'], reason=result.get('reason', ''),
                                  reader_policy=result.get('reader_policy'))
                    if not origin['sha256']:
                        origin['sha256'] = result.get('sha256')
                    if result['status'] == 'validated':
                        key = result['sha256']
                        old_origin = previous
                        if old_origin and old_origin['sha256'] and old_origin['sha256'] != key:
                            raise LibraryError('Origin bytes changed after identification')
                        asset = state['assets'].setdefault(key, {**result, 'origins': [], 'assertions': [],
                            'overrides': {}, 'disposition': 'pending', 'captured_hints': {}})
                        if not asset['assertions']:
                            for field, raw_field in [('genres', 'Genre'), ('public', 'AgeRating')]:
                                raw = result.get('embedded', {}).get(raw_field)
                                if raw:
                                    asset['assertions'].append({'field': field, 'raw': raw,
                                        'normalized': [x.strip() for x in raw.split(',')] if field == 'genres' else raw,
                                        'source': 'embedded', 'upstream': 'embedded-unverified',
                                        'url': 'sha256:' + key, 'retrieved_at': now(), 'validation': 'asserted'})
                        if oid not in asset['origins']:
                            asset['origins'].append(oid)
                        asset['captured_hints'][source] = captured['items'][source]
            except (LibraryError, OSError) as exc:
                origin.update(availability='rejected', reason=str(exc))
                previous_hash = state['origins'].get(oid, {}).get('sha256')
                if previous_hash:
                    origin['sha256'] = previous_hash
            state['origins'][oid] = origin
    store.save(state, expected=state['revision'])
    return store.read()
