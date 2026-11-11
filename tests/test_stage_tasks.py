"""Domain queue behavior through the agreed StageTasks seam."""
from datetime import datetime, timezone, timedelta
from pathlib import Path
import tempfile
import os
import unittest

from library.catalog import empty
from postgres_fixture import create_store
from library.stage_tasks import StageTasks, Obligation

AT = datetime(2026, 11, 9, tzinfo=timezone.utc)


@unittest.skipUnless(os.environ.get('BOOKSYS_TEST_PG_DSN_FILE'), 'isolated PostgreSQL required')
class StageTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = create_store(self, Path(self.temp.name), empty(), authority='tasks')
        StageTasks.upgrade(self.store)
        self.tasks = StageTasks(self.store)

    def test_catalogue_queue_does_not_read_unrelated_source_policy(self):
        from unittest.mock import Mock
        self.tasks.policy = Mock()
        self.tasks.policy.read.side_effect = AssertionError('Unrelated source policy read')
        task = self.tasks.discover(Obligation('metadata', 'independent', '1', 'v1'), now=AT)
        self.assertEqual(self.tasks.select('metadata', now=AT).task.key, task.key)
        started = self.tasks.start(task.key, task.generation, now=AT, resources=True)
        self.assertEqual(started.status, 'active')

    def test_source_queue_reconciles_only_its_own_records(self):
        from dataclasses import asdict
        from unittest.mock import Mock
        other = self.tasks.discover(Obligation('retirement', 'other', '1', 'v1'), now=AT)
        self.tasks.policy = Mock()
        self.tasks.policy.read.return_value = {'tasks': {other.key: asdict(other)}, 'sources': {}}
        self.assertIsNone(self.tasks.select('collection', now=AT).task)
        self.assertEqual(self.tasks.get(other.key).blocked, '')

    def test_source_reconciliation_does_not_spend_the_candidate_selection_window(self):
        import time
        from unittest.mock import patch
        task = self.tasks.discover(Obligation('metadata', 'select-after-recovery', '1', 'v1'), now=AT)
        reconcile = self.tasks._sync_sources
        def delayed(db, queue):
            time.sleep(.55)
            reconcile(db, queue)
        with patch.object(self.tasks, '_sync_sources', side_effect=delayed):
            selected = self.tasks.select('metadata', now=AT).task
        self.assertEqual(selected.key, task.key)

    def test_oldest_runnable_survives_delays_and_resource_wait(self):
        delayed = self.tasks.discover(Obligation('metadata', 'delayed', '1', 'v1', due_at=AT + timedelta(hours=1)), now=AT)
        waiting = self.tasks.discover(Obligation('metadata', 'waiting', '1', 'v1'), now=AT)
        ready = self.tasks.discover(Obligation('metadata', 'ready', '1', 'v1'), now=AT + timedelta(seconds=1))
        result = self.tasks.select('metadata', now=AT + timedelta(seconds=2), resources={waiting.key: 'memory'})
        self.assertEqual(result.task.key, ready.key)
        self.assertEqual(result.waits, {delayed.key: 'deferred', waiting.key: 'memory'})
        self.assertEqual(result.eligible, 2)

    def test_rediscovery_preserves_exhaustion_and_proved_recovery_is_once(self):
        task = self.tasks.discover(Obligation('metadata', 'asset', '1', 'v1'), now=AT)
        now = AT
        for _ in range(3):
            task = self.tasks.start(task.key, task.generation, now=now, resources=True)
            task = self.tasks.finish(task.key, task.generation, now=now, outcome='retry')
            now += timedelta(seconds=900)
        repeated = self.tasks.discover(Obligation('metadata', 'asset', '1', 'v1'), now=now)
        self.assertEqual(repeated.blocked, 'attempts_exhausted')
        restored = self.tasks.recover_dependency(task.key, proof='provider-recovered-1', now=now)
        self.assertEqual(restored.attempts, 0)
        self.assertEqual(restored.first_eligible_at, task.first_eligible_at)
        active = self.tasks.start(restored.key, restored.generation, now=now, resources=True)
        pending = self.tasks.finish(active.key, active.generation, now=now, outcome='retry')
        unchanged = self.tasks.recover_dependency(task.key, proof='provider-recovered-1', now=now)
        self.assertEqual(unchanged.attempts, pending.attempts)
        self.assertEqual(unchanged.due_at, pending.due_at)

    def test_relevant_method_can_replay_unresolved_success_but_keeps_valid_results(self):
        for entity, unresolved in [('valid', False), ('unresolved', True)]:
            task = self.tasks.discover(Obligation('metadata', entity, '1', 'v1'), now=AT)
            task = self.tasks.start(task.key, task.generation, now=AT, resources=True)
            task = self.tasks.finish(task.key, task.generation, now=AT)
            repeated = self.tasks.discover(Obligation('metadata', entity, '1', 'v2', replay=unresolved), now=AT)
            self.assertEqual(repeated.status, 'pending' if unresolved else 'done')
            self.assertEqual(repeated.first_eligible_at, task.first_eligible_at)

    def test_exact_effect_receipt_can_close_a_superseded_interrupted_attempt(self):
        task = self.tasks.discover(Obligation('copies', 'asset', 'old', 'v1'), now=AT)
        active = self.tasks.start(task.key, task.generation, now=AT, resources=True)
        self.tasks.discover(Obligation('copies', 'asset', 'new', 'v1'), now=AT)
        result = self.tasks.reconcile(active.key, active.generation, now=AT,
            owner_stopped=True, outcome='done', proof='exact retained old effect')
        self.assertEqual(result.status, 'superseded')
        self.assertIsNone(self.tasks.active('copies'))
        self.assertEqual(self.tasks.select('copies', now=AT).task.input_revision, 'new')

    def test_budget_deferral_does_not_consume_a_business_attempt(self):
        task = self.tasks.discover(Obligation('metadata', 'asset', '1', 'v1'), now=AT)
        task = self.tasks.start(task.key, task.generation, now=AT, resources=True)
        result = self.tasks.finish(task.key, task.generation, now=AT, outcome='defer', due_at=AT+timedelta(minutes=5))
        self.assertEqual(result.attempts, 0)
        self.assertEqual(result.first_eligible_at, task.first_eligible_at)
        self.assertIsNone(self.tasks.select('metadata', now=AT).task)

    def test_method_change_keeps_age_and_new_input_supersedes_only_old_entity(self):
        old = self.tasks.discover(Obligation('metadata', 'asset', '1', 'v1'), now=AT)
        repeat = self.tasks.discover(Obligation('metadata', 'asset', '1', 'v2'), now=AT + timedelta(seconds=10))
        self.assertEqual(repeat.first_eligible_at, old.first_eligible_at)
        self.assertGreater(repeat.generation, old.generation)
        changed = self.tasks.discover(Obligation('metadata', 'asset', '2', 'v2'), now=AT + timedelta(seconds=20))
        selected = self.tasks.select('metadata', now=AT + timedelta(seconds=30))
        self.assertEqual(selected.task.key, changed.key)
        self.assertEqual(self.tasks.get(old.key).status, 'superseded')
        obsolete = self.tasks.discover(Obligation('metadata', 'asset', '1', 'v3'), now=AT + timedelta(seconds=40))
        self.assertEqual(obsolete.status, 'superseded')

    def test_ties_use_stable_key_and_blocked_work_gets_age_only_when_eligible(self):
        first = self.tasks.discover(Obligation('ocr', 'one', '1', 'v1'), now=AT)
        second = self.tasks.discover(Obligation('ocr', 'two', '1', 'v1'), now=AT)
        self.assertEqual(self.tasks.select('ocr', now=AT).task.key, min(first.key, second.key))
        held = self.tasks.discover(Obligation('metadata', 'held', '1', 'v1', blocked='reader'), now=AT)
        self.assertIsNone(held.first_eligible_at)
        ready = self.tasks.discover(Obligation('metadata', 'held', '1', 'v1'), now=AT + timedelta(seconds=5))
        self.assertEqual(ready.first_eligible_at, (AT + timedelta(seconds=5)).isoformat())

    def test_start_is_atomic_finite_and_rechecks_generation_due_and_queue(self):
        from library.primitives import LibraryError
        task = self.tasks.discover(Obligation('metadata', 'one', '1', 'v1'), now=AT)
        active = self.tasks.start(task.key, task.generation, now=AT, resources=True)
        self.assertEqual(active.attempts, 1)
        other = self.tasks.discover(Obligation('metadata', 'two', '1', 'v1'), now=AT)
        with self.assertRaises(LibraryError):
            self.tasks.start(other.key, other.generation, now=AT, resources=True)
        self.tasks.finish(active.key, active.generation, now=AT, outcome='retry')
        pending = self.tasks.get(task.key)
        self.assertEqual(pending.first_eligible_at, task.first_eligible_at)
        with self.assertRaises(LibraryError):
            self.tasks.start(pending.key, pending.generation, now=AT, resources=True)
        self.assertEqual(self.tasks.get(task.key).attempts, 1)
        self.assertEqual(self.store.revision, 0)

    def test_crashes_require_owner_proof_and_exhaust_across_restarts(self):
        from library.primitives import LibraryError
        task = self.tasks.discover(Obligation('copies', 'one', '1', 'v1'), now=AT)
        now = AT
        for count in range(1, 4):
            task = self.tasks.start(task.key, task.generation, now=now, resources=True)
            self.tasks = StageTasks(self.store)
            with self.assertRaises(LibraryError):
                self.tasks.reconcile(task.key, task.generation, now=now, owner_stopped=False, outcome='absent', proof='retained-check')
            task = self.tasks.reconcile(task.key, task.generation, now=now, owner_stopped=True, outcome='absent', proof='retained-check')
            self.assertEqual(task.attempts, count)
            now += timedelta(seconds=900)
        self.assertEqual(task.status, 'blocked')
        repeat = self.tasks.discover(Obligation('copies', 'one', '1', 'v1'), now=now)
        self.assertEqual(repeat.attempts, 3)
        with self.assertRaises(LibraryError):
            self.tasks.start(repeat.key, repeat.generation, now=now, resources=True)
        fixed = self.tasks.discover(Obligation('copies', 'one', '1', 'v2'), now=now)
        self.assertEqual(fixed.attempts, 0)
        self.assertEqual(fixed.first_eligible_at, AT.isoformat())

    def test_uncertain_effect_cannot_be_reenabled_by_discovery_or_method_change(self):
        from library.primitives import LibraryError
        task = self.tasks.discover(Obligation('copies', 'one', '1', 'v1'), now=AT)
        active = self.tasks.start(task.key, task.generation, now=AT, resources=True)
        self.tasks.finish(active.key, active.generation, now=AT, outcome='uncertain')
        task = self.tasks.discover(Obligation('copies', 'one', '1', 'v2'), now=AT)
        self.assertEqual(task.status, 'active')
        with self.assertRaises(LibraryError):
            self.tasks.start(task.key, task.generation, now=AT, resources=True)
        with self.assertRaises(LibraryError):
            self.tasks.finish(task.key, task.generation - 1, now=AT)

    def test_business_commit_and_obligation_or_result_are_one_transaction(self):
        from library.store import Change
        from library.conflicts import snapshot
        from library.primitives import LibraryError
        work = Obligation('metadata', 'work', '1', 'v1')
        with self.assertRaises(LibraryError):
            self.store.commit(Change(upserts={'editions': {'invalid': {'work': 'absent', 'language': '', 'publisher': '', 'evidence': []}}}),
                              expected_revision=0, obligations=[work], now=AT)
        self.assertIsNone(self.tasks.get(work.key))
        self.store.commit(Change(upserts={'works': {'work': {'title': 'Before'}}}), expected_revision=0, obligations=[work], now=AT)
        self.assertIsNotNone(self.tasks.get(work.key))
        with self.store._database.connection() as db:
            _, token = snapshot(self.store, db, 'identify', {'works': ['work']}, {})
        task = self.tasks.start(work.key, 0, now=AT, resources=True)
        with self.assertRaises(LibraryError):
            self.tasks.finish(task.key, task.generation - 1, now=AT,
                              change=Change(upserts={'works': {'work': {'title': 'Stale'}}}), conflict=token)
        self.assertEqual(self.store.get('works', 'work')['title'], 'Before')
        self.tasks.finish(task.key, task.generation, now=AT,
                          change=Change(upserts={'works': {'work': {'title': 'After'}}}), conflict=token)
        self.assertEqual(self.store.get('works', 'work')['title'], 'After')
        self.assertEqual(self.tasks.get(task.key).status, 'done')
        self.tasks = StageTasks(self.store)
        with self.assertRaises(LibraryError):
            self.tasks.finish(task.key, task.generation, now=AT,
                              change=Change(upserts={'works': {'work': {'title': 'Duplicate'}}}), conflict=token)
        self.assertEqual(self.store.get('works', 'work')['title'], 'After')

    def test_changed_input_fences_an_active_result_and_releases_only_after_reconcile(self):
        from library.primitives import LibraryError
        old = self.tasks.discover(Obligation('metadata', 'one', 'old', 'v1'), now=AT)
        active = self.tasks.start(old.key, old.generation, now=AT, resources=True)
        new = self.tasks.discover(Obligation('metadata', 'one', 'new', 'v1'), now=AT + timedelta(seconds=1))
        with self.assertRaises(LibraryError):
            self.tasks.finish(active.key, active.generation, now=AT + timedelta(seconds=2))
        retired = self.tasks.reconcile(active.key, active.generation, now=AT + timedelta(seconds=2), owner_stopped=True,
                                      outcome='absent', proof='no-effect')
        self.assertEqual(retired.status, 'superseded')
        self.assertEqual(self.tasks.start(new.key, new.generation, now=AT + timedelta(seconds=2), resources=True).attempts, 1)

    def test_prestart_exhaustion_and_restart_preserve_allowance_without_business_revision(self):
        from library.primitives import LibraryError
        task = self.tasks.discover(Obligation('metadata', 'one', '1', 'v1'), now=AT)
        for attempt in range(3):
            task = self.tasks.prestart_failure(task.key, task.generation, now=AT + timedelta(seconds=attempt * 900))
        self.assertEqual((task.status, task.attempts, task.prestart_failures), ('blocked', 0, 3))
        from library.store import Store
        backup = Store(self.store.directory,authority=self.store.authority,database=self.store._database)
        self.assertEqual(StageTasks(backup).get(task.key), task)
        self.assertEqual(backup.revision, 0)
        StageTasks.upgrade(backup)
        self.assertEqual(StageTasks(backup).get(task.key).prestart_failures, 3)
        with self.assertRaises(LibraryError):
            StageTasks(backup).start(task.key, task.generation, now=AT + timedelta(hours=1), resources=True)

    def test_stale_dependencies_do_not_hide_later_eligible_work(self):
        from library.conflicts import snapshot
        from library.store import Change
        self.store.commit(Change(upserts={'works': {'one': {'title': 'Before'}}}), expected_revision=0)
        with self.store._database.connection() as db:
            _, token = snapshot(self.store, db, 'identify', {'works': ['one']}, {})
        old = self.tasks.discover(Obligation('metadata', 'one', '1', 'v1', dependencies=token), now=AT)
        ready = self.tasks.discover(Obligation('metadata', 'two', '1', 'v1'), now=AT + timedelta(seconds=1))
        self.store.commit(Change(upserts={'works': {'one': {'title': 'Changed'}}}), expected_revision=1)
        selection = self.tasks.select('metadata', now=AT + timedelta(seconds=2))
        self.assertEqual(selection.task.key, ready.key)
        self.assertEqual(selection.waits[old.key], 'inputs_changed')
