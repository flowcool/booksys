# /// script
# requires-python = ">=3.14"
# dependencies = ["rq==3.0.0", "redis==8.1.0", "huey==3.5.4"]
# ///
"""Qualify transport only on a newly created, private Redis instance.

Run from the repository root: uv run --script ops/qualification/queues.py.
All state stays in tempfile.mkdtemp() on TMPDIR. No live mounts, API or ports.
The owned Redis container is stopped after logs are saved; evidence is retained.
"""
from __future__ import annotations

import argparse
from collections.abc import Callable
import ctypes
import importlib.metadata
import json
import os
from pathlib import Path
import signal
import select
import subprocess
import sys
import tempfile
import time
from typing import Any
import uuid

from redis import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from rq import Queue, Worker
from rq.exceptions import DuplicateJobError
from rq.job import Job
from huey import SqliteHuey  # type: ignore[import-untyped]

IMAGE = 'redis@sha256:02f2cc4882f8bf87c79a220ac958f58c700bdec0dfb9b9ea61b62fb0e8f1bfcf'
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def wait_for(check: Callable[[], bool], timeout: float = 15) -> float:
    start = time.monotonic()
    while not check():
        if time.monotonic() - start > timeout:
            raise TimeoutError('Qualification condition not reached')
        time.sleep(.05)
    return time.monotonic() - start


def client(directory: Path) -> Redis:
    return Redis(unix_socket_path=str(directory / 'redis.sock'), socket_timeout=3)


def worker(directory: Path, name: str, *, handover: bool = False) -> None:
    connection = client(directory)
    queue = Queue(name, connection=connection)
    if handover:
        Queue.dequeue_any([queue], timeout=None, connection=connection)
        (directory / 'handover').write_text('claimed')
        os._exit(19)  # Crash between atomic dequeue and execution registration.
    Worker([queue], connection=connection, maintenance_interval=1,
           job_monitoring_interval=1).work(burst=True, logging_level='WARNING')


def rss(pid: int) -> int:
    try:
        for line in Path(f'/proc/{pid}/status').read_text().splitlines():
            if line.startswith('VmRSS:'):
                return int(line.split()[1]) * 1024
    except FileNotFoundError:
        pass
    return 0


def reap_actors(workers: list[subprocess.Popen[bytes]], directory: Path) -> None:
    """This process is a Linux subreaper: orphaned RQ horses become its children."""
    for process in workers:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # Parent exited between poll and signal; still reap its horse.
    for process in workers:
        process.wait(timeout=10)
    children = Path(f'/proc/self/task/{os.getpid()}/children')
    # All launched parents have exited. Remaining direct children are adopted
    # horses, even if their job never reached the started-marker write.
    adopted = children.read_text().split()
    for value in adopted:
        pid = int(value)
        try:
            fd = os.pidfd_open(pid)
        except ProcessLookupError:
            continue
        try:
            signal.pidfd_send_signal(fd, signal.SIGKILL)
            poll = select.poll()
            poll.register(fd, select.POLLIN)
            if not poll.poll(10000):
                raise RuntimeError('Owned actor did not exit; inspect retained proof')
            os.waitpid(pid, 0)
        finally:
            os.close(fd)
    assert not children.read_text().strip(), 'Owned actors remain after cleanup'
    (directory / 'actor-cleanup.json').write_text(json.dumps({
        'adopted_actors': len(adopted), 'remaining_children': []}))


def qualify(directory: Path, *, fail_after_start: bool = False) -> dict[str, Any]:
    connection = client(directory)
    workers: list[subprocess.Popen[bytes]] = []
    def launch(name: str, *, handover: bool = False) -> subprocess.Popen[bytes]:
        with (directory / (name + '.log')).open('ab') as log:
            command = [sys.executable, str(Path(__file__).resolve()), '--worker', name,
                       '--directory', str(directory)]
            if handover:
                command.append('--handover')
            process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True)
        workers.append(process)
        return process

    def enqueue(queue: Queue, name: str, duration: float = .3) -> Job:
        return queue.enqueue('ops.qualification.queue_jobs.record', str(directory), name,
                             duration, job_id=name, unique=True, job_timeout=15,
                             result_ttl=86400, failure_ttl=604800)

    report: dict[str, Any] = {'scope': 'synthetic transport; no Booksys effects or capacity proof',
        'versions': {name: importlib.metadata.version(name) for name in ('rq', 'redis', 'huey')},
        'python': sys.version, 'redis': connection.info('server')['redis_version']}
    try:
        queues = [Queue(name, connection=connection) for name in
                  ('collection', 'intake', 'ocr', 'metadata', 'copies', 'routing', 'retirement')]
        for queue in queues:
            enqueue(queue, queue.name + '-1', 10 if fail_after_start else 1)
            enqueue(queue, queue.name + '-2', .1)
        for queue in queues:
            launch(queue.name)
        wait_for(lambda: all((directory / (q.name + '-1.started')).exists() for q in queues))
        if fail_after_start:
            raise RuntimeError('Injected qualification failure with seven live horses')
        report['working_parent_rss_bytes'] = sum(rss(p.pid) for p in workers)
        child_pids = [int((directory / (q.name + '-1.started')).read_text()) for q in queues]
        report['working_child_rss_bytes'] = sum(rss(pid) for pid in child_pids)
        for process in workers:
            if process.wait(timeout=20) != 0:
                raise RuntimeError('Worker failed; inspect retained log')
        intervals = {q.name: [json.loads((directory / (q.name + suffix + '.json')).read_text())
                              for suffix in ('-1', '-2')] for q in queues}
        assert all(items[0]['end'] <= items[1]['start'] for items in intervals.values())
        overlap = min(v[0]['end'] for v in intervals.values()) - max(v[0]['start'] for v in intervals.values())
        assert overlap > 0
        report['seven_queue_overlap_seconds'] = overlap
        report['intervals'] = intervals

        queue = Queue('unique', connection=connection)
        enqueue(queue, 'same-generation')
        try:
            enqueue(queue, 'same-generation')
        except DuplicateJobError:
            pass
        else:
            raise AssertionError('Unique enqueue accepted duplicate')
        assert queue.count == 1
        enqueue(queue, 'next-generation')
        assert queue.count == 2
        report['unique_generation'] = 'duplicate refused; different generation accepted'

        queue = Queue('handover', connection=connection)
        job = enqueue(queue, 'handover-job')
        assert launch('handover', handover=True).wait(timeout=10) == 19
        assert queue.count == 0 and queue.intermediate_queue.get_job_ids() == [job.id]
        # Public maintenance. Wait real time for RQ's fixed >60s intermediate grace.
        maintainer = Worker([queue], connection=connection, maintenance_interval=1)
        queue.intermediate_queue.cleanup(maintainer, queue)
        started = time.monotonic()
        def intermediate_failed() -> bool:
            queue.intermediate_queue.cleanup(maintainer, queue)
            return job.get_status().value == 'failed'
        report['intermediate_recovery_seconds'] = wait_for(intermediate_failed, timeout=70)
        assert time.monotonic() - started >= 60
        assert queue.count == 0  # No blind framework resend.

        queue = Queue('parent-loss', connection=connection)
        job = enqueue(queue, 'orphan-job', .5)
        process = launch('parent-loss')
        wait_for(lambda: (directory / 'orphan-job.started').exists())
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
        wait_for(lambda: (directory / 'orphan-job.json').exists())
        wait_for(lambda: job.get_status().value == 'finished')
        report['parent_loss'] = 'RQ horse survives parent process-group kill and can complete; cgroup/actor exclusion REQUIRED'

        queue = Queue('crash', connection=connection)
        job = enqueue(queue, 'crash-job', 10)
        process = launch('crash')
        wait_for(lambda: (directory / 'crash-job.started').exists())
        actor = int((directory / 'crash-job.started').read_text())
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
        os.kill(actor, signal.SIGKILL)
        assert job.get_status().value == 'started'
        # Real registry expiry, not mocked clocks or changed scores.
        def abandoned_failed() -> bool:
            queue.started_job_registry.cleanup()
            return job.get_status().value == 'failed'
        report['started_recovery_seconds'] = wait_for(abandoned_failed, timeout=90)
        assert queue.count == 0 and not (directory / 'crash-job.json').exists()
        report['framework_retries'] = 'disabled; both failures remain failed for domain reconciliation'

        missing = enqueue(Queue('missing', connection=connection), 'missing-hash')
        connection.delete(missing.key)  # Inject transport record loss on this private server only.
        launch('missing').wait(timeout=10)
        assert Queue('missing', connection=connection).count == 0
        report['missing_hash'] = 'RQ discards missing job; domain obligation reconstruction REQUIRED'

        huey = SqliteHuey('qualification', filename=str(directory / 'huey.sqlite'))
        @huey.task()  # type: ignore[untyped-decorator]
        def probe() -> int:
            return 1
        probe()
        assert huey.pending_count() == 1
        assert huey.dequeue() is not None
        reopened = SqliteHuey('qualification', filename=str(directory / 'huey.sqlite'))
        assert reopened.pending_count() == 0
        report['huey_claim_loss'] = 'dequeued task absent after reopening; no in-flight registry'
        report['redis_memory'] = {k: connection.info('memory')[k] for k in
                                   ('used_memory', 'used_memory_rss', 'used_memory_peak')}
        report['complete'] = True
        return report
    finally:
        reap_actors(workers, directory)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker')
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--handover', action='store_true')
    parser.add_argument('--fail-after-start', action='store_true',
                        help='Inject an expected failure to prove actor cleanup')
    args = parser.parse_args()
    if args.worker:
        if args.directory is None:
            parser.error('--worker needs --directory')
        worker(args.directory, args.worker, handover=args.handover)
        return
    # PR_SET_CHILD_SUBREAPER is process-local. It includes horses that call
    # setpgrp(), unlike a parent process-group kill. Fail before creating Redis.
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), 'Cannot supervise orphaned qualification actors')
    directory = Path(tempfile.mkdtemp(prefix='booksys-queue-proof-'))
    name = 'booksys-queue-proof-' + uuid.uuid4().hex[:12]
    print(json.dumps({'evidence': str(directory), 'container': name}), flush=True)
    subprocess.run(['docker', 'run', '-d', '--name', name, '--network', 'none',
                    '--user', f'{os.getuid()}:{os.getgid()}', '--cap-drop', 'ALL',
                    '--security-opt', 'no-new-privileges', '--memory', '128m', '--cpus', '.5',
                    '--mount', f'type=bind,src={directory},dst=/proof', IMAGE,
                    'redis-server', '--port', '0', '--unixsocket', '/proof/redis.sock',
                    '--unixsocketperm', '700', '--dir', '/proof', '--appendonly', 'yes',
                    '--appendfsync', 'always', '--save', '', '--maxmemory', '64mb',
                    '--maxmemory-policy', 'noeviction'], check=True)
    try:
        wait_for(lambda: (directory / 'redis.sock').exists())
        report = qualify(directory, fail_after_start=args.fail_after_start)
        connection = client(directory)
        before = {job.id: (job.func_name, job.args, job.get_status())
                  for job in Queue('unique', connection=connection).jobs}
        subprocess.run(['docker', 'kill', name], check=True)
        subprocess.run(['docker', 'start', name], check=True)
        def restarted() -> bool:
            try:
                return bool(connection.ping())
            except RedisConnectionError:
                return False
        wait_for(restarted)
        after = {job.id: (job.func_name, job.args, job.get_status())
                 for job in Queue('unique', connection=connection).jobs}
        assert after == before and len(after) == 2
        report['redis_abrupt_restart'] = 'acknowledged two-job ready list and hashes retained by AOF always'
        (directory / 'report.json').write_text(json.dumps(report, indent=2))
        print(json.dumps({'complete': True, 'report': str(directory / 'report.json')}), flush=True)
    finally:
        try:
            with (directory / 'redis.log').open('wb') as log:
                subprocess.run(['docker', 'logs', name], stdout=log, stderr=subprocess.STDOUT, check=True)
        finally:
            subprocess.run(['docker', 'stop', name], check=True)


if __name__ == '__main__':
    main()
