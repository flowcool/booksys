"""Terminal mirror observations never admit unfinished files."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from library.activity import Observer
from library.catalog import Catalog
from library.intake import receive


class MirrorActivityTests(unittest.TestCase):
    def test_terminal_mirrors_are_not_pending_transfers_or_admitted_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            reception = root / 'reception'
            reception.mkdir()
            source = 'https://www.bookys-ebooks.com/bandes-dessinees/1-album'
            statuses = (1, 4, 8, 12, True, '4')
            queue = [{'pid': 1, 'name': 'tracked', 'folder': '', 'links': [
                {'fid': index, 'name': f'mirror-{index}.cbz', 'status': status}
                for index, status in enumerate(statuses)]}]
            with patch('library.intake.inspect_file', side_effect=AssertionError('Unfinished file inspected')):
                state = receive(Catalog(root / 'state'), reception,
                    {'items': {source: {'title': 'Album'}}},
                    {'submissions': {source: {'status': 'queued', 'name': 'tracked', 'package_id': 1}}}, queue)
            self.assertFalse(state['assets'])
            self.assertEqual({o['availability'] for o in state['origins'].values()}, {'pending'})
            reasons = {o['fid']: o['reason'] for o in state['origins'].values()}
            self.assertEqual([reasons[i] for i in range(3)],
                ['Offline mirror link', 'Skipped mirror link', 'Failed mirror link'])
            self.assertEqual([reasons[i] for i in range(3, 6)], ['Transfer is not confirmed finished'] * 3)
            local = Observer(root, root / 'collector', clock=lambda: 1000).snapshot()['stages']['local']
            self.assertEqual(local['counts']['waiting files'], 3)
            self.assertEqual(local['counts']['skipped mirror files'], 1)
            self.assertEqual(local['counts']['unavailable mirror files'], 2)
            items = {state['origins'][item['id']]['fid']: item for item in local['items']}
            self.assertEqual([items[i]['state'] for i in range(3)], ['blocked', 'stopped', 'blocked'])
