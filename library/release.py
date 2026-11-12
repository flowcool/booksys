"""Release finished downloads: record the package in the database, then delete the pyLoad entry.

One independent step. It needs no intake result and no fresh queue copy. The proof is the
pyLoad state itself (every link finished) plus every file present and not empty on disk.
The retained record keeps the package (name, folder, links) so that intake still finds the
files after the entry is gone. Files are never touched. A package with a mirror route is left
to the older retirement path.
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from booksys_core.network_admission import native_operation, native_receipt
from library.operational_state import CollectorState, NativeHistory
from library.primitives import digest, now
from library.reception import reception_path
from mirrors import host_sets, link_identity
from retirement_history import evidence_digest, history_path, same_package


class Release:
    def __init__(self, client: Any, database: Any, reception: Path, collector: Path, *, hooks_safe: Any):
        self.client, self.database, self.reception, self.collector = client, database, reception, collector
        self.hooks_safe = hooks_safe
        self.journal = history_path(collector / "mirror-routing.yaml")

    def _queue(self) -> list[dict[str, Any]]:
        return list(self.client.call('get_queue_data')) + list(self.client.call('get_collector_data'))

    @staticmethod
    def _finished(package: dict[str, Any]) -> bool:
        return bool(package['links']) and all(item.get('status') == 0 for item in package['links'])

    def _files_present(self, package: dict[str, Any], links: list[dict[str, Any]] | None = None,
                       route: dict[str, Any] | None = None) -> bool:
        folders = (route or {}).get('input_folders', {})
        for item in package['links'] if links is None else links:
            path = reception_path(self.reception, str(Path(folders.get(str(item['fid']), package['folder']))), item['name'])
            try:
                # pyLoad "size" is the size the hoster announced; it differs from the real file. Status 0 is the
                # pyLoad proof of a complete download, so the file only has to exist and not be empty.
                if not path.is_file() or path.stat().st_size == 0:
                    return False
            except OSError:
                return False
        return True

    @staticmethod
    def _required(package: dict[str, Any], entry: dict[str, Any], record: dict[str, Any] | None,
                  route: dict[str, Any] | None) -> list[dict[str, Any]] | None:
        """The links that must be finished: all of them, or one complete hoster set when mirrors were skipped."""
        if not route and Release._finished(package):
            return list(package['links'])
        if record is None:
            return None
        try:
            sets = list(host_sets(record).values())
        except Exception:
            return None
        primary = entry.get('primary_links') or (route or {}).get('primary_links')
        by_url = {link_identity(f['url']): f for f in package['links']}
        for candidate in ([primary] if primary else sets):
            if not candidate or not any(sorted(map(link_identity, candidate)) == sorted(map(link_identity, s)) for s in sets):
                continue
            links = [by_url.get(link_identity(u)) for u in candidate]
            if all(item is not None and item.get('status') == 0 for item in links):
                return [item for item in links if item is not None]
        return None

    def candidates(self, limit: int) -> tuple[list[tuple[str, dict[str, Any], dict[str, Any]]], list[dict[str, Any]]]:
        history = NativeHistory(self.collector, database=self.database)
        done = history.retirements()['acquisitions']
        routes = history.routing()['packages']
        state = CollectorState(self.collector, database=self.database)
        entries = state.monitor()['submissions']
        records = state.captured()['items']
        live = self._queue()
        by_pid = {p['pid']: p for p in live}
        chosen = []
        for source in sorted(entries):
            entry = entries[source]
            package = by_pid.get(entry.get('package_id'))
            if entry.get('status') != 'queued' or source in done or package is None:
                continue
            route = routes.get(str(package['pid']))
            if route and route.get('source') not in (None, source):
                route = None  # an entry of an earlier package that had this id
            if route and (route.get('attempt_pid') is not None or route.get('phase') not in ('stopped', 'active', 'restored')):
                continue  # fallback attempts and unsettled routes stay with the older retirement path
            required = self._required(package, entry, records.get(source), route)
            if required is None:
                continue
            host = urlsplit(source).hostname or ''
            expected = sorted(link_identity(u) for u in entry.get('links', []))
            actual = sorted(link_identity(f['url']) for f in package['links'])
            if (not (host == 'bookys-ebooks.com' or host.endswith('.bookys-ebooks.com'))
                    or package['name'] != entry.get('name') or not package['name'].startswith('Booksys-')
                    or not expected or actual != expected or len(set(actual)) != len(actual)):
                continue
            if self._files_present(package, required, route):
                chosen.append((source, entry, package))
            if len(chosen) >= limit:
                break
        return chosen, live

    def reconcile(self) -> int:
        """Finish releases that a crash left in phase 'deleting' (the intent is written before the delete is sent).

        The same package (name and links, not only its id: pyLoad reuses ids) still in the queue means the delete did not
        happen or was not read back: it is sent again, which is safe for a package that is proved identical. A package that
        is gone means the delete happened: the record becomes 'retired'. Without this, a kill between the intent and the
        readback left the source in 'deleting' for good and its package in pyLoad.
        """
        history = NativeHistory(self.collector, database=self.database)
        stuck = {source: record for source, record in history.retirements()['acquisitions'].items()
                 if record.get('phase') == 'deleting' and record.get('release') and len(record.get('packages', {})) == 1}
        if not stuck or not self.hooks_safe():
            return 0
        live = {p['pid']: p for p in self._queue()}
        resend: dict[str, int] = {}
        for source, record in stuck.items():
            retained = next(iter(record['packages'].values()))
            present = live.get(retained['pid'])
            if present is not None and same_package(retained, present):
                resend[source] = retained['pid']
        if resend:
            with native_operation(self.client, 'release-reconcile:' + digest(sorted(resend)), self.journal):
                self.client.call('delete_packages', write=True, package_ids=sorted(resend.values()))
                live = {p['pid']: p for p in self._queue()}
                native_receipt(digest(sorted(live)))
        finished = 0
        for source, record in stuck.items():
            retained = next(iter(record['packages'].values()))
            present = live.get(retained['pid'])
            if present is not None and same_package(retained, present):
                continue                                      # still there after the second delete: stays 'deleting'
            record.update(phase='retired', confirmed_at=now())
            history.retire(source, record)
            finished += 1
        return finished

    def run(self, limit: int = 10) -> dict[str, int]:
        self.reconcile()
        chosen, _ = self.candidates(limit)
        if not chosen or not self.hooks_safe():
            return {'candidates': len(chosen), 'released': 0}
        active = {d['package_id'] for d in self.client.call('status_downloads')}
        chosen = [c for c in chosen if c[2]['pid'] not in active]
        history = NativeHistory(self.collector, database=self.database)
        routes = history.routing()['packages']
        records = {}
        for source, entry, package in chosen:
            operation: dict[str, Any] = {
                'phase': 'deleting', 'created_at': now(), 'source': source, 'submission': entry, 'release': True,
                'packages': {str(package['pid']): copy.deepcopy(package)}, 'origins': {}, 'routing': copy.deepcopy(routes.get(str(package['pid'])) or {}) if routes.get(str(package['pid']), {}).get('source') in (None, source) else {},
                'files': {}, 'unrelated': {}}
            operation['evidence_digest'] = evidence_digest(operation)
            history.retire(source, operation)  # Intent first: a crash leaves 'deleting', never a silent loss.
            records[source] = operation
        ids = [p['pid'] for _, _, p in chosen]
        if ids:
            with native_operation(self.client, 'release:' + digest(sorted(records)), self.journal):
                self.client.call('delete_packages', write=True, package_ids=ids)
                remaining = self._queue()
                native_receipt(digest(sorted(p['pid'] for p in remaining)))  # Readback done: unblock pyLoad writes.
        else:
            remaining = self._queue()
        released = 0
        for source, _, package in chosen:
            if any(p['pid'] == package['pid'] and same_package(package, p) for p in remaining):
                continue  # Still there: stays 'deleting'; the next run never resends it.
            operation = records[source]
            operation.update(phase='retired', confirmed_at=now())
            history.retire(source, operation)
            released += 1
        return {'candidates': len(chosen), 'released': released}
