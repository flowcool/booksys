"""Intake reuses canonical assets across unrelated source origins."""
from __future__ import annotations

import copy
import hashlib
import io
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image

from booksys_core.errors import LibraryError
from library.catalog import Catalog
from library.intake import receive
from library.primitives import digest
from postgres_fixture import create_catalog


def comic(color: str = 'white') -> bytes:
    image = io.BytesIO()
    Image.new('RGB', (8, 8), color).save(image, format='PNG')
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        for name, content in [('001.png', image.getvalue()),
                              ('ComicInfo.xml', b'<ComicInfo><Genre>Adventure</Genre></ComicInfo>')]:
            archive.writestr(zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0)), content)
    return output.getvalue()


class IntakeAssetReuseTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.reception = self.root / 'reception'
        self.reception.mkdir()
        work = self.root / 'work'
        work.mkdir()
        decoder = patch('library.decode.WORK_ROOT', str(work))
        decoder.start()
        self.addCleanup(decoder.stop)
        self.catalog: Catalog = create_catalog(self, self.root / 'state')  # type: ignore[no-untyped-call]

    def ingest(self, number: int, content: bytes, *, bundle: bool = False) -> tuple[str, str]:
        source = f'https://www.bookys-ebooks.com/{number}-album'
        path = self.reception / (f'album-{number}' + ('.zip' if bundle else '.cbz'))
        path.write_bytes(content)
        captured = {'items': {source: {'title': f'Album {number}'}}}
        journal = {'submissions': {source: {'status': 'queued', 'package_id': number, 'name': 'tracked'}}}
        queue = [{'pid': number, 'name': 'tracked', 'folder': '.', 'links': [
            {'fid': number, 'name': path.name, 'status': 0, 'size': len(content)}]}]
        receive(self.catalog, self.reception, captured, journal, queue,
                source_only=source, stable_seconds=0)
        return source, digest({'source': source, 'pid': number, 'fid': number})

    def test_t1_identical_bytes_reuse_asset_and_preserve_metadata(self) -> None:
        content = comic()
        key = hashlib.sha256(content).hexdigest()
        source_a, origin_a = self.ingest(1, content)
        state = self.catalog.result_set('retire', {'assets': [key]}, media=False)
        state['assets'][key].update(disposition='retained', overrides={'title': 'Operator title'})
        self.catalog.save(state, expected=state['revision'])
        before = copy.deepcopy(self.catalog.get('assets', key))
        assert before is not None
        self.assertTrue(before['assertions'])
        source_b, origin_b = self.ingest(2, content)
        after = self.catalog.get('assets', key)
        assert after is not None
        self.assertEqual(self.catalog.backend().ids('assets'), [key])
        self.assertEqual(set(after['origins']), {origin_a, origin_b})
        for field in ('assertions', 'overrides', 'disposition'):
            self.assertEqual(after[field], before[field])
        self.assertEqual(after['captured_hints'], {
            **before['captured_hints'], source_b: {'title': 'Album 2'}})
        self.assertIn(source_a, after['captured_hints'])
        for oid in (origin_a, origin_b):
            origin = self.catalog.get('origins', oid)
            assert origin is not None
            self.assertEqual(origin['sha256'], key)
            self.assertEqual(origin['availability'], 'validated')

    def test_t2_different_bytes_stay_distinct(self) -> None:
        hashes = []
        for number, color in enumerate(('white', 'black'), 1):
            content = comic(color)
            hashes.append(hashlib.sha256(content).hexdigest())
            _, oid = self.ingest(number, content)
            asset = self.catalog.get('assets', hashes[-1])
            assert asset is not None
            self.assertEqual(asset['origins'], [oid])
        self.assertEqual(set(self.catalog.backend().ids('assets')), set(hashes))

    def test_t3_unlinked_existing_asset_keeps_nondefault_fields(self) -> None:
        content = comic()
        key = hashlib.sha256(content).hexdigest()
        record: dict[str, Any] = {'sha256': key, 'size': len(content), 'format': 'cbz',
            'status': 'validated', 'origins': [], 'assertions': [],
            'overrides': {'title': 'Keep this title'}, 'disposition': 'retained',
            'captured_hints': {'prior-source': {'title': 'Prior hint'}}}
        state = self.catalog.result_set('retire', {'assets': [key]}, media=False)
        state['assets'][key] = copy.deepcopy(record)
        self.catalog.save(state, expected=state['revision'])
        source, oid = self.ingest(3, content)
        after = self.catalog.get('assets', key)
        assert after is not None
        self.assertEqual(after['overrides'], record['overrides'])
        self.assertEqual(after['disposition'], record['disposition'])
        self.assertEqual(after['origins'], [oid])
        self.assertEqual(after['captured_hints'], {
            **record['captured_hints'], source: {'title': 'Album 3'}})

    def test_t5_transient_conflict_retries_and_never_rejects_the_file(self) -> None:
        content = comic()
        key = hashlib.sha256(content).hexdigest()
        self.ingest(1, content)
        with patch.object(Catalog, 'extend_result', side_effect=LibraryError('Result conflict: relevant inputs changed')):
            with self.assertRaises(RuntimeError):
                self.ingest(2, content)
        origin = self.catalog.get('origins', digest({'source': 'https://www.bookys-ebooks.com/2-album', 'pid': 2, 'fid': 2}))
        self.assertTrue(origin is None or origin['availability'] != 'rejected')
        self.ingest(2, content)
        asset = self.catalog.get('assets', key)
        assert asset is not None
        self.assertEqual(len(asset['origins']), 2)

    def test_t4_bundle_member_reuses_existing_asset(self) -> None:
        content = comic()
        key = hashlib.sha256(content).hexdigest()
        _, origin_a = self.ingest(1, content)
        before = self.catalog.get('assets', key)
        assert before is not None
        bundle = io.BytesIO()
        with zipfile.ZipFile(bundle, 'w') as archive:
            archive.writestr('member.cbz', content)
        source, parent = self.ingest(2, bundle.getvalue(), bundle=True)
        child = digest({'parent_origin': parent, 'member': 'member.cbz'})
        after = self.catalog.get('assets', key)
        assert after is not None
        self.assertEqual(self.catalog.backend().ids('assets'), [key])
        self.assertEqual(set(after['origins']), {origin_a, child})
        self.assertEqual(after['assertions'], before['assertions'])
        self.assertIn(source + '#bundle-member=' + child, after['captured_hints'])

    def ingest_multipart(self, content: bytes) -> tuple[str, str, list[Path]]:
        """Use retained extraction output; volume selection, child validation and saving stay real."""
        from library.bundle_worker import POLICY
        from library.bundles import digest_name
        source = 'https://www.bookys-ebooks.com/2-multipart'
        parts = [self.reception / f'album.part{i}.rar' for i in (1, 2)]
        for number, part in enumerate(parts, 1):
            part.write_bytes(f'unchanged tracked volume {number}'.encode())
        parent_hash = hashlib.sha256(parts[0].read_bytes()).hexdigest()
        directory = self.root / 'retained-member'
        directory.mkdir(exist_ok=True)
        filename = digest_name('member.cbz') + '.cbz'
        (directory / filename).write_bytes(content)
        manifest = {'status': 'extracted', 'policy': POLICY, 'parent_sha256': parent_hash,
                    'source_parts': [{'name': part.name, 'sha256': hashlib.sha256(part.read_bytes()).hexdigest(),
                                      'size': part.stat().st_size} for part in parts],
                    'volume_end_verified': True, 'expanded_size': len(content),
                    'members': [{'member': 'member.cbz', 'file': filename, 'size': len(content),
                                 'kind': 'comic', 'sha256': hashlib.sha256(content).hexdigest()}]}
        queue = [{'pid': 2, 'name': 'tracked', 'folder': '.', 'links': [
            {'fid': index, 'name': part.name, 'status': 0, 'size': part.stat().st_size,
             'url': f'https://isolated.invalid/{index}'} for index, part in enumerate(parts, 21)]}]
        with patch('library.bundles.retain', return_value=(directory, manifest)):
            receive(self.catalog, self.reception, {'items': {source: {'title': 'Multipart parent'}}},
                    {'submissions': {source: {'status': 'queued', 'package_id': 2, 'name': 'tracked'}}},
                    queue, source_only=source, stable_seconds=0)
        parent = digest({'source': source, 'pid': 2, 'fid': 21})
        return source, digest({'parent_origin': parent, 'member': 'member.cbz'}), parts

    def test_t6_multipart_member_reuses_canonical_asset_without_overwriting_metadata(self) -> None:
        content = comic()
        key = hashlib.sha256(content).hexdigest()
        _, original = self.ingest(1, content)
        state = self.catalog.result_set('retire', {'assets': [key]}, media=False)
        state['assets'][key].update(overrides={'title': 'Keep operator title'}, disposition='retained')
        self.catalog.save(state, expected=state['revision'])
        before = self.catalog.get('assets', key)
        assert before is not None
        source, child, parts = self.ingest_multipart(content)
        after = self.catalog.get('assets', key)
        assert after is not None
        self.assertEqual(self.catalog.backend().ids('assets'), [key])
        self.assertEqual(set(after['origins']), {original, child})
        self.assertEqual({field: value for field, value in after.items() if field not in ('origins', 'captured_hints')},
                         {field: value for field, value in before.items() if field not in ('origins', 'captured_hints')})
        self.assertEqual(after['captured_hints'], {**before['captured_hints'],
            source + '#bundle-member=' + child: {'title': 'member', 'parent_source': source,
                                                 'parent_title': 'Multipart parent'}})
        self.assertEqual(self.catalog.get('origins', child)['availability'], 'validated')  # type: ignore[index]
        self.assertEqual([part.read_bytes() for part in parts],
                         [b'unchanged tracked volume 1', b'unchanged tracked volume 2'])

    def test_t7_multipart_asset_conflict_retries_without_rejecting_the_member(self) -> None:
        content = comic()
        key = hashlib.sha256(content).hexdigest()
        _, original = self.ingest(1, content)
        with patch.object(Catalog, 'extend_result', side_effect=LibraryError('Result conflict: relevant inputs changed')), \
                patch('booksys_core.diagnostics.emit') as diagnostic:
            with self.assertRaisesRegex(RuntimeError, 'Intake asset load failed'):
                self.ingest_multipart(content)
        diagnostic.assert_called_once()
        event = diagnostic.call_args.kwargs
        self.assertEqual(event['exception_type'], 'LibraryError')
        self.assertEqual((event['service'], event['stage'], event['code']), ('library', 'local', 'failed'))
        self.assertIn('load_asset', [frame['function'] for frame in event['stack']])
        self.assertNotIn('relevant inputs changed', str(event))
        self.assertEqual(self.catalog.get('assets', key)['origins'], [original])  # type: ignore[index]
        source = 'https://www.bookys-ebooks.com/2-multipart'
        self.assertFalse(any(origin.get('availability') == 'rejected'
                             for _, origin in self.catalog.records('origins') if origin['source'] == source))
        _, child, _ = self.ingest_multipart(content)
        self.assertEqual(set(self.catalog.get('assets', key)['origins']), {original, child})  # type: ignore[index]
