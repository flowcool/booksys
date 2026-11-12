"""Retry up to three exact legacy format outcomes through normal incremental intake."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from library.catalog import Catalog, atomic_yaml, lock, now
from library.intake import receive
from library.runtime import lab_preflight
from library.operational_state import CollectorState,RuntimeObservation


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--reception', type=Path, default=Path('/data/booksys/downloads'))
    parser.add_argument('--origin', action='append', required=True)
    args = parser.parse_args()
    if not 1 <= len(set(args.origin)) == len(args.origin) <= 3:
        parser.error('Select one to three distinct exact origins')
    root = args.root
    lab_preflight(args.reception,root/'library',root/'state')
    with lock(root / 'state/library-cycle.lock', blocking=False):
        store = Catalog(root / 'state')
        before = store.read()
        selected = {oid: before['origins'][oid] for oid in args.origin}
        database=store.backend()._database
        collector=CollectorState(root/'state',database=database)
        captured=collector.captured()
        journal=collector.monitor()
        journal['submissions'] = {source: value for source, value in journal['submissions'].items()
                                 if source in {o['source'] for o in selected.values()}}
        queue=RuntimeObservation(database).queue()[0]
        for origin in selected.values():
            submission = journal['submissions'].get(origin['source'], {})
            packages = [p for p in queue if p['pid'] == origin['pid']]
            if (submission.get('package_id') != origin['pid'] or submission.get('status') != 'queued'
                    or len(packages) != 1 or packages[0]['name'] != submission.get('name')
                    or len([link for link in packages[0].get('links', [])
                            if link['fid'] == origin['fid']]) != 1):
                raise ValueError('Selected origin differs from saved submission or queue')
        # Keep every package. Restrict only links in these selected submissions.
        # No unselected origin is marked package_missing or otherwise changed.
        for package in queue:
            if package['pid'] in {o['pid'] for o in selected.values()}:
                package['links'] = [link for link in package.get('links', [])
                                    if (package['pid'], link['fid']) in {(o['pid'], o['fid']) for o in selected.values()}]
        evidence = root / 'evidence/format-retry'
        evidence.mkdir(parents=True, exist_ok=True)
        checkpoint = evidence / ('before-' + str(before['revision']) + '.yaml')
        if checkpoint.exists():
            raise ValueError('Recovery checkpoint already exists')
        atomic_yaml(checkpoint,dict(before))
        after = receive(store, args.reception, captured, journal, queue,
                        incremental=True, inspection_limit=3)
        atomic_yaml(evidence / 'result.yaml', {'created_at': now(), 'checkpoint': str(checkpoint),
            'before': selected, 'after': {oid: after['origins'][oid] for oid in selected}})
        print('Targeted incremental intake complete; see ' + str(evidence / 'result.yaml'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
