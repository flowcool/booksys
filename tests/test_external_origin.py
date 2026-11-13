import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from library.business_stages import BusinessStages
from library.intake import external_origins
from library.native_stages import NativeStages
from library.origins import resolve_asset_origin
from library.stage_model import Task
from postgres_fixture import create_catalog
from test_library_readers import image_bytes


class ExternalOriginTests(unittest.TestCase):
    def test_same_file_reuses_origin_and_asset_and_enters_ordinary_stages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            originals = root/'originals'
            originals.mkdir()
            path = originals/'Album.cbz'
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('001.png', image_bytes())
                archive.writestr('ComicInfo.xml', '<ComicInfo><Series>Album</Series>'
                    '<Title>Unverified title</Title><Genre>Adventure</Genre></ComicInfo>')
            first = list(external_origins(originals))
            self.assertEqual(first, list(external_origins(originals)))
            oid, origin = first[0]
            catalog = create_catalog(self, root/'state')
            adapter = NativeStages(catalog, root/'downloads', root/'library', root/'collector',
                browser=None, client=None, snapshots=root/'snapshots')
            tasks = MagicMock()
            at = datetime.now(timezone.utc)
            with patch('library.intake.EXTERNAL_ROOT', originals), \
                 patch('library.native_stages.CollectorState') as collector, \
                 patch('library.native_stages.RuntimeObservation') as observation, \
                 patch('library.native_stages.OperationalState') as cursor:
                collector.return_value.monitor.return_value = {'submissions': {}}
                observation.return_value.queue.return_value = ([], 0)
                cursor.return_value.discovery.return_value = ''
                for _ in range(2):
                    self.assertEqual(adapter.discover_intake(tasks, now=at), 1)
                    obligation = tasks.discover.call_args.args[0]
                    task = Task(obligation.key, obligation.stage, obligation.entity,
                        obligation.input_revision, obligation.method, None, at.isoformat())
                    self.assertEqual(adapter.ready(task, now=at), '')
                    self.assertEqual(adapter.receipt(task), ('absent', ''))
                    prepared = adapter.execute(task, now=at).prepared
                    catalog.backend().commit(prepared.change, expected_revision=prepared.revision,
                                             conflict=prepared.conflict)
                self.assertEqual(tasks.discover.call_args_list[0], tasks.discover.call_args_list[1])
                state = catalog.read()
                self.assertEqual((len(state['origins']), len(state['assets'])), (1, 1))
                aid, asset = next(iter(state['assets'].items()))
                self.assertEqual(resolve_asset_origin(catalog, root/'downloads', state, aid), (oid, path))
            self.assertIsNone(state['origins'][oid]['pid'])
            self.assertIsNone(state['origins'][oid]['fid'])
            self.assertEqual(asset['embedded']['Title'], 'Unverified title')
            self.assertEqual(asset['assertions'][0]['upstream'], 'embedded-unverified')
            obligations = BusinessStages.obligations(state, aid, now=datetime.now(timezone.utc))
            self.assertEqual({item.stage for item in obligations}, {'metadata', 'copies'})

    def test_scan_excludes_managed_stream_temporary_and_symlink_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('Series', 'booksys', '@eaDir', 'ug-tmp-old', 'Series/@eaDir', 'Series/booksys'):
                folder = root/name
                folder.mkdir(parents=True, exist_ok=True)
                (folder/'Album.cbz').write_bytes(b'fixture')
            (root/'Series/ug-tmp-file.cbz').write_bytes(b'fixture')
            (root/'linked.cbz').symlink_to(root/'Series/Album.cbz')
            (root/'linked').symlink_to(root/'Series', target_is_directory=True)
            origins = list(external_origins(root))
            self.assertEqual([origin['path'] for _, origin in origins], ['Series/Album.cbz'])
