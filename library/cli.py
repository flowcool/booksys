"""Versioned local tools. Commands never submit links or modify originals."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

from library.catalog import Catalog
from library.primitives import LibraryError, atomic_yaml, lock
from library.intake import receive
from library.metadata import Providers, enrich
from library.admission import AdmissionPublisher
from library.publication import Capacity
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
    enrich_cmd = commands.add_parser('enrich')
    enrich_cmd.add_argument('--asset', required=True)
    enrich_cmd.add_argument('--source', action='append', default=[], help='provider:reference')
    for name in ('admit', 'reconcile', 'preflight', 'activate'):
        command = commands.add_parser(name)
        command.add_argument('--reception', type=Path, required=True)
        command.add_argument('--library', type=Path, required=True)
        mode = command.add_mutually_exclusive_group()
        mode.add_argument('--isolated', action='store_true')
        mode.add_argument('--lab', action='store_true')
        command.add_argument('--policy', type=Path)
        command.add_argument('--credentials', type=Path)
        command.add_argument('--pyload', default='http://127.0.0.1:8000')
        if name == 'admit':
            command.add_argument('--asset', required=True)
    return result


def run(args: argparse.Namespace) -> Any:
    from library.launcher import verify_startup
    verify_startup(args.state)
    store = Catalog(args.state)
    if args.command == 'status':
        state = store.read()
        from library.operational_state import RuntimeObservation
        try:activation=RuntimeObservation(store.backend()._database).activation(role='library')
        except LibraryError:activation=None
        return {'schema': 1, 'revision': state['revision'],
                'counts': {key: len(value) for key, value in state.items() if isinstance(value, dict)},
                'activation':activation}
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
        if args.isolated:
            captured,journal,queue=load(args.captured),load(args.submissions),load(args.queue)
        else:
            from library.operational_state import CollectorState,RuntimeObservation
            captured=CollectorState(args.captured.parent,database=store.backend()._database).captured()
            journal=CollectorState(args.submissions.parent,database=store.backend()._database).monitor()
            queue=RuntimeObservation(store.backend()._database).queue()[0]
        state=receive(store,args.reception,captured,journal,queue)
        return {'status': 'received', 'assets': len(state['assets']), 'origins': len(state['origins'])}
    if args.command == 'enrich':
        refs = []
        for source in args.source:
            if ':' not in source:
                raise LibraryError('Source reference must be provider:reference')
            refs.append(tuple(source.split(':', 1)))
        providers = Providers(args.state / 'library-provider-cache')
        state = enrich(store, args.asset, providers, refs)
        return {'status': 'enriched', 'identity': state['assets'][args.asset].get('work_result')}
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
            from library.operational_state import RuntimeObservation
            RuntimeObservation(store.backend()._database).activate(qualification,role='library')
        return qualification
    capacity = Capacity()
    if not (args.isolated or args.lab):
        if not args.credentials:
            raise LibraryError('Runtime writes require read-only pyLoad capacity checks')
        from booksys_core.downloader import Pyload
        client = Pyload(args.pyload, json.loads(args.credentials.read_text())['api_key'])
        def external_active() -> bool:
            status = client.call('status_server')
            # Zero active transfers alone does not prevent a queued one starting.
            # Require an already established paused window. Never pause it here.
            return not (status.get('pause') is True and status.get('active') == 0)
        capacity = Capacity(external_active=external_active)
    if args.command == 'admit':
        from library.admission import AdmissionPublisher
        return AdmissionPublisher(store, args.reception, args.library,
            isolated=args.isolated, lab=args.lab,
            exclusive=qualification['status'] == 'qualified', capacity=capacity).publish_asset(args.asset)
    publisher = AdmissionPublisher(store, args.reception, args.library, isolated=args.isolated, lab=args.lab,
        exclusive=qualification['status'] == 'qualified', capacity=capacity)
    if args.command == 'reconcile':
        return publisher.reconcile()
    raise LibraryError('Unknown command')


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command in ('status', 'preflight'):
            output = run(args)
        else:
            # A local mutation is a manual cycle, serialized with the service.
            with lock(args.state / 'library-cycle.lock', blocking=False):
                if args.command not in ('admit', 'reconcile'):
                    from library.operational_state import CopyOperations
                    operations=CopyOperations(Catalog(args.state).backend()._database,'admission')
                    for operation in operations.records():
                        if operation.get('phase') not in ('committed','interrupted','capacity_aborted'):
                            raise LibraryError('Reconcile copy journals before a local catalogue write')
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
