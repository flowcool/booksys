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
from library.scheduling import heavy_stage, routing_candidates, RoutingClient
from library.web import read_yaml
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
    parser.add_argument('--intake-burst', type=int, default=32, help='Sequential intake inspections per turn before backlog recheck')
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
    prior = read_yaml(evidence / 'cycle.yaml')
    turn = prior.get('heavy_turn', 0)
    stage = 'preflight'
    def progress(name: str) -> None:
        nonlocal stage
        stage = name
        atomic_yaml(evidence / 'cycle.yaml', {'status': 'running', 'started_at': started_at,
            'stage': stage, 'stage_started_at': now(), 'heavy_turn': turn})
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
        progress('queue_snapshot')
        if args.queue:
            if args.queue.resolve() != (snapshots / 'queue.yaml').resolve():
                shutil.copyfile(args.queue, snapshots / 'queue.yaml')
        elif not (args.auto and not getattr(args, 'retire_packages', False) and (snapshots / 'queue.yaml').is_file()
                  and time.time() - (snapshots / 'queue.yaml').stat().st_mtime < 60):
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
        progress('recovery')
        command('reconcile', *common)  # Must precede intake/activation/projection after crash.
        from library.resolution import Resolution
        from library.publish import Publisher
        store = Catalog(state)
        Resolution(store, args.reception).apply(Publisher(store, args.reception, library, lab=True))
        with lock(state / 'library-network.lock'):
            atomic_yaml(state / 'library-network-budget.yaml', {'remaining': 50, 'cycle_started': now()})
        progress('intake')
        if args.auto:
            from library.intake import receive
            from library.cli import load
            receive(Catalog(state), args.reception, load(snapshots / 'captured.yaml'),
                    load(snapshots / 'submissions.yaml'), load(snapshots / 'queue.yaml'),
                    incremental=True, inspection_limit=max(1, getattr(args, 'intake_burst', 32)))
        else:
            command('intake', *common, '--captured', snapshots / 'captured.yaml',
                    '--submissions', snapshots / 'submissions.yaml', '--queue', snapshots / 'queue.yaml')
        command('activate', *common)
        command('reconcile', *common)
        from library.metadata import Providers, enrich
        providers = Providers(state / 'library-provider-cache', budget=min(50, max(0, args.budget)), shared=state)
        store = Catalog(state)
        automatic: dict[str, Any] = {}
        intake_backlog = sum(o.get('reason') == 'Intake budget deferred' for o in store.read()['origins'].values())
        if args.auto:
            from library.automatic import process_assets, ready_assets
            from library.admission import process_admissions as process_provisional
            selected = heavy_stage(intake_backlog, turn)
            automatic = {'processed': 0, 'provisional_processed': 0}
            if selected:
                turn += 1
                progress(selected)
                if selected == 'metadata':
                    automatic.update(process_assets(store, args.reception, library, providers, limit=1))
                    if not automatic.get('processed'):
                        progress('provisional')
                        result = process_provisional(store, args.reception, library, limit=1)
                        automatic.update({'provisional_' + k: v for k, v in result.items()})
                else:
                    result = process_provisional(store, args.reception, library, limit=1)
                    automatic.update({'provisional_' + k: v for k, v in result.items()})
                    if not result.get('processed'):
                        progress('metadata')
                        automatic.update(process_assets(store, args.reception, library, providers, limit=1))
            automatic['ready_backlog'] = ready_assets(store)
            # A zero-limit call reports eligible conversion work without writing copies.
            result = process_provisional(store, args.reception, library, limit=0)
            automatic['provisional_ready_backlog'] = result['ready_backlog']
        mirror_results: list[dict[str, Any]] = []
        optional_errors: list[dict[str, Any]] = []
        if getattr(args, 'route_mirrors', False):
            progress('mirror_routing')
            if not args.credentials:
                raise LibraryError('Mirror routing requires controller credentials')
            from pipeline import Pyload
            client = Pyload(args.endpoint, json.loads(args.credentials.read_text())['api_key'])
            from mirror_routing import route_package
            from mirrors import public_hosts
            from library.cli import load
            records, submissions = load(snapshots / 'captured.yaml')['items'], load(snapshots / 'submissions.yaml')['submissions']
            routing_path = args.submissions.parent / 'mirror-routing.yaml'
            routing_catalogue = store.read()  # Whole-cycle lock makes this snapshot stable for routing.
            candidates = routing_candidates(load(snapshots / 'queue.yaml'), submissions,
                                            routing_catalogue, read_yaml(routing_path))
            # One candidate per minute at most. Repeated fast intake turns must not
            # exhaust pyLoad's native per-minute API budget through optional routing.
            routing_prior = read_yaml(evidence / 'mirror-routing-cycle.yaml')
            if candidates and time.time() >= routing_prior.get('next_attempt', 0):
                from urllib.error import HTTPError, URLError
                from booksys import CollectionError
                availability = None
                try:
                    availability = public_hosts()
                except (HTTPError, URLError, OSError, ValueError, CollectionError) as exc:
                    optional_errors.append({'source': 'alldebrid_public_hosts',
                        'http_status': getattr(exc, 'code', None), 'reason': type(exc).__name__})
                if availability is not None:
                    source = candidates[routing_prior.get('cursor', 0) % len(candidates)]
                    routing_client = RoutingClient(client)
                    # A retained intent can already contain an uncertain prior write.
                    routing_client.wrote = bool(read_yaml(routing_path).get('packages', {}).get(
                        str(submissions[source]['package_id'])))
                    try:
                        result = route_package(routing_client, source, submissions[source], records[source], routing_catalogue,
                                               args.reception, routing_path, apply=True, availability=availability)
                        mirror_results.append(result)
                    except HTTPError as exc:
                        if routing_client.wrote:
                            raise  # Preserve every uncertain write and its readback.
                        optional_errors.append({'source': 'pyload', 'endpoint': routing_client.endpoint,
                            'http_status': exc.code, 'reason': 'HTTPError'})
                atomic_yaml(evidence / 'mirror-routing-cycle.yaml', {'created_at': now(),
                    'next_attempt': time.time() + 60, 'cursor': routing_prior.get('cursor', 0) + 1, 'results': mirror_results, 'errors': optional_errors})
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
                   'duration_seconds': round(time.monotonic() - started, 3), **automatic, 'mirror_outcomes': len(mirror_results), 'optional_errors': optional_errors,
                   'stage': 'idle', 'heavy_turn': turn, 'intake_backlog': intake_backlog, 'assets': len(current['assets']), 'origins': len(current['origins']),
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
        from urllib.error import HTTPError
        from urllib.parse import urlsplit
        error = ({'error_endpoint': urlsplit(exc.url).path, 'http_status': exc.code}
                 if isinstance(exc, HTTPError) else {})
        failure = {**error, 'status': 'blocked', 'reason': str(exc), 'stage': stage, 'error_source': stage, 'heavy_turn': turn, 'started_at': started_at,
                   'finished_at': now(), 'duration_seconds': round(time.monotonic() - started, 3)}
        atomic_yaml(evidence / 'cycle.yaml', failure)
        from library.web import build_projection
        atomic_yaml(evidence / 'dashboard.yaml', build_projection(args.root))
        print(json.dumps(failure))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
