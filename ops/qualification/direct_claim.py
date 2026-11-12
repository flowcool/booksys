"""Disposable no-broker cost baseline. This is not a Booksys scheduler.

One synthetic SQLite claim demonstrates the missing crash-recovery obligation.
Use Python3.14. Never point this experiment at a catalogue or external database.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time


def claim(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("UPDATE tasks SET state='active' WHERE id=(SELECT id FROM tasks "
                         "WHERE state='ready' ORDER BY age,id LIMIT 1) RETURNING id").fetchone()
        if row is None:
            raise RuntimeError('Missing synthetic task')
    # There is no execution registry or death detector in a direct SQLite claim.
    path.with_suffix('.claimed').write_text(str(os.getpid()))
    time.sleep(1)
    os._exit(19)


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == '--actor':
        claim(Path(sys.argv[2]))
        return
    directory = Path(tempfile.mkdtemp(prefix='booksys-direct-claim-'))
    path = directory / 'synthetic.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE tasks(id TEXT PRIMARY KEY,age INTEGER,state TEXT)')
        db.executemany('INSERT INTO tasks VALUES (?,?,?)', [('older', 1, 'ready'), ('newer', 2, 'ready')])
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--actor', str(path)])
    try:
        deadline = time.monotonic() + 5
        while not path.with_suffix('.claimed').exists():
            if process.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError('Synthetic actor failed before claim')
            time.sleep(.01)
        rss = next(int(line.split()[1]) * 1024 for line in
                   Path(f'/proc/{process.pid}/status').read_text().splitlines() if line.startswith('VmRSS:'))
        if process.wait(timeout=5) != 19:
            raise RuntimeError('Expected simulated interruption')
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    with sqlite3.connect(path) as db:
        remaining = db.execute('SELECT id,state FROM tasks ORDER BY age,id').fetchall()
    assert remaining == [('older', 'active'), ('newer', 'ready')]
    report = {'scope': 'one synthetic claim, no production scheduler or measured seven-worker pool',
              'python': sys.version, 'sqlite': sqlite3.sqlite_version, 'actor_rss_bytes': rss,
              'after_crash': remaining,
              'missing': ['worker death detection', 'execution registry', 'timeout/child supervision',
                          'failure observation and bounded reconciliation'], 'evidence': str(directory)}
    (directory / 'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
