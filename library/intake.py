"""Receive tracked downloads and read-only external comic origins."""
from __future__ import annotations

from pathlib import Path
import os
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

from library.catalog import Catalog
from library.store import PreparedResult
from library.primitives import LibraryError, WorkingState, digest, now
from library.readers import inspect_file, fingerprint, sha256, READER_POLICY


from library.reception import reception_path as reception_path

EXTERNAL_ROOT = Path(os.environ.get('BOOKSYS_ORIGINALS_ROOT', '/data/booksys/comics-originals'))


def external_origins(root: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    def fail(error: OSError) -> None:
        raise error
    visited = 0
    for folder, directories, files in os.walk(root, onerror=fail):
        directories[:] = [name for name in directories if name not in ('booksys', '@eaDir')
                          and not name.startswith('ug-tmp') and not (Path(folder)/name).is_symlink()]
        visited += len(directories) + len(files)
        if visited > 4096:
            raise LibraryError('External originals scan capacity exceeded')
        for name in files:
            if (name.startswith('ug-tmp') or (Path(folder)/name).is_symlink()
                    or Path(name).suffix.lower() not in ('.cbz', '.cbr', '.cb7', '.pdf')):
                continue
            relative = (Path(folder)/name).relative_to(root)
            path = reception_path(root, str(relative.parent), name)
            stamp = fingerprint(path)
            identity = {'external': str(relative), 'size': stamp[2], 'mtime_ns': stamp[3]}
            yield digest(identity), {'source': 'external:' + str(relative), 'pid': None, 'fid': None,
                'path': str(relative), 'size': stamp[2], 'mtime_ns': stamp[3], 'sha256': None,
                'availability': 'pending'}


def receive_external(store: Catalog, oid: str) -> PreparedResult:
    origin = store.get('origins', oid)
    if origin is None or not origin['source'].startswith('external:'):
        raise LibraryError('External origin is missing')
    relative = Path(origin['path'])
    path = reception_path(EXTERNAL_ROOT, str(relative.parent), relative.name)
    stamp = fingerprint(path)
    if digest({'external': str(relative), 'size': stamp[2], 'mtime_ns': stamp[3]}) != oid:
        raise LibraryError('External origin bytes changed since discovery')
    result = inspect_file(path)
    if result.get('reason') == 'Stage enclosing deadline exceeded':
        raise RuntimeError(result['reason'])
    refs = {'origins': [oid], 'assets': [result['sha256']] if result.get('sha256') else []}
    state = store.result_set('retire', refs, media=False)
    origin = state['origins'][oid]
    origin.update(availability=result['status'], reason=result.get('reason', ''),
                  reader_policy=result.get('reader_policy'), observed_at=now(), fingerprint=list(stamp))
    if result['status'] == 'validated':
        key = result['sha256']
        origin['sha256'] = key
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
    return store.prepare(state)


def receive(store: Catalog, root: Path, captured: dict[str, Any], journal: dict[str, Any],
            queue: list[dict[str, Any]], *, stable_seconds: float = 2,
            incremental: bool = False, source_only: str | None = None,
            prepared: list[PreparedResult] | None = None, media_directory: Path | None=None) -> dict[str, Any]:
    # The caller obtains immutable snapshots without holding a collector lock.
    # Parsing and hashing happen before the short catalogue compare-and-save.
    backend = store.backend()
    refs = {'origins': backend.ids('origins')} if backend is not None and source_only is None else {}
    if source_only is not None:
        from library.stage_inputs import origin_refs
        refs = origin_refs(store, source_only)
        entry = journal.get('submissions', {}).get(source_only, {})
        for candidate_package in queue:
            if candidate_package['pid'] == entry.get('package_id') or candidate_package.get('mirror_source') == source_only:
                refs['origins'].extend(digest({'source': source_only, 'pid': candidate_package['pid'], 'fid': item['fid']}) for item in candidate_package.get('links', []))
    state = store.result_set('retire', refs, media=False)
    assert isinstance(state, WorkingState)
    def save() -> None:
        try:
            store.save(state, expected=state['revision'])
            fresh = store.result_set('retire', {'origins': list(state['origins'])}, media=False)
        except (LibraryError, OSError) as exc:
            raise RuntimeError('Intake save failed') from exc
        assert isinstance(fresh, WorkingState)
        state.clear()
        state.update(fresh)
        state.original, state.conflict = fresh.original, fresh.conflict
    packages = {int(p['pid']): p for p in queue}
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
        if source_only is not None and source != source_only:
            continue
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
            # pyLoad gave the id of a released package to a new one: ours is gone. One such package must not stop the sweep.
            for origin in state['origins'].values():
                if origin['pid'] == pid and origin['source'] == source:
                    origin['availability'] = 'package_missing'
            continue
        from library.multipart import is_multipart, receive_parts
        receive_parts(state, media_directory or store.directory, root, source, package, captured['items'][source],
            stable_seconds, save=save, detached=media_directory is not None)
        for item in package.get('links', []):
            if is_multipart(item['name']):
                continue
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
                    from library.presentation import terminal_transfer
                    terminal = terminal_transfer(item.get('status'))
                    origin['reason'] = terminal[1] if terminal else 'Transfer is not confirmed finished'
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
                    known = state['assets'].get(previous.get('sha256'), {})
                    if (incremental and 'fingerprint' not in previous
                            and previous.get('availability') == 'validated'
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
                        engine_policy = digest({'bundle': POLICY, 'reader': READER_POLICY})
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
                        attempts = int(previous.get('bundle_attempts', 0)) + 1 if same_attempt else 1
                        origin.update(fingerprint=stamp, bundle_policy=POLICY, bundle_engine_policy=engine_policy,
                                      bundle_attempts=attempts, bundle_next_attempt=time.time() + (300 if attempts == 1 else 900))
                        receive_bundle(state, media_directory or store.directory, path, oid, origin,
                            captured['items'][source], save=save,
                            stable_seconds=stable_seconds, detached=media_directory is not None)
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
                            and previous.get('availability') in ('validated', 'rejected')
                            and previous.get('reason') != 'Decoder unavailable; retry later'):
                        if previous.get('availability') == 'validated' and previous.get('sha256') not in state['assets']:
                            raise LibraryError('Cached origin references missing asset')
                        state['origins'][oid] = {**previous, **size_fields, 'observed_at': origin['observed_at'], 'transfer_status': 0, 'transfer_error': bool(item.get('error'))}
                        continue
                    result = inspect_file(path, stable_seconds=stable_seconds)
                    if result.get('reason') == 'Stage enclosing deadline exceeded':
                        raise RuntimeError(result['reason'])
                    if result['status'] == 'rejected':
                        from booksys_core.diagnostics import emit
                        emit(service='library', stage='local', code='failed', reason='invalid_archive')
                    if result['status'] == 'pending' and 'Nested container' in result.get('reason', ''):
                        from library.bundles import receive_bundle
                        receive_bundle(state, media_directory or store.directory, path, oid, origin,
                            captured['items'][source], save=save,
                            stable_seconds=stable_seconds, detached=media_directory is not None)
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
                if str(exc) == 'Stage enclosing deadline exceeded':
                    raise RuntimeError(str(exc)) from exc
                from booksys_core.diagnostics import emit, exception_details
                emit(service='library', stage='local', code='failed',
                     **{**exception_details(exc), 'reason': 'invalid_archive'})
                origin.update(availability='rejected', reason=str(exc))
                previous_hash = state['origins'].get(oid, {}).get('sha256')
                if previous_hash:
                    origin['sha256'] = previous_hash
            state['origins'][oid] = origin
            save()
    for origin in state['origins'].values():
        package = packages.get(origin['pid'], {})
        if package.get('retirement'):
            origin['retirement'] = package['retirement']
    if prepared is not None:
        prepared.append(store.prepare(state))
        return state
    store.save(state, expected=state['revision'])
    if backend is None:
        return store.read()
    state['revision'] = store.revision
    result = WorkingState(state)
    result.media_omitted = True
    return result
