from contextlib import redirect_stderr, redirect_stdout
import hashlib
from io import StringIO
import json
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from library.business_stages import BusinessStages
from library.intake import external_origins
import library.native_stages as native_stages
from library.native_stages import NativeStages
from library.origins import resolve_asset_origin
from library.stage_model import Task
from postgres_fixture import create_catalog
from test_library_readers import image_bytes


class ExternalOriginTests(unittest.TestCase):
    def test_same_file_reuses_origin_and_asset_and_enters_ordinary_stages(self) -> None:
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
            (root/'work').mkdir()
            with patch('library.intake.EXTERNAL_ROOT', originals), \
                 patch('library.decode.WORK_ROOT', str(root/'work')), \
                 patch('library.native_stages.CollectorState') as collector, \
                 patch('library.native_stages.RuntimeObservation') as observation, \
                 patch('library.native_stages.OperationalState') as cursor:
                collector.return_value.monitor.return_value = {'submissions': {}}
                observation.return_value.queue.return_value = ([], 0)
                cursor.return_value.discovery.return_value = ''
                for _ in range(2):
                    native_stages._EXTERNAL_SCAN_AT = 0.0   # the scan runs once a day: open the gate for each pass
                    self.assertEqual(adapter.discover_intake(tasks, now=at), 1)
                    obligation = tasks.discover.call_args.args[0]
                    task = Task(obligation.key, obligation.stage, obligation.entity,
                        obligation.input_revision, obligation.method, None, at.isoformat())
                    self.assertEqual(adapter.ready(task, now=at), '')
                    self.assertEqual(adapter.receipt(task), ('absent', ''))
                    prepared = adapter.execute(task, now=at).prepared
                    assert prepared is not None
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

    def telegram_entry(self, originals: Path, channel: int, message: int, topic: int | None,
                       *, caption: str | None = None, name: str = 'L’Album.cbz') -> dict[str, Any]:
        document = {'id': message, 'file': name}
        if caption is not None:
            document['text'] = caption
        else:
            document['type'] = 'message'
        relative = Path('booksys/telegram')/f'{channel}_{message}'/name
        path = originals/relative
        path.parent.mkdir(parents=True)
        if path.suffix == '.pdf':
            from PIL import Image
            Image.new('RGB', (100, 100), 'white').save(path)
        else:
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr(zipfile.ZipInfo('001.png'), image_bytes())
        data = path.read_bytes()
        return {'receipt': {'id': channel, 'messages': [document]}, 'topic': topic,
                'path': str(relative), 'completed': True, 'size': len(data),
                'sha256': hashlib.sha256(data).hexdigest()}

    def submit_telegram(self, root: Path, *rows: dict[str, Any], dry_run: bool = False) -> list[dict[str, Any]]:
        from library.telegram_intake import main

        manifest = root/'telegram.jsonl'
        manifest.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
        args = [str(manifest), '--state-dir', str(root/'state'), '--scope', '101',
                '--scope', '202:17', '--scope', '1288796629']
        if dry_run:
            args.append('--dry-run')
        output = StringIO()
        with redirect_stdout(output):
            main(args)
        return [json.loads(line) for line in output.getvalue().splitlines()]

    def test_telegram_registration_queues_normal_successors_without_a_scan(self) -> None:
        from library.asset_evidence import bind_evidence
        from library.stage_runtime import StageRuntime
        from library.work_items import WorkItems

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            originals = root/'originals'
            first = self.telegram_entry(originals, 101, 12, None)
            second = self.telegram_entry(originals, 202, 16, 17, caption='Unverified series T99')
            catalog = create_catalog(self, root/'state')
            runtime = StageRuntime(root, root/'downloads', root/'collector', providers=MagicMock(),
                                   client=None, browser=None, isolated=True)
            work = WorkItems(catalog.backend()._database)
            self.addCleanup(work.close)
            at = datetime.now(timezone.utc)
            (root/'work').mkdir()
            with patch('library.intake.EXTERNAL_ROOT', originals), \
                 patch('library.decode.WORK_ROOT', str(root/'work')), \
                 patch('library.stage_runtime.require_nas'), \
                 patch.dict('os.environ', {'BOOKSYS_WORK_STAGES': 'intake,metadata,copies'}), \
                 patch('library.intake.external_origins', side_effect=AssertionError('global scan')) as scan:
                self.assertEqual(runtime.discover_business(now=at), 0)
                dry = self.submit_telegram(root, first, second, dry_run=True)
                self.assertEqual([row['status'] for row in dry], ['would-submit', 'would-submit'])
                self.assertFalse(catalog.read()['origins'])
                with patch.dict('os.environ', {'BOOKSYS_WORK_STAGES': 'metadata,copies'}), \
                     redirect_stderr(StringIO()) as refusal, self.assertRaises(SystemExit):
                    self.submit_telegram(root, first)
                self.assertIn('BOOKSYS_WORK_STAGES', refusal.getvalue())
                self.assertFalse(catalog.read()['origins'])
                queued = self.submit_telegram(root, first, second)
                self.assertEqual([row['status'] for row in queued], ['submitted', 'submitted'])
                actor = runtime.actor('intake')
                self.addCleanup(actor.work.close)
                self.assertEqual(actor.step(now=datetime.now(timezone.utc)), 'done')
                self.assertEqual(actor.step(now=datetime.now(timezone.utc)), 'done')
                state = catalog.read()
                aid, = state['assets']
                self.assertEqual(aid, first['sha256'])
                self.assertEqual(set(state['assets'][aid]['origins']), {row['origin'] for row in queued})
                origin_ids = {origin['telegram']['message']: oid for oid, origin in state['origins'].items()}
                caption_origin = state['origins'][origin_ids[16]]['telegram']
                self.assertEqual(caption_origin['caption'], 'Unverified series T99')
                self.assertEqual((caption_origin['channel'], caption_origin['topic']), (202, 17))
                found_origin, found_path = resolve_asset_origin(catalog, root/'downloads', state, aid)
                self.assertIn(found_origin, origin_ids.values())
                self.assertEqual(hashlib.sha256(found_path.read_bytes()).hexdigest(), aid)
                evidence = bind_evidence(state, aid)
                self.assertIn('Unverified series T99', evidence['discovery_queries'])
                self.assertNotIn('Unverified series T99', evidence['labels'])
                self.assertNotIn('work_result', state['assets'][aid])
                at = datetime.now(timezone.utc)
                self.assertEqual(runtime.discover_business(now=at), 2)
                self.assertIsNotNone(work.claim_item('metadata', aid, 'metadata-worker', now=at))
                self.assertIsNotNone(work.claim_item('copies', aid, 'copies-worker', now=at))
                repeated = self.submit_telegram(root, first, second)
                self.assertEqual([row['status'] for row in repeated], ['already-submitted', 'already-submitted'])
                self.assertIsNone(work.claim_item('intake', 'external-'+origin_ids[12], 'intake-worker', now=at))
                changed = json.loads(json.dumps(second))
                changed['receipt']['messages'][0]['text'] = 'Changed caption'
                with redirect_stderr(StringIO()) as refusal, self.assertRaises(SystemExit):
                    self.submit_telegram(root, changed)
                self.assertIn('changed', refusal.getvalue().lower())

                replaced = self.telegram_entry(originals, 101, 13, None)
                [pending] = self.submit_telegram(root, replaced)
                path = originals/replaced['path']
                original_bytes = path.read_bytes()
                path.write_bytes(b'\0' * len(original_bytes))
                actor.clock = None
                attempt = datetime.now(timezone.utc)
                self.assertEqual(actor.step(now=attempt), 'retry')
                self.assertIsNone(catalog.read()['origins'][pending['origin']]['sha256'])
                path.write_bytes(original_bytes)
                with patch('library.decode.run', return_value={
                    'status': 'pending', 'reason': 'Decoder support unavailable',
                }):
                    self.assertEqual(actor.step(now=attempt + timedelta(minutes=6)), 'retry')
                self.assertEqual(actor.step(now=attempt + timedelta(minutes=22)), 'done')
                self.assertEqual(catalog.read()['origins'][pending['origin']]['sha256'], aid)
                scan.assert_not_called()

    def test_telegram_cli_accepts_webtoonsvf_index_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            originals = root/'originals'
            row = self.telegram_entry(originals, 1288796629, 12, None)
            row['index_title'] = '  Série  de test ! '
            with patch('library.intake.EXTERNAL_ROOT', originals):
                [result] = self.submit_telegram(root, row, dry_run=True)
                self.assertEqual(result['index_title'], row['index_title'])
                with self.assertRaises(SystemExit), redirect_stderr(StringIO()) as refusal:
                    self.submit_telegram(root, {**row, 'index_title': 'Invalid\0Title'}, dry_run=True)
                self.assertIn('index title', refusal.getvalue())

    def test_webtoon_pdf_publishes_and_a_series_decision_refreshes_the_cbz(self) -> None:
        from xml.etree import ElementTree

        from library.api import PublicAPI
        from library.mangaupdates import lookup, STATE_FILE
        from library.stage_runtime import StageRuntime

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            originals = root/'originals'
            row = self.telegram_entry(originals, 1288796629, 12, None, name='Sample Chapter Chapitre 07.pdf')
            row['index_title'] = 'Sample Series'
            catalog = create_catalog(self, root/'state')
            providers = MagicMock(events=[])
            providers.admission.next_due.side_effect = AssertionError('Unexpected provider budget read')
            providers.bdnex.match.side_effect = AssertionError('Unexpected BDneX request')
            runtime = StageRuntime(root, root/'downloads', root/'collector', providers=providers,
                                   client=None, browser=None, isolated=True)
            (root/'work').mkdir()
            with patch('library.intake.EXTERNAL_ROOT', originals), \
                 patch('library.decode.WORK_ROOT', str(root/'work')), \
                 patch('library.stage_runtime.require_nas'), \
                 patch.dict('os.environ', {'BOOKSYS_WORK_STAGES': 'intake,metadata,copies'}):
                [result] = self.submit_telegram(root, row)
                self.assertEqual(result['status'], 'submitted')
                self.assertEqual(catalog.get('origins', result['origin'])['telegram']['index_title'], 'Sample Series')
                intake, metadata, copies = (runtime.actor(stage) for stage in ('intake', 'metadata', 'copies'))
                for actor in (intake, metadata, copies):
                    self.addCleanup(actor.work.close)
                stamp = datetime.now(timezone.utc)
                self.assertEqual(intake.step(now=stamp), 'done')
                self.assertEqual(runtime.discover_business(now=stamp), 1)
                self.assertEqual(metadata.step(now=stamp), 'done')
                self.assertEqual(copies.step(now=stamp), 'done')
                key = row['sha256']
                first = catalog.get('assets', key)['admission']
                self.assertTrue(first['target'].startswith('webtoon/Sample Series/7/'))
                original_copy = root/'library'/first['target']/'book.cbz'
                with zipfile.ZipFile(original_copy) as archive:
                    self.assertIsNone(ElementTree.fromstring(archive.read('ComicInfo.xml')).find('Writer'))

                record = {'series_id': 42, 'title': 'Original Series', 'type': 'Manhwa',
                          'url': 'https://www.mangaupdates.com/series/abc/original-series',
                          'associated': [{'title': 'Sample Series'}],
                          'authors': [{'name': 'Sample Writer', 'type': 'Author'}]}
                lookup(root/'state', ['Sample Series'], request=lambda *_: {'results': [{'record': record}]})
                self.assertTrue((root/'state'/STATE_FILE).is_file())
                self.assertEqual(runtime.business.discover(runtime.tasks, now=stamp, keys=[key]), 1)
                self.assertEqual(copies.step(now=stamp), 'done')
                second = catalog.get('assets', key)['admission']
                self.assertNotEqual(second['signature'], first['signature'])
                self.assertFalse(original_copy.exists())
                with zipfile.ZipFile(root/'library'/second['target']/'book.cbz') as archive:
                    xml = ElementTree.fromstring(archive.read('ComicInfo.xml'))
                    self.assertEqual(xml.findtext('Series'), 'Sample Series')
                    self.assertEqual(xml.findtext('AlternateSeries'), 'Original Series')
                    self.assertEqual(xml.findtext('Writer'), 'Sample Writer')
                self.assertEqual(hashlib.sha256((originals/row['path']).read_bytes()).hexdigest(), key)

                api = PublicAPI(root, host='localhost', reception=root/'downloads')
                status = api.invoke('status', {'host': 'localhost', 'origin': '', 'token': '', 'payload': {}})
                self.assertEqual(status['counts']['webtoon_assets'], 1)
                self.assertEqual(status['counts']['webtoon_copies'], 1)
                self.assertEqual(status['counts']['identification_attempted_assets'], 0)
                providers.bdnex.match.assert_not_called()

    def test_telegram_cli_refuses_unbound_or_unfinished_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            originals = root/'originals'
            row = self.telegram_entry(originals, 202, 16, 17)
            source = originals/row['path']
            linked = source.with_name('Linked.cbz')
            linked.symlink_to(source)
            partial = source.with_name('Album.partial.cbz')
            partial.write_bytes(source.read_bytes())
            cases = (
                ({'completed': False}, 'completed'), ({'size': row['size'] + 1}, 'size'),
                ({'sha256': 'f'*64}, 'SHA-256'), ({'path': '/outside.cbz'}, 'path'),
                ({'path': 'booksys/telegram/../202_16/L’Album.cbz'}, 'path'),
                ({'path': str(linked.relative_to(originals))}, 'symlink'),
                ({'path': str(partial.relative_to(originals))}, 'partial'),
                ({'topic': 18}, 'scope'),
                ({'receipt': {'id': 303, 'messages': row['receipt']['messages']}}, 'scope'),
                ({'receipt': {'id': 202, 'messages': [
                    {**row['receipt']['messages'][0], 'text': '\0'}]}}, 'caption'),
            )
            with patch('library.intake.EXTERNAL_ROOT', originals):
                for changes, reason in cases:
                    with self.subTest(reason=reason), redirect_stderr(StringIO()) as refusal:
                        with self.assertRaises(SystemExit) as raised:
                            self.submit_telegram(root, {**row, **changes}, dry_run=True)
                        self.assertEqual(raised.exception.code, 1)
                        self.assertIn(reason.lower(), refusal.getvalue().lower())
            self.assertFalse((root/'state').exists())

    def test_scan_excludes_managed_stream_temporary_and_symlink_inputs(self) -> None:
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
