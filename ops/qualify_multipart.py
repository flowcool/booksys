"""Qualify a snapshot-owned RAR set on isolated copies; no identity or publication."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import shutil
import sys
import uuid
from typing import Any
import yaml
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from library.catalog import Catalog, LibraryError, atomic_yaml, digest
from library.intake import receive, reception_path
from library.multipart import select_parts
from library.origins import resolve_asset
from library.readers import fingerprint, sha256, inspect_file
from library.transform import transform_archive


def qualify(snapshots: Path, reception: Path, pid: int, output: Path, limit: int = 3) -> dict[str, Any]:
    if not 0 <= limit <= 64 or (output.exists() and not (output / 'originals.yaml').is_file()):
        raise LibraryError('Use an isolated output with verified originals and a conversion limit from zero to 64')
    for protected in (snapshots, reception):
        if output.resolve().is_relative_to(protected.resolve()) or protected.resolve().is_relative_to(output.resolve()):
            raise LibraryError('Qualification output overlaps authoritative inputs')
    if any(p.is_symlink() for p in (output.absolute(), *output.absolute().parents)):
        raise LibraryError('Symlink qualification output')
    queue = yaml.safe_load((snapshots / 'queue.yaml').read_text())
    journal = yaml.safe_load((snapshots / 'submissions.yaml').read_text())
    captured = yaml.safe_load((snapshots / 'captured.yaml').read_text())
    package = next(p for p in queue if p['pid'] == pid)
    source = next(s for s,entry in journal['submissions'].items() if entry.get('package_id') == pid)
    members = select_parts(package['links'], package['links'][0])
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    copied = output / 'reception'
    proofs: list[dict[str, Any]] = []
    for member in members:
        original = reception_path(reception, member.get('input_folder',package['folder']), member['name'])
        before = fingerprint(original)
        key = sha256(original)
        target = copied / original.relative_to(reception)
        target.parent.mkdir(parents=True,exist_ok=True)
        if not target.exists():
            shutil.copyfile(original,target)
        if fingerprint(original) != before or sha256(target) != key or sha256(original) != key:
            raise LibraryError('Source changed during isolated copy')
        proofs.append({'path':str(original.relative_to(reception)), 'sha256':key, 'fingerprint':list(before)})
    originals = output / 'originals.yaml'
    if originals.exists() and yaml.safe_load(originals.read_text()) != {'parts':proofs}:
        raise LibraryError('Qualification original provenance changed')
    atomic_yaml(originals, {'parts':proofs})
    store = Catalog(output / 'state')
    retry_state = store.read()
    retryable = [origin for origin in retry_state['origins'].values() if origin.get('parent_origin') and
                 'isolated reader failed' in origin.get('reason','').lower() and
                 int(origin.get('intake_attempts',1)) < 3]
    if retryable:
        for origin in retryable:
            origin['intake_next_attempt'] = 0
        store.save(retry_state, expected=retry_state['revision'])
    for _ in range(8):
        state = receive(store,copied,captured,{'submissions':{source:journal['submissions'][source]}},
                        [package],stable_seconds=.01,incremental=True,inspection_limit=8)
        children = {oid:o for oid,o in state['origins'].items() if o.get('parent_origin')}
        if not any(o.get('reason') == 'Intake budget deferred' for o in children.values()): break
    parent = state['origins'][digest({'source':source,'pid':pid,'fid':members[0]['fid']})]
    if parent.get('availability') != 'bundle':
        raise LibraryError('Multipart qualification blocked: '+parent.get('reason','unknown'))
    inventory = {oid:o['sha256'] for oid,o in state['origins'].items()}
    rerun = receive(store,copied,captured,{'submissions':{source:journal['submissions'][source]}},
                    [package],stable_seconds=.01,incremental=True,inspection_limit=8)
    if inventory != {oid:o['sha256'] for oid,o in rerun['origins'].items()}:
        raise LibraryError('Multipart retry changed or duplicated origins')
    previous_proof = yaml.safe_load((output / 'proof.yaml').read_text()) if (output / 'proof.yaml').exists() else {}
    previous_members = {item['member']:item for item in previous_proof.get('members',[])}
    proof: dict[str, Any] = {'purpose':'Isolated technical proof; identity unknown; no publication',
        'pid':pid,'source_parts':proofs,'source_set_sha256':parent['source_set_sha256'],
        'rerun_idempotent':True,'members':[]}
    for oid,child in children.items():
        row = {'member':child['member'],'sha256':child['sha256'],'validation':child['availability']}
        prior = previous_members.get(child['member'], {})
        if prior.get('artifact'):
            artifact_path = Path(prior['artifact_path'])
            if (not artifact_path.resolve().is_relative_to(output.resolve()) or
                    sha256(artifact_path) != prior['artifact']['artifact_sha256']):
                raise LibraryError('Previous technical artifact changed')
            row.update({key:prior[key] for key in ('artifact','readback','artifact_path')})
            limit = max(0,limit-1)
        elif child['availability'] == 'validated' and limit:
            destination = output / 'converted' / (oid + '-' + uuid.uuid4().hex)
            destination.mkdir(parents=True)
            artifact = transform_archive(resolve_asset(store,copied,rerun,child['sha256']),destination,
                b'<ComicInfo><Title>Technical qualification; identity unknown</Title></ComicInfo>',child['sha256'])
            readback = inspect_file(destination / 'book.cbz',stable_seconds=.01)
            if readback['status'] != 'validated': raise LibraryError('CBZ readback failed')
            row.update(artifact=artifact,readback=readback,artifact_path=str(destination / 'book.cbz'))
            limit -= 1
        proof['members'].append(row)
        atomic_yaml(output / 'proof.yaml',proof)
    proof['originals_unchanged'] = all(list(fingerprint(reception / p['path'])) == p['fingerprint'] and
                                           sha256(reception / p['path']) == p['sha256'] for p in proofs)
    atomic_yaml(output / 'proof.yaml',proof)
    if not proof['originals_unchanged']: raise LibraryError('Original part changed')
    return proof

def verify_ends(output: Path) -> dict[str, Any]:
    """Recheck exact copied inputs with the final worker, without reconverting."""
    from library.bundles import retain
    import os
    originals = yaml.safe_load((output / 'originals.yaml').read_text())['parts']
    paths = [reception_path(output / 'reception', str(Path(item['path']).parent),
                            Path(item['path']).name) for item in originals]
    if any(sha256(path) != item['sha256'] for path,item in zip(paths,originals)):
        raise LibraryError('Qualification copied source changed')
    directory, manifest = retain(paths[0], output / 'final-end-state', parts=paths, stable_seconds=.01)
    proof = yaml.safe_load((output / 'proof.yaml').read_text())
    expected = {item['member']:item['sha256'] for item in proof['members']}
    actual = {item['member']:item['sha256'] for item in manifest['members'] if item['kind'] == 'comic'}
    if actual != expected or manifest.get('volume_end_verified') is not True:
        raise LibraryError('Final engine changed the retained child set or lacks end proof')
    result = {'image':os.environ.get('BOOKSYS_READER_IMAGE'), 'volume_end_verified':True,
              'same_child_hashes':True,'members':len(actual),'manifest':str(directory / 'manifest.yaml'),
              'manifest_sha256':sha256(directory / 'manifest.yaml')}
    atomic_yaml(output / 'final-end-proof.yaml',result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshots',type=Path,required=True)
    parser.add_argument('--reception',type=Path,required=True)
    parser.add_argument('--pid',type=int,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--transform-limit',type=int,default=3)
    parser.add_argument('--verify-ends',action='store_true',help='Recheck copied set and child hashes with the final worker')
    args = parser.parse_args()
    if args.verify_ends:
        print(json.dumps(verify_ends(args.output)))
        raise SystemExit(0)
    result = qualify(args.snapshots,args.reception,args.pid,args.output,args.transform_limit)
    print(json.dumps({'members':len(result['members']),'converted':sum('artifact' in m for m in result['members']),
                      'originals_unchanged':result['originals_unchanged'],'rerun_idempotent':result['rerun_idempotent']}))
