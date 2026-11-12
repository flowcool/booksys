"""Sequential library orchestration, callable without a CLI or service."""
from __future__ import annotations
import json
from pathlib import Path
import shutil
import time
from typing import Any
from library.catalog import Catalog
from library.primitives import LibraryError, atomic_yaml, lock, now
from library.runtime import lab_preflight
from library.scheduling import heavy_stage, routing_candidates, RoutingClient
from library.views import read_yaml
from booksys_core.errors import CollectionError
from booksys_core.diagnostics import emit


def run_cycle(args: Any) -> dict[str, Any]:
    import uuid
    from booksys_core.diagnostics import context
    with context(cycle_id=uuid.uuid4().hex):
        return _run_cycle(args)


def _run_cycle(args: Any) -> dict[str, Any]:
    started_at, started = now(), time.monotonic()
    state, library = args.root / 'state', args.root / 'library'
    snapshots, evidence = args.root / 'snapshots', args.root / 'evidence'
    from library.operational_state import StatusObservations,CollectorState,RuntimeObservation
    from library.postgres import Database
    database=Database.configured(state)
    statuses=StatusObservations(database)
    collector=CollectorState(args.submissions.parent,database=database)
    observation=RuntimeObservation(database)
    prior=statuses.cycle()
    turn = prior.get('heavy_turn', 0)
    stage = 'preflight'
    def progress(name: str) -> None:
        nonlocal stage
        stage = name
        event_stage = 'retirement' if name == 'retirement' else 'identity' if name in ('metadata', 'admission') else 'local'
        emit(service='library', stage=event_stage, code='started')
        statuses.save_cycle( {'status': 'running', 'started_at': started_at,
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
        progress('queue_snapshot')
        if args.queue:
            # Explicit offline input is ingested once; PostgreSQL owns the selected observation.
            from library.documents import load
            queue_input=load(args.queue)
            if not isinstance(queue_input,list):
                raise LibraryError('Unexpected queue snapshot')
            observation.publish(queue_input,at=time.time(),remaining=None,overshoot=None)
        try:
            saved_queue,saved_at=observation.queue()
            fresh=bool(args.queue) or (args.auto and not getattr(args,'retire_packages',False) and 0<=time.time()-saved_at<60)
        except LibraryError:
            fresh=False
        if not fresh:
            from booksys_core.downloader import Pyload
            client = Pyload(args.endpoint, json.loads(args.credentials.read_text())['api_key'])
            from library.retirement import Retirement
            from library.runtime import configured_scripts
            retirement = Retirement(client, Catalog(state), args.reception, args.submissions, args.captured,
                                    scripts=getattr(args, 'pyload_scripts', None) or configured_scripts())
            retirement.reconcile()
            from mirrors import logical_snapshot
            value = logical_snapshot(client, args.submissions.parent / 'mirror-routing.yaml')
            if not isinstance(value, list):
                raise LibraryError('Unexpected queue snapshot')
            observation.publish(value,at=time.time(),remaining=None,overshoot=None)

        def command(name: str, *arguments: Any) -> Any:
            # Arguments retain the former local call shape. No CLI parsing occurs.
            from library.admission import AdmissionPublisher
            from library.intake import receive
            from library.documents import load
            publisher = AdmissionPublisher(Catalog(state), args.reception, library, lab=True)
            if name == 'reconcile':
                return publisher.reconcile()
            if name == 'activate':
                value = lab_preflight(args.reception, library, state)
                # Selection belongs to qualified activation tooling, never this cycle.
                observation.activation(role="library")
                return value
            if name == 'intake':
                return receive(Catalog(state), args.reception, collector.captured(),
                               collector.monitor(), observation.queue()[0])
            if name == 'printed':
                from library.printed import scan_printed
                return scan_printed(Catalog(state), arguments[1], args.reception)
            raise LibraryError('Unknown cycle action')

        common = ['--lab', '--reception', args.reception, '--library', library]
        progress('recovery')
        command('reconcile', *common)  # Must precede intake/activation/projection after crash.
        progress('intake')
        if args.auto:
            from library.intake import receive
            from library.documents import load
            receive(Catalog(state), args.reception, collector.captured(),
                    collector.monitor(), observation.queue()[0],
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
        intake_backlog = sum(o.get('reason') == 'Intake budget deferred' for _, o in store.records('origins'))
        if args.auto:
            from library.automatic import process_assets, ready_assets
            from library.admission import process_admissions as process_provisional
            selected = heavy_stage(intake_backlog, turn)
            automatic = {'processed': 0, 'provisional_processed': 0}
            if selected:
                turn += 1
                progress('admission' if selected == 'provisional' else selected)
                if selected == 'metadata':
                    automatic.update(process_assets(store, args.reception, library, providers, limit=1))
                    if not automatic.get('processed'):
                        progress('admission')
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
            from booksys_core.downloader import Pyload
            client = Pyload(args.endpoint, json.loads(args.credentials.read_text())['api_key'])
            from mirror_routing import route_package
            from mirrors import public_hosts
            from library.documents import load
            records, submissions = collector.captured()['items'], collector.monitor()['submissions']
            routing_path = args.submissions.parent / 'mirror-routing.yaml'
            from library.operational_state import NativeHistory
            native_history=NativeHistory(routing_path.parent)
            routing_catalogue = store.read()  # Whole-cycle lock makes this snapshot stable for routing.
            candidates = routing_candidates(observation.queue()[0], submissions,
                                            routing_catalogue, native_history.routing())
            # One candidate per minute at most. Repeated fast intake turns must not
            # exhaust pyLoad's native per-minute API budget through optional routing.
            routing_prior = statuses.routing()
            if candidates and time.time() >= routing_prior.get('next_attempt', 0):
                from urllib.error import HTTPError, URLError
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
                    routing_client.wrote = bool(native_history.routing().get('packages', {}).get(
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
                statuses.save_routing( {'created_at': now(),
                    'next_attempt': time.time() + 60, 'cursor': routing_prior.get('cursor', 0) + 1, 'results': mirror_results, 'errors': optional_errors})
        for asset in dict.fromkeys(args.asset if not args.auto else []):
            if store.get('assets', asset) is None:
                raise LibraryError('Selected asset is absent from tracked intake')
            if args.printed:
                command('printed', '--asset', asset, *common)
            enriched = enrich(store, asset, providers, [])
            from library.admission import AdmissionPublisher, eligible
            if eligible(enriched, asset):
                AdmissionPublisher(store, args.reception, library, lab=True).publish_asset(asset)
        command('reconcile', *common)
        retirement_results = []
        if getattr(args, 'retire_packages', False):
            if not args.credentials or args.queue:
                raise LibraryError('Retirement requires live controller credentials and fresh queue state')
            progress('retirement')
            retirement_results = retirement.run(apply=True)
            statuses.save_retirement( {'created_at': now(), 'results': retirement_results})
            from library.documents import load
            from library.intake import receive
            from mirrors import logical_snapshot
            queue_value = logical_snapshot(client, args.submissions.parent / 'mirror-routing.yaml')
            observation.publish(queue_value,at=time.time(),remaining=None,overshoot=None)
            receive(Catalog(state), args.reception, collector.captured(),
                    collector.monitor(), queue_value, incremental=True, inspection_limit=0)
        backend = store.backend()
        current = store.working_set('retire', {'origins': backend.ids('origins')}
                                    if backend is not None else {}, media=False)
        published = {key for key, asset in current['assets'].items() if asset.get('admission')}
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
                   'classified': sum(a.get('admission', {}).get('status') == 'classified' for a in current['assets'].values()),
                   'holding': sum(a.get('admission', {}).get('status') == 'holding' for a in current['assets'].values()),
                   'origin_counts': origin_counts,
                   'concurrent_writer_reserve_guaranteed': False}
        statuses.save_cycle( summary)
        from library.views import build_projection
        atomic_yaml(evidence / 'dashboard.yaml', build_projection(args.root))
        emit(service='library', stage='local', code='completed', duration_ms=int((time.monotonic() - started) * 1000))
        return summary
    except (LibraryError, CollectionError, OSError, ValueError, KeyError, TypeError) as exc:
        from urllib.error import HTTPError
        from urllib.parse import urlsplit
        error = ({'error_endpoint': urlsplit(exc.url).path, 'http_status': exc.code}
                 if isinstance(exc, HTTPError) else {})
        emit(service='library', stage='local', code='failed', reason='unknown_effect',
             exception_type=type(exc).__name__ if type(exc).__name__ in ('LibraryError', 'CollectionError', 'OSError', 'ValueError', 'KeyError', 'TypeError', 'HTTPError') else 'RuntimeError')
        failure = {**error, 'status': 'blocked', 'reason': 'Cycle failed; inspect filtered diagnostics', 'stage': stage, 'error_source': stage, 'heavy_turn': turn, 'started_at': started_at,
                   'finished_at': now(), 'duration_seconds': round(time.monotonic() - started, 3)}
        statuses.save_cycle( failure)
        from library.views import build_projection
        atomic_yaml(evidence / 'dashboard.yaml', build_projection(args.root))
        return failure
