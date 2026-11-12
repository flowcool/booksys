"""systemd owns process death. These helpers neither install nor start units."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import signal
import socket
import threading
from typing import Any, Iterator

from library.primitives import LibraryError
from library.stage_model import QUEUES


@contextmanager
def deadline(seconds: float) -> Iterator[None]:
    def expired(*_: Any) -> None:
        raise LibraryError('Stage enclosing deadline exceeded')
    previous = signal.signal(signal.SIGALRM, expired)
    prior_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    # If Python cannot unwind, ask the existing supervisor to kill the cgroup.
    def force_stop() -> None:
        if os.environ.get('BOOKSYS_SUPERVISOR') == 'compose':
            os.kill(os.getpid(), signal.SIGKILL)
        else:
            notify('WATCHDOG=trigger')
    hard_stop = threading.Timer(seconds + 5, force_stop)
    hard_stop.daemon = True
    hard_stop.start()
    try:
        yield
    finally:
        hard_stop.cancel()
        signal.setitimer(signal.ITIMER_REAL, *prior_timer)
        signal.signal(signal.SIGALRM, previous)


def notify(message: str) -> None:
    address = os.environ.get('NOTIFY_SOCKET')
    if address:
        if address.startswith('@'):
            address = '\0' + address[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
            channel.connect(address)
            channel.sendall(message.encode())


def units(*, code: Path, python: Path, root: Path, collector: Path, reception: Path,
          credentials: Path) -> dict[str, str]:
    paths = (code, python, root, collector, reception, credentials)
    if any(not path.is_absolute() or any(c.isspace() or c in '%$"\\' for c in str(path)) for path in paths):
        raise LibraryError('Unit paths must be absolute and require no expansion')
    result = {'booksys.slice': '[Unit]\nDescription=Booksys shared business envelope\n[Slice]\nCPUQuota=200%\nMemoryMax=3584M\nMemorySwapMax=0\nTasksMax=384\n'}
    for queue in (*QUEUES, 'discovery', 'serial'):
        memory = 512 if queue in ('collection', 'routing', 'retirement', 'discovery', 'serial') else 256
        command = (f'{python} -m library.stage_worker --root {root} --collector {collector} '
                   f'--reception {reception} --credentials {credentials} --queue {queue}')
        result['booksys-stage-' + queue + '.service'] = (
            '[Unit]\nDescription=Booksys ' + queue + '\nStartLimitIntervalSec=900\nStartLimitBurst=3\n'
            '[Service]\nType=notify\nNotifyAccess=main\n'
            f'User={os.getuid()}\nGroup={os.getgid()}\nUMask=0077\n'
            'WorkingDirectory=' + str(code) + '\nExecStart=' + command + '\n'
            f'Slice=booksys.slice\nCPUQuota=25%\nMemoryMax={memory}M\nMemorySwapMax=0\nTasksMax=48\n'
            'KillMode=control-group\nDelegate=no\nTimeoutStopSec=5\nWatchdogSec=15\nRestart=on-failure\nRestartSec=5\n'
            'Environment=BOOKSYS_ADMISSION_STATE=' + str(root/'state') + '\n'
            'Environment=TMPDIR=/data/tmp\nEnvironment=XDG_CACHE_HOME=/data/cache/flow\n'
            '[Install]\nWantedBy=multi-user.target\n')
    return result


def io_bytes(pool: Path, device: str) -> int:
    if device == 'qualified-nfs':
        return nfs_io_bytes()
    try:
        for line in (pool/'io.stat').read_text().splitlines():
            fields = line.split()
            if fields[0] == device:
                values = dict(field.split('=') for field in fields[1:])
                return int(values['rbytes']) + int(values['wbytes'])
        # A qualified device must have observable counters before dispatch.
        raise ValueError('device missing')
    except (OSError, ValueError, KeyError, IndexError) as exc:
        raise LibraryError('Shared I/O accounting unavailable') from exc


@contextmanager
def io_ceiling(pool: Path, device: str, allowance: int) -> Iterator[None]:
    """Conservative shared-pool counter, including children and concurrent work.

    Operation limits bound individual writes. Polling detects a crossed ceiling;
    it does not promise byte-exact kernel throttling. The enclosing deadline and
    supervisor terminate the actor. Grants remain until exact decoder stop proof.
    """
    import threading
    start = io_bytes(pool, device)
    stopped = threading.Event()
    exceeded = threading.Event()
    def check() -> None:
        value = io_bytes(pool, device)
        if value < start or value - start > allowance:
            raise LibraryError('Stage I/O ceiling exceeded')
    def monitor() -> None:
        while not stopped.wait(.1):
            try:
                check()
            except LibraryError:
                exceeded.set()
                os.kill(os.getpid(), signal.SIGALRM)
                return
    watcher = threading.Thread(target=monitor, daemon=True)
    watcher.start()
    try:
        yield
        check()
        if exceeded.is_set():
            raise LibraryError('Stage I/O ceiling exceeded')
    finally:
        stopped.set()
        watcher.join(1)


def nfs_counters(mountinfo: str, mountstats: str, sources: set[str]) -> int:
    """Read application bytes once per NFS superblock, including cached reads.

    Linux nfs_show_stats uses NFS_SB(root->d_sb)->io_stats. Bind views and
    exports sharing a superblock repeat those counters; device IDs de-duplicate
    them. Distinct superblocks still add to the shared conservative ceiling.
    """
    devices: dict[str, set[str]] = {source: set() for source in sources}
    for line in mountinfo.splitlines():
        fields = line.split()
        separator = fields.index('-')
        if fields[separator+1] == 'nfs' and fields[separator+2] in sources:
            devices[fields[separator+2]].add(fields[2])
    if any(len(values) != 1 for values in devices.values()):
        raise LibraryError('Qualified NFS superblock identity unavailable')
    totals: dict[str, int] = {}
    observed: set[str] = set()
    selected: str | None = None
    for line in mountstats.splitlines():
        if line.startswith('device '):
            fields = line.split()
            selected = fields[1] if fields[1] in sources and 'nfs' in fields else None
        elif selected and line.strip().startswith('bytes:'):
            counters = list(map(int, line.split()[1:]))
            if len(counters) < 6 or any(value < 0 for value in counters):
                raise LibraryError('NFS I/O counters are incomplete')
            device = next(iter(devices[selected]))
            totals[device] = max(totals.get(device, 0), sum(counters[:4]))
            observed.add(selected)
    if observed != sources:
        raise LibraryError('Qualified shared NFS I/O accounting unavailable')
    return sum(totals.values())


def nfs_io_bytes() -> int:
    return nfs_counters(Path('/proc/self/mountinfo').read_text(),
        Path('/proc/self/mountstats').read_text(),
        {os.environ['BOOKSYS_NFS_FILES_SOURCE'], os.environ['BOOKSYS_NFS_LIBRARY_SOURCE']})
