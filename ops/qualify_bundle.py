"""Qualify one tracked bundle in isolated state; never assign identity or publish."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from library.catalog import Catalog, LibraryError, atomic_yaml, now
from library.intake import receive, reception_path
from library.origins import resolve_asset
from library.readers import fingerprint, inspect_file, sha256
from library.transform import transform_archive


def qualify(state_path: Path, reception: Path, oid: str, output: Path, *, transform: bool = False, transform_limit: int = 3) -> dict[str, Any]:
    if not 0 <= transform_limit <= 64:
        raise LibraryError('Transformation count must be between zero and 64')
    if any(path.is_symlink() for path in (output.absolute(), *output.absolute().parents)):
        raise LibraryError('Symlink proof output')
    if (output.resolve().is_relative_to(reception.resolve()) or reception.resolve().is_relative_to(output.resolve())
            or output.resolve().is_relative_to(state_path.resolve()) or state_path.resolve().is_relative_to(output.resolve())):
        raise LibraryError('Proof output overlaps authoritative inputs')
    original = Catalog(state_path).read()['origins'][oid]
    path = Path(original['path'])
    parent = reception_path(reception, str(path.parent), path.name)
    before, stamp = sha256(parent), list(fingerprint(parent))
    if original.get('sha256') and original['sha256'] != before:
        raise LibraryError('Tracked bundle source hash changed')
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    store = Catalog(output / 'state')
    source = original['source']
    captured = {'items': {source: {'title': 'Unconfirmed bundle; member filenames are discovery hints'}}}
    journal = {'submissions': {source: {'status': 'queued', 'package_id': original['pid'], 'name': 'bundle-proof'}}}
    queue = [{'pid': original['pid'], 'name': 'bundle-proof', 'folder': str(path.parent), 'links': [
        {'fid': original['fid'], 'name': path.name, 'status': 0, 'size': parent.stat().st_size}]}]
    state = receive(store, reception, captured, journal, queue, stable_seconds=.01,
                    incremental=True)
    children = {key: origin for key, origin in state['origins'].items() if origin.get('parent_origin')}
    parent_record = state['origins'].get(oid, {})
    if parent_record.get('availability') != 'bundle':
        raise LibraryError('Bundle extraction blocked: ' + parent_record.get('reason', 'no parent'))
    inventory = {key: value['sha256'] for key, value in state['origins'].items()}
    rerun = receive(store, reception, captured, journal, queue, stable_seconds=.01,
                    incremental=True)
    if inventory != {key: value['sha256'] for key, value in rerun['origins'].items()}:
        raise LibraryError('Bundle rerun duplicated or changed origins')
    proof_path = output / 'proof.yaml'
    import yaml
    proof = yaml.safe_load(proof_path.read_text()) if proof_path.exists() else {
        'schema': 1, 'purpose': 'Technical qualification only; no identity admission or publication',
        'parent_sha256': before, 'parent_fingerprint': stamp, 'members': {}}
    if proof['parent_sha256'] != before or proof['parent_fingerprint'] != stamp:
        raise LibraryError('Qualification parent changed')
    proof.update(updated_at=now(), rerun_idempotent=True)
    for cid, child in children.items():
        proof['members'].setdefault(cid, {}).update(member=child['member'], sha256=child['sha256'],
            validation=child['availability'], reason=child.get('reason'), identity='pending')
    atomic_yaml(proof_path, proof)
    converted = sum('transformation' in item for item in proof['members'].values())
    for cid, child in children.items():
        record = proof['members'].setdefault(cid, {})
        record.update(member=child['member'], sha256=child['sha256'], validation=child['availability'],
                      reason=child.get('reason'), identity='pending')
        if child['availability'] == 'validated' and transform and (record.get('transformation') or converted < transform_limit):
            derived = resolve_asset(store, reception, rerun, child['sha256'])
            prior = record.get('transformation')
            if prior:
                artifact = output / record['artifact']
                if sha256(artifact) != prior['artifact_sha256']:
                    raise LibraryError('Retained qualification artifact changed')
            else:
                destination = output / 'transformations' / uuid.uuid4().hex
                destination.mkdir(parents=True, mode=0o700)
                result = transform_archive(derived, destination,
                    b'<ComicInfo><Title>Isolated bundle qualification; identity unknown</Title></ComicInfo>', child['sha256'])
                readback = inspect_file(destination / 'book.cbz', stable_seconds=.01)
                if readback['status'] != 'validated':
                    raise LibraryError('Generated child CBZ readback failed')
                converted += 1
                record.update(transformation=result, artifact=str((destination / 'book.cbz').relative_to(output)),
                              readback={'status': readback['status'], 'pages': readback['pages'], 'sha256': readback['sha256']})
        proof['source_unchanged'] = sha256(parent) == before and list(fingerprint(parent)) == stamp
        atomic_yaml(proof_path, proof)
        if not proof['source_unchanged']:
            raise LibraryError('Bundle original changed')
    return proof


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--reception', type=Path, required=True)
    parser.add_argument('--origin', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--transform-limit', type=int, default=3, help='Maximum technical conversions; use 64 to cover every valid member')
    parser.add_argument('--transform', action='store_true', help='Technical CBZ conversion/readback only; no identity admission')
    args = parser.parse_args()
    try:
        proof = qualify(args.state, args.reception, args.origin, args.output, transform=args.transform, transform_limit=args.transform_limit)
        valid = sum(item['validation'] == 'validated' for item in proof['members'].values())
        converted = sum('readback' in item for item in proof['members'].values())
        good = valid == len(proof['members']) and (not args.transform or converted >= min(valid, args.transform_limit))
        print(json.dumps({'status': 'qualified' if good else 'pending', 'members': len(proof['members']),
                          'converted_readbacks': converted, 'source_unchanged': proof['source_unchanged'], 'rerun_idempotent': proof['rerun_idempotent']}))
        return 0 if good else 2
    except (LibraryError, OSError, ValueError, KeyError) as exc:
        print(json.dumps({'status': 'blocked', 'reason': str(exc)}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
