"""Deterministic isolated scale fixtures from the retained qualification snapshot.

This tool never reads live state or calls an external API. Its output is private
benchmark input, not migration authority for the real catalogue.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
from pathlib import Path
import resource
import time
from typing import Any
import yaml

from library.catalog import TABLES, empty, digest, atomic_yaml, validate
from library.codec import state_digest
from library.migration import import_catalogue, activate_catalogue, create_activity
from library.recovery import checkpoint


def clone_dataset(snapshot: dict[str, Any], scale: int, profile: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    state = empty(); state['revision'] = snapshot['revision']
    for clone in range(scale):
        mapping = {ident: hashlib.sha256(f'0005:{clone}:{ident}'.encode()).hexdigest()[:32 if len(ident) == 32 else 64]
                   for kind in TABLES for ident in snapshot[kind]}
        def convert(value: Any) -> Any:
            if isinstance(value, dict):
                return {convert(key): (item + clone * 1000000 if key in ('pid', 'fid', 'package_id', 'attempt_pid') and type(item) is int else convert(item))
                        for key, item in value.items()}
            if isinstance(value, list):
                return [convert(item) for item in value]
            if isinstance(value, str):
                if value in mapping:
                    return mapping[value]
                if value.startswith(('http://', 'https://')):
                    return 'https://fixture.invalid/' + digest([clone, value])
                # Cover strings stay byte-for-byte identical. Content-addressed
                # deduplication is reported separately from entity/cover counts.
            return value
        for kind in TABLES:
            state[kind].update({mapping[ident]: convert(record) for ident, record in snapshot[kind].items()})
    records: list[dict[str, Any]] = []
    if profile == 'identified':
        state['works'].clear(); state['editions'].clear(); state['variants'].clear()
        # Replace the tiny snapshot publication topology with a complete explicit
        # identity fixture. Physical publication/retirement effects are separate
        # integration fixtures, not invented filesystem results in a benchmark.
        state['artifacts'].clear(); state['operations'].clear()
        assets = sorted(state['assets'])
        for index, aid in enumerate(assets):
            wid, eid, vid = [digest([role, aid]) for role in ('work', 'edition', 'variant')]
            work = {'title': f'Fixture album {index:06d}', 'author': 'Fixture author', 'series': f'Series {index % 17:02d}'}
            edition = {'work': wid, 'language': 'fr', 'publisher': 'Fixture publisher',
                       'evidence': {'operator': 'qualification'}, 'reference': 'fixture:' + eid, 'selected': vid}
            state['works'][wid] = work; state['editions'][eid] = edition
            state['variants'][vid] = {'edition': eid, 'asset': aid}
            state['assets'][aid]['identity'] = {'work': wid, 'edition': eid, 'variant': vid}
            # Claims deliberately collide in5% of assets. They remain explicit
            # conflicts. No provider or identification operation runs here.
            if index % 20 == 0:
                edition['isbn'] = '9780000000002'
                edition['reference'] = 'fixture:conflicting-reference'
            if index % 10 == 0:
                state['cases'][digest(['case', aid])] = {'asset': aid, 'status': 'pending'}
        for index, aid in enumerate(assets):
            work = state['works'][state['assets'][aid]['identity']['work']]
            for round_number in range(2):
                action = 'override'
                values = {'title': work['title']} if round_number == 0 else {'series': work['series']}
                if len(assets) > 2 and round_number == 0 and index == 0:
                    action = 'merge_work'
                    values = {'source_work_id': state['assets'][assets[1]]['identity']['work'],
                        'work_id': state['assets'][aid]['identity']['work'], 'evidence': {'operator': 'qualification'}}
                elif len(assets) > 2 and round_number == 0 and index == 1:
                    action = 'separate_edition'
                    values = {'work_id': digest(['separated', aid]), 'work': dict(work), 'evidence': {'operator': 'qualification'}}
                payload = {'schema': 1, 'sequence': len(records) + 1,
                    'previous': records[-1]['id'] if records else None, 'action': action, 'target': aid,
                    'values': values, 'author': 'qualification', 'time': '2026-11-09T00:00:00Z'}
                if index % 20 == 10:
                    identity = state['assets'][aid]['identity']
                    payload['schema'] = 2
                    if round_number == 0:
                        payload.update(action='resolve', affected_editions=[identity['edition']],
                            resolution_inputs=digest({'identity_result': state['assets'][aid].get('identity_result'),
                                                      'assertions': state['assets'][aid].get('assertions', [])}),
                            values={'work_id': identity['work'], 'edition_id': identity['edition'],
                                    'work': dict(work), 'edition': {'publisher': 'Fixture publisher', 'language': 'fr'},
                                    'proof': [], 'select': True})
                    else:
                        payload.update(action='resolution_result', values={'decision': records[-1]['id'],
                            'status': 'blocked', 'reason': 'Fixture blocked copy; no external effect'})
                record = {**payload, 'id': digest(payload)}; records.append(record)
            if index % 20 == 0:
                state['assets'][aid]['contested'] = True
        # A deterministic5% source group exercises many packages and members.
        for oid in sorted(state['origins'])[:max(1, len(state['origins']) // 20)]:
            state['origins'][oid]['source'] = 'https://fixture.invalid/high-fanout'
        # A retained bundle joins many bound members without pretending that a
        # decoder ran. Operation/journal entries below are explicitly synthetic.
        parent_hash, parent_id = digest(['bundle-payload', scale]), digest(['bundle-origin', scale])
        state['origins'][parent_id] = {'source': 'https://fixture.invalid/high-fanout',
            'pid': 900000000, 'fid': 900000000, 'path': 'fixture-bundle.cbr', 'sha256': parent_hash,
            'availability': 'bundle', 'bundle': {'fixture': True}}
        members = [key for key, origin in sorted(state['origins'].items())
                   if key != parent_id and origin.get('sha256') in state['assets']]
        for oid in members[:max(1, len(state['origins']) // 20)]:
            origin = state['origins'][oid]
            origin.update(parent_origin=parent_id, parent_sha256=parent_hash,
                member_sha256=origin['sha256'], member='Fixture member ' + oid,
                path='bundle-inputs/' + parent_hash + '/' + oid + '.cbz')
        for index, aid in enumerate(assets):
            if index % 20 != 5:
                continue
            eid = state['assets'][aid]['identity']['edition']
            operation, artifact = digest(['publication', aid])[:32], digest(['artifact', aid])
            state['editions'][eid]['publication'] = {'asset': aid, 'operation': operation,
                'target': 'fixture-' + eid, 'artifact_format': 'cbz', 'fixture': True,
                'manifest': {'book.cbz': artifact, 'fiche.md': digest(['fiche', aid])}}
            state['artifacts'][artifact] = {'sha256': artifact, 'source_assets': [aid], 'editions': [eid],
                'transformation': {'fixture': True}}
            state['operations'][operation] = {'kind': 'create', 'edition': eid,
                'status': 'committed', 'journal': operation + '.yaml', 'fixture': True}
        # The last10% is a genuine unapplied immutable suffix.
        applied = records[:int(len(records) * .9)]
        from library.catalog import apply_effect
        for record in applied:
            apply_effect(state, record, replay=True)
        state['ledger'] = [record['id'] for record in applied]
    validate(state)
    return state, records


def prepare(snapshot_path: Path, destination: Path, *, scale: int, profile: str) -> dict[str, Any]:
    if destination.exists() or destination.resolve().is_relative_to(Path('/data/booksys')):
        raise ValueError('Benchmark destination must be a new isolated directory')
    destination.mkdir(parents=True, mode=0o700)
    with snapshot_path.open() as incoming:
        snapshot = yaml.load(incoming, Loader=yaml.CSafeLoader)
    validate(snapshot)
    with snapshot_path.open('rb') as incoming:
        source_hash = hashlib.file_digest(incoming, 'sha256').hexdigest()
    started = time.monotonic()
    state, records = clone_dataset(snapshot, scale, profile)
    del snapshot
    root = destination / 'candidate'; directory = root / 'state'; directory.mkdir(parents=True, mode=0o700)
    source = directory / 'library.yaml'
    with source.open('w') as outgoing:
        yaml.dump(state, outgoing, Dumper=yaml.CSafeDumper, allow_unicode=True, sort_keys=True)
    source.chmod(0o600)
    with source.open('rb') as incoming:
        source_digest = hashlib.file_digest(incoming, 'sha256').hexdigest()
    authority = f'qualification-{scale}-{profile}'
    for record in records:
        atomic_yaml(directory / 'library-decisions' / f"{record['sequence']:09d}-{record['id']}.yaml", record, exclusive=True)
    for ident, operation in state['operations'].items():
        atomic_yaml(directory / 'library-operations' / (ident + '.yaml'), operation)
    if records:
        atomic_yaml(directory / 'library-ledger-head.yaml', {'count': len(records), 'head': records[-1]['id']})
    importing = time.monotonic()
    store = import_catalogue(directory, state, authority=authority, source_digest=source_digest, ledger_records=records)
    imported = time.monotonic() - importing
    expected = state_digest(state)
    if store.semantic_digest() != expected:
        raise ValueError('Benchmark import is not semantically identical')
    # Preserve one identical legacy input for the baseline, with shared immutable
    # fixture media strings. It is never selected by the candidate API.
    baseline = destination / 'baseline'; (baseline / 'state').mkdir(parents=True, mode=0o700)
    import shutil
    shutil.copyfile(source, baseline / 'state/library.yaml')
    if records:
        shutil.copytree(directory / 'library-decisions', baseline / 'state/library-decisions')
        shutil.copyfile(directory / 'library-ledger-head.yaml', baseline / 'state/library-ledger-head.yaml')
    if (directory / 'library-operations').exists():
        shutil.copytree(directory / 'library-operations', baseline / 'state/library-operations')
    counts = {kind: len(state[kind]) for kind in TABLES}
    covers = sum(bool(asset.get('cover')) for asset in state['assets'].values())
    cover_bytes = sum(len(asset.get('cover', '')) for asset in state['assets'].values())
    del state
    activate_catalogue(directory, authority=authority)
    activity_at = time.time()
    legacy_activity = {}
    for stage in ('collection', 'byparr', 'downloads', 'local', 'identity', 'retirement'):
        legacy_activity[stage] = {'events': {digest([stage, n]): {'at': activity_at - 86400 + n * 10,
            'state': 'working', 'operation': 'progress', 'reason': 'none', 'title': 'Fixture progress'}
            for n in range(min(8192, 500 * scale))}}
    create_activity(directory / 'activity', authority=authority + '-activity', timestamp=activity_at, legacy=legacy_activity,
                    history_started_at=activity_at - 86400)
    result = {'schema': 1, 'scale': scale, 'profile': profile, 'snapshot_sha256': source_hash,
        'input_sha256': source_digest, 'semantic_sha256': expected, 'authority': authority,
        'counts': counts, 'decisions': len(records), 'cover_fields': covers, 'cover_encoded_bytes': cover_bytes,
        'unique_attachments': len(list(store.payloads.iterdir())), 'import_seconds': imported,
        'fixture_limits': 'Synthetic benchmark journals have no physical effects; separate post-conversion rehearsal proves real copies and retirement',
        'workload_refs': {'source': digest('https://fixture.invalid/high-fanout'),
                          'bundle': digest(['bundle-origin', scale])} if profile == 'identified' else {},
        'prepare_seconds': time.monotonic() - started, 'peak_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
    (destination / 'dataset.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def resume_prepared(snapshot_path: Path, destination: Path, *, scale: int, profile: str) -> dict[str, Any]:
    """Retain failed evidence; resume only the already verified frozen import."""
    from library.store import Store
    from library.sqlite_runtime import connection
    from library.activity_store import ActivityStore
    authority = f'qualification-{scale}-{profile}'
    if destination.resolve().is_relative_to(Path('/data/booksys')) or (destination / 'dataset.json').exists():
        raise ValueError('Only an unfinished isolated fixture can resume')
    directory = destination / 'candidate/state'
    store = Store(directory, authority=authority)
    with connection(store.path) as db:
        revision, _ = store.check(db)
        source_digest, expected = db.execute('SELECT imported_source_digest,source_semantic FROM meta').fetchone()
        counts = {kind: db.execute(f'SELECT count(*) FROM {kind}').fetchone()[0] for kind in TABLES}
        decisions = db.execute('SELECT count(*) FROM ledger_records').fetchone()[0]
        covers, cover_bytes = db.execute('SELECT count(*),coalesce(sum(original_length),0) FROM attachments').fetchone()
    if store.semantic_digest() != expected:
        raise ValueError('Frozen fixture differs from its verified import')
    activate_catalogue(directory, authority=authority, resume=True)
    at = time.time()
    activity = directory / 'activity'
    if not activity.exists():
        create_activity(activity, authority=authority + '-activity', timestamp=at,
            history_started_at=at - 86400, legacy={stage: {'events': {digest([stage, n]): {
                'at': at - 86400 + n * 10, 'state': 'working', 'operation': 'progress', 'reason': 'none',
                'title': 'Fixture progress'} for n in range(min(8192, 500 * scale))}}
                for stage in ('collection', 'byparr', 'downloads', 'local', 'identity', 'retirement')})
    ActivityStore(activity, authority=authority + '-activity').producers()
    with snapshot_path.open('rb') as incoming:
        snapshot_digest = hashlib.file_digest(incoming, 'sha256').hexdigest()
    result = {'schema': 1, 'scale': scale, 'profile': profile, 'snapshot_sha256': snapshot_digest,
        'input_sha256': source_digest, 'semantic_sha256': expected, 'authority': authority,
        'counts': counts, 'decisions': decisions, 'unique_attachments': covers,
        'unique_attachment_encoded_bytes': cover_bytes, 'import_seconds': None, 'prepare_seconds': None,
        'peak_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        'preparation_attempts': [{'exit_code': 137, 'cause': 'unconfirmed; no kernel journal access'},
                                 {'status': 'resumed_from_verified_frozen_transition', 'revision': revision}]}
    (destination / 'dataset.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--scale', type=int, choices=(1, 10), required=True)
    parser.add_argument('--profile', choices=('snapshot', 'identified'), required=True)
    parser.add_argument('--resume-prepared', action='store_true')
    args = parser.parse_args()
    operation = resume_prepared if args.resume_prepared else prepare
    print(json.dumps(operation(args.snapshot, args.destination, scale=args.scale, profile=args.profile), sort_keys=True))


if __name__ == '__main__':
    main()
