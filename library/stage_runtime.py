"""Direct queue wiring. Construction never upgrades or recreates lost authority."""
from __future__ import annotations

from datetime import datetime, timezone
import time
from pathlib import Path
from typing import Any


from booksys_core.network_admission import BudgetWait
from library.business_stages import BusinessStages
from library.catalog import Catalog
from library.metadata import Providers
from library.native_stages import NativeStages
from library.primitives import LibraryError
from library.nas_storage import require_nas
from library.publication import Capacity
from library.source_tasks import SourcePolicy
from library.stage_model import Task, StepResult
from library.stage_tasks import StageTasks

# A stage step must finish within its enclosing deadline; input size alone does not prove that it will fit.
TIMEOUTS = dict(collection=300, intake=1200, metadata=1200, copies=3600, routing=300)


class StageRuntime:
    def __init__(self, root: Path, reception: Path, collector: Path, *, providers: Providers,
                 client: Any, browser: Any, isolated: bool = False, capacity: Capacity | None = None):
        self.root, self.reception, self.collector = root, reception, collector
        self.catalog = Catalog(root/'state')
        store = self.catalog.backend()
        if store is None:
            raise LibraryError('Independent queues require selected PostgreSQL authority')
        self.tasks = StageTasks(store, policy=SourcePolicy(store.directory/'task-policy.yaml'))
        self.business = BusinessStages(self.catalog, reception, root/'library', providers, isolated=isolated, capacity=capacity)
        self.native = NativeStages(self.catalog, reception, root/'library', collector,
            client=client, browser=browser, snapshots=root/'snapshots')
        self.client, self.isolated = client, isolated
        self.failure_cursor = ''
        self._pass_revision: int | None = None      # catalogue revision when the running full pass started
        self._seen_revision = -1                    # revision up to which discovery has looked at the assets
        self._pending_keys: list[str] = []          # assets of an incremental pass not yet looked at
        self._done_at = 0.0                         # monotonic time the last full pass ended

    def discover_business(self, *, now: datetime) -> int:
        """Obligations of the business stages. A full pass every 5 minutes; between two, only the assets written since."""
        from library.operational_state import OperationalState
        state = OperationalState(self.tasks.store._database)
        after = state.discovery()
        store = self.tasks.store
        # Scan at most one hundred assets per pass and reuse changed IDs between five-minute full scans.
        if not after and time.monotonic() - self._done_at >= 300:
            # A full pass reads every asset (844 assets, 65 MB): it also covers the obligations that depend on runtime state.
            self._pass_revision = store.polled_revision()
            self._pending_keys = []
        elif not after:
            if not self._pending_keys:
                revision = store.polled_revision()
                if revision == self._seen_revision:
                    return 0
                self._pending_keys = store.changed_ids('assets', self._seen_revision)
                self._seen_revision = revision
            chunk, self._pending_keys = self._pending_keys[:100], self._pending_keys[100:]
            return self.business.discover(self.tasks, now=now, keys=chunk) if chunk else 0
        page = store.scan('assets', after=after, limit=100)
        count = self.business.discover(self.tasks, now=now, after=after, limit=100)
        finished = len(page) < 100
        state.save_discovery('' if finished else page[-1][0])
        if finished:
            self._done_at = time.monotonic()
            self._seen_revision = self._pass_revision if self._pass_revision is not None else -1
        return count

    def actor(self, queue: str) -> Any:
        business = queue in ('metadata', 'copies')
        def execute(task: Task, stamp: datetime) -> Any:
            if task.stage == 'metadata':
                self.business.providers.events.clear()
            try:
                return self.business.execute(task, now=stamp) if business else self.native.execute(task, now=stamp)
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
            return self.business.receipt(task) if business else self.native.receipt(task)
        from library.work_stages import work_stages
        if queue in work_stages():
            from library.work_actor import WorkActor
            from library.work_stages import work_items_for
            work = work_items_for(self.tasks.store._database)
            return WorkActor(self.tasks.store, work, queue, execute, ready=self.ready,
                             gate=lambda stamp: self.queue_gate(queue, stamp),
                             clock=lambda: datetime.now(timezone.utc),
                             receipt=receipt if queue in ('copies', 'routing') else None)
        raise LibraryError('Stage ' + queue + ' is not listed in BOOKSYS_WORK_STAGES: the stage_tasks actors were removed')

    def queue_gate(self, stage: str, stamp: datetime) -> str:
        """Waits that hold for every task of the stage: NAS and the shared pyLoad budget."""
        from booksys_core.errors import CollectionError
        if stage in ('collection', 'intake', 'copies', 'retirement'):
            try:
                require_nas()
            except LibraryError:
                return 'nas_unavailable'
        try:
            budget = getattr(self.client, 'admission', None) if stage in ('collection', 'routing', 'retirement') else None
            if budget is not None and budget.next_due(timestamp=stamp.timestamp()) > stamp.timestamp():
                return 'network_budget_wait'
        except (CollectionError, OSError):
            return 'network_authority_unavailable'
        return ''

    def ready(self, task: Task, stamp: datetime) -> str:
        from booksys_core.errors import CollectionError
        closed = self.queue_gate(task.stage, stamp)
        if closed:
            return closed
        try:
            budget = self.business.providers.admission if task.stage == 'metadata' else None
            if budget is not None and budget.next_due(timestamp=stamp.timestamp()) > stamp.timestamp():
                return 'network_budget_wait'
        except (CollectionError, OSError):
            return 'network_authority_unavailable'
        if task.stage in ('metadata', 'copies'):
            asset = self.tasks.store.get('assets', task.entity, media=False)
            if asset is None:
                return 'inputs_changed'
            from library.admission import eligible
            current = self.catalog.working_set('admit', {'assets': [task.entity]}, media=False)
            if not eligible(current, task.entity):
                return 'domain_prerequisites'
            if task.stage == 'copies':
                from library.admission import projection
                state = self.catalog.working_set('admit', {'assets': [task.entity]}, media=False)
                if projection(state, task.entity)['signature'] != task.input_revision:
                    return 'inputs_changed'
        return self.native.ready(task, now=stamp) if task.stage not in ('metadata', 'copies') else ''
