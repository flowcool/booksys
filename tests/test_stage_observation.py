"""Queue observations count durable units without dispatch or heartbeat progress."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from library.catalog import empty
from tests.postgres_fixture import create_store
from library.stage_tasks import StageTasks, Obligation
from library.stage_resources import Resources, Demand

AT = datetime(2026, 11, 9, tzinfo=timezone.utc)


class QueueObservationTests(unittest.TestCase):
    def test_counts_are_read_only_and_heartbeat_does_not_invent_progress(self):
        from library.stage_observation import observe, public_queues
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = create_store(self,root,empty())
            StageTasks.upgrade(store)
            tasks = StageTasks(store)
            resources = Resources.create(root/'resources.yaml')
            waiting = tasks.discover(Obligation('metadata', 'waiting', '1', 'v1'), now=AT)
            tasks.discover(Obligation('metadata', 'deferred', '1', 'v1', due_at=AT+timedelta(hours=1)), now=AT)
            tasks.discover(Obligation('metadata', 'blocked', '1', 'v1', blocked='operator'), now=AT)
            resources.admit('holder', age=AT, demand=Demand(memory_mib=2304), free_bytes=100*1024**3, external_bytes=0)
            resources.admit(waiting.key+':1', age=AT, demand=Demand(memory_mib=256), free_bytes=100*1024**3, external_bytes=0)
            for _ in range(2):
                result = observe(tasks, resources, root/'heartbeats', now=AT)
                queue = result['metadata']
                self.assertEqual(queue['counts'], dict(eligible=1, resource_wait=1, deferred=1, blocked=1, active=0, done=0))
                self.assertIsNone(queue['last_progress_at'])
                self.assertEqual(queue['next_due_at'], (AT+timedelta(hours=1)).timestamp())
                self.assertIsNone(queue['active_task'])
                self.assertEqual(tasks.get(waiting.key).attempts, 0)
                self.assertEqual(store.revision, 0)
            result['metadata']['observed_at'] = AT.timestamp()
            result['metadata']['reason'] = 'private input that must not escape'
            public = public_queues(result, timestamp=AT.timestamp()+91)
            self.assertEqual(public['metadata']['freshness'], 'stale')
            self.assertNotIn('reason', public['metadata'])
            tasks.wait(waiting.key, waiting.generation, reason='reader_setup_unavailable', until=AT+timedelta(seconds=30))
            blocked = observe(tasks, resources, root/'heartbeats', now=AT)['metadata']['counts']
            self.assertEqual(blocked['eligible'], 0)
            self.assertEqual(blocked['blocked'], 2)
