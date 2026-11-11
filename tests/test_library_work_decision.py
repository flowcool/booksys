"""Operator work confirmation uses the immutable ledger without edition data."""
from pathlib import Path
import tempfile
import unittest

from library.review import decide, report
from test_library_publish import setup_library


class WorkDecisionTests(unittest.TestCase):
    def test_work_confirmation_has_no_fabricated_edition_and_replays(self):
        with tempfile.TemporaryDirectory() as folder:
            store, reception, source, key = setup_library(Path(folder),self)
            state = store.read()
            state['assets'][key].pop('identity')
            store.save(state, expected=state['revision'])
            case = next(c for c in report(store)['cases'].values() if c.get('asset') == key)
            request = {'request_id': 'work-only', 'case': case['id'], 'version': case['version'],
                'action': 'identify_work', 'values': {'work': {'title': 'Un album autonome'},
                'evidence': {'basis': 'Operator read the title page', 'asset': key}}}
            self.assertEqual(decide(store, [request], author='flowcool')[0]['status'], 'applied')
            value = store.replay()['assets'][key]
            self.assertNotIn('identity', value)
            self.assertEqual(value['work_result']['work']['title'], 'Un album autonome')
            self.assertEqual(value['work_result']['edition_status'], 'unknown')
            self.assertEqual(len(store.ledger()), 1)
