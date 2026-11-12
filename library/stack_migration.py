"""Verified copy primitives for a frozen, explicitly selected migration plan.

These functions do not stop writers or select a runtime. The operation caller
must prove the exact frozen owners. No replay, deletion or snapshot restore is
provided. An interrupted destination and its intent remain for reconciliation.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
from typing import Any

from library.primitives import LibraryError, atomic_yaml, digest, sync_dir
from library.readers import fingerprint, sha256
from library.relocation import record_relocation


def checked_path(value: str) -> Path:
    path = Path(value)
    if (not path.is_absolute() or str(path) != value or '..' in path.parts
            or any(p.is_symlink() for p in (path, *path.parents))):
        raise LibraryError('Migration requires normalized absolute non-symlink paths')
    return path


def inventory(root: Path) -> dict[str, Any]:
    checked_path(str(root))
    if not root.is_dir():
        raise LibraryError('Migration source directory is absent')
    result = {}
    for path in sorted(root.rglob('*')):
        info = path.lstat()
        relative = str(path.relative_to(root))
        if stat.S_ISDIR(info.st_mode):
            result[relative] = {'kind': 'directory', 'mode': stat.S_IMODE(info.st_mode)}
        elif stat.S_ISREG(info.st_mode):
            before = fingerprint(path)
            checksum = sha256(path)
            if fingerprint(path) != before:
                raise LibraryError('Source changed during inventory')
            result[relative] = {'kind': 'file', 'size': info.st_size, 'sha256': checksum,
                                'mode': stat.S_IMODE(info.st_mode)}
        else:
            raise LibraryError('Symlink or special file in migration inventory')
    return result


def preview(copies: list[dict[str, str]]) -> dict[str, Any]:
    result: list[dict[str, Any]] = []
    roots: list[Path] = []
    logical_roots: list[Path] = []
    for item in copies:
        if set(item) != {'source', 'target', 'logical'}:
            raise LibraryError('Invalid migration copy scope')
        source, target, logical = (checked_path(item[k]) for k in ('source', 'target', 'logical'))
        if target.exists():
            raise LibraryError('Migration destination already exists; reconcile, do not replay')
        for path in (source, target):
            if any(path.is_relative_to(other) or other.is_relative_to(path) for other in roots):
                raise LibraryError('Migration roots overlap')
            roots.append(path)
        if any(logical.is_relative_to(other) or other.is_relative_to(logical) for other in logical_roots):
            raise LibraryError('Logical migration namespaces overlap')
        logical_roots.append(logical)
        result.append({**item, 'inventory': inventory(source)})
    return {'schema': 1, 'copies': result, 'retained_source_bytes': sum(
        row.get('size', 0) for item in result for row in item['inventory'].values()),
        'reclaimed_bytes': 0}


def validate_plan(plan: dict[str, Any]) -> None:
    if (set(plan) != {'schema', 'copies', 'retained_source_bytes', 'reclaimed_bytes'}
            or plan['schema'] != 1 or plan['reclaimed_bytes'] != 0 or not plan['copies']):
        raise LibraryError('Invalid migration plan')
    fresh = preview([{key: item[key] for key in ('source', 'target', 'logical')} for item in plan['copies']])
    if fresh != plan:
        raise LibraryError('Migration source drift; no copy permitted')


def copy_verified(plan: dict[str, Any], evidence: Path) -> None:
    validate_plan(plan)
    checked_path(str(evidence))
    all_roots = [Path(item[k]) for item in plan['copies'] for k in ('source', 'target')]
    if any(evidence.is_relative_to(root) or root.is_relative_to(evidence) for root in all_roots):
        raise LibraryError('Migration evidence overlaps copied data')
    evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    atomic_yaml(evidence/'intent.yaml', {'schema': 1, 'plan_sha256': digest(plan), 'state': 'preparing'}, exclusive=True)
    mappings = []
    for item in plan['copies']:
        source, target, logical = (checked_path(item[k]) for k in ('source', 'target', 'logical'))
        target.mkdir(mode=0o700)
        for name, row in item['inventory'].items():
            destination = target/name
            if row['kind'] == 'directory':
                destination.mkdir(mode=row['mode'])
            else:
                origin = source/name
                before = fingerprint(origin)
                fd = os.open(origin, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, 'rb') as incoming, destination.open('xb') as outgoing:
                    shutil.copyfileobj(incoming, outgoing, 1024**2)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
                    os.fchmod(outgoing.fileno(), row['mode'])
                if fingerprint(origin) != before or sha256(destination) != row['sha256']:
                    raise LibraryError('Copy changed or differs; retain partial target and intent')
                mappings.append((origin, destination, logical/name))
        for directory in reversed([p for p in target.rglob('*') if p.is_dir()]):
            sync_dir(directory)
        sync_dir(target)
    if any(inventory(Path(item['source'])) != item['inventory'] for item in plan['copies']):
        raise LibraryError('Source changed after copy; stop dependent actions')
    verify_current(plan)
    record_relocation(mappings, evidence/'relocation.json')
    atomic_yaml(evidence/'copied.yaml', {'schema': 1, 'plan_sha256': digest(plan),
        'state': 'verified', 'retained_source_bytes': plan['retained_source_bytes'], 'reclaimed_bytes': 0}, exclusive=True)


def verify_current(plan: dict[str, Any]) -> None:
    for item in plan['copies']:
        if inventory(checked_path(item['target'])) != item['inventory']:
            raise LibraryError('Target differs or has new effects; retain current state and reconcile')


def main() -> None:
    """Offline rehearsal CLI. Live orchestration requires its operation plan."""
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('preview', 'rehearse', 'verify'))
    parser.add_argument('--scope', type=Path, required=True)
    parser.add_argument('--rehearsal-root', type=Path)
    parser.add_argument('--nas-rehearsal-root', type=Path)
    parser.add_argument('--evidence', type=Path)
    args = parser.parse_args()
    scope = json.loads(args.scope.read_text())
    if args.action == 'preview':
        print(json.dumps(preview(scope), sort_keys=True, indent=2))
    elif args.action == 'verify':
        verify_current(scope)
        print('Verified unchanged copied target; no runtime selection')
    else:
        if not args.rehearsal_root or not args.evidence:
            parser.error('rehearse requires --rehearsal-root and --evidence')
        root = checked_path(str(args.rehearsal_root))
        if root == Path('/data') or not root.is_relative_to('/data'):
            parser.error('use a dedicated /data synthetic rehearsal directory')
        marker = root/'.booksys-synthetic-rehearsal'
        if marker.is_symlink() or not marker.is_file() or marker.read_text() != 'synthetic-only\n':
            parser.error('rehearsal root requires the explicit synthetic-only marker')
        roots = [root]
        if args.nas_rehearsal_root:
            nas = checked_path(str(args.nas_rehearsal_root))
            mount = Path('/mnt/booksys-host/booksys-files')
            if not nas.is_relative_to(mount/'work') or not nas.name.startswith('.qual-compose-'):
                parser.error('NAS rehearsal requires its owned qualification namespace')
            from library.nas_storage import MountIdentity, verify_mounts
            verify_mounts([MountIdentity(mount, '192.0.2.10:/volume2/storage/booksys', '/', False)],
                          Path('/proc/self/mountinfo').read_text())
            marker = nas/'.booksys-synthetic-rehearsal'
            if marker.is_symlink() or not marker.is_file() or marker.read_text() != 'synthetic-only\n':
                parser.error('NAS rehearsal requires the explicit synthetic-only marker')
            roots.append(nas)
        paths = [Path(item[k]) for item in scope['copies'] for k in ('source', 'target')]
        if not all(any(p.is_relative_to(base) and p != base for base in roots) for p in paths) or not args.evidence.is_relative_to(root):
            parser.error('every rehearsal path must stay inside declared synthetic roots')
        copy_verified(scope, args.evidence)
        print('Verified synthetic copy; sources and evidence retained')


if __name__ == '__main__':
    main()
