"""Exact host supervisor qualification for the direct-domain-claim candidate."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ops.qualification.domain_queue import STAGES, initialize, reconcile


def command(*args: str) -> str:
    environment = os.environ.copy()
    environment.setdefault('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')
    environment.setdefault('DBUS_SESSION_BUS_ADDRESS', f'unix:path=/run/user/{os.getuid()}/bus')
    result = subprocess.run(args, capture_output=True, text=True, env=environment, timeout=15)
    if result.returncode:
        raise RuntimeError(result.stderr[:2000])
    return result.stdout


def wait_for(path: Path) -> None:
    deadline = time.monotonic() + 20
    while not path.exists():
        if time.monotonic() > deadline:
            raise TimeoutError(str(path))
        time.sleep(.02)


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix='booksys-systemd-claims-'))
    initialize(root)
    prefix = 'booksys-queue-proof-' + uuid.uuid4().hex[:10]
    units: dict[str, str] = {}
    def start(stage: str, suffix: str = '') -> None:
        unit = prefix + '-' + stage + suffix + '.service'
        command('systemd-run', '--user', '--quiet', '--unit', unit,
                '--property=Type=exec', '--property=MemoryMax=128M',
                '--property=MemorySwapMax=0', '--property=CPUQuota=25%',
                '--property=KillMode=control-group', '--property=RuntimeMaxSec=20',
                '--property=TimeoutStopSec=5', '--property=RemainAfterExit=yes',
                '--working-directory', str(ROOT), sys.executable,
                str(ROOT / 'ops/qualification/supervised_claims.py'), '--actor', str(root), stage)
        units[stage + suffix] = unit
    try:
        for stage in STAGES:
            start(stage)
        for stage in STAGES:
            wait_for(root / (stage + '-old.started'))
        snapshots = {}
        for stage, unit in units.items():
            raw = command('systemctl', '--user', 'show', unit, '--property=MainPID',
                          '--property=ControlGroup', '--property=MemoryPeak', '--property=CPUUsageNSec')
            snapshots[stage] = dict(line.split('=', 1) for line in raw.splitlines())
        paused = int((root / 'ocr-old.started').read_text())
        os.kill(paused, signal.SIGSTOP)
        try:
            reconcile(root, 'ocr')
        except BlockingIOError:
            paused_excluded = True
        else:
            raise AssertionError('Paused actor lost its exclusion')
        finally:
            os.kill(paused, signal.SIGCONT)
        command('systemctl', '--user', 'kill', '--kill-whom=all', '--signal=SIGKILL', units['copies'])
        deadline = time.monotonic() + 5
        while command('systemctl', '--user', 'show', units['copies'], '--property=MainPID', '--value').strip() != '0':
            if time.monotonic() > deadline:
                raise TimeoutError('Stopped actor still present')
            time.sleep(.02)
        started = time.monotonic()
        assert reconcile(root, 'copies') == 1
        start('copies', '-recovered')
        for stage in STAGES:
            wait_for(root / (stage + '-new.interval'))
        # A child can escape a POSIX process group; it cannot escape this
        # non-delegated cgroup. Exercise the supervisor's real timeout path.
        timeout_unit = prefix + '-timeout.service'
        units['timeout'] = timeout_unit
        timeout_code = (
            'import pathlib,subprocess,sys,time; '
            'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"],start_new_session=True); '
            f'pathlib.Path({str(root / "timeout-child")!r}).write_text(str(p.pid)); '
            'time.sleep(60)')
        command('systemd-run', '--user', '--quiet', '--unit', timeout_unit,
                '--property=Type=exec', '--property=KillMode=control-group',
                '--property=RuntimeMaxSec=2', '--property=TimeoutStopSec=1',
                sys.executable, '-c', timeout_code)
        wait_for(root / 'timeout-child')
        child = int((root / 'timeout-child').read_text())
        deadline = time.monotonic() + 10
        while command('systemctl', '--user', 'show', timeout_unit, '--property=MainPID', '--value').strip() != '0':
            if time.monotonic() > deadline:
                raise TimeoutError('Supervisor did not enforce runtime bound')
            time.sleep(.02)
        assert not Path(f'/proc/{child}').exists(), 'Escaped child survived cgroup timeout'
        timeout_result = command('systemctl', '--user', 'show', timeout_unit, '--property=Result').strip()
        assert timeout_result == 'Result=timeout'
        with sqlite3.connect(root / 'domain.sqlite') as db:
            states = db.execute('SELECT state,count(*) FROM tasks GROUP BY state').fetchall()
        assert states == [('done', 14), ('ready', 7)]
        assert len(list(root.glob('*.effect'))) == 14
        report = {'evidence': str(root), 'supervisor': command('systemctl', '--version').splitlines()[0],
                  'units': units, 'active_snapshots': snapshots,
                  'paused_actor_excluded': paused_excluded, 'states': states, 'effect_receipts': 14,
                  'escaped_child_timeout': timeout_result,
                  'recovery_and_remaining_work_seconds': time.monotonic() - started,
                  'scope': 'actual isolated user units, same synthetic domain fixture; no existing service changed'}
        (root / 'report.json').write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2))
    finally:
        errors = []
        stopped = {}
        for unit in units.values():
            try:
                (root / (unit + '.journal')).write_text(command('journalctl', '--user', '-u', unit, '--no-pager'))
            except Exception as error:
                errors.append(f'{unit}: journal: {error}')
            try:
                command('systemctl', '--user', 'stop', unit)
                status = command('systemctl', '--user', 'show', unit,
                    '--property=ActiveState', '--property=MainPID', '--property=ControlGroup')
                stopped[unit] = status.splitlines()
                values = dict(line.split('=', 1) for line in status.splitlines())
                assert values['MainPID'] == '0'
                if group := values['ControlGroup']:
                    events = Path('/sys/fs/cgroup' + group) / 'cgroup.events'
                    assert not events.exists() or 'populated 0' in events.read_text()
            except Exception as error:
                errors.append(f'{unit}: stop/readback: {error}')
        (root / 'stopped.json').write_text(json.dumps(stopped, indent=2))
        (root / 'cleanup-errors.json').write_text(json.dumps(errors))
        if errors:
            raise RuntimeError('Qualification cleanup incomplete; see ' + str(root))


if __name__ == '__main__':
    main()
