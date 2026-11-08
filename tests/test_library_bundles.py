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
    def test_incremental_bundle_members_are_independent_and_rerun_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            reception, path, captured, journal, queue = inputs(root, [('Volume 1.cbz', comic()), ('Volume 2.cbz', comic())])
            before = path.read_bytes()
            store = Catalog(root / 'state')
            state = receive(store, reception, captured, journal, queue, stable_seconds=.01,
                            incremental=True, inspection_limit=1)
            children = [origin for origin in state['origins'].values() if origin.get('parent_origin')]
            self.assertEqual(len(children), 2)
            self.assertEqual(sum(child['availability'] == 'validated' for child in children), 1)
            state = receive(store, reception, captured, journal, queue, stable_seconds=.01,
                            incremental=True, inspection_limit=1)
            self.assertEqual(len(state['assets']), 1)
            self.assertEqual(len(state['origins']), 3)
            self.assertEqual(len(next(iter(state['assets'].values()))['origins']), 2)
            self.assertFalse(any(asset.get('identity') for asset in state['assets'].values()))
            self.assertEqual(path.read_bytes(), before)
            self.assertTrue((root / 'state/bundle-inputs').is_dir())
            again = receive(store, reception, captured, journal, queue, stable_seconds=.01, incremental=True, inspection_limit=1)
            self.assertEqual(len(again['origins']), 3)

    def test_derived_origin_checks_parent_and_child_before_publication_and_ocr(self):
        from library.origins import resolve_asset
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            reception, parent, captured, journal, queue = inputs(root, [('One.cbz', comic())])
            store = Catalog(root / 'state')
            state = receive(store, reception, captured, journal, queue, stable_seconds=.01)
            asset = next(iter(state['assets']))
            derived = resolve_asset(store, reception, state, asset)
            self.assertEqual(derived.read_bytes(), comic())
            derived.write_bytes(b'changed')
            with self.assertRaisesRegex(LibraryError, 'changed'):
                resolve_asset(store, reception, state, asset)
            derived.write_bytes(comic())
            parent.write_bytes(b'changed')
            with self.assertRaisesRegex(LibraryError, 'changed'):
                resolve_asset(store, reception, state, asset)

    def test_unsafe_and_recursive_bundles_leave_no_admitted_child(self):
        cases = [('../escape.cbz', comic()), ('other.zip', comic()), ('image.png', image_bytes()),
                 ('fake.pdf', b'not a PDF')]
        for name, data in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                reception, parent, captured, journal, queue = inputs(root, [('valid.cbz', comic()), (name, data)])
                original = parent.read_bytes()
                state = receive(Catalog(root / 'state'), reception, captured, journal, queue, stable_seconds=.01)
                self.assertFalse(state['assets'])
                self.assertEqual(next(iter(state['origins'].values()))['availability'], 'rejected')
                self.assertEqual(parent.read_bytes(), original)

    def test_complete_preparation_survives_crash_and_unknown_or_changed_inputs_block(self):
        from library.bundles import retain
        for phase in ('intent', 'prepared', 'retained'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                _, parent, *_ = inputs(root, [('valid.cbz', comic())])
                def crash(at):
                    if at == phase:
                        raise RuntimeError('crash')
                with self.assertRaisesRegex(RuntimeError, 'crash'):
                    retain(parent, root / 'state', stable_seconds=.01, fault=crash)
                directory, manifest = retain(parent, root / 'state', stable_seconds=.01)
                self.assertEqual(len(manifest['members']), 1)
                (directory / 'unknown').write_bytes(b'preserve')
                with self.assertRaisesRegex(LibraryError, 'Unknown'):
                    retain(parent, root / 'state', stable_seconds=.01)
                self.assertTrue((directory / 'unknown').exists())

    def test_read_only_projection_distinguishes_parent_and_each_child(self):
        from library.web import build_projection, fiche_html
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            reception, _, captured, journal, queue = inputs(root, [('One.cbz', comic())])
            receive(Catalog(root / 'state'), reception, captured, journal, queue, stable_seconds=.01)
            projection = build_projection(root)
            parent = next(row for row in projection['rows'] if row.get('kind') == 'bundle')
            child = next(row for row in projection['rows'] if row.get('kind') == 'bundle_member')
            self.assertEqual(child['parent'], parent['id'])
            self.assertIn('One.cbz', child['title'])
            self.assertIn('One.cbz', fiche_html(projection['fiches'][parent['id']]))
            self.assertEqual(parent['publication'], 'not applicable')

    def test_declared_limits_duplicates_links_encryption_and_multipart_are_rejected(self):
        import stat
        import struct
        from library.bundles import retain
        for mode in ('count', 'size', 'duplicate', 'link', 'encrypted', 'multipart'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                members = [(f'{i}.cbz', comic()) for i in range(65)] if mode == 'count' else [('One.cbz', comic())]
                if mode == 'duplicate': members.append(('ONE.CBZ', comic()))
                _, parent, *_ = inputs(root, members)
                if mode == 'link':
                    with zipfile.ZipFile(parent, 'a') as archive:
                        entry = zipfile.ZipInfo('linked.cbz'); entry.create_system = 3
                        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
                        archive.writestr(entry, 'One.cbz')
                if mode in ('size', 'encrypted', 'multipart'):
                    data = bytearray(parent.read_bytes())
                    central = data.index(b'PK\x01\x02')
                    if mode == 'size': struct.pack_into('<I', data, central + 24, 1024**3 + 1)
                    if mode == 'encrypted': struct.pack_into('<H', data, central + 8, 1)
                    if mode == 'multipart': struct.pack_into('<H', data, data.rindex(b'PK\x05\x06') + 4, 1)
                    parent.write_bytes(data)
                with self.assertRaises(LibraryError):
                    retain(parent, root / 'state', stable_seconds=.01)

    def test_rar_container_is_not_a_recursive_comic_and_multipart_is_rejected(self):
        from library.bundles import retain
        from test_library_readers import rar_bytes
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'reception').mkdir()
            parent = root / 'reception/complete.rar'
            parent.write_bytes(rar_bytes('One.cbz', comic()))
            directory, manifest = retain(parent, root / 'state', stable_seconds=.01)
            self.assertEqual(manifest['members'][0]['member'], 'One.cbz')
            self.assertEqual((directory / manifest['members'][0]['file']).read_bytes(), comic())
            split = root / 'reception/split.rar'
            split.write_bytes(rar_bytes('One.cbz', comic(), flags=2))
            with self.assertRaisesRegex(LibraryError, 'Multipart'):
                retain(split, root / 'state', stable_seconds=.01)

    def test_pending_child_is_cached_and_policy_change_revalidates_once(self):
        from unittest.mock import patch
        from library.readers import inspect_file
        from library.bundles import POLICY
        nested = io.BytesIO()
        with zipfile.ZipFile(nested, 'w') as archive:
            archive.writestr('nested.cbz', comic())
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            reception, _, captured, journal, queue = inputs(root, [('One.cbz', nested.getvalue())])
            store = Catalog(root / 'state')
            with patch('library.bundles.inspect_file', wraps=inspect_file) as inspector:
                for _ in range(2):
                    receive(store, reception, captured, journal, queue, stable_seconds=.01, incremental=True)
                self.assertEqual(inspector.call_count, 1)
                with patch('library.bundles.READER_POLICY', 'next-reader-policy'):
                    for _ in range(2):
                        receive(store, reception, captured, journal, queue, stable_seconds=.01, incremental=True)
                self.assertEqual(inspector.call_count, 2)

    def test_manifest_and_child_drift_together_cannot_reassign_an_existing_asset(self):
        import hashlib
        import yaml
        from library.catalog import atomic_yaml
        from library.origins import resolve_asset
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            reception, _, captured, journal, queue = inputs(root, [('One.cbz', comic())])
            store = Catalog(root / 'state')
            state = receive(store, reception, captured, journal, queue, stable_seconds=.01)
            asset = next(iter(state['assets']))
            child = next(o for o in state['origins'].values() if o.get('parent_origin'))
            directory = root / 'state/bundle-inputs' / child['parent_sha256']
            manifest = yaml.safe_load((directory / 'manifest.yaml').read_text())
            (directory / manifest['members'][0]['file']).write_bytes(b'%PDF-changed')
            manifest['members'][0]['sha256'] = hashlib.sha256(b'%PDF-changed').hexdigest()
            manifest['members'][0]['size'] = len(b'%PDF-changed')
            manifest['expanded_size'] = len(b'%PDF-changed')
            atomic_yaml(directory / 'manifest.yaml', manifest)
            with self.assertRaisesRegex(LibraryError, 'canonical parent'):
                resolve_asset(store, reception, state, asset)
            updated = receive(store, reception, captured, journal, queue, stable_seconds=.01, incremental=True)
            self.assertEqual(updated['assets'][asset]['origins'], state['assets'][asset]['origins'])
            self.assertTrue(any(o['availability'] == 'rejected' for o in updated['origins'].values() if not o.get('parent_origin')))

    def test_owned_engine_is_stopped_and_partial_evidence_preserved_on_timeout(self):
        import os
        from unittest.mock import patch
        import subprocess
        from library.bundles import retain
        if not os.environ.get('BOOKSYS_READER_IMAGE'):
            self.skipTest('Qualified Docker image required for owned-engine stop proof')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _, parent, *_ = inputs(root, [('One.cbz', comic())])
            with self.assertRaisesRegex(LibraryError, 'wall limit'):
                retain(parent, root / 'state', stable_seconds=.01, wall_limit=.001)
            result = subprocess.run(['docker', 'ps', '--filter', 'name=booksys-bundle-', '--format', '{{.Names}}'],
                                    capture_output=True, check=True)
            self.assertEqual(result.stdout.strip(), b'')
            self.assertTrue(any(path.is_dir() for path in (root / 'state/bundle-inputs').iterdir()))

    def test_derived_file_uses_normal_manual_identity_and_publication_gates(self):
        from library.catalog import apply_effect
        from library.publish import Publisher, Capacity
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            reception, _, captured, journal, queue = inputs(root, [('One.cbz', comic())])
            store = Catalog(root / 'state')
            state = receive(store, reception, captured, journal, queue, stable_seconds=.01)
            asset = next(iter(state['assets']))
            publisher = Publisher(store, reception, root / 'library', isolated=True, capacity=Capacity(reserve=0))
            with self.assertRaisesRegex(LibraryError, 'Identity'):
                publisher.publish(asset)
            state = store.read()
            apply_effect(state, {'action': 'identify', 'target': asset, 'values': {
                'work_id': 'work', 'edition_id': 'edition', 'work': {'title': 'Proven child'},
                'edition': {'publisher': 'Publisher', 'language': 'fr', 'evidence': {'operator': 'fixture'}}}})
            apply_effect(state, {'action': 'select', 'target': asset, 'values': {}})
            store.save(state, expected=state['revision'])
            operation = publisher.publish(asset)
            self.assertEqual(operation['phase'], 'committed')
            self.assertTrue((root / 'library/bd' / operation['target'] / 'book.cbz').exists())

    def test_failed_extraction_has_three_delayed_attempts_and_policy_reset(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            reception, _, captured, journal, queue = inputs(root, [('One.cbz', comic())])
            store = Catalog(root / 'state')
            with patch('library.bundles.retain', side_effect=LibraryError('Isolated bundle worker failed')) as engine:
                for _ in range(2):
                    receive(store, reception, captured, journal, queue, stable_seconds=.01, incremental=True)
                self.assertEqual(engine.call_count, 1)
                for _ in range(3):
                    state = store.read()
                    next(iter(state['origins'].values()))['bundle_next_attempt'] = 0
                    store.save(state, expected=state['revision'])
                    receive(store, reception, captured, journal, queue, stable_seconds=.01, incremental=True)
                self.assertEqual(engine.call_count, 3)
                with patch.dict('os.environ', {'BOOKSYS_READER_IMAGE': 'changed-policy-image'}):
                    receive(store, reception, captured, journal, queue, stable_seconds=.01, incremental=True)
                self.assertEqual(engine.call_count, 4)
                self.assertEqual(next(iter(store.read()['origins'].values()))['bundle_attempts'], 1)

    def test_corrupt_crc_and_total_expansion_are_rejected_before_admission(self):
        import struct
        from library.bundles import retain
        for kind in ('crc', 'actual_short', 'total'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                _, parent, *_ = inputs(root, [(f'{i}.cbz', comic()) for i in range(5)])
                data = bytearray(parent.read_bytes())
                with zipfile.ZipFile(parent) as archive:
                    position = archive.start_dir
                    centers = []
                    for _ in archive.infolist():
                        centers.append(position)
                        lengths = struct.unpack_from('<HHH', data, position + 28)
                        position += 46 + sum(lengths)
                if kind == 'crc':
                    header = data.index(b'PK\x03\x04')
                    name_size, extra_size = struct.unpack_from('<HH', data, header+26)
                    data[header+30+name_size+extra_size+20] ^= 1
                if kind == 'actual_short':
                    struct.pack_into('<I', data, centers[0]+24, len(comic())+1)
                if kind == 'total':
                    for index in centers: struct.pack_into('<I', data, index+24, 1024**3)
                parent.write_bytes(data)
                with self.assertRaises(LibraryError): retain(parent, root / 'state', stable_seconds=.01)

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

    def test_publication_records_the_valid_mirror_it_actually_used(self):
        from library.catalog import apply_effect, digest
        from library.publish import Publisher, Capacity
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            reception, _, captured, journal, queue = inputs(root, [('One.cbz', comic())])
            (reception / 'mirror.cbz').write_bytes(comic())
            queue[0]['links'].append({'fid': 2, 'name': 'mirror.cbz', 'status': 0, 'size': len(comic())})
            store = Catalog(root / 'state')
            state = receive(store, reception, captured, journal, queue, stable_seconds=.01)
            asset = next(iter(state['assets']))
            child = next(o for o in state['origins'].values() if o.get('parent_origin'))
            (root / 'state' / child['path']).write_bytes(b'changed')
            apply_effect(state, {'action': 'identify', 'target': asset, 'values': {
                'work_id': 'work', 'edition_id': 'edition', 'work': {'title': 'Album'},
                'edition': {'language': 'fr', 'publisher': 'Publisher', 'evidence': {'operator': 'fixture'}}}})
            apply_effect(state, {'action': 'select', 'target': asset, 'values': {}})
            store.save(state, expected=state['revision'])
            result = Publisher(store, reception, root / 'library', isolated=True, capacity=Capacity(reserve=0)).publish(asset)
            self.assertEqual(result['source'], digest({'source': SOURCE, 'pid': 1, 'fid': 2}))
