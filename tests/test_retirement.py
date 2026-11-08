import copy
import tempfile
import unittest
import zipfile
import argparse
import contextlib
import io
from unittest.mock import patch
from pathlib import Path

from library.catalog import Catalog, LibraryError, apply_effect, atomic_yaml
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


def fixture(root):
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
    store = Catalog(root / 'state')
    receive(store, reception, {'items': {SOURCE: record}}, {'submissions': {SOURCE: entry}},
            [package], incremental=True, stable_seconds=.001)
    return store, reception, collector, original, Native(package)


class RetirementTests(unittest.TestCase):
    def test_multipart_history_requires_every_part_and_container_end_proof(self):
        from library.readers import sha256, fingerprint
        from library.catalog import digest
        from test_library_bundles import inputs
        import yaml
        for mode in ('missing-end', 'missing-part', 'complete'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                reception, parent, captured, journal, queue = inputs(root, [('One.cbz', comic())])
                store = Catalog(root / 'state')
                state = receive(store, reception, captured, journal, queue, stable_seconds=.001)
                oid = next(k for k, o in state['origins'].items() if o.get('bundle'))
                origin = state['origins'][oid]
                first = parent.with_name('Album.part1.rar')
                parent.rename(first)
                second = first.with_name('Album.part2.rar')
                second.write_bytes(b'retained second volume')
                parts = [{'path': p.name, 'fid': i, 'url': 'https://1fichier.com/?' + str(i),
                          'sha256': sha256(p), 'fingerprint': list(fingerprint(p))}
                         for p, i in ((first, 1), (second, 2))]
                origin.update(path=first.name, fingerprint=list(fingerprint(first)), source_parts=parts,
                              source_set_sha256=digest([{'name': p['path'], 'sha256': p['sha256'],
                                                       'size': p['fingerprint'][2]} for p in parts]))
                state['origins']['second-volume'] = {'source': SOURCE, 'pid': 1, **parts[1],
                    'availability': 'multipart_member', 'multipart_parent': oid}
                store.save(state, expected=state['revision'])
                manifest_path = store.directory / 'bundle-inputs' / origin['sha256'] / 'manifest.yaml'
                manifest = yaml.safe_load(manifest_path.read_text())
                manifest.update(source_parts=[{'name': p['path'], 'sha256': p['sha256'],
                                               'size': p['fingerprint'][2]} for p in parts],
                                volume_end_verified=mode != 'missing-end')
                atomic_yaml(manifest_path, manifest)
                links = [p['url'] for p in parts]
                queue[0].update(name='Booksys-parts', dest=0)
                queue[0]['links'] = [{'fid': p['fid'], 'name': p['path'], 'url': p['url'], 'status': 0} for p in parts]
                journal['submissions'][SOURCE].update(name='Booksys-parts', links=links, primary_links=links)
                captured['items'][SOURCE]['downloads'] = {'one': {'hoster': '1fichier', 'urls': links}}
                collector = root / 'collector'
                atomic_yaml(collector / 'monitor.yaml', journal)
                atomic_yaml(collector / 'state.yaml', captured)
                client = Native(queue[0])
                if mode == 'missing-part': second.unlink()
                result = Retirement(client, store, reception, collector / 'monitor.yaml', collector / 'state.yaml').process(SOURCE, apply=True)
                self.assertEqual(result['outcome'], 'retired' if mode == 'complete' else 'blocked', result)
                self.assertTrue(first.is_file())
                if mode == 'complete':
                    updated = receive(store, reception, captured, journal,
                                      logical_snapshot(client, collector / 'mirror-routing.yaml'),
                                      incremental=True, stable_seconds=.001)
                    self.assertEqual(updated['origins'][oid]['availability'], 'bundle')
                    self.assertEqual(updated['origins']['second-volume']['availability'], 'multipart_member')

    def test_prepared_operator_confirmation_and_publication_survive_retirement(self):
        import yaml
        from library.resolution import Resolution
        from library.publish import Publisher, Capacity
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, reception, collector, original, client = fixture(root)
            state = store.read()
            asset = next(iter(state['assets']))
            apply_effect(state, {'action': 'identify', 'target': asset, 'values': {
                'work_id': 'work', 'edition_id': 'edition', 'work': {'title': 'Album'},
                'edition': {'publisher': 'Publisher', 'language': 'fr',
                            'evidence': {'operator': 'fixture', 'edition': 'labelled'}}}})
            apply_effect(state, {'action': 'select', 'target': asset, 'values': {}})
            store.save(state, expected=state['revision'])
            resolution = Resolution(store, reception)
            page = resolution.page(asset, 1)
            preview = resolution.preview(asset, {'edition_id': 'edition', 'work_id': 'work',
                'work': {}, 'edition': {}, 'select': True, 'proof': [
                    {'page': page['id'], 'field': f, 'text': 'Checked visible ' + f}
                    for f in ('title', 'publisher', 'language')], 'reason': 'Checked visible edition',
                'supersedes': None, 'restore': None})
            Retirement(client, store, reception, collector / 'monitor.yaml', collector / 'state.yaml').process(SOURCE, apply=True)
            receive(store, reception, yaml.safe_load((collector / 'state.yaml').read_text()),
                    yaml.safe_load((collector / 'monitor.yaml').read_text()),
                    logical_snapshot(client, collector / 'mirror-routing.yaml'),
                    incremental=True, stable_seconds=.001)
            self.assertEqual(resolution.confirm({'request_id': 'after-retirement', 'preview': preview['id']})['status'], 'recorded')
            publisher = Publisher(store, reception, root / 'library', isolated=True, capacity=Capacity(reserve=0))
            resolution.apply(publisher)
            result = store.read()['editions']['edition']['publication']
            self.assertTrue((root / 'library/bd' / result['target'] / 'book.cbz').is_file())
            self.assertTrue(original.is_file())

    def test_interrupted_preparation_and_confirmation_resume_without_duplicate_removal(self):
        for phase in ('prepared', 'removed'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                store, reception, collector, original, client = fixture(Path(directory))
                def crash(at):
                    if at == phase: raise RuntimeError('interrupted')
                service = Retirement(client, store, reception, collector / 'monitor.yaml',
                                     collector / 'state.yaml', fault=crash)
                with self.assertRaises(RuntimeError): service.process(SOURCE, apply=True)
                restarted = Retirement(client, store, reception, collector / 'monitor.yaml', collector / 'state.yaml')
                self.assertEqual(restarted.process(SOURCE, apply=True)['outcome'], 'retired')
                self.assertEqual(client.removed, [1])
                self.assertTrue(original.is_file())

    def test_source_drift_before_removal_preserves_the_pyload_package(self):
        with tempfile.TemporaryDirectory() as directory:
            store, reception, collector, original, client = fixture(Path(directory))
            def drift(phase):
                if phase == 'prepared':
                    original.write_bytes(b'changed during evidence write')
            service = Retirement(client, store, reception, collector / 'monitor.yaml',
                                 collector / 'state.yaml', fault=drift)
            with self.assertRaisesRegex(LibraryError, 'changed'):
                service.process(SOURCE, apply=True)
            self.assertEqual(client.removed, [])

    def test_complete_cycle_retires_then_restarts_with_a_historical_operator_row(self):
        from ops.library_cycle import cycle
        from library.web import build_projection
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, reception, collector, original, client = fixture(root)
            credentials = root / 'credentials.json'
            credentials.write_text('{"api_key":"fixture"}')
            args = argparse.Namespace(root=root, reception=reception, captured=collector / 'state.yaml',
                submissions=collector / 'monitor.yaml', queue=None, credentials=credentials,
                endpoint='http://127.0.0.1:8000', asset=[], printed=False, budget=0,
                auto=False, route_mirrors=False, retire_packages=True, pyload_scripts=None)
            with patch('pipeline.Pyload', return_value=client), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cycle(args), 0)
                self.assertEqual(client.removed, [1])
                self.assertEqual(cycle(args), 0)
            projected = build_projection(root)
            self.assertEqual(projected['rows'][0]['retirement'], 'retired')
            self.assertTrue(original.is_file())

    def test_finished_queue_package_is_parked_before_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            store, reception, collector, original, client = fixture(Path(directory))
            client.packages[1]['dest'] = 1
            service = Retirement(client, store, reception, collector / 'monitor.yaml', collector / 'state.yaml')
            self.assertEqual(service.process(SOURCE, apply=True)['outcome'], 'retired')
            self.assertTrue(original.is_file())

    def test_fiches_exports_and_mirror_routing_continue_without_a_live_package(self):
        import yaml
        from pipeline import run_fiche_once
        from mirror_routing import route_package
        with tempfile.TemporaryDirectory() as directory:
            store, reception, collector, original, client = fixture(Path(directory))
            record = yaml.safe_load((collector / 'state.yaml').read_text())['items'][SOURCE]
            entry = yaml.safe_load((collector / 'monitor.yaml').read_text())['submissions'][SOURCE]
            Retirement(client, store, reception, collector / 'monitor.yaml', collector / 'state.yaml').process(SOURCE, apply=True)
            class OfflineBrowser:
                def __call__(self, url): raise AssertionError('No page fetch expected')
                def cover(self, url): raise AssertionError('No cover fetch expected')
            self.assertTrue(run_fiche_once(collector, OfflineBrowser(), client, reception, minimum_free=0))
            exported = yaml.safe_load((collector / 'catalogue.yaml').read_text())['items'][SOURCE]
            self.assertEqual(exported['files'][0]['name'], 'album.cbz')
            self.assertEqual(exported['retirement'], 'retired')
            result = route_package(client, SOURCE, entry, record, store.read(), reception,
                                   collector / 'mirror-routing.yaml', apply=True)
            self.assertEqual(result['outcome'], 'retired')
            self.assertTrue((original.parent / 'fiche.md').is_file())

    def test_active_processing_hooks_and_unexpected_missing_packages_block_removal(self):
        for case in ('active', 'extract', 'scripts', 'missing', 'changed-url', 'changed-bytes'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                store, reception, collector, original, client = fixture(Path(directory))
                if case == 'active': client.active = [{'fid': 10, 'package_id': 1}]
                if case == 'extract': client.extract = 'True'
                if case == 'scripts': client.scripts = 'True'
                if case == 'missing': client.packages.clear()
                if case == 'changed-url': client.packages[1]['links'][0]['url'] = 'https://other.example/unowned'
                if case == 'changed-bytes': original.write_bytes(b'changed')
                service = Retirement(client, store, reception, collector / 'monitor.yaml', collector / 'state.yaml')
                self.assertEqual(service.process(SOURCE, apply=True)['outcome'], 'blocked')
                self.assertEqual(client.removed, [])

    def test_successful_fallback_retires_both_packages_and_keeps_failed_bytes(self):
        import yaml
        with tempfile.TemporaryDirectory() as directory:
            store, reception, collector, original, client = fixture(Path(directory))
            original.write_bytes(b'failed original retained')
            state = store.read()
            for origin in state['origins'].values():
                origin.update(availability='rejected', reason='BadZipFile: Bad CRC')
            store.save(state, expected=state['revision'])
            (reception / 'fallback').mkdir()
            (reception / 'fallback/album.cbz').write_bytes(comic())
            attempt = copy.deepcopy(client.packages[1])
            attempt.update(pid=2, name='Booksys-fallback-fixture', folder='fallback',
                           mirror_source=SOURCE, mirror_original_pid=1)
            attempt['links'][0].update(fid=20, url='https://other.example/album')
            client.packages[2] = attempt
            captured = yaml.safe_load((collector / 'state.yaml').read_text())
            captured['items'][SOURCE]['downloads']['two'] = {'hoster': 'other', 'urls': ['https://other.example/album']}
            atomic_yaml(collector / 'state.yaml', captured)
            journal = yaml.safe_load((collector / 'monitor.yaml').read_text())
            atomic_yaml(collector / 'mirror-routing.yaml', {'schema': 1, 'packages': {'1': {
                'source': SOURCE, 'phase': 'active', 'kind': 'fallback', 'attempt_pid': 2,
                'attempt_name': attempt['name'], 'attempt_folder': 'fallback',
                'fallback_links': ['https://other.example/album']}}})
            receive(store, reception, captured, journal, [attempt], stable_seconds=.001, incremental=True)
            service = Retirement(client, store, reception, collector / 'monitor.yaml', collector / 'state.yaml')
            result = service.process(SOURCE, apply=True)
            self.assertEqual(result['outcome'], 'retired', result)
            self.assertEqual(sorted(client.removed), [1, 2])
            self.assertEqual(original.read_bytes(), b'failed original retained')

    def test_bundle_waits_for_all_children_then_preserves_their_local_access(self):
        import yaml
        with tempfile.TemporaryDirectory() as directory:
            store, reception, collector, original, client = fixture(Path(directory))
            bundle = original.with_suffix('.zip')
            with zipfile.ZipFile(bundle, 'w') as archive:
                archive.writestr('One.cbz', comic())
                archive.writestr('Two.cbz', comic())
            package = client.packages[1]
            package['links'][0].update(name=bundle.name, size=bundle.stat().st_size)
            store = Catalog(Path(directory) / 'bundle-state')
            captured = yaml.safe_load((collector / 'state.yaml').read_text())
            journal = yaml.safe_load((collector / 'monitor.yaml').read_text())
            receive(store, reception, captured, journal, [package], stable_seconds=.001,
                    incremental=True, inspection_limit=1)
            service = Retirement(client, store, reception, collector / 'monitor.yaml', collector / 'state.yaml')
            self.assertEqual(service.process(SOURCE)['outcome'], 'blocked')
            receive(store, reception, captured, journal, [package], stable_seconds=.001,
                    incremental=True, inspection_limit=1)
            self.assertEqual(service.process(SOURCE, apply=True)['outcome'], 'retired')
            state = receive(store, reception, captured, journal,
                            logical_snapshot(client, collector / 'mirror-routing.yaml'),
                            stable_seconds=.001, incremental=True, inspection_limit=1)
            self.assertTrue(bundle.is_file())
            for asset in state['assets']:
                self.assertTrue(resolve_asset(store, reception, state, asset).is_file())

    def test_lost_response_is_reconciled_without_resending(self):
        for removed in (False, True):
            with self.subTest(removed=removed), tempfile.TemporaryDirectory() as directory:
                store, reception, collector, original, client = fixture(Path(directory))
                client.lose_response = removed
                client.lose_before_removal = not removed
                service = Retirement(client, store, reception, collector / 'monitor.yaml', collector / 'state.yaml')
                with self.assertRaises(OSError):
                    service.process(SOURCE, apply=True)
                restarted = Retirement(client, store, reception, collector / 'monitor.yaml', collector / 'state.yaml')
                if removed:
                    self.assertEqual(restarted.process(SOURCE, apply=True)['outcome'], 'retired')
                    self.assertEqual(client.removed, [1])
                else:
                    with self.assertRaisesRegex(LibraryError, 'uncertain'):
                        restarted.process(SOURCE, apply=True)
                    self.assertEqual(client.removed, [])
                self.assertTrue(original.is_file())

    def test_retired_download_survives_intake_restart_and_local_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, reception, collector, original, client = fixture(root)
            before = original.read_bytes()
            service = Retirement(client, store, reception, collector / 'monitor.yaml', collector / 'state.yaml')
            self.assertEqual(service.process(SOURCE)['outcome'], 'eligible')
            self.assertEqual(client.removed, [])
            self.assertEqual(service.process(SOURCE, apply=True)['outcome'], 'retired')
            self.assertEqual(client.removed, [1])
            queue = logical_snapshot(client, collector / 'mirror-routing.yaml')
            self.assertEqual(queue[0]['retirement'], 'retired')
            import yaml
            state = receive(store, reception, yaml.safe_load((collector / 'state.yaml').read_text()),
                            yaml.safe_load((collector / 'monitor.yaml').read_text()), queue,
                            incremental=True, stable_seconds=.001)
            self.assertEqual(next(iter(state['origins'].values()))['availability'], 'validated')
            self.assertEqual(resolve_asset(store, reception, state, next(iter(state['assets']))).read_bytes(), before)
            restarted = Retirement(client, store, reception, collector / 'monitor.yaml', collector / 'state.yaml')
            self.assertEqual(restarted.process(SOURCE, apply=True)['outcome'], 'retired')
            self.assertEqual(client.removed, [1])
            self.assertEqual(original.read_bytes(), before)
