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

    def test_failed_writer_rolls_back_and_releases_the_next_stage_writer(self):
        from concurrent.futures import ThreadPoolExecutor
        from library.stage_tasks import StageTasks, Obligation
        from datetime import datetime, timezone
        with tempfile.TemporaryDirectory() as folder:
            store = create_store(self, Path(folder), empty(), authority='expected')
            StageTasks.upgrade(store)
            tasks = StageTasks(store)
            with self.assertRaisesRegex(ValueError, 'interrupted'):
                with store._database.connection(write=True) as db:
                    db.execute('UPDATE meta SET global_revision=99')
                    raise ValueError('interrupted')
            with ThreadPoolExecutor(max_workers=1) as workers:
                task = workers.submit(tasks.discover, Obligation('metadata', 'after-error', '1', 'v1'),
                                      now=datetime.now(timezone.utc)).result(timeout=5)
            self.assertEqual(tasks.get(task.key).status, 'pending')
            self.assertEqual(store.revision, 0)

    def test_contention_deferral_does_not_hide_other_failures(self):
        from library.locking import defer_contention
        with defer_contention():
            raise LibraryError('storage_busy')
        with self.assertRaisesRegex(LibraryError, 'source_policy_changed'), defer_contention():
            raise LibraryError('source_policy_changed')
        with self.assertRaisesRegex(LibraryError, 'lost authority'), defer_contention():
            raise LibraryError('lost authority')

    def test_initialized_store_is_readable_without_legacy_side_files(self):
        with tempfile.TemporaryDirectory() as folder:
            state = empty()
            state['works']['retained'] = {'title': 'Retained'}
            store = create_store(self, Path(folder), state, authority='expected')
            self.assertFalse(any(Path(folder).rglob('*.sqlite*')))
            self.assertEqual(store.operator_page()['counts']['origins'], 0)
            self.assertEqual(store.export(), state)

    def test_failed_write_preserves_revision_and_all_prior_records(self):
        from library.postgres import DeadlineConnection
        from psycopg.errors import DiskFull
        with tempfile.TemporaryDirectory() as folder:
            state = empty()
            state['works']['retained'] = {'title': 'Retained'}
            store = create_store(self, Path(folder), state, authority='expected')
            execute = DeadlineConnection.execute
            def fail(connection, statement, *args, **kwargs):
                if isinstance(statement, str) and statement.startswith("UPDATE commit_receipts SET state='committed'"):
                    raise DiskFull('isolated write fault')
                return execute(connection, statement, *args, **kwargs)
            with patch.object(DeadlineConnection, 'execute', fail), self.assertRaisesRegex(LibraryError, 'PostgreSQL storage exhausted'):
                store.commit(Change(upserts={'works': {'new': {'title': 'New'}}}), expected_revision=0)
            self.assertEqual(store.export(), state)
            store.audit()

    def test_indexed_pages_and_working_set_keep_stale_global_preview_protection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state = empty()
            state['works'] = {str(i): {'title': 'Work ' + str(i)} for i in range(4)}
            store = create_store(self, root, state, authority='expected')
            first = store.page('works', limit=2)
            self.assertEqual([row['id'] for row in first['items']], ['0', '1'])
            self.assertEqual([row['id'] for row in store.page('works', cursor=first['cursor'], limit=2)['items']], ['2', '3'])
            working = store.working_set('identify', {'works': ['0']})
            self.assertEqual(list(working['works']), ['0'])
            store.commit(Change(upserts={'works': {'3': {'title': 'Changed'}}}), expected_revision=0)
            with self.assertRaisesRegex(LibraryError, 'Stale'):
                store.commit(Change(upserts={'works': {'0': {'title': 'Old preview'}}}),
                             expected_revision=working['revision'])
            with self.assertRaises(LibraryError):
                store.page('works', cursor=first['cursor'], limit=2)

    def test_import_preserves_cover_extensions_revision_and_targeted_commit(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state = empty()
            state['revision'] = 7
            aid = 'a' * 64
            cover = 'Y292ZXI='
            state['assets'][aid] = {'sha256': aid, 'size': 5, 'format': 'cbz',
                'origins': [], 'cover': cover, 'extension': {'$booksys': 'literal'}}
            create_store(self, root, state, authority='test-authority')
            store = Store(root, authority='test-authority')
            self.assertEqual(store.export(), state)
            asset = store.get('assets', aid)
            self.assertEqual(asset['cover'], cover)
            asset['extension']['new'] = None
            asset['provisional'] = {'status': 'published', 'target': aid, 'edition_status': 'unknown',
                'artifact_format': 'cbz', 'operation': 'a' * 32,
                'manifest': {'book.cbz': 'b' * 64, 'fiche.md': 'c' * 64}}
            self.assertEqual(store.commit(Change(upserts={'assets': {aid: asset}}), expected_revision=7), 8)
            with store._database.connection() as db:
                self.assertEqual(db.execute('SELECT provisional_published FROM asset_flags WHERE asset=%s', (aid,)).fetchone(), (1,))
            self.assertIsNone(store.get('assets', aid)['extension']['new'])
            asset['extension']['new'] = 'stale edit'
            with self.assertRaisesRegex(LibraryError, 'Stale'):
                store.commit(Change(upserts={'assets': {aid: asset}}), expected_revision=7)
            self.assertEqual(store.revision, 8)

    def test_runtime_refuses_absence_wrong_authority_and_rejects_invalid_relation_atomically(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with self.assertRaises(LibraryError):
                Store(root, authority='expected').get('works', 'x')
            self.assertFalse((root / 'catalogue/catalogue.sqlite').exists())
            create_store(self, root, empty(), authority='expected')
            with self.assertRaises(LibraryError):
                Store(root, authority='other').get('works', 'x')
            store = Store(root, authority='expected')
            with self.assertRaises(LibraryError):
                store.commit(Change(upserts={'works': {'w': {'title': 'Valid'}},
                    'editions': {'e': {'work': 'missing', 'language': 'fr', 'publisher': 'P', 'evidence': ['fixture']}}}),
                    expected_revision=0)
            self.assertIsNone(store.get('works', 'w'))
            self.assertEqual(store.revision, 0)

    def test_reopen_preserves_new_committed_business_values(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = create_store(self, root, empty(), authority='expected')
            store.commit(Change(upserts={'works': {'w': {'title': 'Post conversion',
                'overrides': {'description': None}, 'evidence': ['retained']}}}), expected_revision=0)
            reopened = Store(root, authority='expected')
            self.assertEqual(reopened.export()['works']['w']['title'], 'Post conversion')
            self.assertEqual(reopened.export(), store.export())
            self.assertIsNone(reopened.export()['works']['w']['overrides']['description'])

    def test_invalid_identity_and_selected_variant_cannot_commit(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state = empty()
            aid = 'a' * 64
            state['works']['w'] = {'title': 'Work'}
            state['editions']['e'] = {'work': 'w', 'language': 'fr', 'publisher': 'P', 'evidence': ['fixture']}
            state['assets'][aid] = {'sha256': aid, 'size': 5, 'format': 'cbz', 'origins': []}
            store = create_store(self, root, state, authority='expected')
            asset = store.get('assets', aid)
            asset['identity'] = {'work': 'w', 'edition': 'e', 'variant': 'absent'}
            with self.assertRaises(LibraryError):
                store.commit(Change(upserts={'assets': {aid: asset}}), expected_revision=0)
            self.assertNotIn('identity', store.get('assets', aid))

    def test_incoming_identity_and_selection_prevent_conflicting_target_updates(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state = empty()
            aid = 'a' * 64
            state['works']['w'] = {'title': 'Work'}
            state['editions'] = {key: {'work': 'w', 'language': 'fr', 'publisher': 'P', 'evidence': ['fixture']}
                                 for key in ('e', 'other')}
            state['editions']['e']['selected'] = 'v'
            state['variants']['v'] = {'edition': 'e', 'asset': aid}
            state['assets'][aid] = {'sha256': aid, 'size': 5, 'format': 'cbz', 'origins': [],
                                    'identity': {'work': 'w', 'edition': 'e', 'variant': 'v'}}
            store = create_store(self, root, state, authority='expected')
            for change in (Change(deletes={'variants': ['v']}), Change(deletes={'editions': ['e']}),
                           Change(upserts={'variants': {'v': {'edition': 'other', 'asset': aid}}})):
                with self.subTest(change=change), self.assertRaises(LibraryError):
                    store.commit(change, expected_revision=0)
            self.assertEqual(store.export(), state)
