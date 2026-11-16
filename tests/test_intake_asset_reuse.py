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
