"""LAN HTTP server and catalogue observation thread."""
from __future__ import annotations

import argparse
from pathlib import Path
import signal
import sys
import threading
import time
import os

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from library.catalog import atomic_yaml
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
    from library.operational_state import CollectorState,NativeHistory
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

    def request_stop(*_: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    snapshot()
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
            stop.wait(120)
    observer_thread = threading.Thread(target=observations, name='catalogue-observer')
    observer_thread.start()
    try:
        while not stop.wait(.5):
            server.check()
    finally:
        server.close()
        if observer_thread is not None:
            observer_thread.join()


if __name__ == '__main__':
    main()
