"""Existing operator decision contracts through targeted PostgreSQL interfaces."""
from pathlib import Path
import hashlib
import tempfile
import unittest
from unittest.mock import patch

from library.catalog import Catalog, LibraryError
from postgres_fixture import require_postgresql
from library.resolution import Resolution, case_view
from test_library_publish import setup_library




class SQLiteDecisionTests(unittest.TestCase):
    def test_targeted_append_rejects_missing_or_altered_indexed_head(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);catalog,_,_,asset=setup_library(root,self)
            first={'action':'override','target':asset,'values':{'title':'First'},
                'author':'fixture','time':'2026-11-09T00:00:00Z'}
            record=catalog.append_decision(first)
            path=catalog.decisions/f"{record['sequence']:09d}-{record['id']}.yaml"
            retained=path.read_bytes();hidden=path.with_suffix('.retained')
            path.rename(hidden)
            for phase in ('missing','altered'):
                if phase=='altered':path.write_text('altered: true\n')
                with self.subTest(phase=phase),self.assertRaisesRegex(LibraryError,'evidence lost or altered'):
                    catalog.append_decision({**first,'values':{'title':'Second'}})
                self.assertEqual(catalog.backend().decision_head(),record)
            path.write_bytes(retained)

    def test_work_confirmation_uses_targeted_case_and_preserves_local_proof(self):
        from library.work_resolution import confirm_work
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            catalog, reception, _, aid = setup_library(root,self)
            state = catalog.read(); state['assets'][aid].pop('identity')
            state['works'].clear(); state['editions'].clear(); state['variants'].clear()
            catalog.save(state, expected=state['revision']); require_postgresql(catalog)
            service = Resolution(catalog, reception)
            page = service.page(aid, 1)
            case = catalog.backend().case(aid)
            from library.review import report
            self.assertEqual(report(catalog)['cases'][case['id']]['version'], case['version'])
            request = {'asset': aid, 'case': case['id'], 'version': case['version'], 'request_id': 'work-confirmation',
                'work': {'title': 'Operator work', 'family': 'BD'}, 'proof': [{'page': page['id'], 'text': 'Visible title'}],
                'reason': 'Read the title on the retained page'}
            with patch.object(catalog, 'read', side_effect=AssertionError('No full catalogue read')):
                result = confirm_work(service, request)
                repeated = confirm_work(service, request)
            self.assertEqual(result['decision'], repeated['decision'])
            self.assertEqual(catalog.get('assets', aid)['work_result']['work']['title'], 'Operator work')

    def test_detail_keeps_one_snapshot_when_a_writer_commits_between_reads(self):
        from library.store import Change
        with tempfile.TemporaryDirectory() as folder:
            catalog, _, _, aid = setup_library(Path(folder),self); require_postgresql(catalog)
            backend = catalog.backend()
            revision = backend.revision
            original = backend.working_set
            def concurrent(*args, **kwargs):
                state = original(*args, **kwargs)
                current = backend.get('assets', aid)
                current['qualification_counter'] = 1
                backend.commit(Change(upserts={'assets': {aid: current}}), expected_revision=backend.revision)
                return state
            with patch.object(backend, 'working_set', side_effect=concurrent):
                state = backend.resolution_state(aid)
            self.assertEqual(state['revision'], revision)
            self.assertNotIn('qualification_counter', state['assets'][aid])
            self.assertEqual(backend.revision, revision + 1)

    def test_missing_proof_blocks_pending_projection_without_a_business_commit(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            catalog, reception, _, aid = setup_library(root,self); require_postgresql(catalog)
            service = Resolution(catalog, reception)
            page = service.page(aid, 1)
            values = {'edition_id': 'edition', 'work_id': 'work', 'work': {}, 'edition': {},
                'select': True, 'proof': [{'page': page['id'], 'field': field, 'text': 'Checked ' + field}
                    for field in ('title', 'publisher', 'language')],
                'reason': 'Checked local proof', 'supersedes': None, 'restore': None}
            with patch.object(catalog, 'ledger', side_effect=AssertionError('No full ledger read per request')):
                preview = service.preview(aid, values)
                result = service.confirm({'request_id': 'fixture', 'preview': preview['id']})
                self.assertEqual(service.confirm({'request_id': 'fixture', 'preview': preview['id']}), result)
            revision = catalog.revision
            piece = catalog.directory / 'library-resolution/pieces' / (page['id'] + '.yaml')
            piece.chmod(0o600); piece.write_text('altered: true\n')
            with self.assertRaises(LibraryError):
                catalog.project_pending()
            self.assertEqual(catalog.revision, revision)
