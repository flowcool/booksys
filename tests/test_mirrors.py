import tempfile
import unittest
from pathlib import Path

from booksys import CollectionError
from pipeline import Pyload
from test_pipeline import MemoryDownloader, NEW
import yaml


class MirrorTests(unittest.TestCase):
    def test_submit_preserves_all_primary_parts_and_retains_other_urls(self):
        downloader = MemoryDownloader()
        downloader.host_availability = lambda: {'hosts': {'one': {'status': True, 'domains': ['1fichier.com']},
                                                         'two': {'status': True, 'domains': ['other.example']}}}
        record = {'title': 'Two complete parts', 'downloads': {
            'a': {'hoster': '1fichier', 'urls': ['https://1fichier.com/?part1', 'https://1fichier.com/?part2']},
            'b': {'hoster': 'other', 'urls': ['https://other.example/one', 'https://other.example/two']}}}
        with tempfile.TemporaryDirectory() as folder:
            journal = {'submissions': {}}
            downloader.submit(NEW, record, journal, Path(folder) / 'monitor.yaml')
            self.assertEqual({link['url'] for link in downloader.packages[0]['links']}, set(record['downloads']['a']['urls']))
            self.assertEqual(len(journal['submissions'][NEW]['all_links']), 4)
            self.assertEqual(journal['submissions'][NEW]['primary_host'], '1fichier')

    def test_unknown_identity_parks_only_when_every_primary_part_is_valid(self):
        from mirror_routing import route_package
        from library.catalog import atomic_yaml
        class Native:
            def __init__(self):
                self.package = {'pid': 1, 'name': 'Booksys-owned Album', 'folder': 'owned', 'dest': 1, 'order': 4,
                    'links': [{'fid': 1, 'url': 'https://1fichier.com/?a', 'status': 0, 'name': 'a.cbz'},
                              {'fid': 2, 'url': 'https://1fichier.com/?b', 'status': 0, 'name': 'b.cbz'},
                              {'fid': 3, 'url': 'https://other.example/x', 'status': 12, 'name': 'x.cbz'}]}
                self.active = [3]; self.writes = []
            def call(self, method, *, write=False, **values):
                if write: self.writes.append(method)
                if method == 'get_package_data': return self.package
                if method == 'status_downloads': return [{'fid': fid, 'package_id': 1} for fid in self.active]
                if method == 'move_package': self.package['dest'] = values['destination']; return
                if method == 'stop_downloads': self.active = []; return
                raise AssertionError(method)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root/'downloads/owned').mkdir(parents=True)
            (root/'downloads/owned/a.cbz').write_bytes(b'a'); (root/'downloads/owned/b.cbz').write_bytes(b'b')
            client = Native()
            entry = {'name': client.package['name'], 'package_id': 1, 'status': 'queued',
                     'links': [item['url'] for item in client.package['links']],
                     'primary_links': [item['url'] for item in client.package['links'][:2]]}
            origins = {'one': {'source': NEW, 'pid': 1, 'fid': 1, 'availability': 'validated'},
                       'two': {'source': NEW, 'pid': 1, 'fid': 2, 'availability': 'pending'}}
            state = {'origins': origins}
            path = root/'routing.yaml'
            self.assertEqual(route_package(client, NEW, entry, {}, state, root/'downloads', path, apply=True)['outcome'], 'pending')
            self.assertEqual(client.writes, [])
            origins['two']['availability'] = 'validated'
            outcome = route_package(client, NEW, entry, {}, state, root/'downloads', path, apply=True)
            self.assertEqual(outcome['outcome'], 'parked')
            self.assertEqual(client.writes, ['move_package', 'stop_downloads'])
            self.assertEqual(client.package['dest'], 0)
            self.assertEqual(route_package(client, NEW, entry, {}, state, root/'downloads', path, apply=True)['outcome'], 'unchanged')
            self.assertEqual(client.writes, ['move_package', 'stop_downloads'])

    def test_fallback_uses_new_package_preserves_original_and_reconciles_lost_create(self):
        from mirror_routing import route_package
        class Native(MemoryDownloader):
            def __init__(self):
                super().__init__()
                self.packages = [{'pid': 1, 'name': 'Booksys-owned Album', 'folder': 'original', 'dest': 1, 'order': 0,
                    'links': [{'fid': 10, 'url': 'https://1fichier.com/?broken', 'name': 'album.cbz', 'status': 0}]}]
                self.creates = 1; self.lose_response = True; self.actions = []
            def call(self, method, *, write=False, **values):
                if method == 'status_downloads': return []
                if method == 'order_package':
                    next(p for p in self.packages if p['pid'] == values['package_id'])['order'] = values['position']
                    return
                if method == 'add_package':
                    self.actions.append(method)
                    self.creates += 1
                    self.packages.append({'pid': self.creates, 'name': values['name'], 'folder': values['name'],
                        'order': 0, 'dest': 0, 'links': [{'fid': 20+i, 'url': u, 'name': 'album.cbz', 'status': 3}
                                                     for i,u in enumerate(values['links'])]})
                    if self.lose_response:
                        self.lose_response = False; raise OSError('Lost response')
                    return self.creates
                return super().call(method, write=write, **values)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root/'downloads/original').mkdir(parents=True)
            failed = root/'downloads/original/album.cbz'; failed.write_bytes(b'corrupt retained bytes')
            client = Native()
            entry = {'name': client.packages[0]['name'], 'package_id': 1, 'status': 'queued',
                     'primary_host': '1fichier', 'links': ['https://1fichier.com/?broken']}
            record = {'downloads': {'a': {'hoster': '1fichier', 'urls': entry['links']},
                                    'b': {'hoster': 'fallback', 'urls': ['https://other.example/a', 'https://other.example/b']}}}
            state = {'origins': {'old': {'source': NEW, 'pid': 1, 'fid': 10, 'availability': 'rejected', 'reason': 'BadZipFile: Bad CRC'}}}
            journal = root/'routing.yaml'
            with self.assertRaises(OSError): route_package(client, NEW, entry, record, state, root/'downloads', journal, apply=True)
            result = route_package(client, NEW, entry, record, state, root/'downloads', journal, apply=True)
            self.assertEqual(result['outcome'], 'fallback_active')
            self.assertEqual(len(client.actions), 1)
            self.assertEqual(client.packages[0]['dest'], 0)
            self.assertEqual(client.packages[1]['dest'], 1)
            self.assertEqual(len(client.packages[1]['links']), 2)
            self.assertEqual(failed.read_bytes(), b'corrupt retained bytes')
            operation = yaml.safe_load(journal.read_text())['packages']['1']
            self.assertEqual(operation['attempt_pid'], 2)
            from mirror_routing import restore_package
            client.packages[1]['name'] = 'foreign-drift'
            with self.assertRaisesRegex(CollectionError, 'ownership changed'):
                restore_package(client, entry, operation)
            self.assertEqual(client.packages[0]['dest'], 0)
            client.packages[1]['name'] = operation['attempt_name']
            restore_package(client, entry, operation)
            self.assertEqual(client.packages[0]['dest'], 1)
            self.assertEqual(client.packages[1]['dest'], 0)

    def test_snapshot_preserves_actual_attempt_ids_and_collector_visibility(self):
        from mirrors import logical_snapshot
        from library.catalog import atomic_yaml
        class Snapshot:
            def call(self, method):
                return ([{'pid': 2, 'name': 'Booksys-fallback-owned', 'folder': 'attempt',
                          'links': [{'fid': 20, 'url': 'https://other.example/a'}]}]
                        if method == 'get_queue_data' else
                        [{'pid': 1, 'name': 'Booksys-original', 'folder': 'original',
                          'links': [{'fid': 10, 'url': 'https://1fichier.com/?a'}]}])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'routing.yaml'
            atomic_yaml(path, {'schema': 1, 'packages': {'1': {'source': NEW,
                'phase': 'active', 'attempt_pid': 2, 'attempt_name': 'Booksys-fallback-owned',
                'input_folders': {'10': 'original'}}}})
            snapshot = logical_snapshot(Snapshot(), path)
            attempt, original = snapshot
            self.assertEqual((attempt['pid'], attempt['links'][0]['fid']), (2, 20))
            self.assertEqual(attempt['mirror_source'], NEW)
            self.assertEqual(attempt['mirror_original_pid'], 1)
            self.assertEqual(original['links'][0]['input_folder'], 'original')

    def test_identity_and_local_reader_failures_do_not_trigger_fallback(self):
        from mirror_routing import technical
        self.assertEqual(technical({'availability': 'validated', 'identity': 'unknown'}, {}), 'valid')
        for reason in ('Isolated reader failed', 'Reader policy changed', 'Timeout validating PDF', 'reserve limit'):
            self.assertEqual(technical({'availability': 'rejected', 'reason': reason}, {}), 'pending')

    def test_bundle_intrinsic_failure_and_private_journal_boundary(self):
        from mirror_routing import technical, route_package
        state = {'origins': {'child': {'parent_origin': 'parent', 'availability': 'rejected', 'reason': 'Bad CRC'}}}
        self.assertEqual(technical({'availability': 'bundle', '_id': 'parent'}, state), 'failed')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root/'source.cbz'; target.write_bytes(b'original')
            with self.assertRaisesRegex(CollectionError, 'outside reception'):
                route_package(None, NEW, {}, {}, {}, root, target, apply=True)
            self.assertEqual(target.read_bytes(), b'original')

    def test_confirmed_offline_primary_plans_fallback_without_source_file(self):
        from mirror_routing import route_package
        class Offline:
            def call(self, method, **kwargs):
                self_package = {'pid': 1, 'name': 'Booksys-owned', 'folder': 'original', 'dest': 1,
                    'links': [{'fid': 1, 'url': 'https://1fichier.com/?offline', 'status': 1, 'name': 'absent.cbz'}]}
                if method == 'get_package_data': return self_package
                raise AssertionError(method)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); reception = root/'downloads'; reception.mkdir()
            record = {'downloads': {'a': {'hoster': 'one', 'urls': ['https://1fichier.com/?offline']},
                                    'b': {'hoster': 'two', 'urls': ['https://other.example/a']}}}
            entry = {'package_id': 1, 'name': 'Booksys-owned', 'primary_links': ['https://1fichier.com/?offline']}
            self.assertEqual(route_package(Offline(), NEW, entry, record, {'origins': {}}, reception,
                root/'journal.yaml')['kind'], 'fallback')

    def test_intake_keeps_fallback_pid_fid_and_original_path(self):
        import zipfile
        from library.catalog import Catalog
        from library.intake import receive
        from test_library_readers import image_bytes
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); reception = root/'downloads'
            for name in ('original', 'attempt'):
                (reception/name).mkdir(parents=True)
                with zipfile.ZipFile(reception/name/'album.cbz', 'w') as archive:
                    archive.writestr('001.png', image_bytes())
            captured = {'items': {NEW: {'title': 'Album'}}}
            journal = {'submissions': {NEW: {'status': 'queued', 'name': 'Booksys-original', 'package_id': 1}}}
            queue = [{'pid': 1, 'name': 'Booksys-original', 'folder': 'original', 'links': [
                {'fid': 10, 'name': 'album.cbz', 'status': 0, 'size': (reception/'original/album.cbz').stat().st_size}]},
                {'pid': 2, 'name': 'Booksys-attempt', 'folder': 'attempt', 'mirror_source': NEW,
                 'mirror_original_pid': 1, 'links': [{'fid': 20, 'name': 'album.cbz', 'status': 0,
                    'size': (reception/'attempt/album.cbz').stat().st_size}]}]
            state = receive(Catalog(root/'state'), reception, captured, journal, queue, stable_seconds=.01)
            self.assertEqual({(o['pid'], o['fid'], o['path']) for o in state['origins'].values()},
                             {(1, 10, 'original/album.cbz'), (2, 20, 'attempt/album.cbz')})
            self.assertTrue(all(o['availability'] == 'validated' for o in state['origins'].values()))
