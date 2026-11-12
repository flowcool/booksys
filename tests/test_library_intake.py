from postgres_fixture import create_catalog
import tempfile
import unittest
import zipfile
from pathlib import Path

from library.catalog import Catalog
from library.intake import receive
from test_library_readers import image_bytes

SOURCE = 'https://www7.bookys-ebooks.com/bandes-dessinees/1-album'


class IntakeTests(unittest.TestCase):

    def test_size_mismatch_does_not_admit_corrupt_truncated_or_unfinished_inputs(self):
        from unittest.mock import patch
        from library.readers import inspect_file
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); reception = root / 'reception'; reception.mkdir()
            good = reception / 'good.cbz'
            with zipfile.ZipFile(good, 'w') as archive:
                archive.writestr('001.png', image_bytes())
            truncated = reception / 'truncated.cbz';truncated.write_bytes(good.read_bytes()[:-20])
            corrupt = reception / 'corrupt.cbz'
            with zipfile.ZipFile(corrupt, 'w') as archive:
                archive.writestr('001.png', b'not an image')
            captured = {'items': {SOURCE: {'title': 'Album'}}}
            journal = {'submissions': {SOURCE: {'status': 'queued', 'name': 'tracked', 'package_id': 1}}}
            queue = [{'pid': 1, 'name': 'tracked', 'folder': '', 'links': [
                {'fid': index, 'name': path.name, 'status': status, 'size': path.stat().st_size + 777}
                for index, (path, status) in enumerate([(truncated, 0), (corrupt, 0), (good, 7)], 1)]}]
            with patch('library.intake.inspect_file', wraps=inspect_file) as reader:
                state = receive(create_catalog(self,root / 'state'), reception, captured, journal, queue, stable_seconds=.01)
                self.assertEqual(reader.call_count, 2)
            self.assertFalse(state['assets'])
            self.assertEqual({o['fid']: o['availability'] for o in state['origins'].values()},
                             {1: 'rejected', 2: 'rejected', 3: 'pending'})


