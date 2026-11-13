from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from contextlib import nullcontext

from booksys_core.diagnostics import REASONS, context, emit, exception_details, opaque
from library.locking import commit_guard, effect_guard, mutation_guard
from library.primitives import LibraryError
from booksys_core.network_admission import before_native_write
from library.stage_model import Change, Obligation, StepResult, Task, instant

from library.stage_tasks import StageTasks
from library.work_items import Item, WorkItems

# A prerequisite that only another stage can satisfy will not change within 30 s (same table as StageActor).
WAIT_BACKOFF = {'retirement_prerequisites': 600, 'native_package_unavailable': 300, 'routing_prerequisites': 120}
STAGE_OF = {'collection': 'collection', 'intake': 'local', 'metadata': 'identity',
            'copies': 'publication', 'routing': 'downloads', 'retirement': 'retirement'}
MAPPED = {'network_budget_wait': 'provider_wait', 'network_authority_unavailable': 'missing_dependency',
          'storage_wait': 'storage_busy'}


def task_view(item: Item, *, source_guard: str = '') -> Task:
    """The Task the business code expects. One attempt is one generation."""
    stamp = instant(item.ready_at)
    key = Obligation(item.stage, item.item, item.input_rev, item.method).key
    return Task(key, item.stage, item.item, item.input_rev, item.method, stamp, stamp, source=item.source,
                generation=item.attempts, attempts=item.attempts, status='active', source_guard=source_guard)


class WorkActor:
    def __init__(self, store: Any, work: WorkItems, queue: str, execute: Callable[[Task, datetime], str | StepResult], *,
                 ready: Callable[[Task, datetime], str] = lambda task, stamp: '',
                 gate: Callable[[datetime], str] = lambda stamp: '',
                 clock: Callable[[], datetime] | None = None,
                 receipt: Callable[[Task], tuple[str, str] | StepResult] | None = None):
        self.receipt = receipt
        self.store, self.work, self.queue = store, work, queue
        self.execute, self.ready, self.gate, self.clock = execute, ready, gate, clock
        self.worker = queue

    # -- reporting (same event codes as StageActor, so logs and health stay comparable) --
    def report(self, code: str, reason: str = 'none', *, item: Item | None = None, **fields: object) -> None:
        reason = MAPPED.get(reason, reason)
        refs: list[dict[str, str]] = []
        extra: dict[str, Any] = {}
        if item is not None:
            refs = [{'kind': 'task', 'id': hashlib.sha256((item.stage + item.item).encode()).hexdigest()}]
            if item.item.startswith('source-') and opaque(item.item[7:]):
                refs.append({'kind': 'source', 'id': item.item[7:]})
            extra = {'refs': refs, 'request_id': hashlib.sha256(item.item.encode()).hexdigest(), 'attempt': item.attempts}
        emit(service='collector' if self.queue == 'collection' else 'library', stage=STAGE_OF[self.queue], queue=self.queue,
             code=code, reason=reason if reason in REASONS else 'unknown', **extra, **fields)

    def _now(self, now: datetime) -> datetime:
        return self.clock() if self.clock is not None else now

    def _task_for(self, item: Item) -> Task:
        """The task of a claim. A source item is bound to its journal input (guard) and, for routing, to its native attempt.

        Raises LibraryError('source_input_missing_or_changed' | 'source_changed_rescan_required') when the input moved.
        """
        if not item.source:
            return task_view(item)
        with self.store._database.connection() as db:
            row = db.execute('SELECT guard FROM source_inputs WHERE source=%s AND entity=%s', (item.source, item.item)).fetchone()
            task = task_view(item, source_guard=row[0] if row else '')
            StageTasks._verify_source(db, task)
            task.native_attempt = StageTasks._bind_native_attempt(db, task)
        return task

    # -- the step --
    def step(self, *, now: datetime) -> str:
        try:
            with context(queue=self.queue):
                outcome = self._step(now=now)
        except Exception as exc:
            self.report('failed', **exception_details(exc))
            raise
        if outcome not in ('done', 'retry', 'defer', 'idle'):
            self.report('waiting', outcome, next_attempt=(now + timedelta(seconds=1)).isoformat())
        return outcome

    def _step(self, *, now: datetime) -> str:
        closed = self.gate(now)                              # NAS or the shared pyLoad budget: waits for the whole stage
        if closed:
            return closed
        item = self.work.claim(self.queue, self.worker, now=self._now(now))
        if item is None:
            return 'idle'
        try:
            if item.stage == 'routing' and item.source:
                from library.codec import decode
                from library.primitives import digest
                with self.store._database.connection(write=True) as db:
                    if db.execute('SELECT 1 FROM source_inputs WHERE source=%s AND entity=%s', (item.source, item.item)).fetchone() is None:
                        for source, payload in db.execute('SELECT source,payload FROM retirement_acquisitions'):
                            retired = decode(payload)
                            if (retired.get('phase') == 'retired' and 'source-' + digest(source) == item.item.split(':')[-1]
                                    and digest(retired.get('submission')) == item.input_rev):
                                return 'done' if self.work.finish_in(db, item, 'superseded', now=self._now(now)) else 'idle'
            task = self._task_for(item)
            if item.attempts > 1 and self.receipt is not None:
                settled = self._reconcile(item, task, now=self._now(now))
                if settled != 'absent':
                    return settled
            if item.attempts > self.work.max_attempts.get(item.stage, item.attempts):
                raise LibraryError('Attempts exhausted after claim expiry')
            reason = self.ready(task, self._now(now))
            if reason:
                delay = timedelta(seconds=WAIT_BACKOFF.get(reason, 30))
                self.work.wait(item, reason, self._now(now) + delay, now=self._now(now))
                self.report('waiting', reason, item=item, next_attempt=(self._now(now) + delay).isoformat())
                return reason
            return self._run(item, task, now=self._now(now))
        except Exception as exc:
            self.work.retry(item, f'{type(exc).__name__}: {str(exc)[:200]}', timedelta(seconds=300 if item.attempts == 1 else 900), now=self._now(now))
            self.report('failed', item=item, **exception_details(exc))
            return 'retry'

    def _acting(self, item: Item, task: Task | None = None) -> Any:
        """Before every external effect and every commit: this process must still hold the claim."""
        from contextlib import contextmanager

        @contextmanager
        def acting() -> Any:
            def check(db: Any) -> None:
                row = db.execute("SELECT input_rev FROM work_items WHERE stage=%s AND item=%s AND state='claimed' AND claimed_by=%s AND attempts=%s",
                                 (item.stage, item.item, item.claimed_by, item.attempts)).fetchone()
                if row is None or row[0] != item.input_rev:
                    raise LibraryError('Stale acting attempt')
                if task is not None and item.source:
                    StageTasks._verify_source(db, task)      # the journal input is still the one this attempt started from

            def before_effect() -> None:
                with self.store._database.connection() as db:
                    check(db)

            before_effect()
            tokens = (effect_guard.set(before_effect), commit_guard.set(check), before_native_write.set(before_effect),
                      mutation_guard.set(lambda change: nullcontext()))
            try:
                yield
            finally:
                mutation_guard.reset(tokens[3])
                before_native_write.reset(tokens[2])
                commit_guard.reset(tokens[1])
                effect_guard.reset(tokens[0])
        return acting()

    def _commit(self, item: Item, step: StepResult, outcome: str, now: datetime) -> None:
        """Result, end of claim and successors in one transaction (Store.commit with the work claim)."""
        prepared = step.prepared
        self.store.commit(prepared.change if prepared else Change(), expected_revision=prepared.revision if prepared else 0,
                          conflict=prepared.conflict if prepared else None, work_claim=(self.work, item), now=now,
                          outcome=outcome, due_at=step.due_at, obligations=step.successors,
                          source_progress=step.source_progress, completions=prepared.completions if prepared else None)
        # No commit_key: the store derives it from the content, so an identical retry is idempotent and a different result
        # after a given-back attempt is a different commit (a key built from the attempt number collided: 49 lost results).

    def _reconcile(self, item: Item, task: Task, *, now: datetime) -> str:
        assert self.receipt is not None
        found = self.receipt(task)
        step = found if isinstance(found, StepResult) else StepResult(found[0])
        outcome = step.outcome
        if outcome == 'absent':
            return 'absent'
        if outcome != 'done':
            raise LibraryError('External readback did not resolve the effect')
        with self._acting(item):
            self._commit(item, step, 'done', now)
        self.report('recovered', item=item, outcome=outcome)
        return 'done'

    def _run(self, item: Item, task: Task, *, now: datetime) -> str:
        started = time.monotonic()
        self.report('started', item=item)
        with context(refs=[{'kind': 'task', 'id': task.key}]), self._acting(item, task):
            result = self.execute(task, now)
            outcome = result.outcome if isinstance(result, StepResult) else result
            if outcome not in ('done', 'retry', 'defer'):
                raise LibraryError('Unknown business step outcome')
            self._commit(item, result if isinstance(result, StepResult) else StepResult(outcome), outcome, now)
        self.report('completed' if outcome == 'done' else 'waiting', 'none' if outcome == 'done' else 'unknown', item=item, outcome=outcome,
                    duration_ms=int((time.monotonic() - started) * 1000))
        return outcome
