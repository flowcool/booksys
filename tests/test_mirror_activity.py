"""Local Activity distinguishes terminal mirrors without admitting their files."""
import tempfile
import unittest
from pathlib import Path
from test_activity import setup, catalog_fixture
from library.activity import Observer


class MirrorActivityTests(unittest.TestCase):
    def test_terminal_mirrors_are_not_reported_as_pending_transfers(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = setup(self, root)
            catalog_fixture(store, {'origins': {
                str(status): {'availability': 'pending', 'transfer_status': status,
                    'reason': 'Transfer is not confirmed finished'} for status in (1, 4, 8, 12)}, 'assets': {}})
            local = Observer(root, root/'collector', clock=lambda: 1000).snapshot()['stages']['local']
            self.assertEqual(local['counts']['waiting files'], 1)
            self.assertEqual(local['counts']['skipped mirror files'], 1)
            self.assertEqual(local['counts']['unavailable mirror files'], 2)
            items = {v['id']: v for v in local['items']}
            self.assertEqual(items['4']['state'], 'stopped')
            self.assertEqual(items['1']['reason'], 'Offline mirror link')
            self.assertEqual(items['8']['reason'], 'Failed mirror link')
