"""Independent observation storage with ownership and loss accounting."""
from pathlib import Path
import psycopg
import tempfile
import time
import unittest
from unittest.mock import patch

from library.activity_store import ActivityStore
from tests.postgres_fixture import create_activity


class ActivityStoreTests(unittest.TestCase):
    def test_missing_configured_authority_does_not_write_legacy_or_stop_business(self):
        from activity import Activity, selected_store
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.dict('os.environ', {'BOOKSYS_ACTIVITY_DIR': str(root / 'selected')}):
                with self.assertRaises(ValueError):
                    selected_store(root)
                producer = Activity(root, 'local', clock=lambda: 100000)
                producer.report('working', 'Work continues')
                producer.heartbeat()
            self.assertFalse((root / 'local.yaml').exists())
            self.assertFalse((root / 'selected/local.yaml').exists())
            self.assertGreater(producer.dropped, 0)

    def test_nonfinite_times_and_uncontrolled_process_fields_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            create_activity(self,root, authority='activity', timestamp=100000)
            store = ActivityStore(root, authority='activity')
            for fields in ({'timestamp': float('nan')}, {'next_attempt': float('inf')},
                           {'process_start': 'password=CANARY'}, {'process_id': -1}):
                with self.subTest(fields=fields), self.assertRaises(ValueError):
                    store.report('local', 'controller', 'one', state='working', operation='started',
                        reason='none', **{'timestamp': 100000, **fields})
            self.assertEqual(store.producers(), {})

    def test_scope_ownership_heartbeat_and_event_identity_survive_reopen(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            create_activity(self,root, authority='activity', timestamp=100000)
            store = ActivityStore(root, authority='activity')
            store.report('byparr', 'collector', 'one', state='working', operation='started',
                         reason='none', timestamp=100000, progressed=True)
            store.report('byparr', 'observer', 'other', state='sleeping', operation='completed',
                         reason='none', timestamp=100001)
            before = store.events(after=None, limit=50)['items']
            store.heartbeat('byparr', 'collector', 'one', timestamp=100015)
            store.heartbeat('byparr', 'collector', 'wrong-session', timestamp=100020)
            current = ActivityStore(root, authority='activity').producers()
            self.assertEqual(current['byparr:collector']['observed_at'], 100015)
            self.assertEqual(current['byparr:collector']['progress_at'], 100000)
            self.assertEqual(current['byparr:observer']['session'], 'other')
            self.assertEqual(store.events(after=None, limit=50)['items'], before)

    def test_busy_observation_drops_immediately_and_next_success_accounts_for_it(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            create_activity(self,root, authority='activity', timestamp=100000)
            store = ActivityStore(root, authority='activity')
            store.report('local','controller','one',state='working',operation='started',reason='none',timestamp=99999)
            blocker = psycopg.connect(store._database.dsn,options='-c search_path='+store._database.schema)
            try:
                blocker.execute("SELECT * FROM activity_producers WHERE stage='local' AND producer_scope='controller' FOR UPDATE")
                start = time.monotonic()
                self.assertFalse(store.report('local', 'controller', 'one', state='working', operation='started',
                                              reason='none', timestamp=100000))
                self.assertLess(time.monotonic() - start, .25)
            finally:
                blocker.close()
            self.assertEqual(store.degradation()['dropped_count'], 1)
            self.assertTrue(store.report('local', 'controller', 'one', state='working', operation='started',
                                         reason='none', timestamp=100001))
            self.assertEqual(store.producers()['local:controller']['dropped_count'], 1)

    def test_import_retains_unexpired_id_time_and_filters_legacy_text(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            create_activity(self,root, authority='activity', timestamp=100000,
                legacy={'local': {'events': {'a' * 64: {'at': 99999, 'state': 'working',
                    'operation': 'private unknown operation', 'reason': 'password=CANARY',
                    'title': 'Album https://private.example/file'}}}})
            store = ActivityStore(root, authority='activity')
            events = store.events(after=None, limit=50)['items']
            self.assertEqual(events[0]['id'], 'a' * 64)
            self.assertEqual(events[0]['at'], 99999)
            self.assertEqual(events[0]['reason'], 'unknown')
            self.assertNotIn('CANARY', str(events))
            self.assertNotIn('private.example', str(events))

    def test_existing_producer_facade_uses_selected_store_without_yaml_writes(self):
        from activity import Activity, read_activity
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            create_activity(self,root, authority='activity', timestamp=100000)
            producer = Activity(root, 'byparr', clock=lambda: 100001)
            producer.report('working', 'Open browser', title='Album', progressed=True)
            observer = Activity(root, 'byparr', clock=lambda: 100002, scope='observer')
            observer.report('sleeping', 'Browser session renewed')
            producer.heartbeat()
            producers = ActivityStore(root, authority='activity').producers()
            self.assertEqual(len(producers), 2)
            self.assertFalse((root / 'byparr.yaml').exists())
            self.assertEqual(read_activity(root, 'byparr')['current']['state'], 'working')
