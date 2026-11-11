"""Separate durable business steps through real catalogue and copy adapters."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from library.business_stages import BusinessStages
from library.stage_tasks import StageTasks
from library.metadata import Providers
from library.publish import Capacity
from test_library_publish import setup_library
from postgres_fixture import require_postgresql

AT = datetime(2026, 11, 9, tzinfo=timezone.utc)

class BusinessStageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog, self.reception, self.source, self.aid = setup_library(self.root,self)
        require_postgresql(self.catalog)
        self.tasks = StageTasks(self.catalog.backend())
        StageTasks.upgrade(self.tasks.store)
        self.stages = BusinessStages(self.catalog, self.reception, self.root/'library',
            Providers(self.root/'cache', fetch=lambda url: (503, ''), interval=0),
            capacity=Capacity(reserve=0), isolated=True)

    def execute(self, task):
        from library.stage_model import StepResult
        active = self.tasks.start(task.key, task.generation, now=AT, resources=True)
        value = self.stages.execute(active, now=AT)
        result = value if isinstance(value, StepResult) else StepResult(value)
        self.tasks.complete(active.key, active.generation, now=AT, outcome=result.outcome,
            proof=result.proof, due_at=result.due_at, prepared=result.prepared,
            successors=result.successors, source_progress=result.source_progress)
        return result

    def test_metadata_has_no_ocr_or_publication_side_effect_and_copies_need_no_drain(self):
        self.stages.discover(self.tasks, now=AT)
        task = self.tasks.select('metadata', now=AT).task
        self.assertIsNotNone(task)
        with patch('library.printed.scan_printed', side_effect=AssertionError('coupled OCR')):
            self.execute(task)
        self.assertIsNone(self.catalog.get('assets', self.aid).get('admission'))
        self.assertTrue(self.catalog.get('assets', self.aid).get('stage_metadata'))
        self.stages.discover(self.tasks, now=AT)
        copy = self.tasks.select('copies', now=AT).task
        self.assertIsNotNone(copy)
        self.execute(copy)
        self.assertEqual(self.catalog.get('assets', self.aid)['admission']['status'], 'classified')
        self.assertTrue(self.source.is_file())

    def test_cutover_keeps_the_existing_weekly_metadata_deadline(self):
        from library.automatic import METHOD, WEEK, evidence_version
        state = self.catalog.result_set('identify', {'assets': [self.aid]})
        asset = state['assets'][self.aid]
        asset['automation'] = {'method': METHOD, 'evidence': evidence_version(asset),
            'outcome': 'pending', 'next_attempt': AT.timestamp()+WEEK, 'attempts': 1}
        self.catalog.save(state, expected=state['revision'])
        self.stages.discover(self.tasks, now=AT)
        self.assertIsNone(self.tasks.select('metadata', now=AT).task)
        self.assertIsNotNone(self.tasks.select('copies', now=AT).task)

    def test_unknown_identity_gets_nonocr_evidence_before_ocr_and_can_get_holding_copy(self):
        state = self.catalog.result_set('identify', {'assets': [self.aid]})
        state['assets'][self.aid].pop('identity')
        state['variants'].clear()
        state['editions']['edition'].pop('selected', None)
        self.catalog.save(state, expected=state['revision'])
        self.stages.discover(self.tasks, now=AT)
        self.assertIsNone(self.tasks.select('ocr', now=AT).task)
        meta = self.tasks.select('metadata', now=AT).task
        self.execute(meta)
        self.stages.discover(self.tasks, now=AT)
        ocr = self.tasks.select('ocr', now=AT).task
        self.assertIsNotNone(ocr)
        copy = self.tasks.select('copies', now=AT).task
        self.assertIsNotNone(copy)
        self.execute(copy)
        self.assertEqual(self.catalog.get('assets', self.aid)['admission']['status'], 'holding')
        with patch('library.printed.read_printed', return_value={'status': 'ok', 'asset': self.aid, 'pages': [], 'tool': 'fixture'}):
            self.execute(ocr)
        self.assertEqual(self.catalog.get('assets', self.aid)['printed']['status'], 'ok')

    def test_provider_retry_does_not_create_a_fresh_allowance_on_rediscovery(self):
        from library.metadata import prepare_enrichment as real_enrich
        self.stages.discover(self.tasks, now=AT)
        task = self.tasks.select('metadata', now=AT).task
        def temporary(*args, **kwargs):
            result = real_enrich(*args, **kwargs)
            self.stages.providers.events.append({'status': 'unavailable', 'http_status': 503})
            return result
        with patch('library.metadata.prepare_enrichment', side_effect=temporary):
            result = self.execute(task)
        self.assertEqual(result.outcome, 'retry')
        self.stages.discover(self.tasks, now=AT)
        self.assertEqual(self.tasks.get(task.key).attempts, 1)
        self.assertIsNone(self.tasks.select('metadata', now=AT).task)
