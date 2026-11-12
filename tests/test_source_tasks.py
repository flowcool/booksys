"""PostgreSQL discovery keeps source identity and attempt policy across rescans."""
from datetime import timedelta
from pathlib import Path
import tempfile
import unittest

from library.catalog import empty, LibraryError
from library.operational_state import CollectorState
from library.stage_tasks import Obligation, StageTasks
from library.source_tasks import SourcePolicy
from postgres_fixture import create_store
from test_stage_tasks import AT


CATEGORY = 'https://example.invalid/category'
PACKAGE = 'https://example.invalid/book'


class SourceTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = create_store(self, self.root, empty(), authority='source')
        self.policy = SourcePolicy.create(self.root / 'task-policy.yaml')
        self.tasks = StageTasks(self.store, policy=self.policy)
        self.journal = self.root / 'monitor.yaml'
        self.state = CollectorState(self.root)
        self.state.category(CATEGORY, {'after_id': 1})

    def test_scan_restart_and_missing_policy_keep_age_and_finite_allowance(self):
        self.assertEqual(self.tasks.scan('collection', self.journal, now=AT).processed, 1)
        task = self.tasks.select('collection', now=AT).task
        active = self.tasks.start(task.key, task.generation, now=AT, resources=True)
        self.tasks = StageTasks(self.store, policy=SourcePolicy(self.policy.path))
        self.tasks.scan('collection', self.journal, now=AT + timedelta(seconds=1))
        self.assertEqual(self.tasks.get(task.key).attempts, 1)
        self.assertEqual(self.tasks.get(task.key).generation, active.generation)
        # Missing source registration is an unavailable source, never a new allowance.
        with self.store._database.connection(write=True) as db:
            db.execute('DELETE FROM source_inputs WHERE source=%s', (task.source,))
            db.execute('DELETE FROM source_registry WHERE id=%s', (task.source,))
        with self.assertRaises(LibraryError):
            self.tasks.start(task.key, active.generation, now=AT + timedelta(seconds=2), resources=True)
        self.assertEqual(self.tasks.get(task.key).first_eligible_at, task.first_eligible_at)
        self.assertEqual(self.tasks.get(task.key).attempts, 1)

    def test_missing_source_registration_does_not_block_independent_postgresql_work(self):
        self.tasks.scan('collection', self.journal, now=AT)
        source = self.tasks.select('collection', now=AT).task.source
        ready = self.tasks.discover(Obligation('metadata', 'asset', '1', 'v1'), now=AT)
        with self.store._database.connection(write=True) as db:
            db.execute('DELETE FROM source_inputs WHERE source=%s', (source,))
            db.execute('DELETE FROM source_registry WHERE id=%s', (source,))
        self.assertEqual(self.tasks.select('metadata', now=AT).task.key, ready.key)
        active = self.tasks.start(ready.key, ready.generation, now=AT, resources=True)
        self.tasks.finish(active.key, active.generation, now=AT)
        self.assertEqual(self.tasks.get(ready.key).status, 'done')

    def test_changed_input_must_rescan_before_claim_and_missing_entity_stays_blocked(self):
        self.tasks.scan('collection', self.journal, now=AT)
        task = self.tasks.select('collection', now=AT).task
        with self.store._database.connection(write=True) as db:
            db.execute('DELETE FROM collection_categories WHERE source=%s', (CATEGORY,))
        with self.assertRaises(LibraryError):
            self.tasks.start(task.key, task.generation, now=AT, resources=True)
        self.assertEqual(self.tasks.get(task.key).attempts, 0)
        self.tasks.scan('collection', self.journal, now=AT+timedelta(seconds=1))
        self.assertIsNone(self.tasks.select('collection', now=AT).task)
        self.assertEqual(self.tasks.get(task.key).first_eligible_at, task.first_eligible_at)

    def test_failed_source_does_not_monopolize_round_robin(self):
        from unittest.mock import patch
        self.state.submission(PACKAGE, {'package_id': 1, 'status': 'queued'})
        self.tasks.scan('collection', self.journal, now=AT)
        self.tasks.scan('routing', self.journal, now=AT + timedelta(seconds=1))
        original = self.policy.journal
        def project(path, *, queue):
            if queue == 'collection':
                raise LibraryError('source_journal_invalid')
            return original(path, queue=queue)
        with patch.object(self.policy, 'journal', side_effect=project):
            self.assertTrue(self.tasks.scan_next(now=AT + timedelta(seconds=62)).blocked)
            restarted = StageTasks(self.store, policy=SourcePolicy(self.policy.path))
            self.assertEqual(restarted.scan_next(now=AT + timedelta(seconds=63)).processed, 1)

    def test_source_identity_is_namespaced_and_naive_deadline_is_refused(self):
        other = self.root / 'other.yaml'
        self.tasks.scan('collection', self.journal, now=AT)
        self.tasks.scan('collection', other, now=AT)
        self.assertEqual(self.tasks.select('collection', now=AT).eligible, 2)
        self.state.patch_monitor(schedule={'not_before':'2026-11-09T00:00:00'})
        self.assertEqual(self.tasks.scan('collection', other, now=AT+timedelta(seconds=1)).blocked,
                         'Invalid native schedule time')
