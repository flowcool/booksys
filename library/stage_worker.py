"""One supervised direct-claim actor. Activation is a separate, exact-candidate step."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import signal
import threading
import time
from typing import Any


from booksys_core.browser import Byparr
from booksys_core.downloader import Pyload
from booksys_core.errors import CollectionError
from booksys_core.network_admission import RollingBudget
from library.authority import code_digest
from library.launcher import verify_startup
from library.metadata import Providers
from library.primitives import LibraryError, lock
from library.stage_model import QUEUES
from library.stage_runtime import StageRuntime, TIMEOUTS
from library.stage_supervisor import deadline, notify
from library.locking import defer_contention


def verify_activation(root: Path) -> dict[str, Any]:
    from library.operational_state import RuntimeObservation
    from library.postgres import Database
    value = RuntimeObservation(Database.configured(root/'state')).activation()
    if (value.get('schema') != 1 or value.get('code_digest') != code_digest()
            or value.get('mode') not in ('independent', 'serial') or value.get('supervisor') not in ('systemd', 'compose')
            or not value.get('qualification_receipt')):
        raise LibraryError('Independent queue activation does not bind this qualified candidate')
    pool = Path(value['cgroup_pool'])
    lines = Path('/proc/self/cgroup').read_text().splitlines()
    unified = next((line.split(':', 2)[2] for line in lines if line.startswith('0::')), None)
    if unified is not None:
        own, root = unified, Path('/sys/fs/cgroup')
        memory_file = 'memory.max'
    else:
        # cgroup v1 (kernels without cgroup v2): one hierarchy per controller and no CPU quota exists.
        own = next(line.split(':', 2)[2] for line in lines if ':memory:' in line)
        root = Path('/sys/fs/cgroup/memory')
        memory_file = 'memory.limit_in_bytes'
    if (not pool.is_relative_to('/sys/fs/cgroup') or pool.is_symlink()
            or not (root/own.lstrip('/')).is_relative_to(pool)):
        raise LibraryError('Actor is outside the qualified shared cgroup')
    memory = int((pool/memory_file).read_text())
    # Refuse application workers outside the qualified aggregate memory (and, on cgroup v2, CPU) envelope.
    if not 0 < memory <= 4608*1024**2:
        raise LibraryError('Shared cgroup envelope is not enforced')
    if unified is not None:
        quota, period = map(int, (pool/'cpu.max').read_text().split())
        if not 0 < quota <= 2*period:
            raise LibraryError('Shared cgroup envelope is not enforced')
    if not value.get('io_device'):
        raise LibraryError('I/O qualification is missing')
    return dict(value)


def snapshot(runtime: StageRuntime) -> None:
    """One observer owns native GETs; no HTTP request dispatches this work."""
    from mirrors import logical_snapshot
    queue = logical_snapshot(runtime.client, runtime.collector/'mirror-routing.yaml')
    if not isinstance(queue, list):
        raise LibraryError('Invalid native observation')
    from library.operational_state import RuntimeObservation
    observation=RuntimeObservation(runtime.catalog.backend()._database)
    observation.publish(queue,at=time.time(),remaining=None,overshoot=None)


# Poll the native queue once per minute and escalate a snapshot failure after five minutes without a good observation.
def snapshot_failure_code(database: Any, *, now: float, limit: float = 300) -> str:
    """A failed snapshot is a warning first and an error once the last good one is old.

    On 2026-11-11 the snapshot failed every minute for 8 hours at level INFO and the whole
    pipeline waited on it without any visible error.
    """
    from library.operational_state import RuntimeObservation
    try:
        age = now - RuntimeObservation(database).capacity()['observed_at']
    except (LibraryError, OSError, KeyError):
        return 'failed'
    return 'failed' if age > limit else 'degraded'


def verify_budgets(state: Path) -> None:
    """Read retained allowances; a concurrent owner never means missing state."""
    for name in ('provider', 'native'):
        RollingBudget(state/f'{name}-admission.yaml').next_due()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('root', 'collector', 'reception', 'credentials'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--queue', choices=(*QUEUES, 'discovery', 'serial'), required=True)
    parser.add_argument('--endpoint', default='http://127.0.0.1:8000')
    parser.add_argument('--byparr', default='http://127.0.0.1:8191/v1')
    args = parser.parse_args()
    from booksys_core.diagnostics import install_logging, emit, exception_details
    service = 'collector' if args.queue in ('collection', 'discovery') else 'retirement' if args.queue == 'retirement' else 'library'
    install_logging(service)
    emit(service=service, stage='diagnostics', queue=args.queue, code='started')
    activation = verify_activation(args.root)
    from library.runtime import lab_preflight
    lab_preflight(args.reception, args.root/'library', args.root/'state', (args.collector, args.credentials))
    # The audit reads every table in full (72 MB of assets): one role does it, not the seven that restart together.
    verify_startup(args.root/'state', audit=args.queue == 'discovery')
    os.environ['BOOKSYS_ADMISSION_STATE'] = str(args.root/'state')
    # Read existing authorities. Constructors must not create a fresh allowance.
    verify_budgets(args.root/'state')
    runtime = StageRuntime(args.root, args.reception, args.collector,
        providers=Providers(args.root/'state/library-provider-cache'),
        client=Pyload(args.endpoint, json.loads(args.credentials.read_text())['api_key']), browser=Byparr(args.byparr))
    runtime.client.downloads = Path(os.environ.get('BOOKSYS_FICHE_ROOT', str(args.reception)))
    runtime.client.fetch_image = runtime.native.browser.cover
    stop = threading.Event()
    def request_stop(*_: object) -> None:
        stop.set()  # Finish the current synchronous effect before releasing ownership.
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    def watchdog() -> None:
        # Use finite heartbeat freshness and health retries; a heartbeat proves liveness only.
        while not stop.wait(5):
            try:
                for queue in QUEUES if args.queue == 'serial' else (args.queue,):
                    from library.operational_state import RuntimeObservation
                    RuntimeObservation(runtime.catalog.backend()._database).heartbeat(queue,at=time.time())
            except Exception as exc:  # A busy database must not end the thread: a dead thread leaves the heartbeat stale forever.
                emit(service=service, stage='diagnostics', queue=args.queue, code='heartbeat_failed', **exception_details(exc))
            notify('WATCHDOG=1')
    pulse = threading.Thread(target=watchdog, daemon=True)
    pulse.start()
    try:
        if args.queue == 'discovery':
            from library.recovery_schedule import RecoverySchedule
            with lock(args.root/'state/stage-discovery.lock', blocking=False):
                with (args.root/'state/library-cycle.lock').open('a') as serial:
                    fcntl.flock(serial, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    notify('READY=1')
                    snapshot_at = release_at = work_revive_at = intake_at = 0.0
                    while not stop.is_set():
                        stamp = datetime.now(timezone.utc)
                        with defer_contention(), (args.root/"state/stage-checkpoint.lock").open("a") as boundary:
                            try:
                                fcntl.flock(boundary, fcntl.LOCK_SH | fcntl.LOCK_NB)
                            except BlockingIOError:
                                stop.wait(1)
                                continue
                            if time.monotonic() >= snapshot_at:
                                snapshot_at = time.monotonic() + 60
                                try:
                                    snapshot(runtime)
                                except (CollectionError, LibraryError, OSError) as exc:
                                    code = snapshot_failure_code(runtime.catalog.backend()._database, now=time.time())
                                    emit(service=service, stage='diagnostics', queue=args.queue, code=code, **exception_details(exc))
                                    pass  # Prior observation ages; disk workers defer safely.
                                try:
                                    from library.dependency_recovery import DependencyRecovery
                                    DependencyRecovery(args.root/'state/dependency-readiness.yaml').probe(
                                        runtime.business.providers, at=stamp.timestamp())
                                except (CollectionError, LibraryError, OSError) as exc:
                                    emit(service=service, stage='diagnostics', queue=args.queue, code='degraded', **exception_details(exc))
                                    pass  # Unproved recovery cannot reopen exhausted work.
                            runtime.discover_business(now=stamp)
                            for queue in ('collection', 'routing'):
                                runtime.tasks.policy.register(queue, args.collector/'monitor.yaml')  # type: ignore[union-attr]
                            if time.monotonic() >= work_revive_at:
                                work_revive_at = time.monotonic() + 300
                                # Items of the stages on work_items that used their attempts get one more round per hour.
                                try:
                                    from library.work_stages import work_stages, work_items_for
                                    from library.versioned import trim
                                    trim(runtime.catalog.backend()._database)  # keep table_changes small
                                    for adopted in sorted(work_stages()):
                                        work_items_for(runtime.catalog.backend()._database).revive(adopted, now=stamp)
                                except (CollectionError, LibraryError, OSError) as exc:
                                    emit(service=service, stage='diagnostics', queue=args.queue, code='degraded', **exception_details(exc))
                            if time.monotonic() >= release_at:
                                release_at = time.monotonic() + 30
                                try:
                                    from library.release import Release
                                    from library.retirement import Retirement
                                    from library.runtime import configured_scripts
                                    hooks = Retirement(runtime.client, runtime.catalog, args.reception, args.collector/'monitor.yaml',
                                                       args.collector/'state.yaml', scripts=configured_scripts(), concurrent=True)._hooks_safe
                                    moved = Release(runtime.client, runtime.catalog.backend()._database, args.reception,
                                                      args.collector, hooks_safe=hooks).run(limit=40)  # one delete call per batch
                                    if moved['released']:
                                        emit(service=service, stage='release', queue=args.queue, code='released',
                                             released=str(moved['released']), candidates=str(moved['candidates']))
                                except (CollectionError, LibraryError, OSError) as exc:
                                    emit(service=service, stage='release', queue=args.queue, code='release_failed', **exception_details(exc))
                            runtime.tasks.scan_next(now=stamp)
                            try:
                                if time.monotonic() >= intake_at:
                                    before = runtime.tasks.changes
                                    runtime.native.discover_intake(runtime.tasks, now=stamp)
                                    # 100 sources per sweep: a sweep that changed nothing waits 30 s, one that did runs again in 5 s.
                                    intake_at = time.monotonic() + (30 if runtime.tasks.changes == before else 5)  # Revisit unchanged intake discovery after thirty seconds and changed passes after five seconds.
                            except (CollectionError, LibraryError, OSError) as exc:
                                emit(service=service, stage='diagnostics', queue=args.queue, code='degraded', **exception_details(exc))
                                pass  # Missing snapshot cannot create an intake obligation.
                        stop.wait(5)  # Wait five seconds between discovery loops; one-second polling consumed 35 percent of a CPU.
        else:
            expected_mode = 'serial' if args.queue == 'serial' else 'independent'
            if activation['mode'] != expected_mode:
                raise LibraryError('Dispatcher mode differs from selected activation')
            actors = [runtime.actor(queue) for queue in (QUEUES if args.queue == 'serial' else (args.queue,))]
            notify('READY=1')
            next_check: dict[str, float] = {}
            while not stop.is_set():
                for actor in actors:
                    if next_check.get(actor.queue, 0) > time.monotonic():
                        continue
                    stamp = datetime.now(timezone.utc)
                    with deadline(TIMEOUTS[actor.queue]):
                        if actor.queue == 'collection':
                            from booksys_core.scheduler import ScheduledCalls
                            try:
                                ScheduledCalls(args.collector, runtime.native.browser).recover_cleanup()
                            except (CollectionError, LibraryError, OSError) as exc:
                                emit(service=service, stage='diagnostics', queue=actor.queue, code='degraded', **exception_details(exc))
                        outcome = actor.step(now=stamp)  # Run one synchronous step per stage worker; independent stages may proceed in parallel.
                    if outcome == 'nas_unavailable':
                        next_check[actor.queue] = time.monotonic()+30  # Cache successful NAS sentinel checks for thirty seconds and back off unavailable stages for thirty seconds.
                    elif outcome == 'idle':
                        next_check[actor.queue] = time.monotonic()+15     # Recheck an empty stage after fifteen seconds.
                stop.wait(1)
    finally:
        graceful = stop.is_set()
        stop.set()
        pulse.join(6)
        if graceful:
            emit(service=service, stage='diagnostics', queue=args.queue, code='completed', outcome='stopped')


if __name__ == '__main__':
    main()
