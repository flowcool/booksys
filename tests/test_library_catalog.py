import tempfile
import unittest
from pathlib import Path

from library.catalog import Catalog, LibraryError, atomic_yaml, lock


class CatalogTests(unittest.TestCase):
    def test_decisions_survive_old_index_and_missing_evidence_blocks(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Catalog(Path(folder))
            state = store.read()
            asset = 'a' * 64
            state['assets'][asset] = {'sha256': asset, 'size': 4, 'format': 'cbz', 'origins': []}
            store.save(state, expected=0)
            old = store.read()
            with lock(Path(folder) / 'decisions.lock'):
                decision = store.append_decision({'action': 'override', 'target': asset,
                    'values': {'genres': ['Adventure']}, 'author': 'flowcool', 'time': '2026-11-08'})
            self.assertEqual(store.replay()['assets'][asset]['overrides']['genres'], ['Adventure'])
            atomic_yaml(store.path, old)
            self.assertEqual(store.replay()['assets'][asset]['overrides']['genres'], ['Adventure'])
            next(store.decisions.glob('*.yaml')).rename(Path(folder) / 'lost.yaml')
            with self.assertRaisesRegex(LibraryError, 'incomplete'):
                store.replay()

    def test_roundtrip_and_reject_unknown_schema(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Catalog(Path(folder))
            state = store.read()
            store.save(state, expected=0)
            self.assertEqual(store.read()['revision'], 1)
            with self.assertRaises(LibraryError):
                store.save(state, expected=0)
            (Path(folder) / 'library.yaml').write_text('schema: 99\n')
            with self.assertRaises(LibraryError):
                store.read()
