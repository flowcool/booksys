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
    def test_collection_continuation_commits_with_claim_and_resumes_without_duplicates(self):
        from unittest.mock import Mock
        from library.work_items import WorkItems
        from library.work_actor import WorkActor
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, BOOKSYS_WORK_STAGES='collection'):
            root = Path(directory)
            store = create_store(self, root, empty())
            WorkItems.upgrade(store._database)
            work = WorkItems(store._database, max_attempts={'collection': 3})
            self.addCleanup(work.close)
            state = CollectorState(root)
            category = 'https://www.bookys-ebooks.com/category'
            state.category(category, {'deep_page': 6})
            sources = [f'https://www.bookys-ebooks.com/{number}-synthetic' for number in range(21)]
            for source in sources:
                state.capture(source, {'title': 'Synthetic', 'downloads': {}, 'complete_hosters': ['*']})
            policy = SourcePolicy(root/'task-policy.yaml', store=store)
            tasks = StageTasks(store, policy=policy)
            client = Mock()
            client.call.return_value = {'pause': False, 'download': True}
            submitted = []
            def submit(source, record, journal, path):
                self.assertNotIn(source, submitted)
                submitted.append(source)
                entry = {'status': 'queued', 'package_id': len(submitted)}
                journal['submissions'][source] = entry
                state.submission(source, entry)
            client.submit.side_effect = submit
            native = NativeStages(Catalog(root, store=store), root, root, root, browser=Mock(), client=client)
            actor = WorkActor(store, work, 'collection', lambda task, stamp: native.execute(task, now=stamp))
            with patch('booksys_core.collection.ScheduledCalls'), \
                    patch('booksys_core.collection.catalogue', return_value=(sources, None)) as pages, \
                    patch('booksys_core.collection.CRAWL_PAGES', 1):
                tasks.scan('collection', root/'monitor.yaml', now=AT)
                self.assertEqual(actor.step(now=AT), 'done')
                self.assertEqual(len(submitted), 20)
                self.assertTrue(state.monitor()['categories'][category]['continuation'])
                # Complete fiches are outside this test's category work.
                for source in sources[:20]:
                    state.fiche(source, {'status': 'complete'})
                resumed_at = datetime.fromisoformat(state.monitor()['categories'][category]['last_cycle'])
                tasks.scan('collection', root/'monitor.yaml', now=resumed_at)
                self.assertEqual(actor.step(now=resumed_at), 'done')
                self.assertEqual(submitted, sources)
                self.assertEqual(pages.call_count, 1)
                self.assertNotIn('continuation', state.monitor()['categories'][category])


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




class SnapshotFreshnessTests(unittest.TestCase):
    def test_queue_snapshot_age_never_blocks_a_stage(self):
        from datetime import datetime, timedelta, timezone
        from unittest.mock import MagicMock, patch
        from library.native_stages import NativeStages
        from library.primitives import LibraryError
        from library.stage_model import Task
        stages = object.__new__(NativeStages)
        stages.snapshots = object()
        stages.catalog = MagicMock()
        stages.binding = MagicMock(side_effect=LibraryError('stop after freshness'))
        now = datetime(2026, 11, 11, tzinfo=timezone.utc)
        task = MagicMock(spec=Task)
        task.stage = 'retirement'
        for age, expected in ((120, 'native_prerequisites_unavailable'), (900, 'native_prerequisites_unavailable')):
            with patch('library.native_stages.RuntimeObservation') as observation:
                observation.return_value.queue.return_value = ([], now.timestamp() - age)
                self.assertEqual(stages.ready(task, now=now), expected)


class BudgetReserveTests(unittest.TestCase):
    def test_reserve_waits_until_enough_slots_fit_the_window(self):
        from booksys_core.network_admission import RollingBudget
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            collector = root/'collector'
            create_store(self, collector, empty())
            budget = RollingBudget.create(collector/'native-admission.yaml', requests=30, period=60)
            base = AT.timestamp()
            for index in range(20):
                budget.debit(timestamp=base + index)
            now = base + 20
            self.assertLessEqual(budget.next_due(timestamp=now), now)
            self.assertLessEqual(budget.next_due(timestamp=now, reserve=10), now)
            self.assertEqual(budget.next_due(timestamp=now, reserve=16), base + 5 + 60)
            self.assertEqual(budget.next_due(timestamp=now, reserve=99), base + 19 + 60)


class QueueSnapshotCacheTests(unittest.TestCase):
    def test_queue_snapshot_is_decoded_once_per_publication(self):
        from unittest.mock import patch
        from library.operational_state import RuntimeObservation
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = create_store(self, root, empty())
            observation = RuntimeObservation(store._database)
            packages = [{'pid': 1, 'name': 'one', 'links': [{'fid': 5, 'status': 0}]},
                        {'pid': 2, 'name': 'two', 'links': []}]
            observation.publish(packages, at=100.0, remaining=None, overshoot=None)
            first, at = observation.queue()
            self.assertEqual((first, at), (packages, 100.0))
            first[0]['links'].clear()  # a caller mutation must not reach the cache
            with patch('library.operational_state.decode', side_effect=AssertionError('decoded again')):
                second, _ = observation.queue()
            self.assertEqual(second, packages)
            observation.publish(packages[:1], at=200.0, remaining=None, overshoot=None)
            third, at = observation.queue()
            self.assertEqual((len(third), at), (1, 200.0))
