"""End-to-end HTTP measurements for an isolated prepared catalogue.

The body is fully received, including a detail's bounded cover request. This is
an exploratory runner until the complete accepted workload matrix is recorded.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import resource
import statistics
import threading
import time
from typing import Any
import urllib.parse
import urllib.request
import urllib.error

from library.api import PublicAPI
from library.catalog import LibraryError
from library.http_runtime import HTTPRuntime
from library.migration import manifest
from library.relationships import Relationships
from library.sqlite_runtime import connection
from library.store import Change, Store
from library.codec import decode, encode


def measure(dataset: Path, *, viewers: int, requests: int, warmups: int, only: str | None = None,
            cold: bool = False) -> dict[str, Any]:
    from library.authority import code_digest
    import sqlite3, sys
    bundle = code_digest()
    descriptor = json.loads((dataset / 'dataset.json').read_text())
    root = dataset / 'candidate'
    if root.resolve().is_relative_to(Path('/data/booksys')):
        raise ValueError('Qualification refuses live Booksys state')
    store = Store(root / 'state', authority=descriptor['authority'])
    with connection(store.path, write=True) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='asset_flags'").fetchone():
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE asset_flags(asset TEXT PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE, '
                       'provisional_published INTEGER NOT NULL CHECK(provisional_published IN (0,1)))')
            for ident, payload in db.execute('SELECT id,payload FROM assets'):
                record = store.restore(payload, media=False)
                db.execute('INSERT INTO asset_flags VALUES (?,?)',
                           (ident, int(record.get('provisional', {}).get('status') == 'published')))
            db.execute('COMMIT')
    # This is an explicit isolated re-selection after code changes. Real
    # conversion uses its approved operation plan and frozen source proof.
    manifest(store.directory, store.authority, 'sqlite')
    relations = Relationships(store); relations.refresh()
    api = PublicAPI(root, host='', reception=dataset / 'no-downloads', relationships=relations)
    invoke_errors: list[dict[str, str]] = []
    phases: dict[str, list[float]] = {'invoke': [], 'ipc': []}
    from library.ipc import Channel
    from unittest.mock import patch
    original_invoke, original_result = api.invoke, Channel.result
    def invoke(action: str, value: dict[str, Any]) -> Any:
        start = time.perf_counter()
        try:
            return original_invoke(action, value)
        except Exception as exc:
            from booksys_core.diagnostics import safe_text
            invoke_errors.append({'action': action, 'type': type(exc).__name__, 'reason': safe_text(str(exc)), 'sqlite_code': str(getattr(exc.__cause__, 'sqlite_errorname', ''))})
            raise
        finally:
            phases['invoke'].append((time.perf_counter() - start) * 1000)
    def result(channel: Channel, ident: str, value: Any, *, chunks: bool = False) -> None:
        start = time.perf_counter()
        try:
            original_result(channel, ident, value, chunks=chunks)
        finally:
            phases['ipc'].append((time.perf_counter() - start) * 1000)
    api_patch = patch.object(api, 'invoke', new=invoke)
    channel_patch = patch.object(Channel, 'result', new=result)
    api_patch.start(); channel_patch.start()
    runtime = HTTPRuntime(('127.0.0.1', 0), api)
    stopped, writer_errors = threading.Event(), []
    asset_id = store.ids('assets')[0]
    writes = [0]
    commit_ms: list[float] = []
    def writer() -> None:
        while not stopped.wait(.1):
            try:
                current = store.get('assets', asset_id)
                if current is None:
                    raise ValueError('Missing fixture asset')
                current['qualification_counter'] = writes[0]
                began = time.perf_counter()
                store.commit(Change(upserts={'assets': {asset_id: current}}), expected_revision=store.revision)
                commit_ms.append((time.perf_counter() - began) * 1000)
                writes[0] += 1
            except LibraryError:
                # HTTP owns no business write; a busy interval is accounted for.
                writer_errors.append('storage_busy')
    thread = threading.Thread(target=writer, daemon=True)
    started_cpu = time.process_time()
    runtime.start()
    base = 'http://127.0.0.1:' + str(runtime.address[1])
    def get(path: str) -> tuple[bytes, float]:
        start = time.perf_counter()
        with urllib.request.urlopen(base + path, timeout=3) as response:
            raw = response.read()
            if response.status != 200:
                raise ValueError('Incomplete HTTP result')
        return raw, (time.perf_counter() - start) * 1000
    try:
        first_raw, first_ms = get('/api/library')
        first = json.loads(first_raw)
        if cold:
            return {'schema': 1, 'code_sha256': bundle, 'python': sys.version.split()[0],
                'sqlite': sqlite3.sqlite_version, 'dataset': descriptor, 'viewers': 1,
                'results': {'fresh_first50': {'requests': 1, 'max_ms': first_ms,
                    'max_bytes': len(first_raw), 'availability': {'complete': 1}}},
                'under_2000ms': first_ms < 2000, 'writer_commits': 0,
                'peak_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                'coverage': 'One first list query in a fresh process; other cold routes are not measured here'}
        with connection(store.path) as db:
            candidates = [(ident, store.restore(payload, media=False)) for ident, payload in
                          db.execute('SELECT id,payload FROM assets ORDER BY id')]
            covers = []
            for ident, payload in db.execute('SELECT id,payload FROM assets ORDER BY id'):
                envelope = decode(payload)
                if envelope.get('cover'):
                    size = db.execute('SELECT original_length FROM attachments WHERE digest=?', (envelope['cover'],)).fetchone()[0]
                    origin = db.execute('SELECT target FROM asset_origin WHERE source=? ORDER BY target LIMIT 1', (ident,)).fetchone()
                    if origin:
                        covers.append((size, ident, origin[0]))
        if not covers:
            raise ValueError('Detail cover fixtures required')
        covers.sort()
        normal, large = covers[len(covers) // 2], covers[-1]
        title = first['groups'][0]['representative']['title'][:12]
        source_ref = descriptor.get('workload_refs', {}).get('source') or first['groups'][0]['id']
        bundle_ref = descriptor.get('workload_refs', {}).get('bundle')
        starting_revision = store.revision
        workloads: dict[str, tuple[str, str | None]] = {
            'first50': ('/api/library?page=1', None),
            'middle50': ('/api/library?page=' + str(max(1, first['pages'] // 2)), None),
            'last50': ('/api/library?page=' + str(first['pages']), None),
            'status_filter': ('/api/library?status=pending', None),
            'title_filter': ('/api/library?' + urllib.parse.urlencode({'search': title}), None),
            'series_filter': ('/api/library?search=Series', None),
            'stable_sort': ('/api/library?sort=title&descending=true', None),
            'normal_detail': ('/fiche/' + normal[2], '/cover/' + normal[1] + '.png'),
            'large_detail': ('/fiche/' + large[2], '/cover/' + large[1] + '.png'),
            'compatibility': ('/api/status', None),
            'high_fanout': ('/api/relationships?' + urllib.parse.urlencode({'kind': 'source', 'id': source_ref}), None),
        }
        workloads['source_children'] = ('/api/library?' + urllib.parse.urlencode({'source': source_ref}), None)
        if bundle_ref:
            workloads['bundle'] = ('/api/relationships?' + urllib.parse.urlencode({'kind': 'origin', 'id': bundle_ref}), None)
        if only is not None:
            workloads = {only: workloads[only]}
        thread.start()
        results: dict[str, Any] = {}
        def request(paths: tuple[str, str | None]) -> tuple[float, int, str]:
            start = time.perf_counter()
            size = 0
            status = 'complete'
            try:
                for path in paths:
                    if path:
                        raw, _ = get(path); size += len(raw)
            except urllib.error.HTTPError as exc:
                status = 'http_' + str(exc.code)
            except (OSError, ValueError):
                status = 'unavailable'
            return (time.perf_counter() - start) * 1000, size, status
        with ThreadPoolExecutor(max_workers=viewers) as clients:
            for name, paths in workloads.items():
                warm = [request(paths) for _ in range(warmups)]
                measured = list(clients.map(lambda _: request(paths), range(requests)))
                durations = sorted(value[0] for value in measured)
                results[name] = {'requests': requests, 'median_ms': statistics.median(durations),
                    'p95_ms': durations[min(len(durations) - 1, int(len(durations) * .95))],
                    'max_ms': max(durations), 'max_bytes': max(value[1] for value in measured),
                    'outliers_ms': [value for value in durations if value >= 2000],
                    'availability': {status: sum(value[2] == status for value in measured) for status in sorted({v[2] for v in measured})},
                    'warmup_failures': [value[2] for value in warm if value[2] != 'complete']}
        if code_digest() != bundle:
            raise ValueError('Application changed during qualification; results are not candidate proof')
        return {'schema': 1, 'code_sha256': bundle, 'python': sys.version.split()[0],
                'sqlite': sqlite3.sqlite_version, 'starting_revision': starting_revision, 'viewers': viewers, 'requests_per_query': requests, 'warmups': warmups,
                'dataset': descriptor, 'results': results, 'writer_commits': writes[0],
                'writer_commit_upper_bound_ms': {'max': max(commit_ms, default=0),
                    'median': statistics.median(commit_ms) if commit_ms else 0,
                    'over_100ms': [value for value in commit_ms if value >= 100]},
                'writer_busy': len(writer_errors), 'invoke_errors': invoke_errors,
                'phases_ms': {key: {'max': max(values, default=0), 'median': statistics.median(values) if values else 0} for key, values in phases.items()},
                'peak_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                'cpu_seconds': time.process_time() - started_cpu,
                'under_2000ms': all(value['max_ms'] < 2000 and value['availability'] == {'complete': requests} and not value['warmup_failures'] for value in results.values()),
                'coverage': 'Exploratory HTTP subset; final images, fresh-process, baseline and diagnostic cap workloads remain separate'}
    finally:
        (dataset / 'last-http-invoke-errors.json').write_text(json.dumps(invoke_errors, indent=2) + '\n')
        stopped.set()
        if thread.is_alive():
            thread.join(5)
        runtime.close()
        api_patch.stop(); channel_patch.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--viewers', type=int, choices=(1, 4), required=True)
    parser.add_argument('--requests', type=int, default=100)
    parser.add_argument('--warmups', type=int, default=5)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--only')
    parser.add_argument('--cold', action='store_true')
    args = parser.parse_args()
    result = measure(args.dataset, viewers=args.viewers, requests=args.requests, warmups=args.warmups, only=args.only, cold=args.cold)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'under_2000ms': result['under_2000ms'], 'writer_commits': result['writer_commits'],
                      'maximum_ms': max(value['max_ms'] for value in result['results'].values()),
                      'failed_workloads': [key for key, value in result['results'].items() if value['availability'] != {'complete': args.requests}]}))
    if not result['under_2000ms']:
        raise SystemExit(2)


if __name__ == '__main__':
    main()
