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
    def test_admission_preserves_atomic_exposure_when_rename_flags_are_unsupported(self):
        import ctypes
        import errno
        from unittest.mock import patch
        class UnsupportedRename:
            def renameat2(self, *args):
                ctypes.set_errno(errno.EINVAL)
                return -1
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store, reception, source, key = setup_library(root,self)
            writer = AdmissionPublisher(store, reception, root/'library', isolated=True,
                                        capacity=Capacity(reserve=0))
            with patch('library.publication.ctypes.CDLL', return_value=UnsupportedRename()):
                operation = writer.publish_asset(key)
            target = writer.root/operation['target']
            self.assertEqual(set(p.name for p in target.iterdir()), {'book.cbz', 'fiche.md'})
            self.assertEqual(store.read()['assets'][key]['admission']['operation'], operation['id'])
            self.assertEqual(sha256(source), key)
            self.assertFalse((writer.staging/operation['id']).exists())

    def test_unsupported_flags_preserve_existing_publication_and_refuse_exchange(self):
        import ctypes
        import errno
        from unittest.mock import patch
        from library.primitives import LibraryError
        from library.publication import publication_lock, rename_atomic
        class UnsupportedRename:
            def renameat2(self, *args):
                ctypes.set_errno(errno.EINVAL)
                return -1
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            root = directory/'library'; root.mkdir(mode=0o700)
            source = root/'prepared'; source.mkdir(mode=0o700)
            (source/'book.cbz').write_bytes(b'prepared')
            target = root/'published'; target.mkdir(mode=0o700)
            (target/'book.cbz').write_bytes(b'published')
            for path in (source/'book.cbz', target/'book.cbz'):
                path.chmod(0o600)
            with patch('library.publication.ctypes.CDLL', return_value=UnsupportedRename()):
                with self.assertRaisesRegex(LibraryError, 'held library-copy lock'):
                    rename_atomic(source, target, private_root=root)
                with publication_lock(directory/'state', root):
                    with self.assertRaisesRegex(LibraryError, 'already exists'):
                        rename_atomic(source, target, private_root=root)
                with self.assertRaisesRegex(LibraryError, 'Invalid argument'):
                    rename_atomic(source, target, exchange=True, private_root=root)
            self.assertEqual((source/'book.cbz').read_bytes(), b'prepared')
            self.assertEqual((target/'book.cbz').read_bytes(), b'published')

    def test_private_directory_fallback_rejects_traversal_for_both_paths(self):
        import ctypes
        import errno
        from unittest.mock import patch
        from library.primitives import LibraryError
        from library.publication import publication_lock, rename_atomic
        class UnsupportedRename:
            def renameat2(self, *args):
                ctypes.set_errno(errno.EINVAL)
                return -1
        for outside_source in (True, False):
            with self.subTest(outside_source=outside_source), tempfile.TemporaryDirectory() as folder:
                directory = Path(folder)
                root = directory/'library'; root.mkdir(mode=0o700)
                source = (directory if outside_source else root)/'prepared'
                source.mkdir(mode=0o700)
                (source/'book.cbz').write_bytes(b'retained'); (source/'book.cbz').chmod(0o600)
                actual_target = (root if outside_source else directory)/'published'
                source_arg = root/'..'/'prepared' if outside_source else source
                target_arg = actual_target if outside_source else root/'..'/'published'
                with patch('library.publication.ctypes.CDLL', return_value=UnsupportedRename()):
                    with publication_lock(directory/'state', root):
                        with self.assertRaisesRegex(LibraryError, 'outside the private library'):
                            rename_atomic(source_arg, target_arg, private_root=root)
                self.assertEqual((source/'book.cbz').read_bytes(), b'retained')
                self.assertFalse(actual_target.exists())

    def test_competing_private_directory_publications_expose_one_complete_winner(self):
        import ctypes
        import errno
        from concurrent.futures import ThreadPoolExecutor
        import threading
        from unittest.mock import patch
        from library.primitives import LibraryError
        from library.publication import publication_lock, rename_atomic
        class UnsupportedRename:
            def renameat2(self, *args):
                ctypes.set_errno(errno.EINVAL)
                return -1
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            root = directory/'library'; root.mkdir(mode=0o700)
            for name in ('one', 'two'):
                stage = root/name; stage.mkdir(mode=0o700)
                for leaf in ('book.cbz', 'fiche.md'):
                    path = stage/leaf; path.write_text(name); path.chmod(0o600)
            barrier = threading.Barrier(2)
            def publish(name):
                barrier.wait()
                try:
                    with publication_lock(directory/'state', root):
                        rename_atomic(root/name, root/'published', private_root=root)
                    return name
                except LibraryError:
                    return None
            with patch('library.publication.ctypes.CDLL', return_value=UnsupportedRename()), ThreadPoolExecutor(2) as pool:
                winners = [name for name in pool.map(publish, ('one', 'two')) if name]
            self.assertEqual(len(winners), 1)
            self.assertEqual({p.read_text() for p in (root/'published').iterdir()}, {winners[0]})
            loser = 'two' if winners[0] == 'one' else 'one'
            self.assertEqual((root/loser/'book.cbz').read_text(), loser)

    def test_interrupted_transform_retains_partials_and_allows_a_bounded_new_attempt(self):
        from library.primitives import LibraryError
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store, reception, source, key = setup_library(root,self)
            def crash(phase, operation):
                if phase == 'transforming':
                    raise LibraryError('interrupted transform')
            writer = AdmissionPublisher(store, reception, root/'library', isolated=True,
                capacity=Capacity(reserve=0), fault=crash)
            with self.assertRaisesRegex(LibraryError, 'interrupted transform'):
                writer.publish_asset(key)
            retained = writer.operations.records()
            self.assertEqual(len(retained), 1)
            recovery = AdmissionPublisher(store, reception, root/'library', isolated=True, capacity=Capacity(reserve=0))
            operations = recovery.reconcile(asset=key)
            self.assertEqual(operations[0]['phase'], 'interrupted')
            self.assertTrue((writer.staging/operations[0]['id']).is_dir())
            completed = recovery.publish_asset(key)
            self.assertNotEqual(completed['id'], operations[0]['id'])
            self.assertEqual(recovery.operations.get(retained[0]['id'])['phase'], 'interrupted')
            self.assertEqual(sha256(source), key)

    def test_operator_cli_admits_valid_unknown_without_edition_fields(self):
        from library.cli import parser, run
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store, reception, source, key = setup_library(root,self)
            state = store.read(); state['assets'][key].pop('identity')
            store.save(state, expected=state['revision'])
            args = parser().parse_args(['--state', str(store.directory), 'admit',
                '--asset', key, '--reception', str(reception), '--library', str(root/'library'), '--isolated'])
            result = run(args)
            self.assertEqual(result['status'], 'holding')
            self.assertEqual(sha256(source), key)

    def test_exact_migration_recovers_after_old_folder_moved_before_catalogue_save(self):
        from unittest.mock import patch
        from library.publish import Publisher, rename_atomic
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store, reception, source, key = setup_library(root,self)
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
            store, reception, source, key = setup_library(root,self)
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

    def test_legacy_promotion_signature_stays_stable_after_retirement(self):
        from unittest.mock import patch
        from library.publish import Publisher
        from postgres_fixture import require_postgresql
        for sqlite in (False, True):
            with self.subTest(sqlite=sqlite), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                store, reception, source, key = setup_library(root,self)
                if sqlite:
                    require_postgresql(store)
                legacy = Publisher(store, reception, root/'library', isolated=True,
                                   capacity=Capacity(reserve=0))
                old = legacy.publish(key)
                writer = AdmissionPublisher(store, reception, root/'library', isolated=True,
                    local_recovery=sqlite, capacity=Capacity(reserve=0))
                first = writer.publish_asset(key)
                with patch('library.admission.transform_archive', side_effect=AssertionError('Unchanged promotion reconverted')):
                    writer.reconcile(asset=key)
                    self.assertEqual(writer.publish_asset(key)['id'], first['id'])
                    self.assertEqual(writer.publish_asset(key)['id'], first['id'])
                self.assertEqual(len(writer.operations.records()), 1)
                self.assertTrue((root/'library/.recovery'/('edition-'+old['id'])/'book.cbz').is_file())
                self.assertEqual(sha256(source), key)

    def test_exact_publication_migrates_to_classified_without_duplicate(self):
        from library.publish import Publisher
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store, reception, source, key = setup_library(root,self)
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
            store, reception, source, key = setup_library(root,self)
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
            store, reception, source, key = setup_library(root,self)
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
            self.assertEqual(len(writer.operations.records()), 1)
