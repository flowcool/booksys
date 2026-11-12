import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from library.catalog import Catalog, LibraryError, atomic_yaml, lock, empty
from postgres_fixture import create_catalog


class CatalogTests(unittest.TestCase):
    def test_safe_catalogue_engine_preserves_types_aliases_and_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            from library.documents import load as read_yaml
            source = Path(folder) / 'library.yaml'
            text = yaml.safe_dump(empty()).replace('works: {}', 'works:\n  sample:\n    title: "Édition 漫画"\n    values: &facts [true, false, null, 42, 1.5, 2026-11-08, "001"]\n    alias: *facts')
            source.write_text(text)
            expected = yaml.load(text, Loader=yaml.SafeLoader)
            self.assertEqual(read_yaml(source), expected)
            if hasattr(yaml, 'CSafeLoader'):
                with patch.object(yaml, 'SafeLoader', side_effect=AssertionError('slow fallback used')):
                    self.assertEqual(read_yaml(source), expected)
            with patch.dict(yaml.__dict__):
                yaml.__dict__.pop('CSafeLoader', None)
                self.assertEqual(read_yaml(source), expected)
            source.write_text(text.replace('"Édition 漫画"', '!!python/object/apply:builtins.str [forbidden]'))
            with self.assertRaises(yaml.constructor.ConstructorError):
                read_yaml(source)
            with patch.dict(yaml.__dict__):
                yaml.__dict__.pop('CSafeLoader', None)
                with self.assertRaises(yaml.constructor.ConstructorError):
                    read_yaml(source)


    def test_decisions_survive_old_index_and_missing_evidence_blocks(self):
        with tempfile.TemporaryDirectory() as folder:
            store = create_catalog(self, Path(folder))
            state = store.read()
            asset = 'a' * 64
            state['assets'][asset] = {'sha256': asset, 'size': 4, 'format': 'cbz', 'origins': []}
            store.save(state, expected=0)
            old = store.read()
            with lock(Path(folder) / 'decisions.lock'):
                decision = store.append_decision({'action': 'override', 'target': asset,
                    'values': {'genres': ['Adventure']}, 'author': 'flowcool', 'time': '2026-11-08'})
            self.assertEqual(store.replay()['assets'][asset]['overrides']['genres'], ['Adventure'])
            atomic_yaml(store.path, dict(old))
            self.assertEqual(store.replay()['assets'][asset]['overrides']['genres'], ['Adventure'])
            next(store.decisions.glob('*.yaml')).rename(Path(folder) / 'lost.yaml')
            with self.assertRaisesRegex(LibraryError, 'incomplete'):
                store.replay()

    def test_roundtrip_and_reject_unknown_schema(self):
        with tempfile.TemporaryDirectory() as folder:
            store = create_catalog(self, Path(folder))
            state = store.read()
            state['works']['roundtrip'] = {'title': 'Retained'}
            store.save(state, expected=0)
            self.assertEqual(store.read()['revision'], 1)
            state['works']['roundtrip']['title'] = 'Stale'
            with self.assertRaises(LibraryError):
                store.save(state, expected=0)
            (Path(folder) / 'library.yaml').write_text('schema: 99\n')
            self.assertEqual(store.read()['revision'], 1)
            invalid = store.read()
            invalid['schema'] = 99
            from library.primitives import validate
            with self.assertRaises(LibraryError):
                validate(invalid)
