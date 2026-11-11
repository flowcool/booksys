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
from library.runtime import lab_preflight
from library.scheduling import heavy_stage, routing_candidates, RoutingClient
from library.web import read_yaml
from booksys_core.errors import CollectionError


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
    from activity import bind, report
    bind(args.root / 'evidence/activity', 'local', 'identity', 'retirement')
    inputs = (args.captured, args.submissions, *(p for p in (args.queue, args.credentials) if p))
    try:
        lab_preflight(args.reception, args.root / 'library', args.root / 'state', inputs)
        with lock(args.root / 'state/library-cycle.lock', blocking=False):
            from library.launcher import verify_startup
            verify_startup(args.root / 'state')
            return cycle(args)
    except LibraryError as exc:
        value = {'status': 'skipped' if str(exc) == 'Another library operation is active' else 'blocked',
                 'reason': str(exc)}
        report('local', 'waiting' if value['status'] == 'skipped' else 'blocked', 'Cycle gate',
               reason='Processing lock occupied' if value['status'] == 'skipped' else 'Cycle preflight failed')
        print(json.dumps(value))
        return 0 if value['status'] == 'skipped' else 2


def cycle(args: argparse.Namespace) -> int:
    from library.cycle import run_cycle
    result = run_cycle(args)
    print(json.dumps(result))
    return 2 if result['status'] == 'blocked' else 0


if __name__ == '__main__':
    raise SystemExit(main())
