"""Typed controller views use PostgreSQL rows and cannot contact business adapters."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from library.api import PublicAPI
from postgres_fixture import create_store
from library.primitives import empty
from library.operational_state import StatusObservations
from library.catalog import LibraryError
from test_library_publish import setup_library


class PublicTests(unittest.TestCase):
    def test_indexed_list_and_targeted_fiche_without_legacy_export_or_raw_cycle(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            catalog, reception, _, asset = setup_library(root,self)
            state = catalog.read()
            StatusObservations(catalog.backend()._database).save_cycle(
                {'status': 'lab', 'reason': 'secret=CANARY https://private.example'})
            api = PublicAPI(root, host='127.0.0.1:1', reception=reception)
            request = {'payload': {}, 'host': api.host, 'origin': '', 'token': ''}
            with patch('library.store.Store.export', side_effect=AssertionError('No full export')):
                page = api.invoke('library', request)
                self.assertEqual(len(page['groups']), 1)
                self.assertNotIn('CANARY', str(page));self.assertNotIn('private.example', str(page))
                status = api.invoke('status', request)
                import json
                status = json.loads(status.body)
                self.assertIn('generated_at', status)
                self.assertIn('activity_dropped_count', status['cycle']['diagnostics'])
                self.assertEqual(set(status['counts']), {'sources', 'origins', 'confirmed_assets',
                    'classified_assets', 'holding_assets', 'provisional_assets'})
                self.assertNotIn('CANARY', str(status)); self.assertNotIn('private.example', str(status))
                origin = next(iter(state['origins']))
                value = api.invoke('fiche', {**request, 'payload': {'id': origin}})
                self.assertIn('Album', value['html'])
            with self.assertRaises(LibraryError):
                api.invoke('confirm', {**request, 'payload': {'request_id': 'x', 'preview': 'x'}})


    def test_activity_snapshot_is_filtered_ages_and_expires_without_request_file_reads(self):
        import json
        from activity import WINDOW
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = create_store(self, root / 'state', empty())
            StatusObservations(store._database).save_activity({
                'stages': {'collection': {'state': 'working', 'observed_at': 1000,
                    'session': 'private', 'title': 'secret=CANARY', 'queue_observed_at': 1000}},
                'events': {'a' * 64: {'at': 1000, 'title': 'secret=CANARY', 'stage': 'collection'}}})
            with patch('library.activity.time.time', return_value=1000):
                api = PublicAPI(root, host='lab', reception=root / 'none')
            request = {'payload': {}, 'host': 'lab', 'origin': '', 'token': ''}
            with patch('library.activity.read_yaml', side_effect=AssertionError('No request disk read')):
                with patch('library.activity.time.time', return_value=1010):
                    value = json.loads(api.invoke('activity', request).body)
                    self.assertEqual(value['stages']['collection']['state'], 'working')
                    self.assertNotIn('CANARY', str(value))
                    self.assertEqual(len(value['events']), 1)
                with patch('library.activity.time.time', return_value=1000 + WINDOW + 1):
                    value = json.loads(api.invoke('activity', request).body)
                    self.assertEqual(value['stages']['collection']['freshness'], 'stale')
                    self.assertEqual(value['stages']['collection']['queue_freshness'], 'stale')
                    self.assertEqual(value['stages']['collection']['state'], 'unknown')
                    self.assertEqual(value['events'], {})
