"""Read-only runtime gates. Never configure pyLoad or create mounts."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from library.catalog import LibraryError, digest, now


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
