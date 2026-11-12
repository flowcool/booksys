"""One bounded domain step per supervised actor. systemd owns process restart."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
import fcntl
import hashlib
import time
from datetime import datetime, timedelta
from typing import Self, Any

from library.primitives import LibraryError, atomic_yaml
from library.decoder_ownership import DecoderOwnership
from library.stage_execution import QueueOwner, acting, effect_scopes
from library.stage_model import Task, StepResult
from library.postgres import CommitUnknown
from library.stage_resources import Resources, Demand
from library.stage_tasks import StageTasks
from booksys_core.errors import CollectionError
from library.locking import defer_contention
from booksys_core.diagnostics import emit, context, exception_details, REASONS, opaque


class StageActor:
    def __init__(self, tasks: StageTasks, resources: Resources, queue: str,
                 execute: Callable[[Task, datetime], str | StepResult], *, demand: Callable[[Task], Demand],
                 cleanup: Callable[[Task], bool], local_failure: Callable[[Task], str] | None = None,
                 scopes: Callable[[Task], list[str]] = lambda task: [],
                 ready: Callable[[Task, datetime], str] = lambda task, stamp: '',
                 receipt: Callable[[Task], tuple[str, str] | StepResult] | None = None):
        self.tasks, self.resources, self.queue = tasks, resources, queue
        self.execute, self.demand, self.cleanup = execute, demand, cleanup
        self.local_failure = local_failure
        self.scopes, self.ready = scopes, ready
        self.receipt = receipt
        self.recovery_cursor = ''
        self.selection_cursor: tuple[str, str] | None = None
        self.lookback_done = False
        from library.operational_state import OperationalState
        self.operational = OperationalState(tasks.store._database)
        self.owner = QueueOwner(tasks.store.directory, queue)

    def __enter__(self) -> Self:
        self.serial = (self.tasks.store.directory / 'library-cycle.lock').open('a')
        try:
            fcntl.flock(self.serial, fcntl.LOCK_SH | fcntl.LOCK_NB)
            self.owner.__enter__()
            self.selection_cursor, self.lookback_done = self.operational.selection(self.queue)
        except (OSError, LibraryError) as exc:
            self.owner.__exit__()
            self.serial.close()
            raise LibraryError('Legacy dispatcher or queue owner is active') from exc
        return self

    def __exit__(self, *args: object) -> None:
        self.owner.__exit__(*args)
        self.serial.close()

    @staticmethod
    def grant_key(task: Task, *, pending: bool = False) -> str:
        return task.key + ':' + str(task.generation + int(pending))

    def diagnostic_fields(self, task: Task) -> dict[str, Any]:
        refs = [{'kind': 'task', 'id': task.key}]
        # Only a canonical bare asset digest or explicitly typed retained entity
        # establishes a relationship. Never infer it from a timestamp or title.
        import re
        if re.fullmatch('[0-9a-f]{64}', task.entity):
            refs.append({'kind': 'asset', 'id': task.entity})
        elif task.entity.startswith('source-') and opaque(task.entity[7:]):
            refs.append({'kind': 'source', 'id': task.entity[7:]})
        elif ':' in task.entity:
            kind, ident = task.entity.split(':', 1)
            if kind in ('source', 'package', 'file', 'origin', 'asset', 'decision', 'operation') and opaque(ident):
                refs.append({'kind': kind, 'id': ident})
        return {'refs': refs, 'request_id': hashlib.sha256(task.entity.encode()).hexdigest(),
                'attempt': task.attempts}

    def report(self, code: str, reason: str = 'none', *, task: Task | None = None, **fields: object) -> None:
        mapped = {'network_budget_wait': 'provider_wait', 'network_authority_unavailable': 'missing_dependency',
                  'reader_setup_unavailable': 'missing_dependency', 'storage_wait': 'storage_busy',
                  'uncertain': 'uncertain_external_write', 'cleanup_required': 'cleanup_failed'}
        reason = mapped.get(reason, reason)
        emit(service='collector' if self.queue == 'collection' else 'retirement' if self.queue == 'retirement' else 'library',
             stage={'collection': 'collection', 'intake': 'local', 'ocr': 'local', 'metadata': 'identity',
                    'copies': 'publication', 'routing': 'downloads', 'retirement': 'retirement'}[self.queue],
             queue=self.queue, code=code, reason=reason if reason in REASONS else 'unknown',
             **(self.diagnostic_fields(task) if task else {}), **fields)

    def step(self, *, now: datetime, free_bytes: int, external_bytes: int | None) -> str:
        try:
            with context(queue=self.queue):
                outcome = self._boundary(now=now, free_bytes=free_bytes, external_bytes=external_bytes)
        except Exception as exc:
            self.report('failed', **exception_details(exc))
            raise
        if outcome not in ('done', 'retry', 'defer', 'continued'):
            self.report('waiting', outcome, next_attempt=(now+timedelta(seconds=1 if outcome not in
                ('uncertain', 'cleanup_required', 'reconciliation_required') else 30)).isoformat())
        return outcome

    def _boundary(self, *, now: datetime, free_bytes: int, external_bytes: int | None) -> str:
        with defer_contention(), (self.tasks.store.directory/'stage-checkpoint.lock').open('a') as boundary:
            try:
                fcntl.flock(boundary, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return 'recovery_wait'
            return self._step(now=now, free_bytes=free_bytes, external_bytes=external_bytes)
        return 'storage_wait'

    def _step(self, *, now: datetime, free_bytes: int, external_bytes: int | None) -> str:
        self.owner.check(self.queue)
        active = self.tasks.active(self.queue)
        if active is not None:
            if self.receipt is None:
                return 'reconciliation_required'
            return self.reconcile(active, now=now, receipt=self.receipt)
        if self.receipt is not None:
            parked = self.tasks.interrupted(self.queue, after=self.recovery_cursor, limit=1)
            self.recovery_cursor = parked[0].key if parked else ''
            if parked:
                recovered = self.reconcile(parked[0], now=now, receipt=self.receipt)
                if recovered != 'uncertain':
                    return recovered
        # A crash can happen after reserving capacity but before start, or after
        # committing the result but before releasing its grant. Only this queue
        # owner can prove its prior synchronous execution has stopped.
        for grant in self.resources.grants():
            key, generation = grant.rsplit(':', 1)
            retained = self.tasks.get(key)
            if retained is None:
                raise LibraryError('Grant lost its task authority')
            if retained.stage != self.queue:
                continue
            if int(generation) == retained.generation + 1 and retained.status != 'active':
                self.resources.release(grant, stopped=True, children_stopped=True)
            elif int(generation) <= retained.generation:
                children = DecoderOwnership(self.tasks.store.directory/'stage-children'/(grant+'.yaml'))
                # Registration is durable before the business attempt starts.
                # A missing registry and zero attempts prove a prestart grant.
                if not children.exists() and retained.attempts == 0:
                    self.resources.release(grant, stopped=True, children_stopped=True)
                    continue
                if not self.cleanup(retained) or not children.cleanup():
                    return 'cleanup_required'
                self.resources.release(grant, stopped=True, children_stopped=True)
            else:
                raise LibraryError('Grant generation requires explicit recovery')
        # Revisit one reservation without rewinding the bounded queue scan.
        oldest_waiter = self.resources.prune_waiters(self.tasks, self.queue, now=now)
        priority = oldest_waiter if (oldest_waiter is not None and self.selection_cursor is not None
            and (oldest_waiter.first_eligible_at or oldest_waiter.due_at, oldest_waiter.key) <= self.selection_cursor) else None
        if not self.lookback_done and self.selection_cursor is not None and self.tasks.earlier_due(self.queue, self.selection_cursor, now=now):
            self.selection_cursor = None
            self.lookback_done = True
        waits: dict[str, str] = {}
        for _ in range(256):
            preferred = priority is not None
            if preferred:
                task, priority = priority, None
            else:
                task = self.tasks.select(self.queue, now=now, resources=waits, after=self.selection_cursor).task
            if task is None:
                self.selection_cursor = None
                self.lookback_done = False
                self.operational.save_selection(self.queue,None,False)
                return next(iter(waits.values()), 'idle')
            if not preferred:
                self.selection_cursor = (task.first_eligible_at or task.due_at, task.key)
                self.operational.save_selection(self.queue,self.selection_cursor,self.lookback_done)
            grant = self.grant_key(task, pending=True)
            age = datetime.fromisoformat(task.first_eligible_at or task.due_at)
            reason = self.ready(task, now)
            if reason:
                self.resources.cancel_wait(grant)
                self.tasks.wait(task.key, task.generation, reason=reason, until=now+timedelta(seconds=30))
                self.report('waiting', reason, task=task, next_attempt=(now+timedelta(seconds=30)).isoformat())
                waits[task.key] = reason
                continue
            reason = self.resources.admit(grant, age=age, demand=self.demand(task),
                free_bytes=free_bytes, external_bytes=external_bytes, queue=self.queue)
            if reason:
                self.tasks.wait(task.key, task.generation, reason=reason, until=now+timedelta(seconds=1))
                self.report('waiting', reason, task=task, next_attempt=(now+timedelta(seconds=1)).isoformat())
                waits[task.key] = reason
                continue
            with ExitStack() as stack:
                try:
                    stack.enter_context(effect_scopes(self.tasks.store.directory, self.scopes(task)))
                except LibraryError:
                    self.resources.release(grant, stopped=True, children_stopped=True)
                    waits[task.key] = 'conflict_wait'
                    continue
                self.selection_cursor = None
                self.lookback_done = False
                self.operational.save_selection(self.queue,None,False)
                return self._run(task, grant, now=now)
        return 'continued'

    def _run(self, task: Task, grant: str, *, now: datetime) -> str:
        path = self.tasks.store.directory / 'stage-children' / (grant + '.yaml')
        decoders = DecoderOwnership(path)
        if decoders.exists():
            if decoders.names():
                raise LibraryError('Unstarted attempt has decoder evidence')
        else:
            decoders = DecoderOwnership.create(path)
        try:
            task = self.tasks.start(task.key, task.generation, now=now, resources=True)
        except LibraryError:
            # No business step or child has started on this path.
            self.resources.release(grant, stopped=True, children_stopped=True)
            raise
        started = time.monotonic()
        self.report('started', task=task)
        try:
            with context(**self.diagnostic_fields(task)), decoders.track(), acting(self.owner, self.tasks, task):
                result = self.execute(task, now)
                outcome = result.outcome if isinstance(result, StepResult) else result
                proof = result.proof if isinstance(result, StepResult) else ''
                if outcome not in ('done', 'retry', 'defer'):
                    raise LibraryError('Unknown business step outcome')
                finished = self.tasks.complete(task.key, task.generation, now=now,
                    outcome=outcome, proof=proof,
                    due_at=result.due_at if isinstance(result, StepResult) else None,
                    prepared=result.prepared if isinstance(result, StepResult) else None,
                    successors=result.successors if isinstance(result, StepResult) else None,
                    source_progress=result.source_progress if isinstance(result, StepResult) else None)
        except CommitUnknown as exc:
            self.report('failed', task=task, duration_ms=int((time.monotonic()-started)*1000),
                        outcome='unresolved', **exception_details(exc))
            return 'uncertain'
        except (CollectionError, LibraryError, OSError, ValueError) as exc:
            self.report('failed', task=task, duration_ms=int((time.monotonic()-started)*1000),
                        outcome='uncertain', **exception_details(exc))
            self.tasks.finish(task.key, task.generation, now=now, outcome='uncertain')
            if task.stage == 'copies' and self.local_failure is not None:
                scope_proof = self.local_failure(task)
                if scope_proof and self.cleanup(task) and decoders.cleanup():
                    self.tasks.park_uncertain(task.key, task.generation, proof=scope_proof, owner_stopped=True)
                    self.resources.release(grant, stopped=True, children_stopped=True)
                    return 'blocked_local'
            # Even a process exit cannot prove an external decoder stopped.
            # Retain the grant and canonical attempt for exact reconciliation.
            return 'uncertain'
        except Exception as exc:
            self.report('failed', task=task, duration_ms=int((time.monotonic()-started)*1000), **exception_details(exc))
            raise
        self.report('completed' if outcome == 'done' else 'waiting',
                    'none' if outcome == 'done' else 'unknown',
                    task=task, outcome=outcome, duration_ms=int((time.monotonic()-started)*1000),
                    **({'next_attempt': finished.due_at} if outcome in ('retry', 'defer') else {}))
        if not self.cleanup(task) or not decoders.cleanup():
            return 'cleanup_required'
        self.resources.release(grant, stopped=True, children_stopped=True)
        return outcome

    def reconcile(self, task: Task, *, now: datetime, receipt: Callable[[Task], tuple[str, str] | StepResult]) -> str:
        self.owner.check(task.stage)
        decoders = DecoderOwnership(self.tasks.store.directory / 'stage-children' / (self.grant_key(task) + '.yaml'))
        if not self.cleanup(task) or not decoders.cleanup():
            return 'cleanup_required'
        current = self.tasks.get(task.key)
        if current is not None and current.status == 'done' and current.generation == task.generation:
            self.resources.release(self.grant_key(task), stopped=True, children_stopped=True)
            return 'done'
        source_progress = None
        prepared=None
        try:
            with effect_scopes(self.tasks.store.directory, self.scopes(task)):
                result = receipt(task)
                if isinstance(result,StepResult):
                    outcome,proof,source_progress = result.outcome,result.proof,result.source_progress
                    prepared=result.prepared
                else:
                    outcome,proof = result
        except (CollectionError, LibraryError, OSError, ValueError):
            outcome, proof = 'uncertain', ''
        if outcome == 'uncertain':
            if task.stage == 'copies' and self.local_failure is not None:
                local = self.local_failure(task)
                current = self.tasks.get(task.key)
                if local and current is not None and current.status == 'active':
                    self.tasks.finish(task.key, task.generation, now=now, outcome='uncertain')
                    self.tasks.park_uncertain(task.key, task.generation, proof=local, owner_stopped=True)
                    self.resources.release(self.grant_key(task), stopped=True, children_stopped=True)
            return 'uncertain'
        self.tasks.reconcile(task.key, task.generation, now=now, owner_stopped=True,
                             outcome=outcome, proof=proof,source_progress=source_progress,prepared=prepared)
        self.resources.release(self.grant_key(task), stopped=True, children_stopped=True)
        self.report('recovered', task=task, outcome=outcome)
        return outcome
