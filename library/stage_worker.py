"""One supervised direct-claim actor. Activation is a separate, exact-candidate step."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import threading
import time
from typing import Any

import yaml

from booksys_core.browser import Byparr
from booksys_core.downloader import Pyload
from booksys_core.errors import CollectionError
from booksys_core.network_admission import RollingBudget
from library.authority import code_digest
from library.launcher import verify_startup
from library.metadata import Providers
from library.primitives import LibraryError, atomic_yaml, lock
from library.source_tasks import bounded_read
from library.stage_model import QUEUES
from library.stage_runtime import StageRuntime, TIMEOUTS, external_capacity
from library.stage_supervisor import deadline, notify
from library.locking import defer_contention
from contextlib import ExitStack


def verify_activation(root: Path) -> dict[str, Any]:
    from library.operational_state import RuntimeObservation
    from library.postgres import Database
    value = RuntimeObservation(Database.configured(root/'state')).activation()
    if (value.get('schema') != 1 or value.get('code_digest') != code_digest()
            or value.get('mode') not in ('independent', 'serial') or value.get('supervisor') not in ('systemd', 'compose')
            or not value.get('qualification_receipt')):
        raise LibraryError('Independent queue activation does not bind this qualified candidate')
    if value['supervisor'] == 'compose' and not os.environ.get('BOOKSYS_READER_SOCKET'):
        raise LibraryError('Compose requires the qualified restricted reader service')
    if value['supervisor'] == 'compose':
        from library.stage_resources import Resources
        capacity = Resources(root/'state/stage-resources.yaml').capacity()
        if capacity.memory_mib > 2048:
            raise LibraryError('Compose grants exceed the shared envelope after control and launcher reserves')
    pool = Path(value['cgroup_pool'])
    own = next(line.split(':', 2)[2] for line in Path('/proc/self/cgroup').read_text().splitlines() if line.startswith('0::'))
    if (not pool.is_relative_to('/sys/fs/cgroup') or pool.is_symlink()
            or not (Path('/sys/fs/cgroup')/own.lstrip('/')).is_relative_to(pool)):
        raise LibraryError('Actor is outside the qualified shared cgroup')
    memory = int((pool/'memory.max').read_text())
    quota, period = map(int, (pool/'cpu.max').read_text().split())
    if not 0 < memory <= 3584*1024**2 or not 0 < quota <= 2*period:
        raise LibraryError('Shared cgroup envelope is not enforced')
    if not value.get('decoder_cgroup_parent') or not value.get('io_device'):
        raise LibraryError('Decoder ownership and I/O qualification are missing')
    os.environ['BOOKSYS_DECODER_CGROUP_PARENT'] = value['decoder_cgroup_parent']
    return dict(value)


def snapshot(runtime: StageRuntime) -> None:
    """One observer owns native GETs; no HTTP request dispatches this work."""
    from mirrors import logical_snapshot
    queue = logical_snapshot(runtime.client, runtime.collector/'mirror-routing.yaml')
    if not isinstance(queue, list):
        raise LibraryError('Invalid native observation')
    from library.operational_state import RuntimeObservation
    observation=RuntimeObservation(runtime.catalog.backend()._database)
    remaining: int | None = 0
    for package in queue:
        for item in package.get('links', []):
            if item.get('status') != 12:
                continue  # Pinned pyLoad DOWNLOADING; offline/finished/parked links do not write.
            size = item.get('size')
            if type(size) is not int or size <= 0:
                remaining = None
                break
            if remaining is not None:
                remaining += size  # Full size is conservative; no progress deduction.
    # Overshoot is qualified at activation, never invented from an empty status.
    activation = observation.activation()
    bound = activation.get('pyload_bound', {})
    actual = {key: runtime.client.call('get_config_value', category='download', option=key)
              for key in ('limit_speed', 'max_speed', 'max_downloads', 'chunks')}
    overshoot = bound.get('overshoot_bytes') if actual == bound.get('configuration') else None
    observation.publish(queue,at=time.time(),remaining=remaining,overshoot=overshoot)


def verify_budgets(state: Path) -> None:
    """Read retained allowances; a concurrent owner never means missing state."""
    for name in ('provider', 'native'):
        until = time.monotonic()+5
        while True:
            try:
                RollingBudget(state/f'{name}-admission.yaml').next_due()
                break
            except CollectionError as exc:
                if str(exc) != 'Shared network lane busy' or time.monotonic() >= until:
                    raise
                time.sleep(.05)


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
    verify_startup(args.root/'state')
    os.environ['BOOKSYS_ADMISSION_STATE'] = str(args.root/'state')
    # Read existing authorities. Constructors must not create a fresh allowance.
    verify_budgets(args.root/'state')
    runtime = StageRuntime(args.root, args.reception, args.collector,
        providers=Providers(args.root/'state/library-provider-cache', budget=50),
        client=Pyload(args.endpoint, json.loads(args.credentials.read_text())['api_key']), browser=Byparr(args.byparr))
    runtime.client.downloads = Path(os.environ.get('BOOKSYS_FICHE_ROOT', str(args.reception)))
    runtime.client.fetch_image = runtime.native.browser.cover
    stop = threading.Event()
    def request_stop(*_: object) -> None:
        stop.set()  # Finish the current synchronous effect before releasing ownership.
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    def watchdog() -> None:
        while not stop.wait(5):
            for queue in QUEUES if args.queue == 'serial' else (args.queue,):
                from library.operational_state import RuntimeObservation
                RuntimeObservation(runtime.catalog.backend()._database).heartbeat(queue,at=time.time())
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
                    source_scan_at = snapshot_at = 0.0
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
                                    emit(service=service, stage='diagnostics', queue=args.queue, code='degraded', **exception_details(exc))
                                    pass  # Prior observation ages; disk workers defer safely.
                                runtime.recover_reader(now=stamp)
                                try:
                                    from library.dependency_recovery import DependencyRecovery
                                    runtime.business.providers.budget = 1
                                    DependencyRecovery(args.root/'state/dependency-readiness.yaml').probe(
                                        runtime.business.providers, at=stamp.timestamp())
                                except (CollectionError, LibraryError, OSError) as exc:
                                    emit(service=service, stage='diagnostics', queue=args.queue, code='degraded', **exception_details(exc))
                                    pass  # Unproved recovery cannot reopen exhausted work.
                            runtime.catalog.project_pending()
                            runtime.discover_business(now=stamp)
                            for queue in ('collection', 'routing', 'retirement'):
                                runtime.tasks.policy.register(queue, args.collector/'monitor.yaml')  # type: ignore[union-attr]
                            from library.operational_state import CollectorState
                            if CollectorState(args.collector,database=runtime.catalog.backend()._database).backfill() is not None:
                                runtime.tasks.policy.register('collection', args.collector/'backfill.yaml',kind='backfill')  # type: ignore[union-attr]
                            if time.monotonic() >= source_scan_at:
                                source_scan_at = time.monotonic() + 1
                                runtime.tasks.scan_next(now=stamp)
                            try:
                                runtime.native.discover_intake(runtime.tasks, now=stamp)
                            except (CollectionError, LibraryError, OSError) as exc:
                                emit(service=service, stage='diagnostics', queue=args.queue, code='degraded', **exception_details(exc))
                                pass  # Missing snapshot cannot create an intake obligation.
                        stop.wait(1)
        else:
            expected_mode = 'serial' if args.queue == 'serial' else 'independent'
            if activation['mode'] != expected_mode:
                raise LibraryError('Dispatcher mode differs from selected activation')
            with ExitStack() as owners:
                actors = [owners.enter_context(runtime.actor(queue)) for queue in (QUEUES if args.queue == 'serial' else (args.queue,))]
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
                            outcome = actor.step(now=stamp, free_bytes=min(shutil.disk_usage(args.root/'library').free, shutil.disk_usage(os.environ.get('BOOKSYS_READER_JOBS', args.root)).free),
                                external_bytes=external_capacity(args.root/'snapshots/external-capacity.yaml', now=stamp))
                        if outcome in ('uncertain', 'cleanup_required', 'reconciliation_required'):
                            next_check[actor.queue] = time.monotonic()+30
                    # Recovery uses the retained attempt and exact receipt. A
                    # worker restart never grants another business allowance.
                    stop.wait(1)
    finally:
        graceful = stop.is_set()
        stop.set()
        pulse.join(6)
        if graceful:
            emit(service=service, stage='diagnostics', queue=args.queue, code='completed', outcome='stopped')


if __name__ == '__main__':
    main()
