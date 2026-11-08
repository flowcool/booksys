import tempfile
import unittest
import zipfile
from pathlib import Path

from library.catalog import Catalog
from library.intake import receive
from test_library_readers import image_bytes

SOURCE = 'https://www7.bookys-ebooks.com/bandes-dessinees/1-album'


class IntakeTests(unittest.TestCase):
    def test_only_tracked_finished_files_and_mirror_origins(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            reception = root / 'reception'
            (reception / 'booksys').mkdir(parents=True)
            (reception / 'lunii').mkdir()
            with zipfile.ZipFile(reception / 'booksys/album.cbz', 'w') as archive:
                archive.writestr('001.png', image_bytes())
            original = (reception / 'booksys/album.cbz').read_bytes()
            (reception / 'booksys/mirror.cbz').write_bytes(original)
            (reception / 'lunii/manual.cbz').write_bytes(original)
            store = Catalog(root / 'state')
            journal = {'submissions': {SOURCE: {'status': 'queued', 'package_id': 1, 'name': 'tracked'}}}
            captured = {'items': {SOURCE: {'title': 'Untrusted source title'}}}
            queue = [{'pid': 1, 'name': 'tracked', 'folder': 'booksys', 'links': [
                {'fid': 1, 'name': 'album.cbz', 'status': 0, 'size': len(original)},
                {'fid': 2, 'name': 'mirror.cbz', 'status': 0, 'size': len(original)},
                {'fid': 3, 'name': 'partial.cbz', 'status': 12, 'size': 5}]},
                {'pid': 2, 'name': 'lunii', 'folder': 'lunii', 'links': [
                    {'fid': 4, 'name': 'manual.cbz', 'status': 0, 'size': len(original)}]}]
            state = receive(store, reception, captured, journal, queue, stable_seconds=.01)
            self.assertEqual(len(state['assets']), 1)
            self.assertEqual(len(state['origins']), 3)
            self.assertEqual(len(next(iter(state['assets'].values()))['origins']), 2)
            self.assertEqual(original, (reception / 'booksys/album.cbz').read_bytes())
            self.assertFalse(any(x['pid'] == 2 for x in state['origins'].values()))
