from postgres_fixture import create_catalog
from library.operational_state import CollectorState, NativeHistory, RuntimeObservation
import copy
import tempfile
import unittest
import zipfile
import argparse
import contextlib
import io
from unittest.mock import patch
from pathlib import Path

from library.catalog import Catalog, LibraryError, atomic_yaml
from library.intake import receive
from library.origins import resolve_asset
from library.retirement import Retirement
from mirrors import logical_snapshot
from test_library_bundles import comic
from test_library_intake import SOURCE


class Native:
    """External downloader boundary; no hoster access or shared state."""

    def __init__(self, package):
        self.packages = {package['pid']: copy.deepcopy(package)}
        self.active = []
        self.removed = []
        self.lose_response = False
        self.lose_before_removal = False
        self.extract = False
        self.scripts = False

    def call(self, method, *, write=False, **values):
        if method in ('get_queue_data', 'get_collector_data'):
            dest = int(method == 'get_queue_data')
            return copy.deepcopy([p for p in self.packages.values() if p['dest'] == dest])
        if method == 'status_downloads':
            return self.active
        if method == 'get_plugin_config':
            return {'ExtractArchive': {'items': [{'name': 'enabled', 'value': self.extract}]},
                    'ExternalScripts': {'items': [{'name': 'enabled', 'value': self.scripts}]}}
        if method == 'get_package_data':
            return copy.deepcopy(self.packages[values['package_id']])
        if method == 'move_package':
            self.packages[values['package_id']]['dest'] = values['destination']
            return None
        if method == 'delete_packages':
            if self.lose_before_removal:
                raise OSError('Lost response before removal')
            for pid in values['package_ids']:
                self.removed.append(pid)
                del self.packages[pid]
            if self.lose_response:
                raise OSError('Lost response after removal')
            return None
        raise AssertionError(method)


def fixture(root,test):
    reception = root / 'reception'
    (reception / 'owned').mkdir(parents=True)
    original = reception / 'owned/album.cbz'
    original.write_bytes(comic())
    package = {'pid': 1, 'name': 'Booksys-fixture Album', 'folder': 'owned', 'dest': 0,
               'links': [{'fid': 10, 'name': 'album.cbz', 'url': 'https://1fichier.com/?album',
                          'status': 0, 'size': original.stat().st_size}]}
    links = [package['links'][0]['url']]
    record = {'title': 'Album', 'page': {'captured_at': '2026-11-08T12:00:00Z', 'details': {}, 'description': ''},
              'downloads': {'one': {'hoster': '1fichier', 'urls': links, 'metadata': 'CBZ'}}}
    entry = {'status': 'queued', 'package_id': 1, 'name': package['name'],
             'links': links, 'primary_links': links, 'primary_host': '1fichier'}
    collector = root / 'collector'
    atomic_yaml(collector / 'state.yaml', {'items': {SOURCE: record}})
    atomic_yaml(collector / 'monitor.yaml', {'submissions': {SOURCE: entry}})
    from tests.postgres_fixture import create_store
    from library.primitives import empty
    from library.operational_state import CollectorState
    import shutil
    backend=create_store(test,root/'state',empty())
    shutil.copyfile(root/'state/postgresql.json',collector/'postgresql.json')
    (collector/'postgresql.json').chmod(0o600)
    canonical=CollectorState(collector);canonical.capture(SOURCE,record);canonical.submission(SOURCE,entry)
    RuntimeObservation(backend._database).activate({'fixture':True},role='library')
    store = Catalog(root / 'state',store=backend)
    receive(store, reception, {'items': {SOURCE: record}}, {'submissions': {SOURCE: entry}},
            [package], incremental=True, stable_seconds=.001)
    return store, reception, collector, original, Native(package)


class RetirementTests(unittest.TestCase):


    def test_bulk_native_calls_are_spaced_and_failed_writes_are_not_retried(self):
        from ops.retire_packages import PacedPyload
        clock, starts = [10.0], []
        def sleep(seconds):
            clock[0] += seconds
        def call(method, **values):
            starts.append(clock[0])
            if method == 'delete_packages':
                raise OSError('Uncertain response')
            return []
        client = PacedPyload('http://127.0.0.1:8000', 'fixture', .7)
        with patch('ops.retire_packages.time.monotonic', side_effect=lambda: clock[0]), \
             patch('ops.retire_packages.time.sleep', side_effect=sleep), \
             patch('pipeline.Pyload.call', side_effect=call) as native:
            client.call('get_queue_data')
            client.call('get_collector_data')
            with self.assertRaisesRegex(OSError, 'Uncertain'):
                client.call('delete_packages', write=True, package_ids=[1])
        self.assertEqual(native.call_count, 3)
        self.assertAlmostEqual(starts[1] - starts[0], .7)
        self.assertAlmostEqual(starts[2] - starts[1], .7)







    def test_active_processing_hooks_and_unexpected_missing_packages_block_removal(self):
        for case in ('active', 'extract', 'scripts', 'missing', 'changed-url', 'changed-bytes', 'unknown-state', 'changed-fid'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                store, reception, collector, original, client = fixture(Path(directory),self)
                if case == 'active': client.active = [{'fid': 10, 'package_id': 1}]
                if case == 'extract': client.extract = 'True'
                if case == 'scripts': client.scripts = 'True'
                if case == 'missing': client.packages.clear()
                if case == 'changed-url': client.packages[1]['links'][0]['url'] = 'https://other.example/unowned'
                if case == 'changed-bytes': original.write_bytes(b'changed')
                if case == 'unknown-state': client.packages[1]['links'][0]['status'] = 99
                if case == 'changed-fid': client.packages[1]['links'][0]['fid'] = 999
                service = Retirement(client, store, reception, collector / 'monitor.yaml', collector / 'state.yaml')
                self.assertEqual(service.process(SOURCE, apply=True)['outcome'], 'blocked')
                self.assertEqual(client.removed, [])




