"""Immutable ledger files stay authoritative; indexed suffix projection is idempotent."""
from pathlib import Path
import tempfile
import unittest
from library.catalog import Catalog, atomic_yaml, empty
from postgres_fixture import create_store


class LedgerTests(unittest.TestCase):
    def test_commit_refuses_unretained_or_reordered_applied_decisions(self):
        from library.catalog import LibraryError
        from library.store import Change
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state = empty(); aid = 'a' * 64
            state['assets'][aid] = {'sha256': aid, 'size': 1, 'format': 'cbz', 'origins': []}
            atomic_yaml(root / 'library.yaml', state)
            store = create_store(self, root, state, authority='ledger')
            with self.assertRaises(LibraryError):
                store.commit(Change(ledger=['unretained']), expected_revision=0)
            self.assertEqual(store.revision, 0)
            catalog = Catalog(root)
            record = catalog.append_decision({'action': 'override', 'target': aid,
                'values': {'title': 'Operator title'}, 'author': 'fixture', 'time': '2026-11-09T00:00:00Z'})
            catalog.project_pending(); revision = store.revision
            for invalid in ([], [record['id'], record['id']], ['unretained']):
                with self.subTest(ledger=invalid), self.assertRaises(LibraryError):
                    store.commit(Change(ledger=invalid), expected_revision=revision)
            self.assertEqual(store.revision, revision)

    def test_file_appended_before_head_and_index_is_recovered_at_startup(self):
        from library.catalog import digest
        from library.launcher import verify_startup
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); aid = 'a' * 64
            state = empty(); state['assets'][aid] = {'sha256': aid, 'size': 1, 'format': 'cbz', 'origins': []}
            atomic_yaml(root / 'library.yaml', state)
            store = create_store(self, root, state, authority='suffix')
            payload = {'schema': 1, 'sequence': 1, 'previous': None, 'action': 'override',
                'target': aid, 'values': {'title': 'Interrupted append'}, 'author': 'fixture', 'time': '2026-11-09T00:00:00Z'}
            record = store.reserve_decision(payload)
            self.assertEqual(record['id'], digest(payload))
            atomic_yaml(root / 'library-decisions' / ('000000001-' + record['id'] + '.yaml'), record, exclusive=True)
            verify_startup(root)
            catalog = Catalog(root)
            self.assertEqual(catalog.get('assets', aid)['overrides']['title'], 'Interrupted append')
            self.assertEqual(catalog.backend().decisions(target=aid), [record])
            appended = catalog.append_decision({'action': 'defer', 'target': aid,
                'values': {}, 'author': 'fixture', 'time': '2026-11-09T00:00:01Z'})
            self.assertEqual(appended['sequence'], 2)

    def test_append_index_pending_overlay_and_incremental_projection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state = empty()
            aid = 'a' * 64
            state['assets'][aid] = {'sha256': aid, 'size': 1, 'format': 'cbz', 'origins': []}
            atomic_yaml(root / 'library.yaml', state)
            create_store(self, root, state, authority='ledger')
            catalog = Catalog(root)
            record = catalog.append_decision({'action': 'override', 'target': aid,
                'values': {'title': 'Operator title'}, 'author': 'fixture', 'time': '2026-11-09T00:00:00Z'})
            self.assertEqual(catalog.backend().decisions(target=aid), [record])
            catalog.project_pending()
            self.assertEqual(catalog.get('assets', aid)['overrides']['title'], 'Operator title')
            revision = catalog.revision
            catalog.project_pending()
            self.assertEqual(catalog.revision, revision)
