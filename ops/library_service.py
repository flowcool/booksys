"""One LAN server and one sequential backlog-aware lab worker."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import signal
import sys
import threading
import time
import json
import os
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from library.catalog import atomic_yaml, now
from library.scheduling import next_delay
from booksys_core.process import run_child
from booksys_core.diagnostics import emit
from library.web import build_projection


def main() -> None:
    from booksys_core.diagnostics import install_logging
    install_logging("library")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--listen', default='192.0.2.10')
    parser.add_argument('--port', type=int, default=8768)
    parser.add_argument('--credentials', type=Path, required=True)
    parser.add_argument('--retire-packages', action='store_true', help='Retire one proven acquisition per successful cycle')
    parser.add_argument('--independent-queues', action='store_true', help='Serve HTTP and observation; separate supervised actors own dispatch')
    parser.add_argument('--public-host')
    parser.add_argument('--public-origin')
    parser.add_argument('--reception', type=Path, default=Path('/data/booksys/downloads'))
    parser.add_argument('--collector', type=Path, default=Path('/data/booksys/state'))
    parser.add_argument('--pyload', default='http://127.0.0.1:8000')
    args = parser.parse_args()
    from library.http import public_origins
    try:
        public_origins(args.public_host or '', args.public_origin)
    except ValueError as exc:
        parser.error(str(exc))
    if args.independent_queues:
        from library.stage_worker import verify_activation
        verify_activation(args.root)
        os.environ['BOOKSYS_ADMISSION_STATE'] = str(args.root / 'state')
    from library.launcher import verify_startup
    verify_startup(args.root / 'state')
    stop = threading.Event()
    failed = threading.Event()
    from library.catalog import Catalog
    from library.api import PublicAPI
    from library.http_runtime import HTTPRuntime
    api = PublicAPI(args.root, host=args.public_host or '', reception=args.reception,
                    public_origin=args.public_origin)
    from library.relationships import Relationships
    from library.store import Store
    backend = api.catalog.backend()
    if not isinstance(backend, Store):
        raise RuntimeError("The library controller requires the selected PostgreSQL store")
    api.relationships = Relationships(backend)
    from library.operational_state import CollectorState,NativeHistory,StatusObservations
    statuses=StatusObservations(backend._database)
    collector_state=CollectorState(args.collector,database=backend._database)
    native_history=NativeHistory(args.collector,database=backend._database)
    server = HTTPRuntime((args.listen, args.port), api)
    server.start()
    evidence = args.root / 'evidence'
    evidence.mkdir(parents=True, exist_ok=True)

    last = {'at': 0.0, 'revision': -1}

    def snapshot() -> None:
        # The projection reads the whole catalogue (65 MB): rebuild it when the catalogue moved, else at most every 5 minutes.
        from library.catalog import Catalog
        revision = Catalog(args.root / 'state').revision
        if revision == last['revision'] and time.monotonic() - last['at'] < 300:
            return
        atomic_yaml(evidence / 'dashboard.yaml', build_projection(args.root))
        last['at'], last['revision'] = time.monotonic(), revision

    def cycles() -> None:
        while not stop.is_set():
            prior = statuses.cycle()
            statuses.save_cycle({'status': 'running', 'started_at': now(), 'stage': 'starting',
                        'stage_started_at': now(), 'next_cycle_at': None,
                        'heavy_turn': prior.get('heavy_turn', 0)})
            snapshot()
            started = time.monotonic()
            # The child holds the whole-cycle lock. This single thread never overlaps children.
            command = [sys.executable, str(Path(__file__).with_name('library_cycle.py')),
                '--root', str(args.root), '--credentials', str(args.credentials), '--auto', '--route-mirrors', '--budget', '50']
            if args.retire_packages:
                command.append('--retire-packages')
            def forward(value: dict[str, Any]) -> None:
                # Records are already validated. The parent's safe sink preserves IDs.
                from booksys_core.diagnostics import relay
                relay(value)
            result = run_child(command, on_event=forward)
            (evidence / 'automatic-cycle.stdout').write_bytes(result.stdout)
            if result.rejected:
                emit(service='library', stage='diagnostics', code='degraded',
                     reason='diagnostic_unavailable', dropped_count=result.rejected)
            try:
                outcome = json.loads(result.stdout)
            except ValueError:
                outcome = {'status': 'blocked', 'reason': 'Cycle failed; inspect saved service evidence'}
            delay = next_delay(outcome)
            finished = time.time()
            value = {**statuses.cycle(), **outcome, 'finished_at': now(),
                     'duration_seconds': round(time.monotonic() - started, 3),
                     'next_cycle_at': datetime.fromtimestamp(finished + delay, timezone.utc).isoformat()}
            statuses.save_cycle(value)
            snapshot()
            stop.wait(delay)  # No overlap. Idle waits; eligible work continues immediately.

    def request_stop(*_: object) -> None:
        stop.set()  # Let an active cycle finish. Never terminate a publication half-way for shutdown.

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    snapshot()
    def supervised_cycles() -> None:
        try:
            cycles()
        except Exception:
            failed.set()
            stop.set()
            try:
                statuses.save_cycle({'status': 'blocked', 'finished_at': now(),
                    'reason': 'Cycle worker failed; service restart required; inspect service log'})
                snapshot()
            except Exception:
                pass  # The process exits nonzero even if its evidence path is unavailable.
            emit(service='library', stage='local', code='failed', reason='unknown_effect')

    worker = None if args.independent_queues else threading.Thread(target=supervised_cycles, name='library-cycle')
    if worker is not None:
        worker.start()
    def observations() -> None:
        while not stop.is_set():
            from library.retirement_observation import observe as retirement_observation
            retirement = retirement_observation(args.collector / 'package-retirement.yaml')
            try:
                api.captured = collector_state.captured()['items']
                if api.relationships is not None:
                    if not isinstance(retirement.get('acquisitions'), dict) and retirement.get('missing') is not True:
                        raise ValueError('Retirement observation unavailable')
                    api.relationships.refresh(submissions=collector_state.monitor(),
                        routing=native_history.routing(),
                        retirement=retirement)
            except Exception:
                emit(service='library', stage='diagnostics', code='degraded', reason='diagnostic_unavailable')
            stop.wait(30)
    observer_thread = threading.Thread(target=observations, name='catalogue-observer')
    observer_thread.start()
    try:
        while not stop.wait(.5):
            server.check()
    finally:
        server.close()
        if worker is not None:
            worker.join()
        if observer_thread is not None:
            observer_thread.join()
    if failed.is_set():
        raise RuntimeError('Library cycle thread failed')


if __name__ == '__main__':
    main()
