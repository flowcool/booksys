"""Capture read-only queue evidence for library intake. Never submit or retry."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from library.catalog import atomic_yaml
from pipeline import Pyload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials', type=Path, required=True)
    parser.add_argument('--endpoint', default='http://127.0.0.1:8000')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--routing', type=Path, help='Private mirror routing journal for retained file folders')
    args = parser.parse_args()
    credentials = json.loads(args.credentials.read_text())
    client = Pyload(args.endpoint, credentials['api_key'])
    from mirrors import logical_snapshot
    value = logical_snapshot(client, args.routing)
    if not isinstance(value, list):
        raise ValueError('Unexpected pyLoad read-only queue response')
    atomic_yaml(args.output, value)
    print(json.dumps({'status': 'captured', 'packages': len(value), 'output': str(args.output)}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
