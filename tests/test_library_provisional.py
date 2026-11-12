import tempfile
import unittest
import zipfile
from pathlib import Path
from library.provisional import process_provisional, ProvisionalPublisher
from library.publish import Capacity, Publisher
from library.catalog import LibraryError
from test_library_publish import setup_library

class ProvisionalTests(unittest.TestCase):
    def test_unknown_copy_has_no_fabricated_identity_and_retry_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store, reception, source, asset = setup_library(root,self)
            state = store.read()
            state['assets'][asset].pop('identity')
            state['works'].clear(); state['editions'].clear(); state['variants'].clear()
            store.save(state, expected=state['revision'])
            args = dict(isolated=True, lab=False, capacity=Capacity(reserve=0))
            self.assertEqual(process_provisional(store,reception,root/'library',**args)['published'],1)
            self.assertEqual(process_provisional(store,reception,root/'library',**args)['processed'],0)
            self.assertEqual(store.read()['editions'], {})
            with zipfile.ZipFile(root/'library/bd-provisional'/asset/'book.cbz') as archive:
                xml = archive.read('ComicInfo.xml').decode()
                self.assertIn('edition=unknown',xml)
                self.assertNotIn('<Publisher>',xml)
                self.assertNotIn('<Number>',xml)
            self.assertTrue(source.exists())

    def test_crash_after_substitution_reconciles_without_second_conversion(self):
        for point in ('prepared', 'verified', 'after_substitution'):
            self.check_recovery(point)

    def check_recovery(self, point):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store, reception, source, asset = setup_library(root,self)
            state=store.read(); state['assets'][asset].pop('identity'); store.save(state,expected=state['revision'])
            def crash(phase, operation):
                if phase == point: raise RuntimeError('crash')
            writer=ProvisionalPublisher(store,reception,root/'library',isolated=True,capacity=Capacity(reserve=0),fault=crash)
            with self.assertRaises(RuntimeError):writer.publish_unknown(asset)
            writer.fault=lambda *_:None
            writer.reconcile()
            self.assertEqual(store.read()['assets'][asset]['provisional']['status'],'published')
            self.assertEqual(len(writer.operations.records()),1)

    def test_confirmed_publication_preserves_provisional_in_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            store,reception,source,asset=setup_library(root,self)
            state=store.read(); identity=state['assets'][asset].pop('identity');store.save(state,expected=state['revision'])
            process_provisional(store,reception,root/'library',isolated=True,lab=False,capacity=Capacity(reserve=0))
            state=store.read();state['assets'][asset]['identity']=identity;store.save(state,expected=state['revision'])
            Publisher(store,reception,root/'library',isolated=True,capacity=Capacity(reserve=0)).publish(asset)
            record=store.read()['assets'][asset]['provisional']
            self.assertEqual(record['status'],'recovered')
            self.assertTrue((root/'library/.recovery'/record['recovery']/'book.cbz').exists())
            self.assertFalse((root/'library/bd-provisional'/asset).exists())
            self.assertTrue(source.exists())

    def test_interrupted_conversion_blocks_new_copies_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            store,reception,source,asset=setup_library(root,self)
            state=store.read();state['assets'][asset].pop('identity');store.save(state,expected=state['revision'])
            def crash(phase, operation):
                if phase=='transforming':raise RuntimeError('crash')
            writer=ProvisionalPublisher(store,reception,root/'library',isolated=True,capacity=Capacity(reserve=0),fault=crash)
            with self.assertRaises(RuntimeError):writer.publish_unknown(asset)
            with self.assertRaisesRegex(LibraryError,'requires recovery'):writer.reconcile()
            self.assertTrue(source.exists())
