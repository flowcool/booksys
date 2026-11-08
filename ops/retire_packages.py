"""Preview or retire bounded proven Booksys packages. Keep every source file."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

from booksys import CollectionError
from library.catalog import Catalog, LibraryError, lock
from library.retirement import Retirement
from library.runtime import lab_preflight
from pipeline import Pyload


def configured_scripts() -> Path:
    compose = Path(__file__).resolve().parents[1] / 'compose.shared.yaml'
    volumes = yaml.safe_load(compose.read_text())['services']['pyload']['volumes']
    roots = [v.split(':')[0] for v in volumes if isinstance(v, str) and v.split(':')[1] == '/config']
    if len(roots) != 1 or not Path(roots[0]).is_absolute():
        raise LibraryError('Cannot bind pyLoad scripts to the shared Compose configuration')
    return Path(roots[0]) / 'scripts'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--reception', type=Path, default=Path('/data/booksys/downloads'))
    parser.add_argument('--captured', type=Path, default=Path('/data/booksys/state/state.yaml'))
    parser.add_argument('--submissions', type=Path, default=Path('/data/booksys/state/monitor.yaml'))
    parser.add_argument('--credentials', type=Path, required=True)
    parser.add_argument('--endpoint', default='http://127.0.0.1:8000')
    parser.add_argument('--pyload-scripts', type=Path)
    parser.add_argument('--source', help='One exact tracked source URL')
    parser.add_argument('--limit', type=int, default=1)
    parser.add_argument('--scan-limit', type=int, default=8)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--reconcile-only', action='store_true', help='Confirm absent targets from pending intents; never resend')
    args = parser.parse_args()
    try:
        lab_preflight(args.reception, args.root / 'library', args.root / 'state',
                      (args.captured, args.submissions, args.credentials))
        client = Pyload(args.endpoint, json.loads(args.credentials.read_text())['api_key'])
        service = Retirement(client, Catalog(args.root / 'state'), args.reception,
                             args.submissions, args.captured, scripts=args.pyload_scripts or configured_scripts())
        with lock(args.root / 'state/library-cycle.lock', blocking=False):
            if args.reconcile_only:
                service.reconcile()
                results = [{'outcome': 'reconciled'}]
            elif args.source:
                results = [service.process(args.source, apply=args.apply)]
            else:
                results = service.run(apply=args.apply, limit=args.limit, inspection_limit=args.scan_limit)
        print(json.dumps({'status': 'applied' if args.apply else 'preview', 'results': results}))
        return 0
    except (LibraryError, CollectionError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'status': 'blocked', 'reason': str(exc)}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
