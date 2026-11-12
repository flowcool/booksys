from postgres_fixture import create_catalog
"""Intake steps retain one acquisition closure and do not rewrite unrelated origins."""
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from library.catalog import Catalog, empty
from library.intake import receive
from library.primitives import atomic_yaml
from postgres_fixture import require_postgresql
from test_library_readers import image_bytes

A = 'https://www.bookys-ebooks.com/111-a'
B = 'https://www.bookys-ebooks.com/222-b'

class IntakeStageTests(unittest.TestCase):
    def test_source_step_preserves_other_package_and_accepts_unrelated_asset_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reception = root/'reception'
            reception.mkdir()
            with zipfile.ZipFile(reception/'a.cbz', 'w') as archive:
                archive.writestr('1.png', image_bytes())
            catalog = create_catalog(self,root/'state')
            atomic_yaml(catalog.path, empty())
            require_postgresql(catalog)
            captured = {'items': {A: {'title': 'A'}, B: {'title': 'B'}}}
            monitor = {'submissions': {A: {'status': 'queued', 'package_id': 1, 'name': 'A'}, B: {'status': 'queued', 'package_id': 2, 'name': 'B'}}}
            queue = [{'pid': 1, 'name': 'A', 'folder': '.', 'links': [{'fid': 1, 'name': 'a.cbz', 'status': 0, 'size': (reception/'a.cbz').stat().st_size}]},
                     {'pid': 2, 'name': 'B', 'folder': '.', 'links': [{'fid': 2, 'name': 'missing.cbz', 'status': 0, 'size': 1}]}]
            result = receive(catalog, reception, captured, monitor, queue, source_only=A, incremental=True, inspection_limit=1, stable_seconds=.01)
            self.assertEqual(len(result['origins']), 1)
            self.assertEqual(next(iter(result['origins'].values()))['source'], A)
            self.assertEqual(len(result['assets']), 1)
