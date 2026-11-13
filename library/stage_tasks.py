"""Durable domain obligations. Execution and resource admission belong to actors."""
from __future__ import annotations

from datetime import datetime, timedelta
import json
from library.postgres import DeadlineConnection, guards
from typing import Any, TYPE_CHECKING
from pathlib import Path

if TYPE_CHECKING:
    from library.source_tasks import SourcePolicy, Scan
    from library.store import Store, Change, PreparedResult, SourceProgress

from library.primitives import LibraryError
from library.stage_model import (Obligation as Obligation, Task as Task, Selection as Selection,
                                 instant, QUEUES)



class StageTasks:
    def __init__(self, store: Store, *, policy: SourcePolicy | None = None):
        self.store = store
        self.policy = policy
        self.changes = 0          # work items created, superseded or blocked by discover: a quiet sweep changed nothing

    @staticmethod
    def upgrade(store: Store, *, importing: bool=False) -> None:
        """Explicit additive extension. Never called by runtime construction."""
        with store._database.connection(write=True) as db:
            store.verify(db, importing=importing)
        from library.work_items import WorkItems
        WorkItems.upgrade(store._database)

    def _check(self, db: DeadlineConnection) -> None:
        self.store.verify(db)

    def discover(self, obligation: Obligation, *, now: datetime) -> Task:
        with self.store._database.connection(write=True) as db:
            self._check(db)
            return self._discover(db, obligation, now=now)

    def _discover(self, db: DeadlineConnection, obligation: Obligation, *, now: datetime, guarded: bool=False) -> Task:
        """Create the work item of an obligation, inside the caller's transaction (stage_tasks no longer exists)."""
        from library.work_stages import work_stages
        if obligation.stage not in work_stages():
            raise LibraryError('Stage ' + obligation.stage + ' is not listed in BOOKSYS_WORK_STAGES')
        if obligation.dependencies is not None:
            raise LibraryError('An obligation with conflict tokens is not supported on work_items')
        from library.work_items import WorkItems
        outcome = WorkItems(self.store._database).add_in(db, obligation.stage, obligation.entity, input_rev=obligation.input_revision,
            method=obligation.method, source=obligation.source, next_at=obligation.due_at, now=now, replay=obligation.replay,
            blocked=obligation.blocked)
        if outcome != 'same':
            self.changes += 1
        return Task(obligation.key, obligation.stage, obligation.entity, obligation.input_revision, obligation.method,
                    None, instant(obligation.due_at or now))

    @staticmethod
    def _bind_native_attempt(db: DeadlineConnection, task: Task) -> dict[str, Any] | None:
        if task.stage not in ('routing', 'retirement') or not task.source:
            return None
        from library.codec import decode
        from library.primitives import digest
        binding = StageTasks._source_binding(db, task)
        if binding is None:
            return None
        if binding['kind'] != 'submission':
            raise LibraryError('Invalid native effect attempt binding')
        source = binding['identity']
        row = db.execute('SELECT payload FROM collection_submissions WHERE source=%s', (source,)).fetchone()
        registration = db.execute('SELECT payload FROM source_registry WHERE id=%s', (task.source,)).fetchone()
        if row is None or registration is None:
            raise LibraryError('Native effect attempt binding missing')
        entry = decode(row[0])
        if digest(entry) != task.input_revision:
            raise LibraryError('Native effect attempt input changed')
        path = Path(decode(registration[0])['path']).parent
        journal = 'mirror-routing.yaml' if task.stage == 'routing' else 'package-retirement.yaml'
        return {'task': task.key, 'generation': task.generation, 'source': source, 'entry': entry,
                'journal': str((path/journal).absolute())}

    @staticmethod
    def _source_binding(db: DeadlineConnection, task: Task) -> dict[str,Any] | None:
        from library.operational_state import source_binding
        return source_binding(db,task)

    @staticmethod
    def _source_guards(db: DeadlineConnection, task: Task) -> set[str]:
        from library.operational_state import native_input_guards
        binding=StageTasks._source_binding(db,task)
        return native_input_guards(binding) if binding is not None else set()

    @staticmethod
    def _verify_source(db: DeadlineConnection, task: Task) -> None:
        if not task.source:return
        row=db.execute('SELECT input_revision,method,guard FROM source_inputs WHERE source=%s AND entity=%s',
                       (task.source,task.entity)).fetchone()
        if row!=(task.input_revision,task.method,task.source_guard):
            raise LibraryError('source_input_missing_or_changed')
        binding=StageTasks._source_binding(db,task)
        if binding is None:return  # Explicit generic prepared-input seam; runtime native views always bind.
        from library.operational_state import native_input
        active=task.status=='active'
        current=native_input(db,binding,task.stage,active=active)
        if current is None:raise LibraryError('source_changed_rescan_required')
        if active and binding['kind']=='category':
            valid=current==(binding['original'],task.method,task.source_guard)
        else:valid=current==(task.input_revision,task.method,task.source_guard)
        if not valid:raise LibraryError('source_changed_rescan_required')

    def scan(self, queue: str, journal: Path, *, now: datetime, kind: str = 'monitor') -> Scan:
        """Rescan a retained bounded journal view; no business step executes here.

        T8 owns projections from native collection/routing/retirement formats.
        The view preserves source input/method/deadline and explicit block reasons.
        """
        from library.stage_model import Scan, SOURCE_PAGE as PAGE
        from library.codec import decode
        if queue not in ('collection', 'routing', 'retirement'):
            raise LibraryError('Not a YAML-owned queue')
        if self.policy is None:
            return Scan(blocked='source_policy_missing')
        try:
            source, cursor = self.policy.register(queue, journal,kind=kind)
            view = self.policy.journal(journal, queue=queue)
            registration=view.pop('_source_registration',None)
            if registration is not None and decode(registration)['cursor']!=cursor:
                raise LibraryError('source_cursor_changed')
            keys = sorted(view['items'])
            pending = [key for key in keys if key > cursor]
            page = pending[:PAGE]
            continued = len(pending) > PAGE
            obligations = []
            for entity in page:
                record = view['items'][entity]
                obligations.append(Obligation(queue, source + ':' + entity, record['input_revision'], record['method'],
                    datetime.fromisoformat(record['due_at']) if record.get('due_at') else None,
                    record.get('blocked', ''), source=source))
            with self.store._database.connection(write=True) as db:
                self._check(db)
                guard_keys = {'source:'+source, 'queue:'+queue}
                for item in obligations:
                    guard_keys.update(('task:'+item.key, 'task-entity:'+item.stage+':'+item.entity))
                guards(db, guard_keys)
                self.policy._snapshot(db,source,view,instant(now),page[-1] if continued else '',expected=registration)
                for item in obligations:
                    self._discover(db,item,now=now,guarded=True)
            return Scan(len(page), continued)
        except (LibraryError, OSError, ValueError, TypeError, KeyError) as exc:
            return Scan(blocked=str(exc) if isinstance(exc, LibraryError) else 'source_journal_invalid')

    def scan_next(self, *, now: datetime) -> Scan:
        from library.stage_model import Scan
        if self.policy is None:
            return Scan(blocked='source_policy_missing')
        try:
            sources = self.policy.sources()
            if not sources:
                return Scan()
            from datetime import timedelta
            retry_before = instant(now-timedelta(seconds=60))
            due = {key: source for key, source in sources.items()
                   if source.get('cursor') or source.get('last_scan', '') <= retry_before}
            if not due:
                return Scan()
            ident, source = min(due.items(), key=lambda item: (item[1].get('last_scan', ''), item[0]))
            result = self.scan(source['queue'], Path(source['path']), now=now,kind=source.get('kind','monitor'))
            if result.blocked and result.blocked != 'source_cursor_changed':
                self.policy.retain_scan_failure(ident, source, instant(now))
            return result
        except LibraryError as exc:
            return Scan(blocked=str(exc))
