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

    def test_prior_engine_policy_failure_retries_once_then_uses_cache(self):
        from unittest.mock import patch
        from library.catalog import digest
        from library.readers import inspect_file, fingerprint, READER_POLICY
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            reception = root / 'reception'; reception.mkdir()
            path = reception / 'album.cbz'
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('001.png', image_bytes())
            store = Catalog(root / 'state')
            state = store.read()
            oid = digest({'source': SOURCE, 'pid': 1, 'fid': 1})
            state['origins'][oid] = {'source': SOURCE, 'pid': 1, 'fid': 1, 'path': 'album.cbz',
                'sha256': None, 'availability': 'rejected', 'reader_policy': 'single-comic-2',
                'reason': 'Reader policy differs from controller', 'fingerprint': list(fingerprint(path)),
                'intake_attempts': 3, 'intake_next_attempt': 9999999999}
            store.save(state, expected=0)
            captured = {'items': {SOURCE: {'title': 'Album'}}}
            journal = {'submissions': {SOURCE: {'status': 'queued', 'package_id': 1, 'name': 'tracked'}}}
            queue = [{'pid': 1, 'name': 'tracked', 'folder': '.', 'links': [
                {'fid': 1, 'name': path.name, 'status': 0, 'size': path.stat().st_size}]}]
            with patch('library.intake.inspect_file', wraps=inspect_file) as inspector:
                first = receive(store, reception, captured, journal, queue, stable_seconds=.01, incremental=True)
                second = receive(store, reception, captured, journal, queue, stable_seconds=.01, incremental=True)
            self.assertEqual(inspector.call_count, 1)
            self.assertEqual(first['origins'][oid]['reader_policy'], READER_POLICY)
            self.assertEqual(second['origins'][oid]['availability'], 'validated')
            self.assertEqual(second['origins'][oid]['intake_attempts'], 1)
