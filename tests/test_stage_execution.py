"""Queue lifetime exclusion and stale fences at actual effect/commit boundaries."""
from datetime import datetime, timezone
from pathlib import Path
import os
import signal
import subprocess
import sys
import tempfile
import unittest

from library.stage_execution import QueueOwner, acting
from library.stage_tasks import StageTasks, Obligation
from library.primitives import LibraryError
from library.publication import rename_atomic
from library.store import Change
from postgres_fixture import create_store
from library.catalog import empty

AT = datetime(2026, 11, 9, tzinfo=timezone.utc)

class StageExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = create_store(self, self.root, empty(), authority='execution')
        StageTasks.upgrade(self.store)
        self.tasks = StageTasks(self.store)

    def test_paused_alive_actor_excludes_takeover_until_actual_exit(self):
        script = "from library.stage_execution import QueueOwner; from pathlib import Path; import sys,time;\nwith QueueOwner(Path(sys.argv[1]), 'copies'):\n print('owned',flush=True); time.sleep(60)"
        child = subprocess.Popen([sys.executable, '-c', script, str(self.root)], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'owned')
            os.kill(child.pid, signal.SIGSTOP)
            with self.assertRaises(LibraryError):
                with QueueOwner(self.root, 'copies'):
                    self.fail('paused owner replaced')
            with QueueOwner(self.root, 'metadata'):
                pass
        finally:
            child.kill()
            child.wait()
            child.stdout.close()
        with QueueOwner(self.root, 'copies'):
            pass

    def test_superseded_attempt_cannot_rename_or_commit_even_while_owner_alive(self):
        task = self.tasks.discover(Obligation('copies', 'asset', '1', 'v1'), now=AT)
        task = self.tasks.start(task.key, 0, now=AT, resources=True)
        source, target = self.root/'prepared', self.root/'published'
        source.mkdir()
        with QueueOwner(self.root, 'copies') as owner:
            with acting(owner, self.tasks, task):
                self.tasks.discover(Obligation('copies', 'asset', '2', 'v1'), now=AT)
                with self.assertRaises(LibraryError):
                    rename_atomic(source, target)
                with self.assertRaises(LibraryError):
                    self.store.commit(Change(upserts={'works': {'one': {'title': 'stale'}}}), expected_revision=0)
        self.assertFalse(target.exists())
        self.assertIsNone(self.store.get('works', 'one'))
        self.assertTrue(source.is_dir())

    def test_acting_requires_owned_matching_queue_and_releases_no_ownership_early(self):
        task = self.tasks.discover(Obligation('ocr', 'asset', '1', 'v1'), now=AT)
        task = self.tasks.start(task.key, 0, now=AT, resources=True)
        owner = QueueOwner(self.root, 'ocr')
        with self.assertRaises(LibraryError):
            with acting(owner, self.tasks, task):
                pass
        with QueueOwner(self.root, 'metadata') as wrong:
            with self.assertRaises(LibraryError):
                with acting(wrong, self.tasks, task):
                    pass

    def test_parked_local_uncertainty_fences_same_entity_across_new_inputs(self):
        task = self.tasks.discover(Obligation('copies', 'one', '1', 'v1'), now=AT)
        active = self.tasks.start(task.key, 0, now=AT, resources=True)
        self.tasks.finish(active.key, active.generation, now=AT, outcome='uncertain')
        self.tasks.park_uncertain(active.key, active.generation, proof='verified-local-journal', owner_stopped=True)
        self.tasks.discover(Obligation('copies', 'one', '1', 'v2'), now=AT)
        self.tasks.discover(Obligation('copies', 'one', '2', 'v1'), now=AT)
        other = self.tasks.discover(Obligation('copies', 'two', '1', 'v1'), now=AT)
        self.assertEqual(self.tasks.select('copies', now=AT).task.key, other.key)
        self.assertEqual(self.tasks.get(task.key).blocked, 'effect_uncertain')
        resolved = self.tasks.reconcile(task.key, active.generation, now=AT,
            owner_stopped=True, outcome='absent', proof='canonical-copy-journal-readback')
        self.assertEqual(resolved.status, 'superseded')
        self.assertEqual(resolved.blocked, '')
        self.assertEqual(resolved.attempts, 1)
