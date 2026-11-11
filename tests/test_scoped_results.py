"""Prepared domain results and manual decisions use relevant conflict sets."""
from pathlib import Path
import tempfile
import unittest

from library.catalog import Catalog, empty, LibraryError
from postgres_fixture import create_store
from library.store import Change


class ScopedResultsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        state = empty()
        state['works'] = {'one': {'title': 'One'}, 'two': {'title': 'Two'}}
        self.store=create_store(self,root,state,authority='scope')
        self.catalog = Catalog(root)

    def test_prepared_result_survives_unrelated_commit_but_rejects_related_change(self):
        prepared = self.catalog.result_set('identify', {'works': ['one']})
        prepared['works']['one']['title'] = 'Prepared'
        self.store.commit(Change(upserts={'works': {'two': {'title': 'Unrelated'}}}), expected_revision=0)
        self.catalog.save(prepared, expected=prepared['revision'])
        self.assertEqual(self.catalog.get('works', 'one')['title'], 'Prepared')
        stale = self.catalog.result_set('identify', {'works': ['one']})
        stale['works']['one']['title'] = 'Stale'
        self.store.commit(Change(upserts={'works': {'one': {'title': 'Operator'}}}), expected_revision=2)
        with self.assertRaisesRegex(LibraryError, 'conflict'):
            self.catalog.save(stale, expected=stale['revision'])
        self.assertEqual(self.catalog.get('works', 'one')['title'], 'Operator')

    def test_absence_membership_and_pending_decisions_fence_results(self):
        prepared = self.catalog.result_set('identify', {'works': ['absent']})
        prepared['works']['absent'] = {'title': 'Prepared'}
        self.store.commit(Change(upserts={'works': {'absent': {'title': 'Competing'}}}), expected_revision=0)
        with self.assertRaisesRegex(LibraryError, 'conflict'):
            self.catalog.save(prepared, expected=prepared['revision'])
        prepared = self.catalog.result_set('identify', {'works': ['one']})
        prepared['works']['one']['title'] = 'Prepared'
        self.store.commit(Change(upserts={'editions': {'new': {'work': 'one', 'language': 'fr', 'publisher': 'P', 'evidence': []}}}), expected_revision=1)
        with self.assertRaisesRegex(LibraryError, 'conflict'):
            self.catalog.save(prepared, expected=prepared['revision'])

    def test_real_ocr_result_survives_unrelated_commit(self):
        from unittest.mock import patch
        from test_library_publish import setup_library
        from postgres_fixture import require_postgresql
        from library.printed import scan_printed
        root = Path(self.temp.name) / 'ocr'
        catalog, reception, _, aid = setup_library(root,self)
        require_postgresql(catalog)
        backend = catalog.backend()
        def read(path, asset):
            backend.commit(Change(upserts={'works': {'unrelated': {'title': 'Other'}}}), expected_revision=backend.revision)
            return {'status': 'ok', 'asset': aid, 'pages': [], 'tool': 'fixture'}
        with patch('library.printed.read_printed', side_effect=read):
            scan_printed(catalog, aid, reception)
        self.assertEqual(catalog.get('assets', aid)['printed']['tool'], 'fixture')

    def test_real_metadata_result_survives_unrelated_provider_time_commit(self):
        from test_library_publish import setup_library
        from postgres_fixture import require_postgresql
        from library.metadata import Providers, enrich
        root = Path(self.temp.name) / 'metadata'
        catalog, _, _, aid = setup_library(root,self)
        require_postgresql(catalog)
        backend = catalog.backend()
        def fetch(url):
            backend.commit(Change(upserts={'works': {'unrelated': {'title': url}}}), expected_revision=backend.revision)
            return 503, ''
        providers = Providers(root / 'cache', fetch=fetch, interval=0)
        enrich(catalog, aid, providers, [('bnf', '9782871297833')])
        self.assertTrue(catalog.get('assets', aid).get('source_attempts'))

    def test_work_case_ignores_unrelated_works_and_queue_bookkeeping(self):
        from test_library_publish import setup_library
        from postgres_fixture import require_postgresql
        from library.stage_tasks import StageTasks, Obligation
        from test_stage_tasks import AT
        catalog, _, _, aid = setup_library(Path(self.temp.name) / 'case',self)
        require_postgresql(catalog)
        backend = catalog.backend()
        case = backend.case(aid)
        StageTasks.upgrade(backend)
        StageTasks(backend).discover(Obligation('metadata', aid, '1', 'v1'), now=AT)
        self.assertEqual(backend.case(aid)['version'], case['version'])
        backend.commit(Change(upserts={'works': {'other': {'title': 'Other'}}}), expected_revision=backend.revision)
        self.assertEqual(backend.case(aid)['version'], case['version'])

    def test_real_admission_keeps_prepared_output_after_unrelated_commit(self):
        from unittest.mock import patch
        from test_library_publish import setup_library
        from postgres_fixture import require_postgresql
        from library.admission import AdmissionPublisher
        from library.publication import Capacity
        from library.transform import transform_archive
        root = Path(self.temp.name) / 'admission'
        catalog, reception, source, aid = setup_library(root,self)
        require_postgresql(catalog)
        backend = catalog.backend()
        def transform(*args, **kwargs):
            result = transform_archive(*args, **kwargs)
            backend.commit(Change(upserts={'works': {'other': {'title': 'Other'}}}), expected_revision=backend.revision)
            return result
        writer = AdmissionPublisher(catalog, reception, root / 'library', isolated=True, capacity=Capacity(reserve=0))
        with patch('library.admission.transform_archive', side_effect=transform):
            operation = writer.publish_asset(aid)
        self.assertEqual(operation['phase'], 'committed')
        writer.reconcile()
        self.assertTrue(source.is_file())

    def test_real_publication_survives_unrelated_commit_before_substitution(self):
        from test_library_publish import setup_library
        from postgres_fixture import require_postgresql
        from library.publish import Publisher, Capacity
        root = Path(self.temp.name) / 'publication'
        catalog, reception, _, aid = setup_library(root,self)
        require_postgresql(catalog)
        backend = catalog.backend()
        writer = Publisher(catalog, reception, root / 'library', isolated=True, capacity=Capacity(reserve=0))
        def fault(phase, operation):
            if phase == 'before_substitution':
                backend.commit(Change(upserts={'works': {'other': {'title': 'Other'}}}), expected_revision=backend.revision)
        writer.fault = fault
        operation = writer.publish(aid)
        self.assertEqual(operation['phase'], 'committed')
        writer.reconcile()

    def test_preview_checks_named_work_before_new_edition_exists(self):
        from test_library_publish import setup_library
        from postgres_fixture import require_postgresql
        from library.resolution import Resolution
        catalog, reception, _, aid = setup_library(Path(self.temp.name) / 'preview',self)
        state = catalog.read()
        state['assets'][aid].pop('identity')
        catalog.save(state, expected=state['revision'])
        require_postgresql(catalog)
        backend = catalog.backend()
        service = Resolution(catalog, reception)
        page = service.page(aid, 1)
        values = {'edition_id': None, 'work_id': 'work', 'work': {'title': 'Album'},
            'edition': {'publisher': 'P', 'language': 'fr'}, 'select': True,
            'proof': [{'page': page['id'], 'field': field, 'text': 'Checked ' + field}
                for field in ('title', 'publisher', 'language')],
            'reason': 'Checked local proof', 'supersedes': None, 'restore': None}
        preview = service.preview(aid, values)
        backend.commit(Change(upserts={'works': {'work': {'title': 'Changed'}}}), expected_revision=backend.revision)
        with self.assertRaisesRegex(LibraryError, 'Stale preview'):
            service.confirm({'request_id': 'changed-work', 'preview': preview['id']})

    def test_pending_manual_exclusion_rejects_automatic_ocr_without_losing_decision(self):
        from unittest.mock import patch
        from test_library_publish import setup_library
        from postgres_fixture import require_postgresql
        from library.printed import scan_printed
        from library.primitives import lock
        catalog, reception, _, aid = setup_library(Path(self.temp.name) / 'exclude',self)
        require_postgresql(catalog)
        def read(path, asset):
            with lock(catalog.directory / 'decisions.lock'):
                catalog.append_decision({'action': 'exclude', 'target': aid, 'values': {}, 'author': 'flowcool'})
            return {'status': 'ok', 'asset': aid, 'pages': [], 'tool': 'fixture'}
        with patch('library.printed.read_printed', side_effect=read):
            with self.assertRaisesRegex(LibraryError, 'conflict'):
                scan_printed(catalog, aid, reception)
        catalog.project_pending()
        self.assertEqual(catalog.get('assets', aid)['disposition'], 'exclude')
        self.assertNotIn('printed', catalog.get('assets', aid))

    def test_real_intake_survives_unrelated_decoder_time_commit(self):
        from unittest.mock import patch
        from test_library_publish import setup_library, SOURCE
        from postgres_fixture import require_postgresql
        from library.intake import receive, inspect_file
        catalog, reception, path, aid = setup_library(Path(self.temp.name) / 'intake',self)
        require_postgresql(catalog)
        backend = catalog.backend()
        def inspect(*args, **kwargs):
            result = inspect_file(*args, **kwargs)
            backend.commit(Change(upserts={'works': {'other': {'title': 'Other'}}}), expected_revision=backend.revision)
            return result
        with patch('library.intake.inspect_file', side_effect=inspect):
            receive(catalog, reception, {'items': {SOURCE: {'title': 'Album'}}},
                {'submissions': {SOURCE: {'status': 'queued', 'name': 'tracked', 'package_id': 1}}},
                [{'pid': 1, 'name': 'tracked', 'folder': 'package', 'links': [
                    {'fid': 1, 'name': 'album.cbz', 'status': 0, 'size': path.stat().st_size}]}], stable_seconds=.01)
        self.assertEqual(catalog.get('assets', aid)['status'], 'validated')

    def test_new_competing_isbn_claim_invalidates_existing_edition_result(self):
        self.store.commit(Change(upserts={'editions': {'edition': {'work': 'one', 'language': 'fr', 'publisher': 'P',
            'evidence': [], 'isbn': '9782871297833'}}}), expected_revision=0)
        prepared = self.catalog.result_set('publish', {'editions': ['edition']})
        prepared['works']['one']['title'] = 'Prepared'
        self.store.commit(Change(upserts={'editions': {'competing': {'work': 'two', 'language': 'fr', 'publisher': 'P',
            'evidence': [], 'isbn': '9782871297833'}}}), expected_revision=1)
        with self.assertRaisesRegex(LibraryError, 'conflict'):
            self.catalog.save(prepared, expected=prepared['revision'])

    def test_prepared_withdrawal_recovers_after_unrelated_business_commit(self):
        from test_library_publish import setup_library
        from postgres_fixture import require_postgresql
        from library.publish import Publisher, Capacity
        catalog, reception, _, aid = setup_library(Path(self.temp.name) / 'withdraw',self)
        require_postgresql(catalog)
        backend = catalog.backend()
        writer = Publisher(catalog, reception, Path(self.temp.name) / 'withdraw/library', isolated=True, capacity=Capacity(reserve=0))
        writer.publish(aid)
        def crash(phase, operation):
            if phase == 'after_substitution':
                raise RuntimeError('isolated crash')
        writer.fault = crash
        with self.assertRaises(RuntimeError):
            writer.withdraw('edition', expected=catalog.revision)
        backend.commit(Change(upserts={'works': {'other': {'title': 'Other'}}}), expected_revision=backend.revision)
        writer.fault = lambda *_: None
        writer.reconcile()
        self.assertNotIn('publication', catalog.get('editions', 'edition'))
