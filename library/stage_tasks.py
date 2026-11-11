"""Durable domain obligations. Execution and resource admission belong to actors."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from contextlib import contextmanager
from typing import Iterator
from datetime import datetime, timedelta
import json
from library.postgres import DeadlineConnection, guards
import time
from typing import Any, TYPE_CHECKING
from pathlib import Path

if TYPE_CHECKING:
    from library.source_tasks import SourcePolicy, Scan
    from library.store import Store, Change, PreparedResult, SourceProgress

from library.primitives import LibraryError
from library.stage_model import (Obligation as Obligation, Task as Task, Selection as Selection,
                                 instant, QUEUES)



@dataclass
class _TaskWrite:
    db: DeadlineConnection
    replayed: bool
    result: Any = None

    def retain(self, task: Task) -> Task:
        self.result = asdict(task)
        return task


class StageTasks:
    def __init__(self, store: Store, *, policy: SourcePolicy | None = None):
        self.store = store
        self.policy = policy

    @contextmanager
    def _write(self, operation: str, request: dict[str, Any], *, identity: str | None = None) -> Iterator[_TaskWrite]:
        """Concrete task receipts fence a delayed claim or control transition."""
        from library.codec import encode, decode
        import hashlib
        value = hashlib.sha256(encode([operation,request]).encode()).hexdigest()
        key = identity or 'task-control:' + operation + ':' + value
        from library.locking import acquisition_deadline, bounded_acquisition
        from library.postgres import CommitUnknown
        stop = min(acquisition_deadline.get() or float('inf'),time.monotonic()+10)
        with bounded_acquisition(stop):
            try:
                self.store._register(key,value)
            except CommitUnknown:
                if self.store.outcome(key,input_digest=value)['state'] != 'committed':
                    raise
                # Continue to the receipt read and yield, including an already
                # committed duplicate whose registration acknowledgement was lost.
            try:
                with self.store._database.connection(write=True) as db:
                    row = db.execute('SELECT digest,state,receipt FROM commit_receipts WHERE key=%s FOR UPDATE',(key,)).fetchone()
                    if not row or row[0] != value:
                        raise LibraryError('Commit key input mismatch')
                    if row[1] == 'aborted':
                        raise LibraryError('Commit key was fenced aborted')
                    tx = _TaskWrite(db,row[1] == 'committed',decode(row[2]) if row[2] is not None else None)
                    if not tx.replayed and request.get('key'):
                        current = self._get(db,request['key'])
                        if current:
                            keys = {'queue:'+current.stage,'task:'+current.key,
                                    'task-entity:'+current.stage+':'+current.entity}
                            if current.source:
                                keys.add('source:'+current.source)
                                keys.update(self._source_guards(db,current))
                            progress=request.get('source_progress')
                            if progress:
                                if operation == 'reconcile' and current.superseded_by:
                                    keys.add('native-pyload-effect')
                                from library.operational_state import completion_guards,OperationalCompletion
                                for completion in progress.get('completions',[]):
                                    keys.update(completion_guards(OperationalCompletion(**completion)))
                            guards(db,keys)
                    yield tx
                    if not tx.replayed:
                        db.execute("UPDATE commit_receipts SET state='committed',receipt=%s WHERE key=%s",(encode(tx.result),key))

            except CommitUnknown:
                if self.store.outcome(key,input_digest=value)['state'] != 'committed':
                    raise

    @staticmethod
    def upgrade(store: Store, *, importing: bool=False) -> None:
        """Explicit additive extension. Never called by runtime construction."""
        with store._database.connection( write=True) as db:
            store.check(db, importing=importing)
            db.execute('CREATE TABLE IF NOT EXISTS stage_schema(singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL)')
            row = db.execute('SELECT version FROM stage_schema').fetchone()
            if row and row[0] != 1:
                raise LibraryError('Unknown task schema')
            db.execute('INSERT INTO stage_schema VALUES (1,1) ON CONFLICT DO NOTHING')
            db.execute('CREATE TABLE IF NOT EXISTS stage_tasks(key TEXT COLLATE \"C\" PRIMARY KEY, stage TEXT COLLATE \"C\" NOT NULL, '
                       'status TEXT COLLATE \"C\" NOT NULL, age TEXT COLLATE \"C\", due TEXT COLLATE \"C\" NOT NULL, blocked TEXT COLLATE \"C\" NOT NULL, payload TEXT COLLATE \"C\" NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS task_order ON stage_tasks(stage,status,blocked,due,age,key)')
            db.execute("CREATE INDEX IF NOT EXISTS task_entity ON stage_tasks(stage,((payload::jsonb->>'entity')))")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS active_queue ON stage_tasks(stage) WHERE status='active'")

    def _check(self, db: DeadlineConnection) -> None:
        self.store.check(db)
        if db.execute('SELECT version FROM stage_schema WHERE singleton=1').fetchone() != (1,):
            raise LibraryError('Task schema requires explicit upgrade')

    def _put(self, db: DeadlineConnection, task: Task) -> None:
        # Every queue, including source-derived tasks, has one SQL authority.
        task.policy_sequence += 1
        self._index(db, task)

    @staticmethod
    def _index(db: DeadlineConnection, task: Task) -> None:
        db.execute('INSERT INTO stage_tasks VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(key) DO UPDATE SET '
                   'status=excluded.status,age=excluded.age,due=excluded.due,blocked=excluded.blocked,payload=excluded.payload',
                   (task.key, task.stage, task.status, task.first_eligible_at, task.due_at, task.blocked,
                    json.dumps(asdict(task), sort_keys=True, separators=(',', ':'), ensure_ascii=False)))

    def _get(self, db: DeadlineConnection, key: str) -> Task | None:
        row = db.execute('SELECT payload FROM stage_tasks WHERE key=%s', (key,)).fetchone()
        return Task(**json.loads(row[0])) if row else None

    def queue(self, key: str) -> str | None:
        """Read immutable queue ownership without resolving a source journal."""
        with self.store._database.connection() as db:
            self._check(db)
            row = db.execute('SELECT stage FROM stage_tasks WHERE key=%s', (key,)).fetchone()
            return str(row[0]) if row else None

    def _waiter_tasks(self, keys: set[str], queue: str) -> dict[str, Task | None]:
        """Read retained waiter owners together without resolving other queues."""
        ordered = sorted(keys)
        result: dict[str, Task | None] = {}
        if ordered:
            with self.store._database.connection() as db:
                self._check(db)
                for offset in range(0, len(ordered), 256):
                    rows = db.execute('SELECT key,stage,CASE WHEN stage=%s THEN payload END '
                                      'FROM stage_tasks WHERE key=ANY(%s::text[])',
                                      (queue,ordered[offset:offset+256]))
                    for key, stage, payload in rows:
                        result[key] = Task(**json.loads(payload)) if stage == queue else None
        return result

    def get(self, key: str) -> Task | None:
        with self.store._database.connection() as db:
            self._check(db)
            return self._get(db, key)

    def discover(self, obligation: Obligation, *, now: datetime) -> Task:
        with self._write('discover', {'obligation':asdict(obligation),'now':now}) as tx:
            db = tx.db
            if tx.replayed:
                return Task(**tx.result)
            self._check(db)
            task = self._discover(db, obligation, now=now)
            return tx.retain(task)

    def _discover(self, db: DeadlineConnection, obligation: Obligation, *, now: datetime, guarded: bool=False) -> Task:
        stamp = instant(now)
        due = instant(obligation.due_at or now)
        keys = {'queue:'+obligation.stage,'task:'+obligation.key, 'task-entity:'+obligation.stage+':'+obligation.entity}
        if obligation.source:
            keys.add('source:'+obligation.source)
        if not guarded:guards(db,keys)
        task = self._get(db, obligation.key)
        if task is None:
            for (payload,) in db.execute("SELECT payload FROM stage_tasks WHERE stage=%s AND (payload::jsonb->>'entity')=%s",
                                        (obligation.stage, obligation.entity)).fetchall():
                previous = Task(**json.loads(payload))
                if previous.entity == obligation.entity and previous.input_revision != obligation.input_revision and previous.status in ('pending', 'active', 'blocked'):
                    previous.superseded_by = obligation.key
                    if previous.status != 'active' and previous.blocked != 'effect_uncertain':
                        previous.status = 'superseded'
                    self._put(db, previous)
            task = Task(obligation.key, obligation.stage, obligation.entity, obligation.input_revision,
                        obligation.method, None, due, obligation.blocked, obligation.dependencies, obligation.source)
        elif (task.stage, task.entity, task.input_revision) != (obligation.stage, obligation.entity, obligation.input_revision):
            raise LibraryError('Task key collision')
        if task.blocked == 'effect_uncertain':
            return task
        if task.source and task.status != 'active':
            source = db.execute('SELECT input_revision,method,guard FROM source_inputs '
                                'WHERE source=%s AND entity=%s', (task.source,task.entity)).fetchone()
            if source is None or source[:2] != (task.input_revision,obligation.method):
                raise LibraryError('source_input_missing_or_changed')
            task.source_guard = source[2]
        if task.status != 'active':
            task.due_at = max(task.due_at, due)
            if task.blocked not in ('attempts_exhausted', 'prestart_exhausted'):
                task.blocked = obligation.blocked
            if task.method != obligation.method and task.status != 'superseded' and (task.status != 'done' or obligation.replay):
                task.method = obligation.method
                task.generation += 1
                task.episode += 1
                task.attempts = task.prestart_failures = 0
                task.status = 'pending'
                task.blocked = obligation.blocked
                task.completed_at = None
            if task.first_eligible_at is None and not task.blocked and task.due_at <= stamp:
                task.first_eligible_at = stamp
        self._put(db, task)
        return task

    def dependency_failed(self, key: str, generation: int, *, dependency: str, at: float) -> None:
        if dependency != 'reader-engine' or at < 0:
            raise LibraryError('Unknown task dependency')
        with self._write('dependency_failed', {'key':key,'generation':generation,'dependency':dependency,'at':at}) as tx:
            db = tx.db
            if tx.replayed:
                return
            self._check(db)
            guards(db, {'task:'+key})
            task = self._get(db, key)
            if task is None or task.status != 'active' or task.generation != generation:
                raise LibraryError('Stale dependency failure')
            task.technical_dependency, task.dependency_failed_at = dependency, at
            self._put(db, task)

    def technical_failures(self, *, after: str = '', limit: int = 100) -> list[Task]:
        if not 1 <= limit <= 256:
            raise LibraryError('Invalid dependency recovery page')
        with self.store._database.connection() as db:
            self._check(db)
            rows = db.execute("SELECT payload FROM stage_tasks WHERE status='blocked' AND blocked='attempts_exhausted' "
                "AND (payload::jsonb->>'technical_dependency')='reader-engine' AND key>%s ORDER BY key LIMIT %s", (after, limit)).fetchall()
            return [Task(**json.loads(row[0])) for row in rows]

    def recover_dependency(self, key: str, *, proof: str, now: datetime) -> Task:
        """Consume one observed dependency recovery, never a restart identifier.

        Caller must bind proof to a failed-to-ready transition. Manual, remote
        and uncertain-effect blocks cannot be cleared through this interface.
        Due times and first-eligible age are preserved.
        """
        if not proof or len(proof) > 256:
            raise LibraryError('Dependency recovery requires bounded readiness proof')
        with self._write('recover_dependency', {'key':key,'proof':proof,'now':now}) as tx:
            db = tx.db
            if tx.replayed:
                return self._get(db,key) or Task(**tx.result)
            self._check(db)
            guards(db, {'task:'+key})
            task = self._get(db, key)
            if task is None:
                raise LibraryError('Recovery task is missing')
            if (task.recovery_version != proof and task.status == 'blocked'
                    and task.blocked in ('attempts_exhausted', 'prestart_exhausted')):
                task.recovery_version = proof
                task.episode += 1
                task.generation += 1
                task.attempts = task.prestart_failures = 0
                task.status, task.blocked = 'pending', ''
                self._put(db, task)
            return tx.retain(task)

    def wait(self, key: str, generation: int, *, reason: str, until: datetime) -> None:
        """Record a bounded prerequisite observation without consuming an attempt."""
        with self._write('wait', {'key':key,'generation':generation,'reason':reason,'until':until}) as tx:
            db = tx.db
            if tx.replayed:
                return
            self._check(db)
            guards(db, {'task:'+key})
            task = self._get(db, key)
            if task is not None and task.status == 'pending' and task.generation == generation:
                task.wait_reason, task.wait_until = reason, instant(until)
                self._put(db, task)

    def select(self, queue: str, *, now: datetime, resources: dict[str, str] | None = None,
               after: tuple[str, str] | None = None) -> Selection:
        stamp = instant(now)
        if queue not in QUEUES:
            raise LibraryError('Unknown queue')
        result = Selection()
        # Each native collection binding requires several canonical reads, both
        # before and under writer guards. Keep that work inside the existing
        # acquisition deadline; the actor already retains its selection cursor.
        window = 8 if queue == 'collection' else 256
        resources = resources or {}
        if len(resources) > 10000:
            raise LibraryError('Resource view exceeds selection bound')
        excluded = sorted(resources)
        clause = ' AND key NOT IN (' + ','.join('%s' for _ in excluded) + ')' if excluded else ''
        clause += " AND ((payload::jsonb->>'wait_until') IS NULL OR (payload::jsonb->>'wait_until')<=%s)"
        continuation = ' AND (coalesce(age,%s),key)>(%s,%s)' if after is not None else ''
        continuation_args = (stamp, *after) if after is not None else ()
        observation_sql=f"SELECT payload FROM stage_tasks WHERE stage=%s AND status IN ('pending','blocked') ORDER BY coalesce(age,%s),key LIMIT {window}"
        candidate_sql=("SELECT payload FROM stage_tasks WHERE stage=%s AND status='pending' AND blocked='' AND due<=%s"
            +clause+continuation+f' ORDER BY coalesce(age,%s),key LIMIT {window+1}')
        candidate_args=(queue,stamp,*excluded,stamp,*continuation_args,stamp)
        with self.store._database.connection() as view:
            observed=[Task(**json.loads(row[0])) for row in view.execute(observation_sql,(queue,stamp))]
            candidates=[Task(**json.loads(row[0])) for row in view.execute(candidate_sql,candidate_args)]
            source_tasks=list({task.key:task for task in observed+candidates[:window]}.values())
            policy_row = view.execute("SELECT md5(coalesce(string_agg(payload,'' ORDER BY key),'')) FROM stage_tasks WHERE stage=%s",(queue,)).fetchone()
            assert policy_row is not None
            policy_view = policy_row[0]
        source_signature='';source_keys:set[str]=set();policy=self.policy
        for candidate in source_tasks:
            source_keys.update(('task:'+candidate.key,'task-entity:'+candidate.stage+':'+candidate.entity))
            if candidate.source:source_keys.add('source:'+candidate.source)
        if any(task.source for task in source_tasks):
            from library.operational_state import native_validation
            if policy is not None:
                source_signature,native_keys=policy.validation_snapshot(source_tasks)
            else:
                with self.store._database.connection() as snapshot:
                    source_signature,native_keys=native_validation(tasks=source_tasks,db=snapshot)
            source_keys.update(native_keys)
        with self._write('select', {'queue':queue,'now':now,'resources':resources,'after':list(after) if after else None,'policy_view':policy_view,'native_view':source_signature}) as tx:
            db = tx.db
            guards(db,{'queue:'+queue,*source_keys})
            current_policy = db.execute("SELECT md5(coalesce(string_agg(payload,'' ORDER BY key),'')) FROM stage_tasks WHERE stage=%s",(queue,)).fetchone()
            if current_policy is None or current_policy[0] != policy_view:
                raise LibraryError('storage_busy')
            if source_signature and (policy.validation_snapshot(source_tasks,db=db) if policy is not None
                                     else native_validation(tasks=source_tasks,db=db))[0]!=source_signature:
                raise LibraryError('storage_busy')
            if tx.replayed:
                cached = dict(tx.result)
                cached['task'] = Task(**cached['task']) if cached['task'] else None
                if cached['task'] is not None:
                    self._verify_source(db,cached['task'])
                return Selection(**cached)
            self._check(db)
            self._sync_sources(db, queue)
            result.eligible = db.execute("SELECT count(*) FROM stage_tasks WHERE stage=%s AND status='pending' AND blocked='' AND due<=%s", (queue, stamp)).fetchall()[0][0]
            # The initial guard set covers both bounded windows, including continuations.
            observed_by_key={task.key:task for task in observed}
            for task in observed:
                try:
                    self._verify_source(db,task)
                except LibraryError as exc:
                    if not task.blocked and task.status == 'pending' and task.due_at <= stamp:
                        result.eligible -= 1
                    task.blocked=str(exc)
                    self._put(db,task)
                reason = task.blocked or ('deferred' if task.due_at > stamp else resources.get(task.key, ''))
                if reason:
                    result.waits[task.key] = reason
                if not task.blocked and task.due_at <= stamp and task.first_eligible_at is None:
                    task.first_eligible_at = stamp
                    self._put(db, task)
            deadline = time.monotonic() + .5
            for candidate in candidates[:window]:
                if time.monotonic() > deadline:
                    result.continued = True
                    break
                task=observed_by_key.get(candidate.key,candidate)
                if task.blocked:continue
                if task.key not in observed_by_key:
                    try:self._verify_source(db,task)
                    except LibraryError as exc:
                        task.blocked=str(exc);self._put(db,task)
                        result.waits[task.key]=task.blocked;result.eligible-=1
                        continue
                if self._uncertain_entity(db, task):
                    result.waits[task.key] = 'entity_effect_uncertain'
                    continue
                if task.dependencies is not None:
                    from library.conflicts import verify
                    try:
                        verify(self.store, db, task.dependencies)
                    except LibraryError:
                        task.blocked = 'inputs_changed'
                        self._put(db, task)
                        result.waits[task.key] = task.blocked
                        result.eligible -= 1
                        continue
                if task.first_eligible_at is None:
                    task.first_eligible_at = stamp
                    self._put(db, task)
                result.task = task
                break
            else:
                result.continued = len(candidates) > window
            tx.result = asdict(result)
        return result

    @staticmethod
    def _uncertain_entity(db: DeadlineConnection, task: Task) -> bool:
        return db.execute("SELECT 1 FROM stage_tasks WHERE stage=%s AND blocked='effect_uncertain' "
            "AND (payload::jsonb->>'entity')=%s AND key<>%s LIMIT 1", (task.stage, task.entity, task.key)).fetchone() is not None

    def park_uncertain(self, key: str, generation: int, *, proof: str, owner_stopped: bool) -> None:
        """Release queue occupancy only after exact local scope and child proof.

        The source journal owns reconciliation. Its uncertain entity stays fenced.
        Unknown copy ownership and native uncertainty must never use this path.
        """
        if not proof or not owner_stopped:
            raise LibraryError('Local scope and acting-process cleanup required')
        with self._write('park_uncertain', {'key':key,'generation':generation,'proof':proof,'owner_stopped':owner_stopped}) as tx:
            db = tx.db
            if tx.replayed:
                return
            self._check(db)
            guards(db, {'task:'+key})
            task = self._get(db, key)
            if task is None or task.stage != 'copies' or task.generation != generation or task.status != 'active' or task.blocked != 'effect_uncertain':
                raise LibraryError('Only a known local copy uncertainty can be parked')
            task.status = 'blocked'
            task.reconciliation = proof
            self._put(db, task)

    def active(self, queue: str) -> Task | None:
        with self.store._database.connection() as db:
            self._check(db)
            row = db.execute("SELECT key FROM stage_tasks WHERE stage=%s AND status='active'", (queue,)).fetchone()
            return self._get(db, row[0]) if row else None

    def earlier_due(self, queue: str, before: tuple[str, str], *, now: datetime) -> bool:
        """An expired older prerequisite must be reconsidered before a cursor."""
        stamp = instant(now)
        with self.store._database.connection() as db:
            self._check(db)
            return db.execute("SELECT 1 FROM stage_tasks WHERE stage=%s AND status='pending' AND blocked='' AND due<=%s "
                "AND ((payload::jsonb->>'wait_until') IS NULL OR (payload::jsonb->>'wait_until')<=%s) "
                "AND (coalesce(age,%s),key)<=(%s,%s) LIMIT 1", (queue, stamp, stamp, stamp, *before)).fetchone() is not None

    def interrupted(self, queue: str, *, after: str = '', limit: int = 32) -> list[Task]:
        if queue not in QUEUES or not 1 <= limit <= 256:
            raise LibraryError('Invalid interrupted-task page')
        with self.store._database.connection() as db:
            self._check(db)
            rows = db.execute("SELECT key FROM stage_tasks WHERE stage=%s AND status='blocked' "
                              "AND blocked='effect_uncertain' AND key>%s ORDER BY key LIMIT %s",
                              (queue, after, limit)).fetchall()
            return [task for (key,) in rows if (task := self._get(db, key)) is not None]

    def start(self, key: str, generation: int, *, now: datetime, resources: bool) -> Task:
        """Caller owns queue exclusion and a complete resource grant (T6/T7)."""
        with self._write('start', {'key':key,'generation':generation,'now':now,'resources':resources}, identity=f'task-start:{key}:{generation}') as tx:
            db = tx.db
            if tx.replayed:
                return Task(**tx.result)
            self._check(db)
            prepared = self._get(db,key)
            if prepared is None:
                raise LibraryError('Task cannot start')
            keys = {'queue:'+prepared.stage, 'task:'+key, 'task-entity:'+prepared.stage+':'+prepared.entity}
            if prepared.source:
                keys.add('source:'+prepared.source)
            guards(db, keys)
            task = self._get(db, key)
            if (task is None or task.status != 'pending' or task.generation != generation
                    or task.blocked or task.superseded_by or task.due_at > instant(now) or not resources
                    or task.wait_until is not None and task.wait_until > instant(now)
                    or task.attempts >= 3 or task.prestart_failures >= 3):
                raise LibraryError('Task cannot start')
            if self._uncertain_entity(db, task):
                raise LibraryError('Related effect needs reconciliation')
            self._verify_source(db,task)
            if db.execute("SELECT 1 FROM stage_tasks WHERE stage=%s AND status='active'", (task.stage,)).fetchone():
                raise LibraryError('Queue already has an active attempt')
            if task.dependencies is not None:
                from library.conflicts import verify
                verify(self.store, db, task.dependencies)
            task.generation += 1
            task.attempts += 1
            task.status = 'active'
            task.native_attempt = self._bind_native_attempt(db, task)
            task.wait_reason, task.wait_until = '', None
            if task.first_eligible_at is None:
                task.first_eligible_at = instant(now)
            self._put(db, task)
            return tx.retain(task)

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
        if active and binding['kind']=='backfill':
            valid=current[0]==binding['original']
        elif active and binding['kind']=='category':
            valid=current==(binding['original'],task.method,task.source_guard)
        else:valid=current==(task.input_revision,task.method,task.source_guard)
        if not valid:raise LibraryError('source_changed_rescan_required')

    def _finish(self, db: DeadlineConnection, key: str, generation: int, *, now: datetime,
                outcome: str, due_at: datetime | None = None, proof: str = '', reconciled: bool = False,
                source_progress: SourceProgress | None = None) -> Task:
        guards(db, {'task:'+key})
        task = self._get(db, key)
        if reconciled and task is not None and task.stage=='copies' and task.status=='blocked' and task.blocked=='effect_uncertain' and task.generation==generation:
            if db.execute("SELECT 1 FROM stage_tasks WHERE stage=%s AND status='active'",(task.stage,)).fetchone():
                raise LibraryError('Queue has another active attempt')
            task.status='active';task.blocked=''
        if task is None or task.status != 'active' or task.generation != generation:
            raise LibraryError('Stale or inactive task result')
        if outcome == 'done' and task.superseded_by and not reconciled:
            raise LibraryError('Result conflict: input superseded')
        if outcome not in ('done', 'retry', 'uncertain', 'defer'):
            raise LibraryError('Unknown task outcome')
        if task.source and outcome == 'done':
            if (source_progress is None or (source_progress.source,source_progress.entity,
                source_progress.input_revision,source_progress.method,source_progress.guard) !=
                (task.source,task.entity,task.input_revision,task.method,task.source_guard)):
                raise LibraryError('Source completion requires exact canonical progress')
            from library.codec import encode
            from library.operational_state import apply_completion, verify_superseded_native_readback
            if reconciled and task.superseded_by:
                verify_superseded_native_readback(db, task, proof, source_progress.completions)
            else:
                self._verify_source(db,task)
            for completion in source_progress.completions:
                apply_completion(db,completion)
            if not (reconciled and task.superseded_by):
                db.execute('UPDATE source_inputs SET progress=%s WHERE source=%s AND entity=%s',
                           (encode(source_progress.result),task.source,task.entity))
        if task.dependencies is not None and outcome == 'done' and not reconciled:
            from library.conflicts import verify
            verify(self.store, db, task.dependencies)
        if outcome == 'done':
            task.status = 'superseded' if task.superseded_by else 'done'
            task.blocked = ''
            task.completed_at = instant(now)
        elif outcome == 'defer':
            task.status = 'superseded' if task.superseded_by else 'pending'
            task.attempts = max(0, task.attempts-1)
            task.due_at = max(task.due_at, instant(due_at or now+timedelta(seconds=30)))
        elif outcome == 'uncertain':
            task.blocked = 'effect_uncertain'
            # Retain the active attempt: method changes cannot bypass recovery.
        else:
            task.status = 'superseded' if task.superseded_by else 'pending' if task.attempts < 3 else 'blocked'
            task.blocked = '' if task.attempts < 3 else 'attempts_exhausted'
            delay = 300 if task.attempts == 1 else 900
            task.due_at = max(task.due_at, instant(now + timedelta(seconds=delay)), instant(due_at or now))
        if proof:
            task.reconciliation = proof
        self._put(db, task)
        return task

    def finish(self, key: str, generation: int, *, now: datetime, outcome: str = 'done',
               due_at: datetime | None = None, change: Change | None = None,
               conflict: dict[str, Any] | None = None, proof: str = '') -> Task:
        from library.stage_model import PreparedResult
        prepared = PreparedResult(change, conflict, 0) if change is not None else None
        return self.complete(key, generation, now=now, outcome=outcome,
                             due_at=due_at, proof=proof, prepared=prepared)

    def complete(self, key: str, generation: int, *, now: datetime, outcome: str = 'done',
                 due_at: datetime | None = None, proof: str = '',
                 prepared: PreparedResult | None = None,
                 successors: list[Obligation] | None = None,
                 source_progress: SourceProgress | None = None) -> Task:
        """Commit the prepared result, canonical task and successors together."""
        from library.stage_model import Change
        from library.postgres import CommitUnknown
        change = prepared.change if prepared else Change()
        request: dict[str, Any] = dict(expected_revision=prepared.revision if prepared else 0,
                       conflict=prepared.conflict if prepared else None, attempt=(key, generation),
                       now=now, outcome=outcome, due_at=due_at, proof=proof, obligations=successors,
                       source_progress=source_progress,completions=prepared.completions if prepared else None)
        commit_key = f'task:{key}:{generation}:complete:{outcome}'
        identity = self.store.transition_digest(change, **request)
        try:
            self.store.commit(change, **request, commit_key=commit_key)
        except CommitUnknown:
            if self.store.outcome(commit_key, input_digest=identity)['state'] != 'committed':
                raise
        try:
            result = self.get(key)
        except LibraryError:
            # The transition may already be committed. Retain its logical key;
            # never emit a second failure transition after an unavailable readback.
            raise CommitUnknown('commit_unresolved') from None
        if result is None:
            raise LibraryError('Committed task disappeared')
        return result

    def reconcile(self, key: str, generation: int, *, now: datetime, owner_stopped: bool,
                  outcome: str, proof: str, source_progress: SourceProgress | None = None, prepared: PreparedResult | None=None) -> Task:
        """Actual adapter must prove exclusion and exact effect outcome before retry.

        No elapsed lease or supervisor timeout substitutes for that proof.
        Uncertain effects retain their active reference and exclude another start.
        """
        if not owner_stopped or not proof or outcome not in ('absent', 'done', 'uncertain'):
            raise LibraryError('Reconciliation needs owner and canonical outcome proof')
        if prepared is not None:
            if outcome!='done':raise LibraryError('Prepared reconciliation requires a terminal result')
            from library.postgres import CommitUnknown
            request: dict[str,Any]=dict(expected_revision=prepared.revision,conflict=prepared.conflict,attempt=(key,generation),now=now,outcome=outcome,proof=proof,source_progress=source_progress,completions=prepared.completions,reconciled=True)
            commit_key=f'task:{key}:{generation}:reconcile:done'
            identity=self.store.transition_digest(prepared.change,**request)
            try:self.store.commit(prepared.change,**request,commit_key=commit_key)
            except CommitUnknown:
                if self.store.outcome(commit_key,input_digest=identity)['state']!='committed':raise
            task=self.get(key)
            if task is None:raise LibraryError('Reconciled task unavailable')
            return task
        with self._write('reconcile', {'key':key,'generation':generation,'now':now,'owner_stopped':owner_stopped,
                         'outcome':outcome,'proof':proof,'source_progress':asdict(source_progress) if source_progress else None}) as tx:
            db = tx.db
            if tx.replayed:
                return Task(**tx.result)
            self._check(db)
            current = self._get(db, key)
            if (current is not None and current.stage == 'copies' and current.status == 'blocked'
                    and current.blocked == 'effect_uncertain' and current.generation == generation):
                if db.execute("SELECT 1 FROM stage_tasks WHERE stage=%s AND status='active'", (current.stage,)).fetchone():
                    raise LibraryError('Queue has another active attempt')
                current.status = 'active'
                current.blocked = ''
                self._put(db, current)
            task = self._finish(db, key, generation, now=now,
                                outcome='retry' if outcome == 'absent' else outcome, proof=proof, reconciled=True,
                                source_progress=source_progress)
            return tx.retain(task)

    def prestart_failure(self, key: str, generation: int, *, now: datetime) -> Task:
        with self._write('prestart_failure', {'key':key,'generation':generation,'now':now}) as tx:
            db = tx.db
            if tx.replayed:
                return Task(**tx.result)
            self._check(db)
            guards(db, {'task:'+key})
            task = self._get(db, key)
            if task is None or task.status != 'pending' or task.generation != generation:
                raise LibraryError('Stale prestart failure')
            task.prestart_failures += 1
            task.due_at = max(task.due_at, instant(now + timedelta(seconds=300 if task.prestart_failures == 1 else 900)))
            if task.prestart_failures >= 3:
                task.status, task.blocked = 'blocked', 'prestart_exhausted'
            self._put(db, task)
            return tx.retain(task)

    def _sync_sources(self, db: DeadlineConnection, queue: str) -> None:
        # Policy is already canonical in PostgreSQL. No YAML index repair.
        return

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
            with self._write('source-scan', {'source':source,'view':view,'cursor':cursor,'registration':registration,'now':now}) as tx:
                if tx.replayed:
                    return Scan(**tx.result)
                db = tx.db
                self._check(db)
                guard_keys = {'source:'+source, 'queue:'+queue}
                for item in obligations:
                    guard_keys.update(('task:'+item.key, 'task-entity:'+item.stage+':'+item.entity))
                guards(db, guard_keys)
                self.policy._snapshot(db,source,view,instant(now),page[-1] if continued else '',expected=registration)
                for item in obligations:
                    self._discover(db,item,now=now,guarded=True)
                tx.result = asdict(Scan(len(page),continued))
            return Scan(len(page), continued)
        except (LibraryError, OSError, ValueError, TypeError, KeyError) as exc:
            return Scan(blocked=str(exc) if isinstance(exc, LibraryError) else 'source_journal_invalid')

    def scan_next(self, *, now: datetime) -> Scan:
        from library.stage_model import Scan
        if self.policy is None:
            return Scan(blocked='source_policy_missing')
        try:
            sources = self.policy.read()['sources']
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
