"""One LAN read-only server and one sequential five-minute lab cycle thread."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import json

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from library.catalog import atomic_yaml, now
from library.web import build_projection, handler, read_yaml


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--listen', default='192.0.2.10')
    parser.add_argument('--port', type=int, default=8768)
    parser.add_argument('--credentials', type=Path, required=True)
    parser.add_argument('--retire-packages', action='store_true', help='Retire one proven acquisition per successful cycle')
    args = parser.parse_args()
    stop = threading.Event()
    failed = threading.Event()
    from library.catalog import Catalog
    from library.resolution import Resolution
    server = ThreadingHTTPServer((args.listen, args.port), handler(args.root, resolution=Resolution(
        Catalog(args.root / 'state'), Path('/data/booksys/downloads'))))
    server.timeout = 1
    evidence = args.root / 'evidence'
    evidence.mkdir(parents=True, exist_ok=True)

    def snapshot() -> None:
        atomic_yaml(evidence / 'dashboard.yaml', build_projection(args.root))

    def cycles() -> None:
        while not stop.is_set():
            prior = read_yaml(evidence / 'cycle.yaml')
            atomic_yaml(evidence / 'cycle.yaml', {**prior, 'status': 'running', 'started_at': now(),
                        'next_cycle_at': None})
            snapshot()
            started = time.monotonic()
            # The child holds the whole-cycle lock. This single thread never overlaps children.
            command = [sys.executable, str(Path(__file__).with_name('library_cycle.py')),
                '--root', str(args.root), '--credentials', str(args.credentials), '--auto', '--route-mirrors', '--budget', '50']
            if args.retire_packages:
                command.append('--retire-packages')
            result = subprocess.run(command, capture_output=True, text=True)
            (evidence / 'automatic-cycle.stdout').write_text(result.stdout)
            (evidence / 'automatic-cycle.stderr').write_text(result.stderr)
            try:
                outcome = json.loads(result.stdout)
            except ValueError:
                outcome = {'status': 'blocked', 'reason': 'Cycle failed; inspect saved service evidence'}
            finished = time.time()
            value = {**read_yaml(evidence / 'cycle.yaml'), **outcome, 'finished_at': now(),
                     'duration_seconds': round(time.monotonic() - started, 3),
                     'next_cycle_at': datetime.fromtimestamp(finished + 300, timezone.utc).isoformat()}
            atomic_yaml(evidence / 'cycle.yaml', value)
            snapshot()
            stop.wait(300)  # Five minutes after completion, no calendar overlap or catch-up burst.

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
                atomic_yaml(evidence / 'cycle.yaml', {'status': 'blocked', 'finished_at': now(),
                    'reason': 'Cycle worker failed; service restart required; inspect service log'})
                snapshot()
            except Exception:
                pass  # The process exits nonzero even if its evidence path is unavailable.
            import traceback
            traceback.print_exc()

    worker = threading.Thread(target=supervised_cycles, name='library-cycle')
    worker.start()
    try:
        while not stop.is_set():
            server.handle_request()
    finally:
        server.server_close()
        worker.join()
    if failed.is_set():
        raise RuntimeError('Library cycle thread failed')


if __name__ == '__main__':
    main()
