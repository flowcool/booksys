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
    def test_exact_migration_recovers_after_old_folder_moved_before_catalogue_save(self):
        from unittest.mock import patch
        from library.publish import Publisher, rename_atomic
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store, reception, source, key = setup_library(root)
            old = Publisher(store, reception, root/'library', isolated=True, capacity=Capacity(reserve=0)).publish(key)
            writer = AdmissionPublisher(store, reception, root/'library', isolated=True, capacity=Capacity(reserve=0))
            def interrupted_move(before, after, *args, **kwargs):
                rename_atomic(before, after, *args, **kwargs)
                if before.parent.name == 'bd':
                    raise RuntimeError('crash after legacy move')
            with patch('library.admission.rename_atomic', side_effect=interrupted_move):
                with self.assertRaises(RuntimeError):
                    writer.publish_asset(key)
            Publisher(store, reception, root/'library', isolated=True,
                      capacity=Capacity(reserve=0)).reconcile()
            self.assertEqual(store.read()['assets'][key]['admission']['status'], 'classified')
            self.assertFalse((root/'library/bd'/old['target']).exists())
            self.assertEqual(sha256(source), key)

    def test_holding_then_manual_edition_reconciles_and_reclassifies_once(self):
        from library.publish import Publisher
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store, reception, source, key = setup_library(root)
            state = store.read(); identity = state['assets'][key].pop('identity')
            store.save(state, expected=state['revision'])
            writer = AdmissionPublisher(store, reception, root/'library', isolated=True, capacity=Capacity(reserve=0))
            holding = writer.publish_asset(key)
            state = store.read(); state['assets'][key]['identity'] = identity
            store.save(state, expected=state['revision'])
            legacy = Publisher(store, reception, root/'library', isolated=True, capacity=Capacity(reserve=0))
            old = legacy.publish(key)
            legacy.reconcile()
            classified = writer.publish_asset(key)
            self.assertFalse((root/'library/bd'/old['target']).exists())
            self.assertFalse((root/'library'/holding['target']).exists())
            self.assertTrue((root/'library'/classified['target']/'book.cbz').exists())
            self.assertEqual(sha256(source), key)

    def test_exact_publication_migrates_to_classified_without_duplicate(self):
        from library.publish import Publisher
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store, reception, source, key = setup_library(root)
            legacy = Publisher(store, reception, root/'library', isolated=True, capacity=Capacity(reserve=0))
            old = legacy.publish(key)
            writer = AdmissionPublisher(store, reception, root/'library', isolated=True, capacity=Capacity(reserve=0))
            new = writer.publish_asset(key)
            self.assertTrue(new['target'].startswith('classified/'))
            self.assertFalse((root/'library/bd'/old['target']).exists())
            self.assertTrue((root/'library/.recovery'/('edition-'+old['id'])/'book.cbz').exists())
            self.assertFalse(any(e.get('publication') for e in store.read()['editions'].values()))
            legacy.reconcile()
            writer.reconcile()
            self.assertEqual(sha256(source), key)

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
