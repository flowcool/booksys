"""Direct queue wiring. Construction never upgrades or recreates lost authority."""
from __future__ import annotations

from datetime import datetime, timezone
import os
import subprocess
import shutil
from pathlib import Path
from typing import Any

import yaml

from booksys_core.network_admission import BudgetWait
from library.readers import ReaderUnavailable
from library.reader_inputs import ReaderBusy
from library.business_stages import BusinessStages
from library.catalog import Catalog
from library.metadata import Providers
from library.native_stages import NativeStages
from library.primitives import LibraryError, atomic_yaml
from library.publication import Capacity
from library.source_tasks import SourcePolicy, bounded_read
from library.stage_actor import StageActor
from library.stage_model import Task, StepResult
from library.stage_resources import Resources, Demand
from library.stage_tasks import StageTasks

TIMEOUTS = dict(collection=300, intake=1200, ocr=900, metadata=1200, copies=3600, routing=300, retirement=1200)


from library.external_capacity import external_capacity as external_capacity


def demand(task: Task) -> Demand:
    heavy = task.stage in ('intake', 'ocr', 'copies')
    disk = heavy or task.stage == 'retirement'
    native = task.stage in ('collection', 'routing', 'retirement')
    return Demand(memory_mib=1792 if heavy else 512 if native else 256,
        cpu_millis=1250 if heavy else 250, disk=int(disk),
        browser=int(task.stage == 'collection'), native=int(native),
        output_bytes=12*1024**3 if task.stage in ('intake', 'copies') else 4*1024**3 if task.stage == 'ocr' else 0)


class StageRuntime:
    def __init__(self, root: Path, reception: Path, collector: Path, *, providers: Providers,
                 client: Any, browser: Any, isolated: bool = False, capacity: Capacity | None = None):
        self.root, self.reception, self.collector = root, reception, collector
        self.catalog = Catalog(root/'state')
        store = self.catalog.backend()
        if store is None:
            raise LibraryError('Independent queues require selected PostgreSQL authority')
        self.tasks = StageTasks(store, policy=SourcePolicy(store.directory/'task-policy.yaml'))
        self.resources = Resources(store.directory/'stage-resources.yaml')
        self.business = BusinessStages(self.catalog, reception, root/'library', providers, isolated=isolated, capacity=capacity)
        self.native = NativeStages(self.catalog, reception, root/'library', collector,
            client=client, browser=browser, snapshots=root/'snapshots')
        self.client, self.isolated = client, isolated
        self.provider_allowance = providers.budget
        self.failure_cursor = ''
        from library.nas_storage import configured_contract, NASMonitor
        contract = configured_contract()
        self.nas = NASMonitor(contract) if contract else None

    def discover_business(self, *, now: datetime) -> int:
        """One current key page; a full pass revisits insertions behind the cursor."""
        from library.operational_state import OperationalState
        state = OperationalState(self.tasks.store._database)
        after = state.discovery()
        store = self.tasks.store
        page = store.scan('assets', after=after, limit=100)
        count = self.business.discover(self.tasks, now=now, after=after, limit=100)
        state.save_discovery(page[-1][0] if len(page) == 100 else '')
        return count

    def recover_reader(self, *, now: datetime) -> None:
        """Probe a failed execution dependency, then consume one recovery version."""
        from library.dependency_recovery import DependencyRecovery
        observations = DependencyRecovery(self.catalog.directory/'dependency-readiness.yaml')
        dependency = observations.read()['dependencies'].get('reader-engine', {})
        if dependency.get('available') is False:
            from library.reader_client import endpoint, available as probe_available
            image = os.environ.get('BOOKSYS_READER_IMAGE')
            if endpoint():
                available = probe_available()
            elif image:
                from library.decoder_ownership import DecoderOwnership
                command = ['docker', 'run', '--rm', '--network=none', '--memory=128m', '--cpus=.25', '--pids-limit=32',
                    '--entrypoint=/app/.venv/bin/python', image, '-c',
                    "import shutil; import library.reader_worker, library.printed_worker; assert all(shutil.which(x) for x in ('unrar','tesseract','pdftoppm'))"]
                path = self.catalog.directory/'reader-dependency-children.yaml'
                owner = DecoderOwnership(path)
                if not owner.exists():
                    owner = DecoderOwnership.create(path)
                if not owner.cleanup():
                    return
                if owner.names():
                    from uuid import uuid4
                    atomic_yaml(self.root/'evidence/reader-probes'/(uuid4().hex+'.yaml'),
                                {'names': owner.names(), 'stopped': True, 'observed_at': now.timestamp()})
                    owner.reset_stopped()
                try:
                    result = subprocess.run(owner.prepare(command), capture_output=True, timeout=15)
                    available = result.returncode == 0
                except (OSError, subprocess.TimeoutExpired):
                    available = False
                finally:
                    if not owner.cleanup():
                        raise LibraryError('Reader dependency probe cleanup is not proved')
            else:
                available = all(shutil.which(x) for x in ('bwrap', 'unrar', 'tesseract', 'pdftoppm'))
            # A task-specific Docker125 must not manufacture shared recovery.
            # Only a failed standalone engine probe followed by a healthy one
            # proves the shared execution capability actually recovered.
            observations.observe('reader-probe', available=available, at=now.timestamp())
            observations.observe('reader-engine', available=available, at=now.timestamp())
        page = self.tasks.technical_failures(after=self.failure_cursor)
        for task in page:
            proof = observations.proof({'reader-probe': task.dependency_failed_at})
            if proof:
                self.tasks.recover_dependency(task.key, proof=proof, now=now)
        self.failure_cursor = page[-1].key if len(page) == 100 else ''

    def actor(self, queue: str) -> StageActor:
        business = queue in ('metadata', 'ocr', 'copies')
        def execute(task: Task, stamp: datetime) -> Any:
            if task.method == 'manual-proof-v1':
                from library.manual_reader import execute_page
                from library.resolution import Resolution
                return execute_page(self.catalog, task, Resolution(self.catalog, self.reception))
            if task.stage == 'metadata':
                self.business.providers.budget = self.provider_allowance
                self.business.providers.events.clear()
            try:
                return self.business.execute(task, now=stamp) if business else self.native.execute(task, now=stamp)
            except ReaderBusy:
                # The service proves this request never entered execution.
                # Deferral retains age and generation without spending a try.
                from datetime import timedelta
                return StepResult('defer', due_at=stamp+timedelta(seconds=30))
            except ReaderUnavailable:
                from library.dependency_recovery import DependencyRecovery
                import time
                failed_at = time.time()
                self.tasks.dependency_failed(task.key, task.generation, dependency='reader-engine', at=failed_at)
                DependencyRecovery(self.catalog.directory/'dependency-readiness.yaml').observe(
                    'reader-engine', available=False, at=failed_at)
                raise
            except BudgetWait as exc:
                from booksys_core.network_admission import NativeMutation
                import math
                budget = getattr(self.client, 'admission', None)
                if not business and budget is not None and NativeMutation(budget.path.parent, 'readback', self.collector/'monitor.yaml').read()['state'] == 'uncertain':
                    raise
                due = max(stamp.timestamp()+30, exc.due) if math.isfinite(exc.due) else stamp.timestamp()+300
                from booksys_core.diagnostics import emit
                emit(service='library' if business else 'collector', stage='identity' if business else 'downloads',
                     code='waiting', reason='provider_budget', next_attempt=datetime.fromtimestamp(due, timezone.utc).isoformat())
                return StepResult('defer', due_at=datetime.fromtimestamp(due, timezone.utc))
        def receipt(task: Task) -> tuple[str,str] | StepResult:
            if task.method == 'manual-proof-v1':
                from library.manual_reader import page_receipt
                return page_receipt(self.catalog, task)
            return self.business.receipt(task) if business else self.native.receipt(task)
        return StageActor(self.tasks, self.resources, queue, execute, demand=demand,
            cleanup=lambda task: True,  # Synchronous adapter returned; decoder registry proves external children.
            scopes=lambda task: [] if task.method == 'manual-proof-v1' else self.business.scopes(task) if business else self.native.scopes(task),
            ready=self.ready, local_failure=self.business.local_failure if queue == 'copies' else None,
            receipt=receipt)

    def ready(self, task: Task, stamp: datetime) -> str:
        from booksys_core.errors import CollectionError
        if self.nas is not None and task.stage in ('collection', 'intake', 'ocr', 'copies', 'retirement') and not self.nas.ready():
            return 'nas_unavailable'
        try:
            budget = (self.business.providers.admission if task.stage == 'metadata' else
                      getattr(self.client, 'admission', None) if task.stage in ('collection', 'routing', 'retirement') else None)
            if budget is not None and budget.next_due(timestamp=stamp.timestamp()) > stamp.timestamp():
                return 'network_budget_wait'
        except (CollectionError, OSError):
            return 'network_authority_unavailable'
        from library.reader_client import endpoint, available
        if task.stage in ('intake', 'ocr', 'copies') and endpoint():
            if not available(idle=True):
                return 'reader_setup_unavailable'
        elif task.stage in ('intake', 'ocr', 'copies'):
            image = os.environ.get('BOOKSYS_READER_IMAGE')
            if image:
                try:
                    result = subprocess.run(['docker', 'image', 'inspect', '--format', '{{.Id}}', image],
                                            capture_output=True, timeout=10)
                    if result.returncode != 0:
                        return 'reader_setup_unavailable'
                except (OSError, subprocess.TimeoutExpired):
                    return 'reader_setup_unavailable'
            elif not shutil.which('bwrap'):
                return 'reader_setup_unavailable'
        if task.method == 'manual-proof-v1':
            return ''
        if task.stage in ('metadata', 'ocr', 'copies') and not task.entity.startswith('decision:'):
            asset = self.tasks.store.get('assets', task.entity, media=False)
            if asset is None:
                return 'inputs_changed'
            if asset.get('resolution', {}).get('status') == 'pending':
                return 'operator_decision_pending'
            from library.admission import eligible
            current = self.catalog.working_set('admit', {'assets': [task.entity]}, media=False)
            if not eligible(current, task.entity):
                return 'domain_prerequisites'
            if task.stage == 'copies':
                from library.admission import projection
                state = self.catalog.working_set('admit', {'assets': [task.entity]}, media=False)
                if projection(state, task.entity)['signature'] != task.input_revision:
                    return 'inputs_changed'
        return self.native.ready(task, now=stamp) if task.stage not in ('metadata', 'ocr', 'copies') else ''
