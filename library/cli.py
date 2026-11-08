"""Versioned local tools. Commands never submit links or modify originals."""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

import yaml

from library.catalog import Catalog, LibraryError, atomic_yaml
from library.intake import receive
from library.metadata import Providers, enrich
from library.publish import Publisher, Capacity
from library.review import decide, render_report, report
from library.runtime import preflight, lab_preflight


def load(path: Path) -> Any:
    return yaml.safe_load(path.read_text())


def isolated_paths(*paths: Path) -> None:
    for path in paths:
        if path.resolve().is_relative_to(Path('/data/booksys')):
            raise LibraryError('Isolated command refuses live Booksys paths')
        if any(p.is_symlink() for p in (path.absolute(), *path.absolute().parents)):
            raise LibraryError('Isolated paths must not contain symlinks')


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--state', type=Path, required=True)
    commands = result.add_subparsers(dest='command', required=True)
    commands.add_parser('status')
    commands.add_parser('replay')
    report_cmd = commands.add_parser('report')
    report_cmd.add_argument('--output', type=Path, required=True)
    decisions = commands.add_parser('decide')
    decisions.add_argument('--batch', type=Path, required=True)
    decisions.add_argument('--author', choices=['flowcool'], required=True)
    intake = commands.add_parser('intake')
    intake.add_argument('--reception', type=Path, required=True)
    intake.add_argument('--captured', type=Path, required=True)
    intake.add_argument('--submissions', type=Path, required=True)
    intake.add_argument('--queue', type=Path, required=True, help='Read-only get_queue_data snapshot')
    intake_mode = intake.add_mutually_exclusive_group()
    intake_mode.add_argument('--isolated', action='store_true')
    intake_mode.add_argument('--lab', action='store_true')
    intake.add_argument('--policy', type=Path)
    intake.add_argument('--library', type=Path)
    preview = commands.add_parser('preview')
    preview.add_argument('--reception', type=Path, required=True)
    preview.add_argument('--captured', type=Path, required=True)
    preview.add_argument('--submissions', type=Path, required=True)
    preview.add_argument('--queue', type=Path, required=True)
    preview.add_argument('--output', type=Path, required=True)
    printed_cmd = commands.add_parser('printed')
    printed_cmd.add_argument('--asset', required=True)
    printed_cmd.add_argument('--reception', type=Path, required=True)
    printed_mode = printed_cmd.add_mutually_exclusive_group()
    printed_mode.add_argument('--isolated', action='store_true')
    printed_mode.add_argument('--lab', action='store_true')
    printed_cmd.add_argument('--policy', type=Path)
    printed_cmd.add_argument('--library', type=Path)
    enrich_cmd = commands.add_parser('enrich')
    enrich_cmd.add_argument('--asset', required=True)
    enrich_cmd.add_argument('--source', action='append', default=[], help='provider:reference')
    enrich_cmd.add_argument('--budget', type=int, default=12)
    for name in ('publish', 'reconcile', 'withdraw', 'restore', 'preflight', 'activate'):
        command = commands.add_parser(name)
        command.add_argument('--reception', type=Path, required=True)
        command.add_argument('--library', type=Path, required=True)
        mode = command.add_mutually_exclusive_group()
        mode.add_argument('--isolated', action='store_true')
        mode.add_argument('--lab', action='store_true')
        command.add_argument('--policy', type=Path)
        command.add_argument('--credentials', type=Path)
        command.add_argument('--pyload', default='http://127.0.0.1:8000')
        if name == 'publish':
            command.add_argument('--asset', required=True)
            command.add_argument('--replace', action='store_true')
        if name == 'withdraw':
            command.add_argument('--edition', required=True)
        if name == 'restore':
            command.add_argument('--operation', required=True)
        if name in ('withdraw', 'restore'):
            command.add_argument('--expected', required=True, type=int)
    return result


def run(args: argparse.Namespace) -> Any:
    store = Catalog(args.state)
    if args.command == 'status':
        state = store.read()
        records = store.ledger()
        return {'schema': 1, 'revision': state['revision'], 'decisions': len(records),
                'counts': {key: len(value) for key, value in state.items() if isinstance(value, dict)},
                'activation': load(args.state / 'library-activation.yaml') if (args.state / 'library-activation.yaml').exists() else None}
    if args.command == 'replay':
        return {'status': 'replayed', 'revision': store.replay()['revision']}
    if args.command == 'report':
        value = report(store)
        atomic_yaml(args.output, value)
        args.output.with_suffix('.md').write_text(render_report(value))
        return {'status': 'written', 'cases': len(value['cases']), 'output': str(args.output)}
    if args.command == 'decide':
        batch = load(args.batch)
        if not isinstance(batch, dict) or set(batch) != {'schema', 'requests'} or batch['schema'] != 1 or not isinstance(batch['requests'], list):
            raise LibraryError('Invalid decision batch')
        return decide(store, batch['requests'], author=args.author)
    if args.command == 'preview':
        isolated_paths(args.output, args.reception)
        if args.output.exists():
            raise LibraryError('Preview output must be new; no overwrite')
        args.output.mkdir(parents=True, mode=0o700)
        preview_store = Catalog(args.output / 'state')
        if store.path.exists():
            preview_store.directory.mkdir(parents=True, exist_ok=True)
            shutil.copy2(store.path, preview_store.path)
            if store.decisions.exists():
                shutil.copytree(store.decisions, preview_store.decisions)
            head = args.state / 'library-ledger-head.yaml'
            if head.exists():
                shutil.copy2(head, preview_store.directory / head.name)
        receive(preview_store, args.reception, load(args.captured), load(args.submissions), load(args.queue))
        value = report(preview_store)
        atomic_yaml(args.output / 'review.yaml', value)
        (args.output / 'review.md').write_text(render_report(value))
        return {'status': 'preview_only', 'cases': len(value['cases']), 'output': str(args.output)}
    if args.command == 'intake':
        if args.lab:
            if args.library is None:
                raise LibraryError('Lab intake requires --library')
            lab_preflight(args.reception, args.library, args.state,
                          (args.captured, args.submissions, args.queue))
        elif args.isolated:
            isolated_paths(args.state, args.reception)
        elif args.policy and args.library:
            preflight(args.reception, args.library, args.state, args.policy)
        else:
            raise LibraryError('Runtime intake requires --policy and --library')
        state = receive(store, args.reception, load(args.captured), load(args.submissions), load(args.queue))
        return {'status': 'received', 'assets': len(state['assets']), 'origins': len(state['origins'])}
    if args.command == 'printed':
        if args.lab:
            if args.library is None:
                raise LibraryError('Lab printed evidence requires --library')
            lab_preflight(args.reception, args.library, args.state)
        elif args.isolated:
            isolated_paths(args.state, args.reception)
        elif args.policy and args.library:
            preflight(args.reception, args.library, args.state, args.policy)
        else:
            raise LibraryError('Runtime printed evidence requires --policy and --library')
        from library.printed import scan_printed
        value = scan_printed(store, args.asset, args.reception)
        return {key: value.get(key) for key in ('status', 'asset', 'window', 'tool', 'reason')}
    if args.command == 'enrich':
        refs = []
        for source in args.source:
            if ':' not in source:
                raise LibraryError('Source reference must be provider:reference')
            refs.append(tuple(source.split(':', 1)))
        providers = Providers(args.state / 'library-provider-cache', budget=min(50, max(0, args.budget)))
        state = enrich(store, args.asset, providers, refs)
        return {'status': 'enriched', 'identity': state['assets'][args.asset].get('identity_result')}
    if args.lab:
        qualification = lab_preflight(args.reception, args.library, args.state)
    elif args.isolated:
        isolated_paths(args.state, args.reception, args.library)
        qualification = {'status': 'isolated', 'capacity_window': 'owned_fixture'}
    else:
        if args.policy is None:
            raise LibraryError('Runtime command requires a qualified policy')
        qualification = preflight(args.reception, args.library, args.state, args.policy)
    if args.command in ('preflight', 'activate'):
        if args.command == 'activate':
            atomic_yaml(args.state / 'library-activation.yaml', qualification)
        return qualification
    capacity = Capacity()
    if not (args.isolated or args.lab):
        if not args.credentials:
            raise LibraryError('Runtime writes require read-only pyLoad capacity checks')
        from pipeline import Pyload
        client = Pyload(args.pyload, json.loads(args.credentials.read_text())['api_key'])
        def external_active() -> bool:
            status = client.call('status_server')
            # Zero active transfers alone does not prevent a queued one starting.
            # Require an already established paused window. Never pause it here.
            return not (status.get('pause') is True and status.get('active') == 0)
        capacity = Capacity(external_active=external_active)
    publisher = Publisher(store, args.reception, args.library, isolated=args.isolated, lab=args.lab,
        exclusive=qualification['status'] == 'qualified', capacity=capacity)
    if args.command == 'publish':
        return publisher.publish(args.asset, replace=args.replace)
    if args.command == 'withdraw':
        return publisher.withdraw(args.edition, expected=args.expected)
    if args.command == 'restore':
        return publisher.restore(args.operation, expected=args.expected)
    if args.command == 'reconcile':
        return publisher.reconcile()
    raise LibraryError('Unknown command')


def main() -> int:
    args = parser().parse_args()
    try:
        output = run(args)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        if isinstance(output, list) and any(isinstance(item, dict) and item.get('status') in ('stale', 'rejected') for item in output):
            return 3
        return 0
    except (LibraryError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'status': 'blocked', 'reason': str(exc)}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
