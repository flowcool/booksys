"""Actual process loss at PostgreSQL commit boundaries; isolated stores."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from library.catalog import empty
from postgres_fixture import create_store
from library.operational_state import CollectorState
from library.source_tasks import SourcePolicy
from library.stage_tasks import StageTasks, Obligation
from test_stage_tasks import AT

CHILD = r'''
from pathlib import Path
from unittest.mock import patch
import os, sys
from library.store import Store, Change
from library.stage_tasks import StageTasks, Obligation
from library.source_tasks import SourcePolicy
from library.postgres import DeadlineConnection
from library.conflicts import snapshot
from datetime import datetime, timezone
root, boundary = Path(sys.argv[1]), sys.argv[2]
now = datetime(2026, 11, 9, tzinfo=timezone.utc)
store = Store(root, authority='crashes')
tasks = StageTasks(store)
work = Obligation('metadata', 'work', '1', 'v1')
original_execute = DeadlineConnection.execute
transitions = set()
def interrupted(connection, statement, *args, **kwargs):
    if isinstance(statement, str) and statement.startswith("UPDATE commit_receipts SET state='committed'"):
        transitions.add(connection)
    if connection in transitions and statement == 'COMMIT' and boundary.endswith('before'):
        os._exit(19)
    result = original_execute(connection, statement, *args, **kwargs)
    if connection in transitions and statement == 'COMMIT' and boundary.endswith('after'):
        os._exit(19)
    return result
if boundary.startswith('business'):
    with patch.object(DeadlineConnection, 'execute', interrupted):
        store.commit(Change(upserts={'works': {'work': {'title': 'Before'}}}), expected_revision=0, obligations=[work], now=now)
elif boundary.startswith('claim'):
    with patch.object(DeadlineConnection, 'execute', interrupted):
        tasks.start(work.key, 0, now=now, resources=True)
elif boundary.startswith('result'):
    with store._database.connection() as db:
        _, token = snapshot(store, db, 'identify', {'works': ['work']}, {})
    task = tasks.get(work.key)
    with patch.object(DeadlineConnection, 'execute', interrupted):
        tasks.finish(task.key, task.generation, now=now, conflict=token,
                     change=Change(upserts={'works': {'work': {'title': 'After'}}}))
elif boundary == 'source_after':
    with patch.object(DeadlineConnection, 'execute', interrupted):
        tasks.start(sys.argv[3], 0, now=now, resources=True)
os._exit(19)
'''


class SourcePolicySnapshotTests(unittest.TestCase):
    def test_read_never_returns_mutable_shared_state_and_observes_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = create_store(self, root, empty())
            policy = SourcePolicy(root / 'policy.yaml', store=store)
            first = policy.read()
            first['sources']['uncommitted'] = {}
            self.assertEqual(policy.read()['sources'], {})
            source, _ = policy.register('collection', root / 'monitor.yaml')
            self.assertEqual(list(policy.read()['sources']), [source])

class StageCrashTests(unittest.TestCase):
    def test_committed_and_interrupted_boundaries_have_no_lost_or_duplicate_result(self):
        for boundary in ('business_before', 'business_after', 'claim_before', 'claim_after', 'result_before', 'result_after'):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                store = create_store(self, root, empty(), authority='crashes')
                StageTasks.upgrade(store)
                tasks = StageTasks(store)
                work = Obligation('metadata', 'work', '1', 'v1')
                if boundary.startswith(('claim', 'result')):
                    from library.store import Change
                    store.commit(Change(upserts={'works': {'work': {'title': 'Before'}}}), expected_revision=0, obligations=[work], now=AT)
                if boundary.startswith('result'):
                    tasks.start(work.key, 0, now=AT, resources=True)
                child = subprocess.run([sys.executable, '-c', CHILD, str(root), boundary], capture_output=True, timeout=15)
                self.assertEqual(child.returncode, 19, child.stderr.decode())
                task = StageTasks(store).get(work.key)
                if boundary == 'business_before':
                    self.assertIsNone(task)
                    self.assertIsNone(store.get('works', 'work'))
                else:
                    status = 'done' if boundary == 'result_after' else 'active' if boundary in ('claim_after', 'result_before') else 'pending'
                    self.assertEqual(task.status, status)
                    self.assertEqual(task.attempts, 0 if status == 'pending' else 1)
                    self.assertEqual(store.get('works', 'work')['title'], 'After' if status == 'done' else 'Before')
                store.audit()

    def test_source_policy_charge_survives_crash_after_atomic_commit(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = create_store(self, root, empty(), authority='crashes')
            StageTasks.upgrade(store)
            policy = SourcePolicy.create(root / 'task-policy.yaml')
            journal = root / 'monitor.yaml'
            CollectorState(root).category('https://example.invalid/category', {'after_id': 1})
            tasks = StageTasks(store, policy=policy)
            tasks.scan('collection', journal, now=AT)
            task = tasks.select('collection', now=AT).task
            child = subprocess.run([sys.executable, '-c', CHILD, str(root), 'source_after', task.key], capture_output=True, timeout=15)
            self.assertEqual(child.returncode, 19, child.stderr.decode())
            resumed = StageTasks(store, policy=SourcePolicy(policy.path))
            self.assertIsNone(resumed.select('collection', now=AT).task)
            active = resumed.get(task.key)
            self.assertEqual((active.status, active.attempts), ('active', 1))
            recovered = resumed.reconcile(active.key, active.generation, now=AT, owner_stopped=True,
                                          outcome='absent', proof='isolated-child-exited-without-effect')
            self.assertEqual((recovered.status, recovered.attempts), ('pending', 1))
