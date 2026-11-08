"""One on-demand lab cycle. Read sources; publish only selected proven editions."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
import time
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from library.catalog import Catalog, LibraryError, atomic_yaml, lock, now
from library.cli import parser as cli_parser, run
from library.runtime import lab_preflight
from booksys import CollectionError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--reception', type=Path, default=Path('/data/booksys/downloads'))
    parser.add_argument('--captured', type=Path, default=Path('/data/booksys/state/state.yaml'))
    parser.add_argument('--submissions', type=Path, default=Path('/data/booksys/state/monitor.yaml'))
    queue = parser.add_mutually_exclusive_group(required=True)
    queue.add_argument('--queue', type=Path, help='Existing read-only queue snapshot')
    queue.add_argument('--credentials', type=Path, help='Controller-only queue GET credentials')
    parser.add_argument('--endpoint', default='http://127.0.0.1:8000')
    parser.add_argument('--asset', action='append', default=[], help='Selected SHA-256; others stay pending')
    parser.add_argument('--printed', action='store_true', help='Bounded OCR on selected assets only')
    parser.add_argument('--budget', type=int, default=2, help='Total provider request budget for this cycle')
    parser.add_argument('--auto', action='store_true', help='Bounded new-input processing; uncertain identities are not retried')
    parser.add_argument('--route-mirrors', action='store_true', help='Apply one journaled native mirror routing operation after intake')
    parser.add_argument('--retire-packages', action='store_true', help='Retire one technically complete Booksys acquisition after a successful cycle')
    parser.add_argument('--pyload-scripts', type=Path, help='Audited local pyLoad scripts directory; must contain no scripts')
    args = parser.parse_args()
    inputs = (args.captured, args.submissions, *(p for p in (args.queue, args.credentials) if p))
    try:
        lab_preflight(args.reception, args.root / 'library', args.root / 'state', inputs)
        with lock(args.root / 'state/library-cycle.lock', blocking=False):
            return cycle(args)
    except LibraryError as exc:
        value = {'status': 'skipped' if str(exc) == 'Another library operation is active' else 'blocked',
                 'reason': str(exc)}
        print(json.dumps(value))
        return 0 if value['status'] == 'skipped' else 2


def cycle(args: argparse.Namespace) -> int:
    started_at, started = now(), time.monotonic()
    state, library = args.root / 'state', args.root / 'library'
    snapshots, evidence = args.root / 'snapshots', args.root / 'evidence'
    try:
        inputs = (args.captured, args.submissions, *(p for p in (args.queue, args.credentials) if p))
        lab_preflight(args.reception, library, state, inputs)
        # Snapshot destinations also stay outside all source inputs and outputs.
        lab_preflight(args.reception, snapshots, state, inputs)
        lab_preflight(args.reception, evidence, state, inputs)
        for path in (snapshots / 'captured.yaml', snapshots / 'submissions.yaml',
                     snapshots / 'queue.yaml', evidence / 'review.yaml', evidence / 'review.md',
                     evidence / 'cycle.yaml'):
            if path.is_symlink():
                raise LibraryError('Lab cycle destinations must not be symlinks')
        for directory in (state, snapshots, evidence):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        for source, name in ((args.captured, 'captured.yaml'), (args.submissions, 'submissions.yaml')):
            if source.resolve() != (snapshots / name).resolve():
                shutil.copyfile(source, snapshots / name)
        if args.queue:
            if args.queue.resolve() != (snapshots / 'queue.yaml').resolve():
                shutil.copyfile(args.queue, snapshots / 'queue.yaml')
        else:
            from pipeline import Pyload
            client = Pyload(args.endpoint, json.loads(args.credentials.read_text())['api_key'])
            from library.retirement import Retirement
            from ops.retire_packages import configured_scripts
            retirement = Retirement(client, Catalog(state), args.reception, args.submissions, args.captured,
                                    scripts=getattr(args, 'pyload_scripts', None) or configured_scripts())
            retirement.reconcile()
            from mirrors import logical_snapshot
            value = logical_snapshot(client, args.submissions.parent / 'mirror-routing.yaml')
            if not isinstance(value, list):
                raise LibraryError('Unexpected queue snapshot')
            atomic_yaml(snapshots / 'queue.yaml', value)

        def command(*arguments: Any) -> Any:
            return run(cli_parser().parse_args(['--state', str(state), *map(str, arguments)]))

        common = ['--lab', '--reception', args.reception, '--library', library]
        command('reconcile', *common)  # Must precede intake/activation/projection after crash.
        from library.resolution import Resolution
        from library.publish import Publisher
        store = Catalog(state)
        Resolution(store, args.reception).apply(Publisher(store, args.reception, library, lab=True))
        with lock(state / 'library-network.lock'):
            atomic_yaml(state / 'library-network-budget.yaml', {'remaining': 50, 'cycle_started': now()})
        if args.auto:
            from library.intake import receive
            from library.cli import load
            receive(Catalog(state), args.reception, load(snapshots / 'captured.yaml'),
                    load(snapshots / 'submissions.yaml'), load(snapshots / 'queue.yaml'),
                    incremental=True, inspection_limit=8)
        else:
            command('intake', *common, '--captured', snapshots / 'captured.yaml',
                    '--submissions', snapshots / 'submissions.yaml', '--queue', snapshots / 'queue.yaml')
        command('activate', *common)
        command('reconcile', *common)
        from library.metadata import Providers, enrich
        providers = Providers(state / 'library-provider-cache', budget=min(50, max(0, args.budget)), shared=state)
        store = Catalog(state)
        automatic = {}
        if args.auto:
            from library.automatic import process_assets
            automatic = process_assets(store, args.reception, library, providers)
        mirror_results = []
        if getattr(args, 'route_mirrors', False):
            if not args.credentials:
                raise LibraryError('Mirror routing requires controller credentials')
            from pipeline import Pyload
            client = Pyload(args.endpoint, json.loads(args.credentials.read_text())['api_key'])
            from mirror_routing import route_package
            from mirrors import public_hosts
            from library.cli import load
            availability = public_hosts()
            records, submissions = load(snapshots / 'captured.yaml')['items'], load(snapshots / 'submissions.yaml')['submissions']
            routing_path = args.submissions.parent / 'mirror-routing.yaml'
            routing_catalogue = store.read()  # Whole-cycle lock makes this snapshot stable for routing.
            cursor_path = state / 'library-mirror-cursor.yaml'
            sources = sorted(s for s, e in submissions.items()
                             if e.get('status') == 'queued' and s in records)
            cursor_record = load(cursor_path) if cursor_path.exists() else {}
            if (not isinstance(cursor_record, dict)
                    or (cursor_record.get('source') is not None and not isinstance(cursor_record['source'], str))):
                raise LibraryError('Invalid mirror routing cursor')
            cursor = cursor_record.get('source')
            if cursor in sources:
                index = sources.index(cursor) + 1
                sources = sources[index:] + sources[:index]
            # Bound native API reads as well as writes. A full catalogue scan
            # exceeds the shared downloader's per-client rate limit.
            for source in sources[:8]:
                entry = submissions[source]
                result = route_package(client, source, entry, records[source], routing_catalogue, args.reception,
                                       routing_path, apply=True, availability=availability)
                mirror_results.append(result)
                atomic_yaml(cursor_path, {'source': source})
                if result['outcome'] in ('parked', 'fallback_active'):
                    break
            atomic_yaml(evidence / 'mirror-routing-cycle.yaml', {'created_at': now(), 'results': mirror_results})
        for asset in dict.fromkeys(args.asset if not args.auto else []):
            if asset not in store.read()['assets']:
                raise LibraryError('Selected asset is absent from tracked intake')
            if args.printed:
                command('printed', '--asset', asset, *common)
            enriched = enrich(store, asset, providers, [])
            current = enriched['assets'][asset]
            publication = (enriched['editions'][current['identity']['edition']].get('publication')
                           if current.get('identity') else None)
            if publication and publication['asset'] == asset:
                continue  # Existing exports require an explicit replacement, never an automatic rewrite.
            identity = current.get('identity')
            if (identity and current['status'] == 'validated' and not current.get('contested')
                    and current.get('disposition') not in ('exclude', 'defer')
                    and enriched['editions'][identity['edition']].get('selected') == identity['variant']
                    and {v['edition'] for v in enriched['variants'].values() if v['asset'] == asset and v.get('active', True)} == {identity['edition']}):
                command('publish', '--asset', asset, *common)
        command('reconcile', *common)
        if not args.auto:
            command('report', '--output', evidence / 'review.yaml')
        retirement_results = []
        if getattr(args, 'retire_packages', False):
            if not args.credentials or args.queue:
                raise LibraryError('Retirement requires live controller credentials and fresh queue state')
            retirement_results = retirement.run(apply=True)
            atomic_yaml(evidence / 'retirement-cycle.yaml', {'created_at': now(), 'results': retirement_results})
            from library.cli import load
            from library.intake import receive
            from mirrors import logical_snapshot
            queue_value = logical_snapshot(client, args.submissions.parent / 'mirror-routing.yaml')
            atomic_yaml(snapshots / 'queue.yaml', queue_value)
            receive(Catalog(state), args.reception, load(snapshots / 'captured.yaml'),
                    load(snapshots / 'submissions.yaml'), queue_value, incremental=True, inspection_limit=0)
        current = store.read()
        published = {edition['publication']['asset'] for edition in current['editions'].values()
                     if edition.get('publication')}
        origin_counts = {'published': 0, 'pending': 0, 'rejected': 0}
        for origin in current['origins'].values():
            kind = ('rejected' if origin['availability'] == 'rejected' else
                    'published' if origin['availability'] == 'validated' and origin['sha256'] in published else 'pending')
            origin_counts[kind] += 1
        intake_backlog = sum(o.get('reason') == 'Intake budget deferred' for o in current['origins'].values())
        summary = {'status': 'lab', 'started_at': started_at, 'finished_at': now(),
                   'duration_seconds': round(time.monotonic() - started, 3), **automatic, 'mirror_outcomes': len(mirror_results),
                   'intake_backlog': intake_backlog, 'assets': len(current['assets']), 'origins': len(current['origins']),
                   'retired_acquisitions': sum(r['outcome'] == 'retired' for r in retirement_results),
                   'published': len(published), 'pending_assets': len(set(current['assets']) - published),
                   'origin_counts': origin_counts, 'report': str(evidence / 'review.md'),
                   'concurrent_writer_reserve_guaranteed': False}
        atomic_yaml(evidence / 'cycle.yaml', summary)
        from library.web import build_projection
        atomic_yaml(evidence / 'dashboard.yaml', build_projection(args.root))
        print(json.dumps(summary))
        return 0
    except (LibraryError, CollectionError, OSError, ValueError, KeyError, TypeError) as exc:
        failure = {'status': 'blocked', 'reason': str(exc), 'started_at': started_at,
                   'finished_at': now(), 'duration_seconds': round(time.monotonic() - started, 3)}
        atomic_yaml(evidence / 'cycle.yaml', failure)
        from library.web import build_projection
        atomic_yaml(evidence / 'dashboard.yaml', build_projection(args.root))
        print(json.dumps(failure))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
