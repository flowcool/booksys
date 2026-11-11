"""Actors arbitrate independent queues through real task and resource authorities."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import threading
import unittest

from library.catalog import empty
from tests.postgres_fixture import create_store
from library.stage_tasks import StageTasks, Obligation
from library.stage_resources import Resources, Demand
from library.stage_actor import StageActor

AT = datetime(2026, 11, 9, tzinfo=timezone.utc)

class StageActorTests(unittest.TestCase):
    def test_post_effect_contention_reconciles_without_replaying_business(self):
        from unittest.mock import patch
        from library.primitives import LibraryError
        for failures in (1, 2):
            with self.subTest(failures=failures):
                self._post_effect_contention(failures)

    def _post_effect_contention(self, failures):
        from unittest.mock import patch
        from library.primitives import LibraryError
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = create_store(self,root,empty(),authority='actor')
            StageTasks.upgrade(store)
            tasks, resources = StageTasks(store), Resources.create(root/'resources.yaml')
            task = tasks.discover(Obligation('metadata', 'one', '1', 'v1'), now=AT)
            executed = []
            with StageActor(tasks, resources, 'metadata', lambda task, stamp: executed.append(task.key) or 'done',
                            receipt=lambda task: ('done', 'retained-receipt'),
                            cleanup=lambda task: True, demand=lambda task: Demand(memory_mib=256)) as actor:
                original = tasks.complete
                remaining = [failures]
                def finish(*args, **kwargs):
                    if remaining[0]:
                        remaining[0] -= 1
                        raise LibraryError('storage_busy')
                    return original(*args, **kwargs)
                with patch.object(tasks, 'complete', side_effect=finish):
                    self.assertEqual(actor.step(now=AT, free_bytes=100*1024**3, external_bytes=0),
                                     'uncertain' if failures == 1 else 'storage_wait')
                self.assertEqual(tasks.get(task.key).status, 'active')
                self.assertEqual(actor.step(now=AT, free_bytes=100*1024**3, external_bytes=0), 'done')
            self.assertEqual(executed, [task.key])
            self.assertEqual(tasks.get(task.key).attempts, 1)

    def test_prestart_sqlite_contention_keeps_actor_and_business_allowance(self):
        from unittest.mock import patch
        from library.primitives import LibraryError
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = create_store(self,root,empty(),authority='actor')
            StageTasks.upgrade(store)
            tasks, resources = StageTasks(store), Resources.create(root/'resources.yaml')
            task = tasks.discover(Obligation('metadata', 'one', '1', 'v1'), now=AT)
            executed = []
            with StageActor(tasks, resources, 'metadata', lambda task, stamp: executed.append(task.key) or 'done',
                            cleanup=lambda task: True, demand=lambda task: Demand(memory_mib=256)) as actor:
                with patch.object(tasks, 'select', side_effect=LibraryError('storage_busy')):
                    self.assertEqual(actor.step(now=AT, free_bytes=100*1024**3, external_bytes=0), 'storage_wait')
                self.assertEqual(tasks.get(task.key).attempts, 0)
                self.assertEqual(executed, [])
                self.assertEqual(actor.step(now=AT, free_bytes=100*1024**3, external_bytes=0), 'done')
            self.assertEqual(len(executed), 1)

    def test_restart_continues_past_a_waiting_page_and_keeps_oldest_age(self):
        from datetime import timedelta
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = create_store(self,root,empty(),authority='actor')
            StageTasks.upgrade(store)
            tasks, resources = StageTasks(store), Resources.create(root/'resources.yaml')
            for index in range(257):
                tasks.discover(Obligation('metadata', 'old'+str(index), '1', 'v1'), now=AT+timedelta(seconds=index))
            final = tasks.discover(Obligation('metadata', 'ready', '1', 'v1'), now=AT+timedelta(seconds=300))
            def actor():
                return StageActor(tasks, resources, 'metadata', lambda task, stamp: 'done',
                    cleanup=lambda task: True, demand=lambda task: Demand(memory_mib=256),
                    ready=lambda task, stamp: '' if task.entity == 'ready' else 'reader_setup_unavailable')
            with actor() as first:
                self.assertEqual(first.step(now=AT+timedelta(seconds=400), free_bytes=100*1024**3, external_bytes=0), 'continued')
            # Expired older prerequisites are reconsidered before continuation.
            with actor() as restarted:
                self.assertEqual(restarted.step(now=AT+timedelta(seconds=500), free_bytes=100*1024**3, external_bytes=0), 'continued')
                self.assertEqual(restarted.step(now=AT+timedelta(seconds=550), free_bytes=100*1024**3, external_bytes=0), 'done')
            self.assertEqual(tasks.get(final.key).status, 'done')

    def test_resource_waiter_behind_continuation_is_resumed_before_later_work(self):
        from datetime import timedelta
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = create_store(self,root,empty(),authority='resource-fifo')
            StageTasks.upgrade(store)
            tasks, resources = StageTasks(store), Resources.create(root/'resources.yaml')
            oldest = tasks.discover(Obligation('metadata', 'oldest', '1', 'v1'), now=AT)
            later = tasks.discover(Obligation('metadata', 'later', '1', 'v1'), now=AT+timedelta(seconds=1))
            resources.admit('occupied', age=AT, demand=Demand(memory_mib=resources.capacity().memory_mib), free_bytes=100*1024**3, external_bytes=0)
            self.assertEqual(resources.admit(oldest.key+':1', age=AT, demand=Demand(memory_mib=256), free_bytes=100*1024**3, external_bytes=0), 'resource_wait')
            resources.release('occupied', stopped=True, children_stopped=True)
            executed = []
            with StageActor(tasks, resources, 'metadata', lambda task, stamp: executed.append(task.key) or 'done', cleanup=lambda task: True, demand=lambda task: Demand(memory_mib=256)) as actor:
                actor.selection_cursor = (later.first_eligible_at, later.key)
                actor.lookback_done = True
                self.assertEqual(actor.step(now=AT+timedelta(seconds=2), free_bytes=100*1024**3, external_bytes=0), 'done')
            self.assertEqual(executed, [oldest.key])

    def test_blocked_priority_preserves_continuation_past_a_full_page(self):
        from datetime import timedelta
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = create_store(self,root,empty(),authority='resource-page')
            StageTasks.upgrade(store)
            tasks, resources = StageTasks(store), Resources.create(root/'resources.yaml')
            old = [tasks.discover(Obligation('copies', 'large'+str(n), '1', 'v1'), now=AT) for n in range(256)]
            later = tasks.discover(Obligation('copies', 'small', '1', 'v1'), now=AT+timedelta(seconds=1))
            occupied = tasks.discover(Obligation('routing', 'occupied', '1', 'v1'), now=AT)
            resources.admit(occupied.key+':1', age=AT, demand=Demand(memory_mib=768), free_bytes=100*1024**3, external_bytes=0)
            oldest = min(old, key=lambda task: task.key)
            self.assertEqual(resources.admit(oldest.key+':1', age=AT, demand=Demand(memory_mib=1792), free_bytes=100*1024**3, external_bytes=0), 'resource_wait')
            executed = []
            with StageActor(tasks, resources, 'copies', lambda task, stamp: executed.append(task.key) or 'done', cleanup=lambda task: True, demand=lambda task: Demand(memory_mib=256 if task.key==later.key else 1792)) as actor:
                actor.selection_cursor = (oldest.first_eligible_at, '~')
                actor.lookback_done = True
                self.assertEqual(actor.step(now=AT+timedelta(seconds=2), free_bytes=100*1024**3, external_bytes=0), 'done')
            self.assertEqual(executed, [later.key])
            self.assertEqual(tasks.get(oldest.key).attempts, 0)

    def test_expired_older_prerequisite_wins_over_persisted_continuation(self):
        from datetime import timedelta
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = create_store(self,root,empty(),authority='fifo')
            StageTasks.upgrade(store)
            tasks, resources = StageTasks(store), Resources.create(root/'resources.yaml')
            work = [tasks.discover(Obligation('metadata', 'asset'+str(index), '1', 'v1'), now=AT+timedelta(seconds=index)) for index in range(258)]
            ready, executed = [False], []
            def actor():
                return StageActor(tasks, resources, 'metadata', lambda task, stamp: executed.append(task.entity) or 'done',
                    cleanup=lambda task: True, demand=lambda task: Demand(memory_mib=256),
                    ready=lambda task, stamp: '' if ready[0] else 'reader_setup_unavailable')
            with actor() as first:
                self.assertEqual(first.step(now=AT+timedelta(seconds=300), free_bytes=100*1024**3, external_bytes=0), 'continued')
            ready[0] = True
            with actor() as restarted:
                self.assertEqual(restarted.step(now=AT+timedelta(seconds=331), free_bytes=100*1024**3, external_bytes=0), 'done')
            self.assertEqual(executed, ['asset0'])

    def test_prestart_grant_before_registry_survives_method_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            store=create_store(self,root,empty(),authority='actor')
            StageTasks.upgrade(store)
            tasks, resources=StageTasks(store), Resources.create(root/'resources.yaml')
            task=tasks.discover(Obligation('metadata','one','1','v1'),now=AT)
            resources.admit(task.key+':1',age=AT,demand=Demand(memory_mib=256),free_bytes=100*1024**3,external_bytes=0)
            tasks.discover(Obligation('metadata','one','1','v2'),now=AT)
            with StageActor(tasks,resources,'metadata',lambda task,stamp:'done',cleanup=lambda task:True,
                            demand=lambda task:Demand(memory_mib=256)) as actor:
                self.assertEqual(actor.step(now=AT,free_bytes=100*1024**3,external_bytes=0),'done')
            self.assertEqual(resources.grants(),[])

    def test_restart_reconciles_receipt_before_selecting_later_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = create_store(self,root,empty(),authority='actor')
            StageTasks.upgrade(store)
            tasks = StageTasks(store)
            resources = Resources.create(root/'resources.yaml')
            old = tasks.discover(Obligation('metadata', 'old', '1', 'v1'), now=AT)
            later = tasks.discover(Obligation('metadata', 'later', '1', 'v1'), now=AT)
            trace = []
            def interrupted(task, now):
                trace.append('effect')
                raise OSError('lost acknowledgement')
            with StageActor(tasks, resources, 'metadata', interrupted, cleanup=lambda task: True,
                            demand=lambda task: Demand(memory_mib=256)) as actor:
                # Stable tie-break is irrelevant: bind the actually selected key.
                self.assertEqual(actor.step(now=AT, free_bytes=100*1024**3, external_bytes=0), 'uncertain')
            active = tasks.active('metadata')
            def receipt(task):
                trace.append('receipt')
                return 'done', 'exact durable result'
            with StageActor(tasks, resources, 'metadata', lambda task, now: trace.append('next') or 'done',
                            cleanup=lambda task: True, demand=lambda task: Demand(memory_mib=256),
                            receipt=receipt) as actor:
                self.assertEqual(actor.step(now=AT, free_bytes=100*1024**3, external_bytes=0), 'done')
                self.assertEqual(tasks.get(active.key).status, 'done')
                self.assertEqual(trace, ['effect', 'receipt'])
                self.assertEqual(actor.step(now=AT, free_bytes=100*1024**3, external_bytes=0), 'done')
            self.assertEqual(trace, ['effect', 'receipt', 'next'])

    def test_other_queue_progresses_during_long_step_without_drain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = create_store(self,root,empty(),authority='actor')
            StageTasks.upgrade(store)
            tasks = StageTasks(store)
            resources = Resources.create(root/'resources.yaml')
            for stage in ('metadata', 'copies', 'intake'):
                for i in range(2):
                    tasks.discover(Obligation(stage, stage+str(i), '1', 'v1'), now=AT)
            entered, finished = threading.Event(), threading.Event()
            errors = []
            def execute(task, now):
                if task.stage == 'metadata':
                    entered.set()
                    if not finished.wait(5):
                        raise AssertionError('other queue starved')
                else:
                    finished.set()
                return 'done'
            def metadata():
                try:
                    with StageActor(tasks, resources, 'metadata', execute,
                                    cleanup=lambda task: True, demand=lambda task: Demand(memory_mib=256, cpu_millis=250)) as actor:
                        self.assertEqual(actor.step(now=AT, free_bytes=100*1024**3, external_bytes=0), 'done')
                except BaseException as exc:
                    errors.append(exc)
            thread = threading.Thread(target=metadata)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                with StageActor(tasks, resources, 'copies', execute,
                                cleanup=lambda task: True, demand=lambda task: Demand(memory_mib=1792, cpu_millis=1250, disk=1)) as actor:
                    self.assertEqual(actor.step(now=AT, free_bytes=100*1024**3, external_bytes=0), 'done')
            finally:
                finished.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(tasks.select('intake', now=AT).eligible, 2)
            self.assertEqual(tasks.select('copies', now=AT).eligible, 1)

    def test_failed_effect_retains_attempt_and_grant_until_exact_reconciliation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = create_store(self,root,empty(),authority='actor')
            StageTasks.upgrade(store)
            tasks = StageTasks(store)
            resources = Resources.create(root/'resources.yaml')
            task = tasks.discover(Obligation('copies', 'one', '1', 'v1'), now=AT)
            def fail(task, now):
                raise OSError('uncertain effect')
            with StageActor(tasks, resources, 'copies', fail, cleanup=lambda task: True, demand=lambda task: Demand(disk=1)) as actor:
                self.assertEqual(actor.step(now=AT, free_bytes=100*1024**3, external_bytes=0), 'uncertain')
            self.assertEqual(tasks.get(task.key).blocked, 'effect_uncertain')
            self.assertEqual(resources.admit('another', age=AT, demand=Demand(disk=1),
                                            free_bytes=100*1024**3, external_bytes=0), 'resource_wait')

    def test_scope_conflict_does_not_spend_an_attempt(self):
        from library.stage_execution import effect_scopes
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            store=create_store(self,root,empty(),authority='actor')
            StageTasks.upgrade(store)
            tasks=StageTasks(store)
            resources=Resources.create(root/'resources.yaml')
            task=tasks.discover(Obligation('copies','one','1','v1'),now=AT)
            entered, release=threading.Event(), threading.Event()
            def holder():
                with effect_scopes(root,['edition:shared']):
                    entered.set()
                    release.wait(5)
            thread=threading.Thread(target=holder)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                with StageActor(tasks,resources,'copies',lambda task,now:'done',demand=lambda task:Demand(disk=1),
                                cleanup=lambda task:True,scopes=lambda task:['edition:shared']) as actor:
                    self.assertEqual(actor.step(now=AT,free_bytes=100*1024**3,external_bytes=0),'conflict_wait')
                self.assertEqual(tasks.get(task.key).attempts,0)
                self.assertEqual(resources.admit('independent',age=AT,demand=Demand(disk=1),free_bytes=100*1024**3,external_bytes=0),'')
            finally:
                release.set()
                thread.join(5)

    def test_seven_queue_actors_make_catalogue_progress_under_sustained_intake(self):
        from datetime import timedelta
        import time
        from library.catalog import Catalog
        from library.stage_model import QUEUES
        from library.primitives import LibraryError
        from library.stage_runtime import demand
        from library.primitives import atomic_yaml
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            atomic_yaml(root/'library.yaml', empty())
            store = create_store(self,root,empty(),authority='mixed')
            StageTasks.upgrade(store)
            tasks, resources, catalog = StageTasks(store), Resources.create(root/'resources.yaml'), Catalog(root,store=store)
            for queue in QUEUES:
                for number in range(8):
                    tasks.discover(Obligation(queue, queue+str(number), '1', 'v1'), now=AT)
            counts = dict.fromkeys(QUEUES, 0)
            trace, errors = [], []
            guard = threading.Lock()
            started = threading.Barrier(8)
            intake_remaining = []
            def execute(task, stamp):
                state = catalog.result_set('identify', {'assets': []}, media=False)
                state['works'][task.entity] = {'title': task.entity}
                time.sleep(.01)
                try:
                    catalog.save(state, expected=state['revision'])
                except LibraryError as exc:
                    if str(exc) in ('storage_busy', 'processing_lock_busy'):
                        # This fixture performs only the scoped SQL commit.
                        # No external effect exists when acquisition is refused.
                        from library.stage_model import StepResult
                        return StepResult('defer', due_at=stamp+timedelta(seconds=1))
                    raise
                with guard:
                    counts[task.stage] += 1
                    trace.append((task.stage, time.monotonic()))
                    if task.stage == 'intake':
                        tasks.discover(Obligation('intake', 'arrival'+str(counts['intake']), '1', 'v1'), now=stamp)
                return 'done'
            def worker(queue):
                try:
                    with StageActor(tasks, resources, queue, execute, cleanup=lambda task: True, demand=demand) as actor:
                        started.wait(timeout=10)
                        for tick in range(100):
                            if errors:
                                return
                            outcome = actor.step(now=AT+timedelta(seconds=tick), free_bytes=100*1024**3, external_bytes=0)
                            if outcome in ('uncertain', 'cleanup_required'):
                                raise AssertionError((queue, outcome, errors))
                            with guard:
                                complete = all(counts[name] >= 4 for name in QUEUES)
                            if complete:
                                return
                            time.sleep(.01)
                        raise AssertionError('A healthy queue did not progress under sustained intake')
                except BaseException as exc:
                    errors.append(exc)
            threads = [threading.Thread(target=worker, args=(queue,)) for queue in QUEUES]
            for thread in threads:
                thread.start()
            started.wait(timeout=10)
            for thread in threads:
                thread.join(60)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertTrue(all(count >= 4 for count in counts.values()), counts)
            self.assertGreater(tasks.select('intake', now=AT+timedelta(seconds=600)).eligible, 0)
            self.assertEqual(resources.grants(), [])
            self.assertEqual(len(list(catalog.records('works'))), sum(counts.values()))
