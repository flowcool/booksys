from postgres_fixture import create_catalog
import tempfile
import unittest
import zipfile
from pathlib import Path

from library.catalog import Catalog
from library.intake import receive
from test_library_readers import image_bytes

SOURCE = 'https://www7.bookys-ebooks.com/bandes-dessinees/1-album'


class IntakeTests(unittest.TestCase):

    def test_size_mismatch_does_not_admit_corrupt_truncated_or_unfinished_inputs(self):
        from unittest.mock import patch
        from library.readers import inspect_file
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); reception = root / 'reception'; reception.mkdir()
            good = reception / 'good.cbz'
            with zipfile.ZipFile(good, 'w') as archive:
                archive.writestr('001.png', image_bytes())
            truncated = reception / 'truncated.cbz';truncated.write_bytes(good.read_bytes()[:-20])
            corrupt = reception / 'corrupt.cbz'
            with zipfile.ZipFile(corrupt, 'w') as archive:
                archive.writestr('001.png', b'not an image')
            captured = {'items': {SOURCE: {'title': 'Album'}}}
            journal = {'submissions': {SOURCE: {'status': 'queued', 'name': 'tracked', 'package_id': 1}}}
            queue = [{'pid': 1, 'name': 'tracked', 'folder': '', 'links': [
                {'fid': index, 'name': path.name, 'status': status, 'size': path.stat().st_size + 777}
                for index, (path, status) in enumerate([(truncated, 0), (corrupt, 0), (good, 7)], 1)]}]
            with patch('library.intake.inspect_file', wraps=inspect_file) as reader:
                state = receive(create_catalog(self,root / 'state'), reception, captured, journal, queue, stable_seconds=.01)
                self.assertEqual(reader.call_count, 2)
            self.assertFalse(state['assets'])
            self.assertEqual({o['fid']: o['availability'] for o in state['origins'].values()},
                             {1: 'rejected', 2: 'rejected', 3: 'pending'})


    def test_validated_origin_survives_missing_or_reused_package(self):
        self.check_missing_package('validated', 'validated')

    def check_missing_package(self, availability, expected):
        from test_library_publish import setup_library
        with tempfile.TemporaryDirectory() as folder:
            store, reception, _, key = setup_library(Path(folder), self)
            state = store.working_set('admit', {'assets': [key]})
            oid = state['assets'][key]['origins'][0]
            state['origins'][oid]['availability'] = availability
            store.save(state, expected=state['revision'])
            for queue in ([], [{'pid': 1, 'name': 'reused'}]):
                result = receive(store, reception, {'items': {SOURCE: {}}}, {'submissions': {
                    SOURCE: {'status': 'queued', 'package_id': 1, 'name': 'tracked'}}}, queue)
                self.assertEqual(result['origins'][oid]['availability'], expected)

    def test_unvalidated_origin_becomes_package_missing(self):
        self.check_missing_package('pending', 'package_missing')

    def test_copies_waits_for_validated_origin_with_matching_hash(self):
        from datetime import datetime, timezone
        from unittest.mock import patch
        from library.stage_runtime import StageRuntime
        from types import SimpleNamespace
        from library.admission import projection
        from test_library_publish import setup_library
        with tempfile.TemporaryDirectory() as folder, patch('library.stage_runtime.require_nas'):
            root = Path(folder); store, reception, _, key = setup_library(root, self)
            runtime = object.__new__(StageRuntime); runtime.catalog = store; runtime.client = None
            for availability, checksum, expected in [('package_missing', key, 'intake_pending'), ('validated', '0'*64, 'intake_pending'), ('validated', key, '')]:
                state = store.working_set('admit', {'assets': [key]})
                state['origins'][state['assets'][key]['origins'][0]].update(availability=availability, sha256=checksum)
                store.save(state, expected=state['revision'])
                self.assertEqual(runtime.ready(SimpleNamespace(stage='copies', entity=key, input_revision=projection(state, key)['signature']), datetime.now(timezone.utc)), expected)
