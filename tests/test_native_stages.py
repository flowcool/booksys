"""Native bounded stage adapters keep exact source and package ownership."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import os

from library.native_stages import NativeStages
from library.source_tasks import SourcePolicy
from library.stage_tasks import StageTasks
from library.catalog import empty, Catalog
from postgres_fixture import create_store
from library.operational_state import CollectorState, NativeHistory
from library.primitives import digest

AT = datetime(2026, 11, 9, tzinfo=timezone.utc)
SOURCE = 'https://www.bookys-ebooks.com/123-album'

class NativeStageTests(unittest.TestCase):
    def test_retirement_without_intent_reconciles_only_unchanged_owned_package(self):
        from test_retirement import fixture
        from test_library_intake import SOURCE as source
        from booksys_core.network_admission import RollingBudget, NativeMutation
        for change in ('none', 'owned_pointer', 'name', 'file', 'url', 'folder', 'pointer', 'pointer_journal', 'journal', 'deleting'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                catalog, reception, collector, original, client = fixture(root, self)
                client.admission = RollingBudget.create(root/'state/native-admission.yaml', requests=100, period=60)
                tasks = StageTasks(catalog.backend(), policy=SourcePolicy.create(root/'state/task-policy.yaml'))
                tasks.scan('retirement', collector/'monitor.yaml', now=AT)
                task = tasks.select('retirement', now=AT).task
                task = tasks.start(task.key, task.generation, now=AT, resources=True)
                journal = collector/'package-retirement.yaml'
                mutation = NativeMutation(root/'state', 'retirement:'+source, journal)
                if change == 'pointer':
                    with NativeMutation(root/'state', 'another-owner', journal).hold() as other:
                        other.prepare()
                if change in ('owned_pointer', 'pointer_journal'):
                    pointer = mutation if change == 'owned_pointer' else NativeMutation(root/'state', 'retirement:'+source, collector/'other.yaml')
                    with pointer.hold(): pointer.prepare()
                if change == 'deleting':
                    from library.retirement import Retirement
                    client.lose_before_removal = True
                    with self.assertRaises(OSError):
                        Retirement(client, catalog, reception, collector/'monitor.yaml', collector/'state.yaml').process(source, apply=True)
                if change == 'journal':
                    task.native_attempt['journal'] = str(collector/'other.yaml')
                if change == 'name': client.packages[1]['name'] += '-changed'
                if change == 'file': client.packages[1]['links'][0]['fid'] = 99
                if change == 'url': client.packages[1]['links'][0]['url'] += '-changed'
                if change == 'folder': client.packages[1]['folder'] += '-changed'
                adapter = NativeStages(catalog, reception, root/'library', collector, browser=None, client=client)
                result = adapter.receipt(task)
                outcome = result.outcome if hasattr(result, 'outcome') else result[0]
                proof = result.proof if hasattr(result, 'proof') else result[1]
                self.assertEqual(outcome, 'absent' if change in ('none', 'owned_pointer') else 'uncertain')
                if change in ('none', 'owned_pointer'): self.assertTrue(proof)
                if change == 'owned_pointer': self.assertEqual(mutation.read()['state'], 'resolved')
                self.assertEqual(client.removed, [])

    def test_fiche_uses_separate_managed_media_without_writing_reception(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            collector, reception, media = (root/name for name in ('collector', 'downloads', 'fiches'))
            for path in (collector, reception, media):
                path.mkdir()
            store = create_store(self, root/'state', empty(), authority='fiche')
            state = CollectorState(collector, database=store._database)
            state.submission(SOURCE, {'status': 'queued', 'package_id': 1})
            tasks = StageTasks(store, policy=SourcePolicy.create(root/'state/policy.yaml'))
            tasks.scan('collection', collector/'monitor.yaml', now=AT)
            task = tasks.select('collection', now=AT).task
            adapter = NativeStages(Catalog(root/'state'), reception, root/'library', collector,
                                   browser=object(), client=object())
            def complete_fiche(*args, **kwargs):
                kwargs['completions'].append(state.prepare_completion('fiche', SOURCE, {'status': 'complete'}))
                return True
            with patch.dict(os.environ, {'BOOKSYS_FICHE_ROOT': str(media)}), patch(
                    'booksys_core.collection.run_fiche_once', side_effect=complete_fiche) as fiche:
                adapter.execute(task, now=AT)
            self.assertEqual(fiche.call_args.args[3], media)
            self.assertEqual(list(reception.iterdir()), [])

    def test_routing_recovery_reads_back_a_lost_move_without_resend(self):
        from booksys_core.network_admission import RollingBudget, NativeMutation
        from library.stage_model import Obligation
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            collector = root/'collector'
            store = create_store(self, collector, empty())
            budget = RollingBudget.create(collector/'native-admission.yaml', requests=30, period=60)
            entry = {'status': 'queued', 'package_id': 1, 'name': 'Booksys-owned'}
            CollectorState(collector).submission(SOURCE, entry)
            operation = {'source': SOURCE, 'pid': 1, 'name': entry['name'], 'phase': 'intent',
                         'kind': 'park', 'original_files': [[2, 'https://host.invalid/file']]}
            journal = collector/'mirror-routing.yaml'
            NativeHistory(collector).route('1', operation)
            mutation = NativeMutation(collector, 'routing:'+SOURCE, journal)
            with mutation.hold():
                mutation.prepare()
            calls = []
            def native_call(method, *, write=False, **parameters):
                self.assertFalse(write)
                calls.append(method)
                self.assertEqual(method, 'get_package_data')
                return {'pid': 1, 'name': entry['name'], 'dest': 0,
                        'links': [{'fid': 2, 'url': 'https://host.invalid/file'}]}
            adapter = NativeStages(Catalog(collector, store=store), root/'reception', root/'library', collector,
                                   browser=None, client=SimpleNamespace(admission=budget, call=native_call))
            from library.stage_model import Task
            work = Obligation('routing', 'source-'+digest(SOURCE), '1', 'v1')
            task = Task(work.key, work.stage, work.entity, work.input_revision, work.method, None, AT.isoformat())
            outcome, proof = adapter.receipt(task)
            self.assertEqual(outcome, 'absent')
            self.assertTrue(proof)
            self.assertEqual(mutation.read()['state'], 'resolved')
            self.assertEqual(NativeHistory(collector).routing()['packages']['1']['phase'], 'parked')
            self.assertEqual(calls, ['get_package_data'])

    def test_collection_executes_one_retained_category_step_and_returns_canonical_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = create_store(self, root/'state', empty(), authority='native')
            tasks = StageTasks(store, policy=SourcePolicy.create(root/'state/policy.yaml'))
            collector = root/'collector'
            collector.mkdir()
            category = 'https://www.bookys-ebooks.com/bandes-dessinees'
            CollectorState(collector, database=store._database).category(category, {'after_id': 100})
            tasks.scan('collection', collector/'monitor.yaml', now=AT)
            task = tasks.select('collection', now=AT).task
            adapter = NativeStages(Catalog(root/'state'), root/'reception', root/'library', collector,
                                   browser=object(), client=object())
            with patch('booksys_core.collection.run_once', return_value={'queued': 0, 'continued': True}) as call:
                result = adapter.execute(task, now=AT)
            self.assertEqual(result.outcome, 'done')
            self.assertTrue(result.proof)
            self.assertEqual(call.call_args.kwargs['step_limit'], 1)
            self.assertEqual(call.call_args.kwargs['category'], category)

    def test_intake_selects_one_source_and_retirement_uses_concurrent_scoped_mode(self):
        # Bind native sources through the public policy adapter, not synthetic task records.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = create_store(self, root/'state', empty(), authority='native')
            tasks = StageTasks(store, policy=SourcePolicy.create(root/'state/policy.yaml'))
            collector = root/'collector'
            collector.mkdir()
            CollectorState(collector, database=store._database).submission(SOURCE, {'status': 'queued', 'package_id': 1, 'name': 'Booksys-one'})
            tasks.scan('retirement', collector/'monitor.yaml', now=AT)
            task = tasks.select('retirement', now=AT).task
            adapter = NativeStages(Catalog(root/'state'), root/'reception', root/'library', collector,
                                   browser=object(), client=object())
            with patch('library.retirement.Retirement') as retirement:
                retirement.return_value.process.return_value = {'outcome': 'retired', 'source': SOURCE}
                result = adapter.execute(task, now=AT)
            self.assertEqual(result.outcome, 'done')
            self.assertTrue(retirement.call_args.kwargs['concurrent'])
            retirement.return_value.process.assert_called_once_with(SOURCE, apply=True, completions=[], task_attempt=None)
