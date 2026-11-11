"""Exact mount identity admission. No mounting, export setup or local fallback."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import threading
import uuid
from typing import Any

from library.primitives import LibraryError


@dataclass(frozen=True)
class MountIdentity:
    target: Path
    source: str
    root: str
    readonly: bool


def unescape(value: str) -> str:
    return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), value)


def verify_mounts(expected: list[MountIdentity], mountinfo: str) -> None:
    rows = [row.split() for row in mountinfo.splitlines()]
    for item in expected:
        matches = [row for row in rows if len(row) > 6 and unescape(row[4]) == str(item.target)]
        if len(matches) != 1:
            raise LibraryError('Expected exact NAS mount is absent or shadowed')
        row = matches[0]
        separator = row.index('-')
        if (row[separator + 1] != 'nfs' or unescape(row[separator + 2]) != item.source
                or unescape(row[3]) != item.root
                or ('ro' in row[5].split(',')) != item.readonly
                or 'hard' not in row[separator + 3].split(',')):
            raise LibraryError('NAS mount identity or access differs from qualified configuration')


def check_contract(path: Path) -> dict[str, Any]:
    """Run in a supervised child: sentinel reads can block on a hard mount.

    The caller must retain the exact child on timeout and prove its termination
    before another probe or effects. A cached sentinel proves identity only.
    Fresh write/fsync health belongs to the separately owned health probe.
    """
    value = json.loads(path.read_bytes())
    if set(value) != {'schema', 'mounts', 'sentinels', 'probe_directory'} or value['schema'] != 1:
        raise LibraryError('Invalid NAS contract')
    expected = [MountIdentity(Path(row['target']), row['source'], row['root'], row['readonly'])
                for row in value['mounts']]
    verify_mounts(expected, Path('/proc/self/mountinfo').read_text())
    for row in value['sentinels']:
        target = Path(row['target'])
        if target.is_symlink() or target.stat().st_size > 4096:
            raise LibraryError('Invalid NAS sentinel')
        if hashlib.sha256(target.read_bytes()).hexdigest() != row['sha256']:
            raise LibraryError('NAS sentinel identity changed')
    probe = Path(value['probe_directory']) / ('health-' + uuid.uuid4().hex)
    with probe.open('xb') as stream:
        stream.write(os.urandom(32))
        stream.flush()
        os.fsync(stream.fileno())
    probe.unlink()
    from library.primitives import sync_dir
    sync_dir(probe.parent)
    return {'schema': 1, 'identity': 'qualified', 'health': 'fresh_write_fsync'}


def configured_contract() -> Path | None:
    path = os.environ.get('BOOKSYS_NAS_CONTRACT')
    return Path(path) if path else None


class NASMonitor:
    """One supervised probe at a time; timeout never manufactures stop proof."""
    def __init__(self, contract: Path, *, state: Path | None = None):
        self.contract = contract
        state = state or Path(os.environ.get('BOOKSYS_ADMISSION_STATE', contract.parent))
        role = os.environ.get('BOOKSYS_NAS_PROBE_ROLE', 'process')
        if not re.fullmatch('[a-z][a-z0-9-]{0,63}', role):
            raise LibraryError('Invalid configured NAS probe role')
        self.lock = state/('nas-probe-'+role+'.lock')
        self.process: subprocess.Popen[bytes] | None = None
        self.started = self.verified = 0.0
        self.timed_out = False
        self.guard = threading.Lock()

    def ready(self) -> bool:
        with self.guard:
            return self._ready()

    def _ready(self) -> bool:
        stamp = time.monotonic()
        if self.process is not None:
            code = self.process.poll()
            if code is None:
                if stamp - self.started > 5:
                    self.timed_out = True
                    self.process.kill()
                    self.verified = 0
                return not self.timed_out and stamp - self.verified < 5
            if code == 0 and not self.timed_out:
                self.verified = stamp
            else:
                self.verified = 0
            self.process = None
        if stamp - self.started >= 2:
            self.started, self.timed_out = stamp, False
            self.process = subprocess.Popen([sys.executable, '-m', 'library.nas_storage', str(self.contract),
                '--owner-lock', str(self.lock)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return bool(self.verified and stamp - self.verified < 5)

    def require(self) -> None:
        if not self.ready():
            raise LibraryError('NAS identity or fresh write health unavailable; dependent effects suspended')

    def close(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.kill()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired as exc:
                raise LibraryError('NAS probe termination uncertain; do not replace this owner') from exc


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('contract', type=Path)
    parser.add_argument('--owner-lock', type=Path, required=True)
    args = parser.parse_args()
    # The probe itself owns this LOCAL lock. An unkillable old NFS syscall
    # retains it even after its supervising actor dies. A new actor fails
    # closed rather than creating another blocked probe on every restart.
    with args.owner_lock.open('a') as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print(json.dumps(check_contract(args.contract)))
