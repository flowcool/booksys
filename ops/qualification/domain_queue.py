"""Disposable domain-claim comparison. Synthetic rows/effects only, no live paths.

This is qualification code, not an application queue framework. The callable
surface lets both transport candidates execute the same synthetic obligation.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import time
from collections.abc import Iterator

STAGES = ('collection', 'intake', 'ocr', 'metadata', 'copies', 'routing', 'retirement')


def initialize(root: Path) -> None:
    with sqlite3.connect(root / 'domain.sqlite') as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE tasks(id TEXT PRIMARY KEY,stage TEXT,age INTEGER,due REAL,'
                   'generation INTEGER,attempts INTEGER,state TEXT)')
        db.execute('CREATE INDEX ready ON tasks(stage,state,due,age,id)')
        for stage in STAGES:
            db.executemany('INSERT INTO tasks VALUES (?,?,?,?,?,?,?)',
                           [(stage + '-old', stage, 1, 0, 1, 0, 'ready'),
                            (stage + '-new', stage, 2, 0, 1, 0, 'ready'),
                            (stage + '-delayed', stage, 0, time.time() + 3600, 1, 0, 'ready')])


@contextmanager
def owner(root: Path, stage: str) -> Iterator[None]:
    with (root / (stage + '.lock')).open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def select(root: Path, stage: str) -> tuple[str, int] | None:
    with sqlite3.connect(root / 'domain.sqlite') as db:
        row = db.execute("SELECT id,generation FROM tasks WHERE stage=? AND state='ready' "
                         'AND due<=? ORDER BY age,id LIMIT 1', (stage, time.time())).fetchone()
    return None if row is None else (str(row[0]), int(row[1]))


def execute(root: str, stage: str, key: str, generation: int) -> None:
    """Shared domain checks/effect. An interruption must consult this evidence."""
    directory = Path(root)
    with owner(directory, stage):
        with sqlite3.connect(directory / 'domain.sqlite') as db:
            changed = db.execute("UPDATE tasks SET state='running',attempts=attempts+1 "
                                 "WHERE id=? AND generation=? AND state='ready' AND attempts<3",
                                 (key, generation)).rowcount
        if not changed:
            return
        started = time.monotonic()
        if stage == 'metadata':
            # Include the delivered domain imports in the measured actor footprint.
            import library.metadata  # noqa: F401
        elif stage == 'copies':
            import library.admission  # noqa: F401
        elif stage == 'intake':
            import library.intake  # noqa: F401
        elif stage == 'ocr':
            import library.printed  # noqa: F401
        elif stage == 'collection':
            import booksys_core.collection  # noqa: F401
        elif stage == 'routing':
            import mirror_routing  # noqa: F401
        else:
            import library.retirement  # noqa: F401
        (directory / (key + '.started')).write_text(str(os.getpid()))
        time.sleep(1.5)
        # Exclusive creation models a retained, idempotent effect receipt. This
        # is not proof for real filesystem publication or native downloader effects.
        with (directory / (key + '.effect')).open('x') as handle:
            handle.write(json.dumps({'key': key, 'generation': generation}))
            handle.flush()
            os.fsync(handle.fileno())
        with sqlite3.connect(directory / 'domain.sqlite') as db:
            db.execute("UPDATE tasks SET state='done' WHERE id=? AND generation=?",
                       (key, generation))
        (directory / (key + '.interval')).write_text(json.dumps({
            'start': started, 'end': time.monotonic(), 'pid': os.getpid()}))


def reconcile(root: Path, stage: str) -> int:
    """Called only after proving the former acting process cannot still run."""
    with owner(root, stage), sqlite3.connect(root / 'domain.sqlite') as db:
        rows = db.execute("SELECT id,generation,attempts FROM tasks WHERE stage=? AND state='running'",
                          (stage,)).fetchall()
        for key, generation, attempts in rows:
            receipt = root / (key + '.effect')
            if receipt.exists():
                value = json.loads(receipt.read_text())
                if value != {'key': key, 'generation': generation}:
                    raise RuntimeError('Uncertain synthetic receipt')
                state = 'done'
            else:
                state = 'blocked' if attempts >= 3 else 'ready'
                generation += 1
            db.execute('UPDATE tasks SET state=?,generation=? WHERE id=?', (state, generation, key))
    return len(rows)
