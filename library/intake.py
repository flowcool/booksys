"""Receive tracked downloads and read-only external comic origins."""
from __future__ import annotations

from pathlib import Path
import copy
import os
import shutil
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

from library.catalog import Catalog
from library.multipart_model import is_multipart
from library.store import PreparedResult
from library.primitives import LibraryError, WorkingState, digest, now
from library.readers import GENERIC_ARCHIVE_SUFFIXES, inspect_file, fingerprint, sha256, READER_POLICY


from library.reception import reception_path as reception_path

EXTERNAL_ROOT = Path(os.environ.get('BOOKSYS_ORIGINALS_ROOT', '/data/booksys/comics-originals'))
GENERIC_ARCHIVE_POLICY = 'generic-image-archive-1'


def decoder_readiness() -> str:
    """Installation/removal of a required native decoder is an explicit recovery input."""
    return digest({tool: shutil.which(tool) is not None for tool in ('unrar', 'pdfimages', 'pdftoppm')})


def retry_at(origin: dict[str, Any], *, bundle: bool = False) -> float | None:
    """A finite retry deadline is scheduling state, never an input revision."""
    if origin.get('availability') not in ('pending', 'rejected'):
        return None
    temporary = any(text in origin.get('reason', '').lower() for text in (
        'isolated reader failed', 'isolated bundle worker failed', 'cannot create isolated bundle engine',
        'resource limit exceeded', 'timed out', 'wall limit', 'reader policy differs',
        'decoder failed', 'decoder unavailable', 'support unavailable', 'timeoutexpired',
        'timeouterror', 'memoryerror', 'filenotfounderror'))
    prefix = 'bundle' if bundle else 'intake'
    if temporary and 0 < int(origin.get(prefix + '_attempts', 0)) < 3:
        return float(origin.get(prefix + '_next_attempt', 0))
    return None


def intake_signature(root: Path, package: dict[str, Any]) -> str:
    """Only external inputs can reopen intake; saved outcomes never fence its claim."""
    from library.bundle_worker import POLICY as BUNDLE_POLICY
    files = []
    for item in package.get('links', []):
        record = {key: item.get(key) for key in ('fid', 'name', 'status', 'size', 'input_folder')}
        if type(item.get('status')) is int and item['status'] == 0:
            name = item['name']
            if Path(name).suffix.lower() in GENERIC_ARCHIVE_SUFFIXES and not is_multipart(name):
                record['generic_archive'] = GENERIC_ARCHIVE_POLICY
            try:
                record['fingerprint'] = list(fingerprint(reception_path(root,
                    item.get('input_folder', package['folder']), item['name'])))
            except (LibraryError, OSError) as exc:
                record['fingerprint'] = type(exc).__name__
        files.append(record)
    return digest({'package': {key: package.get(key) for key in ('pid', 'name', 'folder', 'retirement')},
                   'files': files, 'reader': READER_POLICY, 'bundle': BUNDLE_POLICY,
                   'decoder_readiness': decoder_readiness()})


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
    if result.get('reason'):
        from booksys_core.diagnostics import emit, diagnostic_fields
        emit(service='library', stage='local', code='failed' if result['status'] == 'rejected' else 'waiting',
             reason='invalid_archive' if result['status'] == 'rejected' else 'recovery_wait',
             refs=[{'kind': 'origin', 'id': oid}], **diagnostic_fields({'detail': str(result['reason'])}))
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
    readiness = decoder_readiness()
    from library.bundle_worker import POLICY as BUNDLE_POLICY
    engine = {'bundle': BUNDLE_POLICY, 'reader': READER_POLICY, 'decoder_readiness': readiness}
    bundle_engine = digest(engine)
    generic_bundle_engine = digest({**engine, 'generic_archive': GENERIC_ARCHIVE_POLICY})
    def load_asset(key: str) -> None:
        if key in state['assets']:
            return
        assert state.conflict is not None
        try:
            state.conflict = store.extend_result(state.conflict, {'assets': [key]})
            existing = store.get('assets', key)
            # Bind the canonical read to the expanded snapshot, including absent keys.
            store.verify_result(state.conflict)
        except (LibraryError, OSError) as exc:
            from booksys_core.diagnostics import emit, exception_details
            emit(service='library', stage='local', code='failed', **exception_details(exc))
            # Same boundary as save(): a transient conflict retries the step, it never rejects the file.
            raise RuntimeError('Intake asset load failed') from exc
        if existing is not None:
            state['assets'][key] = copy.deepcopy(existing)
            state.original['assets'][key] = copy.deepcopy(existing)
    def save() -> None:
        try:
            store.save(state, expected=state['revision'])
            fresh = store.result_set('retire', {'origins': list(state['origins'])}, media=False)
        except (LibraryError, OSError) as exc:
            from booksys_core.diagnostics import emit, exception_details
            emit(service='library', stage='local', code='failed', **exception_details(exc))
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
                if (origin['pid'] == pid and origin['source'] == source
                        and origin.get('availability') not in ('validated', 'bundle', 'multipart_member')):
                    origin['availability'] = 'package_missing'
            continue
        if package['name'] != submission['name']:
            # pyLoad gave the id of a released package to a new one: ours is gone. One such package must not stop the sweep.
            for origin in state['origins'].values():
                if (origin['pid'] == pid and origin['source'] == source
                        and origin.get('availability') not in ('validated', 'bundle', 'multipart_member')):
                    origin['availability'] = 'package_missing'
            continue
        from library.multipart import receive_parts
        receive_parts(state, media_directory or store.directory, root, source, package, captured['items'][source],
            stable_seconds, save=save, detached=media_directory is not None, load_asset=load_asset)
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
                    generic_archive = path.suffix.lower() in GENERIC_ARCHIVE_SUFFIXES
                    engine_policy = generic_bundle_engine if generic_archive else bundle_engine
                    same_attempt = (previous.get('fingerprint') == stamp and
                                    previous.get('bundle_engine_policy') == engine_policy)
                    nested = (previous.get('availability') == 'pending'
                              and 'Nested container' in previous.get('reason', ''))
                    use_bundle = (previous.get('bundle') or same_attempt
                                  or (not generic_archive and (previous.get('bundle_engine_policy') or nested)))
                    result: dict[str, Any] = {}
                    policy_retry = (previous.get('validation_policy', previous.get('reader_policy')) != READER_POLICY
                                    or previous.get('decoder_readiness', readiness) != readiness)
                    if not use_bundle:
                        due = retry_at(previous)
                        retry_due = due is not None and due <= time.time()
                        if (incremental and not retry_due and not policy_retry and previous.get('fingerprint') == stamp
                                and previous.get('path') == origin['path']
                                and previous.get('availability') in ('validated', 'rejected', 'pending')):
                            if previous.get('availability') == 'validated' and previous.get('sha256') not in state['assets']:
                                raise LibraryError('Cached origin references missing asset')
                            state['origins'][oid] = {**previous, **size_fields, 'observed_at': origin['observed_at'],
                                'transfer_status': 0, 'transfer_error': bool(item.get('error')), 'decoder_readiness': readiness}
                            continue
                        result = inspect_file(path, stable_seconds=stable_seconds)
                        if result.get('reason') == 'Stage enclosing deadline exceeded':
                            raise RuntimeError(result['reason'])
                        use_bundle = result['status'] == 'bundle' or (
                            result['status'] == 'pending' and 'Nested container' in result.get('reason', ''))
                    if use_bundle:
                        from library.bundles import receive_bundle
                        bundle_due = retry_at(previous, bundle=True)
                        retry_bundle = bundle_due is not None and bundle_due <= time.time()
                        if (incremental and not previous.get('bundle') and same_attempt and
                                previous.get('availability') in ('rejected', 'pending') and not retry_bundle):
                            state['origins'][oid] = {**previous, **size_fields, 'observed_at': origin['observed_at']}
                            continue
                        attempts = (int(previous.get('bundle_attempts', 0)) if previous.get('bundle') else
                                    int(previous.get('bundle_attempts', 0)) + 1 if same_attempt else 1)
                        origin.update(fingerprint=stamp, bundle_policy=BUNDLE_POLICY, bundle_engine_policy=engine_policy,
                                      bundle_attempts=attempts, bundle_next_attempt=previous.get('bundle_next_attempt', 0)
                                      if previous.get('bundle') else time.time() + (300 if attempts == 1 else 900))
                        receive_bundle(state, media_directory or store.directory, path, oid, origin,
                            captured['items'][source], save=save, load_asset=load_asset,
                            stable_seconds=stable_seconds, detached=media_directory is not None)
                        continue
                    if result['status'] == 'rejected':
                        from booksys_core.diagnostics import emit
                        emit(service='library', stage='local', code='failed', reason='invalid_archive')
                    count = (int(previous.get('intake_attempts', 0)) + 1
                             if previous.get('fingerprint') == stamp and not policy_retry else 1)
                    origin.update(intake_attempts=count, intake_next_attempt=time.time() + (300 if count == 1 else 900))
                    if list(fingerprint(path)) == stamp:
                        origin['fingerprint'] = stamp
                    origin.update(availability=result['status'], reason=result.get('reason', ''),
                                  reader_policy=result.get('reader_policy'), validation_policy=READER_POLICY,
                                  decoder_readiness=readiness)
                    if not origin['sha256']:
                        origin['sha256'] = result.get('sha256')
                    if result['status'] == 'validated':
                        key = result['sha256']
                        old_origin = previous
                        if old_origin and old_origin['sha256'] and old_origin['sha256'] != key:
                            raise LibraryError('Origin bytes changed after identification')
                        load_asset(key)
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
