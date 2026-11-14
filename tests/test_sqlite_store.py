"""Catalogue entity/transaction seam accepted in spec0005."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from library.catalog import empty, LibraryError
from postgres_fixture import create_store
from library.store import Store, Change


class StoreTests(unittest.TestCase):
    def test_audit_rejects_broken_relation_index(self):
        with tempfile.TemporaryDirectory() as folder:
            state = empty()
            aid = 'a' * 64
            state['origins']['parent'] = {'source': 'fixture', 'pid': 1, 'fid': 1,
                                          'path': 'album.cbz', 'sha256': aid, 'availability': 'validated'}
            state['assets'][aid] = {'sha256': aid, 'size': 1, 'format': 'cbz', 'origins': ['parent']}
            store = create_store(self, Path(folder), state)
            store.audit()
            with store._database.connection(write=True) as db:
                db.execute('DELETE FROM asset_origin')
            with self.assertRaisesRegex(LibraryError, 'Catalogue relation index differs from record'):
                store.audit()

    def test_audit_rejects_broken_bundle_link(self):
        from library.codec import encode
        with tempfile.TemporaryDirectory() as folder:
            state = empty()
            aid = 'a' * 64
            state['origins']['z-parent'] = {'source': 'fixture', 'pid': 1, 'fid': 1,
                                            'path': 'bundle.zip', 'sha256': aid, 'availability': 'validated'}
            state['origins']['a-child'] = {'source': 'fixture', 'pid': 1, 'fid': 2,
                'path': f'bundle-inputs/{aid}/album.cbz', 'sha256': aid,
                'parent_origin': 'z-parent', 'parent_sha256': aid, 'member_sha256': aid,
                'member': 'album.cbz', 'availability': 'validated'}
            store = create_store(self, Path(folder), state)
            store.audit()
            # Preserve both the valid FK and relation index. Only provenance differs.
            state['origins']['a-child']['parent_sha256'] = 'b' * 64
            with store._database.connection(write=True) as db:
                db.execute('UPDATE origins SET payload=%s WHERE id=%s',
                           (encode({'record': state['origins']['a-child']}), 'a-child'))
            with self.assertRaisesRegex(LibraryError, 'Invalid bundle child provenance'):
                store.audit()

    def test_worker_commit_mutex_keeps_total_budget_and_operator_deadline(self):
        import time
        from library.primitives import catalogue_commit_lock
        from library.locking import acquisition_deadline, bounded_acquisition
        with tempfile.TemporaryDirectory() as folder:
            start = time.monotonic()
            with catalogue_commit_lock(Path(folder), wait=True):
                self.assertGreater(acquisition_deadline.get(), start + 9)
                self.assertLessEqual(acquisition_deadline.get(), start + 10.1)
            deadline = time.monotonic() + .1
            with bounded_acquisition(deadline), catalogue_commit_lock(Path(folder), wait=True):
                self.assertEqual(acquisition_deadline.get(), deadline)


    def test_contention_deferral_does_not_hide_other_failures(self):
        from library.locking import defer_contention
        with defer_contention():
            raise LibraryError('storage_busy')
        with self.assertRaisesRegex(LibraryError, 'source_policy_changed'), defer_contention():
            raise LibraryError('source_policy_changed')
        with self.assertRaisesRegex(LibraryError, 'lost authority'), defer_contention():
            raise LibraryError('lost authority')
