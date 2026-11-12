"""Supervised direct-domain-claim baseline for T1; synthetic state only.

Run with the locked project Python3.14 environment. No generic registry,
transport, lease engine or business adapter is installed by this experiment.
"""
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

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ops.qualification.domain_queue import STAGES, execute, initialize, reconcile, select


def actor(root: Path, stage: str) -> None:
    while item := select(root, stage):
        execute(str(root), stage, *item)


def launch(root: Path, stage: str) -> subprocess.Popen[bytes]:
    with (root / (stage + '.log')).open('ab') as log:
        return subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                                 '--actor', str(root), stage], stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)


def wait_for_file(path: Path) -> None:
    deadline = time.monotonic() + 15
    while not path.exists():
        if time.monotonic() > deadline:
            raise TimeoutError(str(path))
        time.sleep(.01)


def main() -> None:
    if len(sys.argv) == 4 and sys.argv[1] == '--actor':
        actor(Path(sys.argv[2]), sys.argv[3])
        return
    root = Path(tempfile.mkdtemp(prefix='booksys-supervised-claims-'))
    initialize(root)
    processes: list[subprocess.Popen[bytes]] = []
    try:
        for stage in STAGES:
            processes.append(launch(root, stage))
        for stage in STAGES:
            wait_for_file(root / (stage + '-old.started'))
        rss = sum(int(line.split()[1]) * 1024 for process in processes for line in
                  Path(f'/proc/{process.pid}/status').read_text().splitlines() if line.startswith('VmRSS:'))
        # Stop one actual actor while its oldest task owns execution. The other
        # six queues keep running. There is no escaped horse in this model.
        lost = processes[STAGES.index('copies')]
        os.kill(lost.pid, signal.SIGKILL)
        lost.wait(timeout=5)
        started = time.monotonic()
        assert reconcile(root, 'copies') == 1
        replacement = launch(root, 'copies')
        processes.append(replacement)
        recovery = time.monotonic() - started
        for process in processes:
            if process is not lost:
                assert process.wait(timeout=20) == 0
        intervals = {stage: [json.loads((root / (stage + label + '.interval')).read_text())
                              for label in ('-old', '-new')] for stage in STAGES}
        assert all(items[0]['end'] <= items[1]['start'] for items in intervals.values())
        unaffected = [items[0] for stage, items in intervals.items() if stage != 'copies']
        overlap = min(v['end'] for v in unaffected) - max(v['start'] for v in unaffected)
        assert overlap > 0
        with sqlite3.connect(root / 'domain.sqlite') as db:
            states = db.execute('SELECT state,count(*) FROM tasks GROUP BY state').fetchall()
        assert states == [('done', 14), ('ready', 7)]
        assert len(list(root.glob('*.effect'))) == 14
        report = {'scope': 'seven supervised domain actors with real module imports; synthetic obligations/effects',
                  'evidence': str(root), 'actors': 7, 'rss_bytes_sum': rss,
                  'unaffected_overlap_seconds': overlap, 'recovery_after_proven_exit_seconds': recovery,
                  'states': states, 'effect_receipts': 14, 'intervals': intervals,
                  'limit': 'Python parent supervises this baseline; final service supervisor and real effect paths not activated'}
        (root / 'report.json').write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


if __name__ == '__main__':
    main()
