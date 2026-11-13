"""Catalogue entity/transaction seam accepted in spec0005."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from library.catalog import empty, LibraryError
from postgres_fixture import create_store
from library.store import Store, Change


class StoreTests(unittest.TestCase):
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
