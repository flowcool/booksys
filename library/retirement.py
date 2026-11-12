"""Retire one proven acquisition. Preserve source files and terminal evidence."""
from __future__ import annotations

import copy
from contextlib import ExitStack, nullcontext
from collections.abc import Callable
from pathlib import Path
from typing import Any


from library.catalog import Catalog
from booksys_core.network_admission import native_operation, native_receipt
from library.relocation import matches_retained
from library.primitives import LibraryError, LibraryLockBusy, digest, lock, now
from library.operational_state import CollectorState,NativeHistory,OperationalCompletion
from library.intake import reception_path
from library.origins import resolve_asset, verify_source_parts
from library.readers import fingerprint, sha256
from mirrors import host_sets, link_identity
from retirement_history import evidence_digest, history_path, package_identity, read_history, same_package


class Retirement:
    def __init__(self, client: Any, store: Catalog, reception: Path,
                 submissions: Path, captured: Path, *, scripts: Path | None = None,
                 fault: Callable[[str], None] = lambda _: None, concurrent: bool = False):
        self.client, self.store, self.reception = client, store, reception
        self.submissions, self.captured = submissions, captured
        self.routing = submissions.parent / 'mirror-routing.yaml'
        self.path = history_path(self.routing)
        self.fault = fault
        self.scripts = scripts
        self.concurrent = concurrent

    def reconcile(self) -> None:
        for source, record in read_history(self.path)['acquisitions'].items():
            if record['phase'] == 'deleting':
                self.process(source, apply=True)  # Confirms absence only; never resends.

    def run(self, *, apply: bool = False, limit: int = 1,
            inspection_limit: int = 8) -> list[dict[str, Any]]:
        history = read_history(self.path)
        entries = CollectorState(self.submissions.parent).monitor()['submissions']
        # A source without technical origins cannot pass retirement. This local
        # shortlist avoids native reads for the unprocessed historical backlog.
        evidenced = {o['source'] for _, o in self.store.records('origins')
                     if o.get('availability') in ('validated', 'bundle', 'multipart_member')}
        sources = sorted(s for s, e in entries.items() if e.get('status') == 'queued'
                         and history['acquisitions'].get(s, {}).get('phase') != 'retired'
                         and (not apply or s in evidenced or s in history['acquisitions']))
        cursor = history.get('cursor')
        if cursor in sources:
            index = sources.index(cursor) + 1
            sources = sources[index:] + sources[:index]
        outcomes = []
        removed = 0
        for source in sources[:max(0, inspection_limit)]:
            result = self.process(source, apply=apply)
            outcomes.append(result | {'source': source})
            if apply:
                with lock(self.submissions.parent / 'pipeline.lock', blocking=False):
                    history = read_history(self.path)
                    history['cursor'] = source
                    NativeHistory(self.path.parent).retirement_cursor(source)
            removed += result['outcome'] in ('retired', 'eligible')
            if removed >= max(1, limit):
                break
        return outcomes

    def _hooks_safe(self) -> bool:
        config = self.client.call('get_plugin_config')
        def enabled(plugin: str) -> str:
            values = [str(i['value']).lower() for i in config.get(plugin, {}).get('items', [])
                      if i['name'] == 'enabled']
            return values[0] if len(values) == 1 else 'unknown'
        if enabled('ExtractArchive') != 'false':
            return False  # No race with background extraction or delete-after-extract.
        scripts = enabled('ExternalScripts')
        if scripts == 'false':
            return True
        if scripts != 'true' or self.scripts is None or not self.scripts.is_dir():
            return False
        if any(p.is_symlink() for p in (self.scripts, *self.scripts.parents)):
            return False
        return not any(p.is_symlink() or not p.is_dir() for p in self.scripts.rglob('*'))

    def _file_proof(self, relative: str, proofs: dict[str, Any], *, scope: str = 'reception',
                    expected: str | None = None) -> None:
        root = self.reception if scope == 'reception' else self.store.directory
        path = reception_path(root, str(Path(relative).parent), Path(relative).name)
        stamp = list(fingerprint(path))
        checksum = sha256(path)
        if (expected is not None and checksum != expected) or list(fingerprint(path)) != stamp:
            raise LibraryError('Retained input changed from technical proof')
        proofs[scope + '/' + relative] = {'scope': scope, 'path': relative,
                                         'sha256': checksum, 'fingerprint': stamp}

    def _origin_proof(self, oid: str, state: dict[str, Any], proofs: dict[str, Any],
                      ancestors: frozenset[str] = frozenset()) -> None:
        if oid in ancestors:
            raise LibraryError('Cyclic origin provenance')
        ancestors = ancestors | {oid}
        origin = state['origins'][oid]
        if origin.get('availability') == 'multipart_member':
            parent = origin.get('multipart_parent')
            if parent not in state['origins'] or not state['origins'][parent].get('source_parts'):
                raise LibraryError('Multipart source set lacks complete assembly proof')
            records = state['origins'][parent]['source_parts']
            if not any(all(p.get(k) == origin.get(k) for k in ('fid', 'path', 'sha256')) for p in records):
                raise LibraryError('Multipart volume binding differs from canonical parent')
            self._origin_proof(parent, state, proofs, ancestors)
            return
        if origin.get('availability') == 'bundle':
            from library.bundles import verify_directory
            self._file_proof(origin['path'], proofs, expected=origin['sha256'])
            directory = self.store.directory / 'bundle-inputs' / origin['sha256']
            manifest = verify_directory(directory, origin['sha256'])
            verify_source_parts(self.reception, origin, manifest)
            if manifest['members'] != origin['bundle']['members']:
                raise LibraryError('Bundle manifest changed')
            self._file_proof(str((directory / 'manifest.yaml').relative_to(self.store.directory)), proofs, scope='state')
            for member in manifest['members']:
                if member['kind'] == 'comic':
                    cid = digest({'parent_origin': oid, 'member': member['member']})
                    if cid not in state['origins']:
                        raise LibraryError('Bundle member lacks technical proof')
                    self._origin_proof(cid, state, proofs, ancestors)
            for part in origin.get('source_parts', []):
                self._file_proof(part['path'], proofs, expected=part['sha256'])
            return
        if origin.get('availability') != 'validated' or not origin.get('sha256'):
            raise LibraryError('Required input lacks technical proof')
        if state['assets'].get(origin['sha256'], {}).get('status') != 'validated':
            raise LibraryError('Required asset lacks technical validation')
        resolve_asset(self.store, self.reception, state, origin['sha256'])
        self._file_proof(origin['path'], proofs, scope='state' if origin.get('parent_origin') else 'reception',
                         expected=origin['sha256'])

    def _verify_files(self, operation: dict[str, Any]) -> None:
        for proof in operation['files'].values():
            root = self.reception if proof['scope'] == 'reception' else self.store.directory
            relative = proof['path']
            path = reception_path(root, str(Path(relative).parent), Path(relative).name)
            if not matches_retained(path, proof['fingerprint'], proof.get('sha256')) or sha256(path) != proof['sha256']:
                raise LibraryError('Retained source changed during retirement')

    def _confirm(self, history: dict[str, Any], operation: dict[str, Any], completions: list[OperationalCompletion] | None = None) -> dict[str, Any]:
        live = self.client.call('get_queue_data') + self.client.call('get_collector_data')
        by_pid = {str(p['pid']): {f['fid'] for f in p['links']} for p in live}
        for pid, fids in operation['unrelated'].items():
            if pid not in by_pid or not set(fids).issubset(by_pid[pid]):
                raise LibraryError('Unrelated pyLoad IDs disappeared; inspect before proceeding')
        self._verify_files(operation)
        operation.update(phase='retired', confirmed_at=now())
        if completions is not None:
            completions.append(CollectorState(self.path.parent).prepare_completion('retirement',operation['source'],dict(operation)))
        else:
            NativeHistory(self.path.parent).retire(operation['source'],operation)
        return {'outcome': 'retired', 'source': operation['source'],
                'package_ids': [p['pid'] for p in operation['packages'].values()]}

    def process(self, source: str, *, apply: bool = False, completions: list[OperationalCompletion] | None = None,
                task_attempt: tuple[str, int] | None = None,
                receipt_only: dict[str, Any] | None = None) -> dict[str, Any]:
        from booksys_core.downloader import references
        from booksys_core.values import digest
        # Collector -> routing -> catalogue is the shared lock order. No catalogue
        # save occurs here. The cycle may already hold its outer cycle lock.
        with ExitStack() as held:
            try:
                held.enter_context(lock(self.submissions.parent / 'pipeline.lock', blocking=False))
                held.enter_context(lock(self.routing.with_suffix('.lock'), blocking=False))
                held.enter_context(nullcontext() if self.concurrent else
                                   lock(self.store.directory / 'library.lock', blocking=False))
            except LibraryLockBusy:
                return {'outcome': 'waiting', 'reason': 'processing_lock_busy'}
            held.enter_context(references([{'kind': 'source', 'id': digest(source)},
                                           {'kind': 'operation', 'id': digest(['retirement', source])}]))
            with native_operation(self.client, 'retirement:' + source, self.path):
                result = self._process(source, apply=apply,completions=completions, task_attempt=task_attempt, receipt_only=receipt_only)
                if apply and result['outcome'] == 'retired':
                    native_receipt(digest(result))
                return result

    def _process(self, source: str, *, apply: bool, completions: list[OperationalCompletion] | None = None,
                 task_attempt: tuple[str, int] | None = None,
                 receipt_only: dict[str, Any] | None = None) -> dict[str, Any]:
        history = read_history(self.path)
        prior = history['acquisitions'].get(source)
        if receipt_only is not None and (prior != receipt_only or prior is None
                                        or prior['phase'] not in ('deleting', 'retired')):
            raise LibraryError('Retirement receipt operation changed')
        if prior and prior['phase'] == 'retired':
            if receipt_only is not None:
                live = self.client.call('get_queue_data') + self.client.call('get_collector_data')
                if any(str(p['pid']) in prior['packages'] and same_package(prior['packages'][str(p['pid'])], p) for p in live):
                    raise LibraryError('Retired package reappeared during readback')
                self._verify_files(prior)
            return {'outcome': 'retired', 'source': source, 'already_retired': True}
        pending = [s for s, r in history['acquisitions'].items() if r['phase'] == 'deleting' and s != source]
        if pending:
            raise LibraryError('Another removal is uncertain; reconcile its intent first')
        if prior and prior['phase'] == 'deleting':
            live = self.client.call('get_queue_data') + self.client.call('get_collector_data')
            if any(str(p['pid']) in prior['packages'] and same_package(prior['packages'][str(p['pid'])], p) for p in live):
                raise LibraryError('Removal is uncertain; inspect target without automatic resend')
            if not apply:
                return {'outcome': 'reconcile', 'source': source}
            return self._confirm(history, prior,completions)
        entries = CollectorState(self.submissions.parent).monitor()['submissions']
        records = CollectorState(self.captured.parent).captured()['items']
        entry, record = entries.get(source), records.get(source)
        if not entry or not record or entry.get('status') != 'queued':
            return {'outcome': 'blocked', 'reason': 'Source is not a tracked submission'}
        from urllib.parse import urlsplit
        host = urlsplit(source).hostname or ''
        if not (host == 'bookys-ebooks.com' or host.endswith('.bookys-ebooks.com')):
            return {'outcome': 'blocked', 'reason': 'Source is not Booksys'}
        packages = self.client.call('get_queue_data') + self.client.call('get_collector_data')
        package = next((p for p in packages if p['pid'] == entry['package_id']), None)
        if package is None:
            return {'outcome': 'blocked', 'reason': 'Tracked package is unexpectedly missing'}
        if package['name'] != entry['name'] or not package['name'].startswith('Booksys-'):
            return {'outcome': 'blocked', 'reason': 'Package ownership changed'}
        actual = [link_identity(f['url']) for f in package['links']]
        expected = [link_identity(u) for u in entry.get('links', [])]
        if not expected or sorted(actual) != sorted(expected) or len(set(actual)) != len(actual):
            return {'outcome': 'blocked', 'reason': 'Tracked URL set changed'}
        routing = NativeHistory(self.routing.parent).routing()
        route = routing['packages'].get(str(package['pid']), {})
        if route.get('source') not in (None, source):
            route = {}  # an entry of an earlier package that had this id
        if route and route.get('phase') not in ('stopped', 'active', 'restored'):
            return {'outcome': 'blocked', 'reason': 'Mirror routing is not settled'}
        from mirror_routing import package_signature
        if route and route.get('original_files') != package_signature(package):
            return {'outcome': 'blocked', 'reason': 'Original routing file IDs changed'}
        owned = [package]
        winner = package
        sets = list(host_sets(record).values())
        primary = entry.get('primary_links') or route.get('primary_links')
        candidates = [primary] if primary else sets
        if route.get('attempt_pid') is not None:
            attempt = next((p for p in packages if p['pid'] == route['attempt_pid']), None)
            if (route.get('phase') != 'active' or route.get('source') != source or attempt is None
                    or attempt['name'] != route['attempt_name'] or attempt['folder'] != route['attempt_folder']
                    or route.get('attempt_files') != package_signature(attempt)
                    or sorted(link_identity(f['url']) for f in attempt['links']) !=
                       sorted(link_identity(u) for u in route['fallback_links'])):
                return {'outcome': 'blocked', 'reason': 'Fallback ownership or completion state changed'}
            attempt.update(mirror_source=source, mirror_original_pid=package['pid'])
            owned.append(attempt)
            winner, candidates = attempt, [route['fallback_links']]
        for item in package['links']:
            if str(item['fid']) in route.get('input_folders', {}):
                item['input_folder'] = route['input_folders'][str(item['fid'])]
        backend = self.store.backend()
        refs: dict[str, list[str]] = {'origins': []}
        if self.concurrent:
            from library.stage_inputs import origin_refs
            refs = origin_refs(self.store, source)
        elif backend is not None:
            cursor = None
            while True:
                page = backend.page('origins', filters={'source_ref': source}, cursor=cursor, limit=100)
                refs['origins'].extend(row['id'] for row in page['items'])
                cursor = page['cursor']
                if cursor is None:
                    break
        state = (self.store.result_set if self.concurrent else self.store.working_set)('retire', refs, media=False)
        if not route:
            original_fids = {o['fid'] for o in state['origins'].values()
                             if o['source'] == source and o['pid'] == package['pid'] and not o.get('parent_origin')}
            if {f['fid'] for f in package['links']} != original_fids:
                return {'outcome': 'blocked', 'reason': 'Original file IDs differ from intake evidence'}
        origins = {o['fid']: oid for oid, o in state['origins'].items()
                   if o['source'] == source and o['pid'] == winner['pid'] and not o.get('parent_origin')}
        by_url = {link_identity(f['url']): f for f in winner['links']}
        proofs: dict[str, Any] = {}
        failure = 'Required input set is unknown'
        for candidate in candidates:
            proofs = {}
            try:
                if (not candidate or not any(sorted(map(link_identity, candidate)) == sorted(map(link_identity, urls))
                                             for urls in sets)):
                    raise LibraryError('Required hoster set differs from captured complete set')
                for url in candidate:
                    item = by_url.get(link_identity(url), {})
                    oid = origins.get(item.get('fid'))
                    if item.get('status') != 0 or oid is None:
                        raise LibraryError('Required input lacks technical proof')
                    self._origin_proof(oid, state, proofs)
                break
            except (LibraryError, OSError) as exc:
                failure = str(exc)
        else:
            return {'outcome': 'blocked', 'reason': failure}
        ids = [p['pid'] for p in owned]
        if not self._hooks_safe():
            return {'outcome': 'blocked', 'reason': 'Extraction or deletion hooks are not qualified'}
        if any(d['package_id'] in ids for d in self.client.call('status_downloads')):
            return {'outcome': 'blocked', 'reason': 'Target processing is active'}
        if any(type(f.get('status')) is not int or f['status'] not in (0, 1, 2, 3, 4, 5, 6, 8, 9)
               for p in owned for f in p['links']):
            return {'outcome': 'blocked', 'reason': 'Target link processing is active or unknown'}
        for p in owned:
            for item in p['links']:
                relative = str(Path(item.get('input_folder', p['folder'])) / item['name'])
                file = reception_path(self.reception, str(Path(relative).parent), Path(relative).name)
                if file.exists():
                    self._file_proof(relative, proofs)
        if not apply:
            return {'outcome': 'eligible', 'source': source, 'package_ids': ids}
        operation = {'phase': 'prepared', 'created_at': now(), 'source': source,
                     'submission': entry, 'packages': {str(p['pid']): copy.deepcopy(p) for p in owned},
                     'origins': {oid: o for oid, o in state['origins'].items() if o['source'] == source and o['pid'] in ids},
                     'routing': route, 'files': proofs,
                     'unrelated': {str(p['pid']): [f['fid'] for f in p['links']] for p in packages if p['pid'] not in ids}}
        from booksys_core.values import digest
        if task_attempt is not None:
            operation['task_attempt'] = list(task_attempt)
        operation['diagnostic_operation'] = digest(['retirement', source])
        operation['evidence_digest'] = evidence_digest(operation)
        history['acquisitions'][source] = operation
        NativeHistory(self.path.parent).retire(operation['source'],operation)
        self.fault('prepared')
        if self.concurrent:
            conflict = getattr(state, 'conflict', None)
            if conflict is None:
                raise LibraryError('Concurrent retirement requires scoped authority')
            self.store.verify_result(conflict)
        for p in owned:
            if p['dest'] != 0:
                self.client.call('move_package', write=True, destination=0, package_id=p['pid'])
                current = self.client.call('get_package_data', package_id=p['pid'])
                if current['dest'] != 0 or package_identity(current) != package_identity(p):
                    raise LibraryError('Parking before retirement is not confirmed')
                native_receipt(digest(current))
                p['dest'] = 0
                operation['packages'][str(p['pid'])]['dest'] = 0
        operation['evidence_digest'] = evidence_digest(operation)
        NativeHistory(self.path.parent).retire(operation['source'],operation)
        # Recheck the native identities and hooks after all hashing and evidence IO.
        for p in owned:
            if package_identity(self.client.call('get_package_data', package_id=p['pid'])) != package_identity(p):
                raise LibraryError('Retirement target changed before removal')
        if not self._hooks_safe() or any(d['package_id'] in ids for d in self.client.call('status_downloads')):
            raise LibraryError('Target processing changed before removal')
        self._verify_files(operation)
        operation['phase'] = 'deleting'
        NativeHistory(self.path.parent).retire(operation['source'],operation)
        self.client.call('delete_packages', write=True, package_ids=ids)
        self.fault('removed')
        remaining = self.client.call('get_queue_data') + self.client.call('get_collector_data')
        if any(p['pid'] in ids for p in remaining):
            raise LibraryError('Removal not confirmed; preserve intent and inspect')
        return self._confirm(history, operation,completions)
