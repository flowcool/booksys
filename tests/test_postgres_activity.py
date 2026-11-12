"""Activity public behavior with real isolated PostgreSQL state."""
from pathlib import Path
import tempfile
import unittest
from tests.postgres_fixture import create_store
from library.primitives import empty
from library.activity_store import ActivityStore

class PostgresActivityTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.store=create_store(self,Path(self.tmp.name),empty())
  self.activity=ActivityStore.create(Path(self.tmp.name),authority='activity',database=self.store._database,timestamp=100000)
 def report(self,stamp=100000):
  return self.activity.report('local','controller','one',state='working',operation='started',reason='none',timestamp=stamp,progressed=True)
 def test_business_transaction_does_not_block_activity(self):
  with self.store._database.connection(write=True) as db:
   db.execute('SELECT * FROM meta FOR UPDATE')
   self.assertTrue(self.report())
   self.assertEqual(self.activity.producers()['local:controller']['progress_at'],100000)
 def test_same_producer_wait_is_bounded_and_drop_count_recovers(self):
  self.assertTrue(self.report())
  with self.store._database.connection(write=True) as db:
   db.execute('SELECT * FROM activity_producers FOR UPDATE')
   self.assertFalse(self.report(100001))
  self.assertEqual(self.activity.degradation()['dropped_count'],1)
  self.assertTrue(self.report(100002))
  self.assertEqual(self.activity.producers()['local:controller']['dropped_count'],1)
 def test_heartbeat_session_and_event_order_survive_reopen(self):
  self.assertTrue(self.report())
  self.assertFalse(self.activity.heartbeat('local','controller','other',timestamp=100001))
  self.assertTrue(self.activity.heartbeat('local','controller','one',timestamp=100002))
  self.assertEqual(self.activity.producers()['local:controller']['observed_at'],100002)
  reopened=ActivityStore(Path(self.tmp.name),authority='activity',database=self.store._database)
  events=reopened.events(after=None)['items'];self.assertEqual(len(events),1)
  self.assertEqual(events[0]['at'],100000)
  self.assertTrue(reopened.prune(timestamp=200000));self.assertEqual(reopened.events(after=None)['items'],[])

 def test_missing_producer_insert_and_prune_drop_on_lock_contention(self):
  import time
  with self.store._database.connection(write=True) as db:
   db.execute("LOCK TABLE activity_producers IN EXCLUSIVE MODE")
   start=time.monotonic();self.assertFalse(self.report());self.assertLess(time.monotonic()-start,.2)
  self.assertTrue(self.report())
  with self.store._database.connection(write=True) as db:
   db.execute('SELECT * FROM activity_events FOR UPDATE')
   start=time.monotonic();self.assertFalse(self.activity.prune(timestamp=200000));self.assertLess(time.monotonic()-start,.2)
