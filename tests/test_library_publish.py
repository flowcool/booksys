import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from library.catalog import Catalog, LibraryError, apply_effect
from library.intake import receive
from library.publish import Publisher, Capacity
from test_library_intake import SOURCE
from test_library_readers import image_bytes


def setup_library(root,test):
    reception = root / 'reception'
    (reception / 'package').mkdir(parents=True)
    path = reception / 'package/album.cbz'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('001.png', image_bytes())
    from tests.postgres_fixture import create_store
    from library.primitives import empty
    store = Catalog(root/'state',store=create_store(test,root/'state',empty()))
    (root/'postgresql.json').write_bytes((store.directory/'postgresql.json').read_bytes())
    (root/'postgresql.json').chmod(0o600)
    state = receive(store, reception, {'items': {SOURCE: {'title': 'Album'}}},
        {'submissions': {SOURCE: {'status': 'queued', 'name': 'tracked', 'package_id': 1}}},
        [{'pid': 1, 'name': 'tracked', 'folder': 'package', 'links': [
            {'fid': 1, 'name': 'album.cbz', 'status': 0, 'size': path.stat().st_size}]}], stable_seconds=.01)
    asset = next(iter(state['assets']))
    apply_effect(state, {'action': 'identify', 'target': asset, 'values': {'work_id': 'work',
        'edition_id': 'edition', 'work': {'title': 'Album'}, 'edition': {'publisher': 'Publisher',
        'language': 'fr', 'evidence': {'operator': 'fixture', 'edition': 'labelled'}}}})
    apply_effect(state, {'action': 'select', 'target': asset, 'values': {}})
    store.save(state, expected=state['revision'])
    return store, reception, path, asset


def page_bytes(path):
    with zipfile.ZipFile(path) as archive:
        return archive.read('001.png')


class PublishTests(unittest.TestCase):
    def test_transform_capacity_loss_cleans_only_owned_temporary_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store, reception, original, asset = setup_library(root,self)
            class Saturated(Capacity):
                calls = 0
                def check(self, path, required):
                    self.calls += 1
                    if self.calls > 1:
                        raise LibraryError('Insufficient capacity')
            publisher = Publisher(store, reception, root / 'library', isolated=True,
                                  capacity=Saturated(reserve=0))
            with self.assertRaisesRegex(LibraryError, 'capacity'):
                publisher.publish(asset)
            self.assertEqual(list(publisher.staging.iterdir()), [])
            operation = publisher.load_operation(publisher.operations.records()[0]["id"])
            self.assertEqual(operation['phase'], 'capacity_aborted')
            self.assertTrue(original.exists())
            self.assertEqual(list(publisher.exposed.iterdir()), [])

    def test_replacement_crash_recovery_and_rollback(self):
        for phase in ('intent', 'copying', 'prepared', 'verified', 'before_substitution',
                      'after_substitution', 'published', 'committed'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                store, reception, original, asset = setup_library(root,self)
                publisher = Publisher(store, reception, root / 'library', isolated=True, capacity=Capacity(reserve=0))
                first = publisher.publish(asset)
                target = root / 'library/bd' / first['target']
                old_fiche = (target / 'fiche.md').read_bytes()
                state = store.read()
                state['assets'][asset]['assertions'] = [{'summary': 'New operator summary'}]
                store.save(state, expected=state['revision'])
                def crash(point, operation):
                    if point == phase:
                        raise RuntimeError('simulated crash')
                publisher.fault = crash
                with self.assertRaises(RuntimeError):
                    publisher.publish(asset, replace=True)
                publisher.fault = lambda *_: None
                publisher.reconcile()
                self.assertEqual(page_bytes(target / 'book.cbz'), page_bytes(original))
                new_fiche = (target / 'fiche.md').read_bytes()
                self.assertTrue(new_fiche == old_fiche or b'New operator summary' in new_fiche)
                if new_fiche != old_fiche:
                    current = store.read()['editions']['edition']['publication']
                    recovery = root / 'library/.recovery' / current['operation']
                    self.assertEqual((recovery / 'fiche.md').read_bytes(), old_fiche)

    def test_external_edit_before_substitution_blocks_and_preserves_output(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store, reception, original, asset = setup_library(root,self)
            publisher = Publisher(store, reception, root / 'library', isolated=True, capacity=Capacity(reserve=0))
            first = publisher.publish(asset)
            target = root / 'library/bd' / first['target']
            state = store.read()
            state['assets'][asset]['assertions'] = [{'summary': 'new'}]
            store.save(state, expected=state['revision'])
            def edit(point, operation):
                if point == 'before_substitution':
                    (target / 'fiche.md').write_text('external edit')
            publisher.fault = edit
            with self.assertRaisesRegex(LibraryError, 'drift'):
                publisher.publish(asset, replace=True)
            self.assertEqual((target / 'fiche.md').read_text(), 'external edit')

    def test_withdrawal_and_explicit_recovery_preserve_revisions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store, reception, original, asset = setup_library(root,self)
            publisher = Publisher(store, reception, root / 'library', isolated=True, capacity=Capacity(reserve=0))
            first = publisher.publish(asset)
            withdrawal = publisher.withdraw('edition', expected=store.read()['revision'])
            self.assertFalse((root / 'library/bd' / first['target']).exists())
            self.assertTrue((root / 'library/.recovery' / withdrawal['id'] / 'book.cbz').exists())
            publisher.restore(withdrawal['id'], expected=store.read()['revision'])
            self.assertEqual(page_bytes(root / 'library/bd' / first['target'] / 'book.cbz'), page_bytes(original))

    def test_independent_copy_and_no_unmanaged_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store, reception, original, asset = setup_library(root,self)
            publisher = Publisher(store, reception, root / 'library', isolated=True,
                                  capacity=Capacity(reserve=0))
            result = publisher.publish(asset)
            self.assertEqual(result['phase'], 'committed')
            target = root / 'library/bd' / result['target']
            self.assertEqual(page_bytes(target / 'book.cbz'), page_bytes(original))
            self.assertNotEqual((target / 'book.cbz').read_bytes(), original.read_bytes())
            self.assertNotEqual((target / 'book.cbz').stat().st_ino, original.stat().st_ino)
            self.assertIn('Album', (target / 'fiche.md').read_text())
            self.assertEqual(publisher.publish(asset)['id'], result['id'])
            (target / 'fiche.md').write_text('Unmanaged change')
            with self.assertRaisesRegex(LibraryError, 'drift'):
                publisher.publish(asset, replace=True)
            self.assertEqual(page_bytes(original), page_bytes(target / 'book.cbz'))

    def test_crash_at_every_creation_transition(self):
        for phase in ('intent', 'transforming', 'transformed', 'copying', 'prepared', 'verified', 'published', 'committed'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                store, reception, original, asset = setup_library(root,self)
                def crash(point, operation):
                    if point == phase:
                        raise RuntimeError('simulated crash')
                publisher = Publisher(store, reception, root / 'library', isolated=True,
                    capacity=Capacity(reserve=0), fault=crash)
                with self.assertRaises(RuntimeError):
                    publisher.publish(asset)
                publisher.fault = lambda *_: None
                publisher.reconcile()
                targets = list((root / 'library/bd').iterdir())
                self.assertLessEqual(len(targets), 1)
                for target in targets:
                    self.assertEqual(page_bytes(target / 'book.cbz'), page_bytes(original))
                    self.assertTrue((target / 'fiche.md').exists())

    def test_capacity_unknown_external_writer_blocks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store, reception, original, asset = setup_library(root,self)
            publisher = Publisher(store, reception, root / 'library', isolated=True,
                capacity=Capacity(reserve=20 * 1024**3, free=lambda _: 20 * 1024**3))
            with self.assertRaises(LibraryError):
                publisher.publish(asset)
            self.assertEqual(list((root / 'library/bd').iterdir()), [])

    def test_default_capacity_checks_remaining_copy_without_disk_reserve(self):
        capacity = Capacity(free=lambda _: 1024)
        capacity.check(Path('.'), 1024)
        with self.assertRaises(LibraryError):
            capacity.check(Path('.'), 1025)
