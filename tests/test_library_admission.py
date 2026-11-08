"""Safe work/holding CBZ publication and recoverable promotion."""
import tempfile
import unittest
import zipfile
from pathlib import Path

from library.admission import AdmissionPublisher
from library.publish import Capacity
from library.readers import sha256
from test_library_publish import setup_library


class AdmissionTests(unittest.TestCase):
    def test_holding_to_classified_preserves_original_and_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store, reception, source, key = setup_library(root)
            state = store.read()
            state['assets'][key].pop('identity')
            state['assets'][key]['embedded'] = {}
            store.save(state, expected=state['revision'])
            writer = AdmissionPublisher(store, reception, root/'library', isolated=True, capacity=Capacity(reserve=0))
            first = writer.publish_asset(key)
            self.assertTrue(first['target'].startswith('holding/'))
            self.assertEqual(sha256(source), key)
            state = store.read()
            state['assets'][key]['work_result'] = {'status': 'identified', 'edition_status': 'unknown',
                'work': {'title': 'Naruto Uzumaki', 'series': 'Naruto', 'number': '1', 'family': 'Manga'},
                'field_conflicts': {}}
            store.save(state, expected=state['revision'])
            second = writer.publish_asset(key)
            self.assertTrue(second['target'].startswith('classified/Manga/Naruto/'))
            self.assertTrue((root/'library/.recovery'/second['id']/'book.cbz').is_file())
            self.assertFalse((root/'library'/first['target']).exists())
            with zipfile.ZipFile(root/'library'/second['target']/'book.cbz') as archive:
                xml = archive.read('ComicInfo.xml').decode()
                self.assertIn('<Title>Naruto Uzumaki</Title>', xml)
                self.assertNotIn('<Publisher>', xml)
            self.assertEqual(sha256(source), key)
            self.assertEqual(writer.publish_asset(key)['id'], second['id'])

    def test_interruption_after_substitution_recovers_without_reconversion(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store, reception, source, key = setup_library(root)
            state = store.read(); state['assets'][key].pop('identity')
            store.save(state, expected=state['revision'])
            def crash(phase, operation):
                if phase == 'after_substitution':
                    raise RuntimeError('crash')
            writer = AdmissionPublisher(store, reception, root/'library', isolated=True,
                                        capacity=Capacity(reserve=0), fault=crash)
            with self.assertRaises(RuntimeError):
                writer.publish_asset(key)
            writer.fault = lambda *_: None
            writer.reconcile()
            self.assertEqual(store.read()['assets'][key]['admission']['status'], 'holding')
            self.assertEqual(len(list(writer.journals.glob('*.yaml'))), 1)
