"""Read-only runtime gates. Never configure pyLoad or create mounts."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from library.primitives import LibraryError, digest, now


def readonly_mount(path: Path) -> bool:
    path = path.resolve()
    mounts = []
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        fields = line.split()
        mount = Path(fields[4].replace('\\040', ' '))
        if path == mount or path.is_relative_to(mount):
            mounts.append((len(mount.parts), 'ro' in fields[5].split(',')))
    return max(mounts, default=(0, False))[1]


def preflight(reception: Path, root: Path, state: Path, policy_path: Path) -> dict[str, Any]:
    policy = yaml.safe_load(policy_path.read_text())
    required = {'schema', 'writer_uid', 'reader_mount_readonly', 'dedicated_uid_verified',
                'no_other_writers_verified', 'backup_verified', 'recovery_verified',
                'pyload_postprocessing_audited', 'capacity_window', 'evidence', 'valid_until'}
    if not isinstance(policy, dict) or set(policy) != required or policy['schema'] != 1:
        raise LibraryError('Invalid runtime qualification policy')
    if (policy['writer_uid'] != os.getuid() or os.getuid() in (0, 1000) or
            not all(policy[k] is True for k in ('reader_mount_readonly', 'dedicated_uid_verified',
                'no_other_writers_verified', 'backup_verified', 'recovery_verified',
                'pyload_postprocessing_audited')) or not policy['evidence']):
        raise LibraryError('Runtime exclusivity, backup or recovery qualification missing')
    if not readonly_mount(reception):
        raise LibraryError('Reception is not mounted read-only')
    if policy['capacity_window'] != 'no_concurrent_writers':
        raise LibraryError('Only a verified no-concurrent-writer window is qualified for runtime V1')
    deadline = datetime.fromisoformat(str(policy['valid_until']))
    remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
    if not 0 < remaining <= 3600:
        raise LibraryError('Runtime qualification window must expire within one hour')
    for path in (root, state):
        if not path.is_dir() or path.is_symlink() or path.stat().st_uid != os.getuid():
            raise LibraryError('Library/state must exist and belong to the dedicated writer UID')
        if path.stat().st_mode & 0o077:
            raise LibraryError('Library/state must deny access to other host UIDs')
    if root.resolve().is_relative_to(reception.resolve()) or state.resolve().is_relative_to(reception.resolve()):
        raise LibraryError('Reception must be separate from writable library and state')
    return {'status': 'qualified', 'time': now(), 'policy_hash': digest(policy),
            'writer_uid': os.getuid(), 'reception_readonly': True,
            'capacity_window': 'no_concurrent_writers', 'evidence': policy['evidence']}


def lab_preflight(reception: Path, root: Path, state: Path,
                  inputs: tuple[Path, ...] = ()) -> dict[str, Any]:
    """Lab writes use dedicated data paths; source inputs remain read-only."""
    paths = (reception, root, state, *inputs)
    for path in paths:
        if any(p.is_symlink() for p in (path.absolute(), *path.absolute().parents)):
            raise LibraryError('Lab paths must not contain symlinks')
    if not reception.is_dir():
        raise LibraryError('Lab reception must exist')
    outputs = (root.resolve(), state.resolve())
    for path in outputs:
        if not path.is_relative_to('/data') or path == Path('/data'):
            raise LibraryError('Lab outputs require dedicated paths under /data')
    for output, other in ((outputs[0], outputs[1]), (outputs[1], outputs[0])):
        if output.is_relative_to(other):
            raise LibraryError('Lab library and state must be disjoint')
        for source in (reception.resolve(), Path('/data/booksys/state'),
                       Path('/data/booksys/secrets'), Path('/data/booksys/downloads'),
                       *(p.resolve() for p in inputs)):
            if output.is_relative_to(source) or source.is_relative_to(output):
                raise LibraryError('Lab outputs must be separate from reception and snapshot inputs')
    return {'status': 'lab', 'writer_uid': os.getuid(),
            'capacity_window': 'available_space_checks_only',
            'concurrent_writer_reserve_guaranteed': False}


def configured_scripts() -> Path:
    configured = os.environ.get('BOOKSYS_PYLOAD_SCRIPTS_ROOT')
    if configured:
        target = Path(configured)
        if not target.is_absolute() or any(p.is_symlink() for p in (target, *target.parents)) or not target.is_dir():
            raise LibraryError('Invalid configured pyLoad scripts root')
        return target
    compose = Path(__file__).resolve().parents[1] / 'compose.shared.yaml'
    volumes = yaml.safe_load(compose.read_text())['services']['pyload']['volumes']
    roots = [v.split(':')[0] for v in volumes if isinstance(v, str) and v.split(':')[1] == '/config']
    if len(roots) != 1 or not Path(roots[0]).is_absolute():
        raise LibraryError('Cannot bind pyLoad scripts to the shared Compose configuration')
    return Path(roots[0]) / 'scripts'
