"""One on-demand lab cycle. Read sources; publish only selected proven editions."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from library.catalog import Catalog, LibraryError, atomic_yaml
from library.cli import parser as cli_parser, run
from library.runtime import lab_preflight


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
    args = parser.parse_args()
    state, library = args.root / 'state', args.root / 'library'
    snapshots, evidence = args.root / 'snapshots', args.root / 'evidence'
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
        if args.queue:
            if args.queue.resolve() != (snapshots / 'queue.yaml').resolve():
                shutil.copyfile(args.queue, snapshots / 'queue.yaml')
        else:
            from pipeline import Pyload
            client = Pyload(args.endpoint, json.loads(args.credentials.read_text())['api_key'])
            value = client.call('get_queue_data')
            if not isinstance(value, list):
                raise LibraryError('Unexpected queue snapshot')
            atomic_yaml(snapshots / 'queue.yaml', value)

        def command(*arguments: Any) -> Any:
            return run(cli_parser().parse_args(['--state', str(state), *map(str, arguments)]))

        common = ['--lab', '--reception', args.reception, '--library', library]
        command('intake', *common, '--captured', snapshots / 'captured.yaml',
                '--submissions', snapshots / 'submissions.yaml', '--queue', snapshots / 'queue.yaml')
        command('activate', *common)
        command('reconcile', *common)
        from library.metadata import Providers, enrich
        providers = Providers(state / 'library-provider-cache', budget=min(50, max(0, args.budget)))
        store = Catalog(state)
        for asset in dict.fromkeys(args.asset):
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
                    and {v['edition'] for v in enriched['variants'].values() if v['asset'] == asset} == {identity['edition']}):
                command('publish', '--asset', asset, *common)
        command('reconcile', *common)
        command('report', '--output', evidence / 'review.yaml')
        current = store.read()
        published = {edition['publication']['asset'] for edition in current['editions'].values()
                     if edition.get('publication')}
        origin_counts = {'published': 0, 'pending': 0, 'rejected': 0}
        for origin in current['origins'].values():
            kind = ('rejected' if origin['availability'] == 'rejected' else
                    'published' if origin['availability'] == 'validated' and origin['sha256'] in published else 'pending')
            origin_counts[kind] += 1
        summary = {'status': 'lab', 'assets': len(current['assets']), 'origins': len(current['origins']),
                   'published': len(published), 'pending_assets': len(set(current['assets']) - published),
                   'origin_counts': origin_counts, 'report': str(evidence / 'review.md'),
                   'concurrent_writer_reserve_guaranteed': False}
        atomic_yaml(evidence / 'cycle.yaml', summary)
        print(json.dumps(summary))
        return 0
    except (LibraryError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'status': 'blocked', 'reason': str(exc)}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
