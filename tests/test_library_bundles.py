from postgres_fixture import create_catalog
import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from library.catalog import Catalog, LibraryError
from library.intake import receive
from test_library_intake import SOURCE
from test_library_readers import image_bytes


def comic():
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr(zipfile.ZipInfo('001.png', date_time=(2020, 1, 1, 0, 0, 0)), image_bytes())
    return data.getvalue()


def inputs(root, members):
    reception = root / 'reception'
    reception.mkdir()
    path = reception / 'bundle.zip'
    with zipfile.ZipFile(path, 'w') as archive:
        for name, data in members:
            archive.writestr(name, data)
    captured = {'items': {SOURCE: {'title': 'Bundle title is not child identity'}}}
    journal = {'submissions': {SOURCE: {'status': 'queued', 'package_id': 1, 'name': 'tracked'}}}
    queue = [{'pid': 1, 'name': 'tracked', 'folder': '.', 'links': [
        {'fid': 1, 'name': path.name, 'status': 0, 'size': path.stat().st_size}]}]
    return reception, path, captured, journal, queue


class BundleTests(unittest.TestCase):


    def test_unsafe_and_recursive_bundles_leave_no_admitted_child(self):
        cases = [('../escape.cbz', comic()), ('other.zip', comic()), ('image.png', image_bytes()),
                 ('fake.pdf', b'not a PDF')]
        for name, data in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                reception, parent, captured, journal, queue = inputs(root, [('valid.cbz', comic()), (name, data)])
                original = parent.read_bytes()
                state = receive(create_catalog(self,root / 'state'), reception, captured, journal, queue, stable_seconds=.01)
                self.assertFalse(state['assets'])
                self.assertEqual(next(iter(state['origins'].values()))['availability'], 'rejected')
                self.assertEqual(parent.read_bytes(), original)










    def test_source_size_limit_is_checked_before_reading_or_extracting(self):
        from library.bundles import retain
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root / 'reception').mkdir()
            parent = root / 'reception/oversized.rar'
            with parent.open('wb') as handle:
                handle.truncate(4 * 1024**3 + 1)
            with self.assertRaisesRegex(LibraryError, 'source limit'):
                retain(parent, root / 'state', stable_seconds=.01)
            self.assertFalse((root / 'state').exists())
