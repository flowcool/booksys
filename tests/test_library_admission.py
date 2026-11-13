"""Safe work/holding CBZ publication and recoverable promotion."""
import tempfile
import unittest
import zipfile
from pathlib import Path

from library.admission import AdmissionPublisher
from library.publication import Capacity
from library.readers import sha256
from test_library_publish import setup_library


class AdmissionTests(unittest.TestCase):

    def test_scoped_recovery_on_postgresql(self):
        from library.operational_state import CopyOperations
        from library.primitives import empty, LibraryError
        from postgres_fixture import create_store
        publisher = object.__new__(AdmissionPublisher)
        with tempfile.TemporaryDirectory() as folder:
            store = create_store(self, Path(folder), empty())
            operations = CopyOperations(store._database, 'admission')
            selected = {'id': 'selected', 'asset': 'a'*64, 'kind': 'admission',
                        'target': 'holding/x--'+'a'*64+'--'+'c'*12, 'manifest': {'book.cbz': 'proof'}}
            other = {**selected, 'id': 'other', 'asset': 'b'*64,
                     'target': 'holding/y--'+'b'*64+'--'+'c'*12}
            operations.phase(selected)
            operations.phase(other)
            self.assertEqual(operations.for_asset('a'*64, publisher.operation_applies), [selected])
            operations.phase({**other, 'id': 'invalid', 'asset': 'unknown'})
            with self.assertRaisesRegex(LibraryError, 'conflict scope'):
                operations.for_asset('a'*64, publisher.operation_applies)

    def test_scoped_recovery_refuses_inconsistent_unrelated_journals(self):
        from unittest.mock import MagicMock
        from library.codec import encode
        from library.operational_state import CopyOperations
        from library.primitives import LibraryError
        publisher = object.__new__(AdmissionPublisher)
        database = MagicMock()
        db = database.connection.return_value.__enter__.return_value
        operations = CopyOperations(database, 'admission')
        for scope, reason in (({'asset': 'unknown'}, 'conflict scope'),
                              ({'asset': 'b'*64, 'kind': 'admission', 'target': 'bad'}, 'destination scope')):
            with self.subTest(reason=reason):
                db.execute.return_value = [(encode(scope), None)]
                with self.assertRaisesRegex(LibraryError, reason):
                    operations.for_asset('a'*64, publisher.operation_applies)

    def test_scoped_recovery_loads_full_payloads_only_for_selected_asset(self):
        from unittest.mock import MagicMock, patch
        from library.codec import encode, decode
        from library.operational_state import CopyOperations
        publisher = object.__new__(AdmissionPublisher)
        database = MagicMock()
        db = database.connection.return_value.__enter__.return_value
        scope = lambda key: {'asset': key, 'kind': 'admission', 'target': 'holding/x--'+key+'--'+'c'*12}
        selected = {**scope('a'*64), 'id': 'selected', 'manifest': {'book.cbz': 'proof'}}
        db.execute.return_value = [(encode(scope('b'*64)), None), (encode(scope('a'*64)), encode(selected))]
        with patch('library.operational_state.decode', wraps=decode) as loaded:
            self.assertEqual(CopyOperations(database, 'admission').for_asset('a'*64, publisher.operation_applies), [selected])
        self.assertEqual([call.args[0] for call in loaded.call_args_list],
                         [encode(scope('b'*64)), encode(scope('a'*64)), encode(selected)])
        query, params = db.execute.call_args.args
        self.assertIn('CASE WHEN', query)
        self.assertIn("->>'asset'=%s THEN payload END", query)
        self.assertEqual(params, ('a'*64, 'admission'))

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








