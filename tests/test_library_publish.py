import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from library.catalog import Catalog, LibraryError
from library.publication import Capacity
from library.intake import receive
from test_library_intake import SOURCE
from test_library_readers import image_bytes


def setup_library(root,test):
    reception = root / 'reception'
    (reception / 'package').mkdir(parents=True)
    path = reception / 'package/album.cbz'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('001.png', image_bytes())
    from tests.postgres_fixture import create_store
    from library.primitives import empty
    store = Catalog(root/'state',store=create_store(test,root/'state',empty()))
    (root/'postgresql.json').write_bytes((store.directory/'postgresql.json').read_bytes())
    (root/'postgresql.json').chmod(0o600)
    state = receive(store, reception, {'items': {SOURCE: {'title': 'Album'}}},
        {'submissions': {SOURCE: {'status': 'queued', 'name': 'tracked', 'package_id': 1}}},
        [{'pid': 1, 'name': 'tracked', 'folder': 'package', 'links': [
            {'fid': 1, 'name': 'album.cbz', 'status': 0, 'size': path.stat().st_size}]}], stable_seconds=.01)
    asset = next(iter(state['assets']))
    store.save(state, expected=state['revision'])
    return store, reception, path, asset


def page_bytes(path):
    with zipfile.ZipFile(path) as archive:
        return archive.read('001.png')


class PublishTests(unittest.TestCase):







    def test_default_capacity_checks_remaining_copy_without_disk_reserve(self):
        capacity = Capacity(free=lambda _: 1024)
        capacity.check(Path('.'), 1024)
        with self.assertRaises(LibraryError):
            capacity.check(Path('.'), 1025)
