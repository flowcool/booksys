"""Scale generation changes canonical IDs consistently and keeps payload types."""
from library.catalog import empty, validate
from ops.qualification.storage_dataset import clone_dataset
import unittest


class DatasetTests(unittest.TestCase):
    def test_tenfold_clones_and_identified_ledger_preserve_relations(self):
        state = empty()
        for index in range(3):
            aid = str(index + 1) * 64
            oid = 'origin' + str(index)
            state['assets'][aid] = {'sha256': aid, 'size': 1, 'format': 'cbz', 'origins': [oid], 'custom': None}
            state['origins'][oid] = {'source': 'https://private/CANARY', 'pid': index + 1,
                'fid': index + 1, 'path': 'fixture.cbz', 'sha256': aid, 'availability': 'validated'}
        candidate, ledger = clone_dataset(state, 10, 'identified')
        validate(candidate)
        self.assertEqual(len(candidate['assets']), 30)
        self.assertEqual(len(ledger), 60)
        self.assertEqual(len(set(candidate['assets']) & set(state['assets'])), 0)
        self.assertNotIn('CANARY', str(candidate))
        self.assertIn('merge_work', [record['action'] for record in ledger])
        self.assertIn('separate_edition', [record['action'] for record in ledger])
        self.assertTrue(any(a.get('resolution', {}).get('status') == 'blocked' for a in candidate['assets'].values()))
        self.assertTrue(any(o.get('bundle') for o in candidate['origins'].values()))
        self.assertTrue(any(e.get('publication') for e in candidate['editions'].values()))
        self.assertTrue(candidate['operations'])
        self.assertEqual(clone_dataset(state, 10, 'identified'), (candidate, ledger))
