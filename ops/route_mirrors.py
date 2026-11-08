"""Plan or apply bounded tracked native mirror routing; no global queue operation."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline import Pyload
from library.catalog import Catalog, lock, atomic_yaml
from mirror_routing import route_package, restore_package
from mirrors import public_hosts
import yaml


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials', type=Path, required=True)
    parser.add_argument('--endpoint', default='http://127.0.0.1:8000')
    parser.add_argument('--captured', type=Path, required=True)
    parser.add_argument('--submissions', type=Path, required=True)
    parser.add_argument('--library-state', type=Path, required=True)
    parser.add_argument('--reception', type=Path, required=True)
    parser.add_argument('--journal', type=Path, required=True)
    parser.add_argument('--source', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--rollback', action='store_true')
    args = parser.parse_args()
    client = Pyload(args.endpoint, json.loads(args.credentials.read_text())['api_key'])
    records = yaml.safe_load(args.captured.read_text())['items']
    entries = yaml.safe_load(args.submissions.read_text())['submissions']
    entry = entries[args.source]
    if args.rollback:
        if not args.apply:
            parser.error('Rollback requires explicit --apply')
        operation = yaml.safe_load(args.journal.read_text())['packages'][str(entry['package_id'])]
        with lock(args.journal.with_suffix('.lock')):
            value = yaml.safe_load(args.journal.read_text())
            operation = value['packages'][str(entry['package_id'])]
            restore_package(client, entry, operation)
            operation['phase'] = 'restored'
            atomic_yaml(args.journal, value)
        print(json.dumps({'outcome': 'restored', 'pid': entry['package_id']}))
    else:
        result = route_package(client, args.source, entry, records[args.source], Catalog(args.library_state).read(),
                               args.reception, args.journal, apply=args.apply, availability=public_hosts())
        print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
