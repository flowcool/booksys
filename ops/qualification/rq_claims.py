"""RQ comparison against supervised_claims using identical synthetic domain work."""
from __future__ import annotations

import ctypes
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
from rq import Queue, Worker
from rq.exceptions import DuplicateJobError
from ops.qualification.domain_queue import STAGES, initialize, reconcile, select
from ops.qualification.queues import IMAGE, client, reap_actors, rss, wait_for


def worker(root: Path, stage: str) -> None:
    Worker([Queue(stage, connection=client(root))], connection=client(root),
           job_monitoring_interval=1).work(burst=True, logging_level='WARNING')


def dispatch(root: Path, stage: str) -> str | None:
    item = select(root, stage)
    if item is None:
        return None
    key, generation = item
    queue = Queue(stage, connection=client(root))
    reference = key + '--' + str(generation)
    try:
        queue.enqueue('ops.qualification.domain_queue.execute', str(root), stage,
                      key, generation, job_id=reference, unique=True, job_timeout=15)
    except DuplicateJobError:
        job = queue.fetch_job(reference)
        if job is None or job.args != (str(root), stage, key, generation):
            raise RuntimeError('Ambiguous transport ownership') from None
    return reference


def launch(root: Path, stage: str) -> subprocess.Popen[bytes]:
    with (root / (stage + '.log')).open('ab') as log:
        return subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                                 '--worker', str(root), stage], stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)


def main() -> None:
    if len(sys.argv) == 4 and sys.argv[1] == '--worker':
        worker(Path(sys.argv[2]), sys.argv[3])
        return
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0):
        raise OSError('Cannot supervise orphaned RQ actors')
    root = Path(tempfile.mkdtemp(prefix='booksys-rq-domain-claims-'))
    initialize(root)
    name = 'booksys-queue-proof-' + uuid.uuid4().hex[:12]
    processes: list[subprocess.Popen[bytes]] = []
    subprocess.run(['docker', 'run', '-d', '--name', name, '--network', 'none',
                    '--user', f'{os.getuid()}:{os.getgid()}', '--cap-drop', 'ALL',
                    '--security-opt', 'no-new-privileges', '--memory', '128m', '--cpus', '.5',
                    '--mount', f'type=bind,src={root},dst=/proof', IMAGE,
                    'redis-server', '--port', '0', '--unixsocket', '/proof/redis.sock',
                    '--unixsocketperm', '700', '--dir', '/proof', '--appendonly', 'yes',
                    '--appendfsync', 'always', '--save', ''], check=True)
    try:
        wait_for(lambda: (root / 'redis.sock').exists())
        for stage in STAGES:
            assert dispatch(root, stage) == stage + '-old--1'
            processes.append(launch(root, stage))
        wait_for(lambda: all((root / (s + '-old.started')).exists() for s in STAGES))
        actors = [int((root / (s + '-old.started')).read_text()) for s in STAGES]
        footprint = {'parents': sum(rss(p.pid) for p in processes),
                     'horses': sum(rss(pid) for pid in actors),
                     'dispatcher': rss(os.getpid()), 'redis': client(root).info('memory')['used_memory_rss']}
        lost_actor = actors[STAGES.index('copies')]
        os.kill(lost_actor, signal.SIGKILL)
        for process in processes:
            assert process.wait(timeout=20) == 0
        started = time.monotonic()
        assert reconcile(root, 'copies') == 1
        for stage in STAGES:
            if select(root, stage):
                dispatch(root, stage)
                processes.append(launch(root, stage))
        for process in processes:
            assert process.wait(timeout=20) == 0
        # Copies recovered its interrupted old item. All other queues processed
        # their new item; one more bounded handover completes copies-new.
        assert dispatch(root, 'copies') == 'copies-new--1'
        processes.append(launch(root, 'copies'))
        assert processes[-1].wait(timeout=20) == 0
        with sqlite3.connect(root / 'domain.sqlite') as db:
            states = db.execute('SELECT state,count(*) FROM tasks GROUP BY state').fetchall()
        assert states == [('done', 14), ('ready', 7)]
        assert len(list(root.glob('*.effect'))) == 14
        report = {'scope': 'RQ transport over the same domain prototype and module imports as direct claims',
                  'evidence': str(root), 'footprint_bytes': footprint,
                  'reconciliation_and_remaining_handover_seconds': time.monotonic() - started,
                  'states': states, 'effect_receipts': 14, 'workers': 7, 'horses': 7,
                  'extra_processes': ['dispatcher', 'redis'],
                  'limit': 'Synthetic receipts only; no real downloader/publication or whole-host availability claim'}
        (root / 'report.json').write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2))
    finally:
        try:
            reap_actors(processes, root)
        finally:
            try:
                with (root / 'redis.log').open('wb') as log:
                    subprocess.run(['docker', 'logs', name], stdout=log, stderr=subprocess.STDOUT, check=True)
            finally:
                subprocess.run(['docker', 'stop', name], check=True)


if __name__ == '__main__':
    main()
