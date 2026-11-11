"""Independent business adapters behind the retained serial-dispatch interlock."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from library.primitives import LibraryError, lock
from library.stage_tasks import StageTasks
from library.stage_resources import Resources
from library.source_tasks import SourcePolicy
from library.metadata import Providers
from library.publish import Capacity
from test_library_publish import setup_library
from postgres_fixture import create_store, require_postgresql
from library.primitives import empty
from library.operational_state import RuntimeObservation

AT = datetime(2026, 11, 9, tzinfo=timezone.utc)

class StageRuntimeTests(unittest.TestCase):
    def test_startup_budget_validation_waits_for_lane_without_reset(self):
        from booksys_core.network_admission import RollingBudget
        from library.postgres import guards
        from library.stage_worker import verify_budgets
        import threading
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            create_store(self, root, empty())
            for name in ('provider', 'native'):
                RollingBudget.create(root/(name+'-admission.yaml'), requests=2, period=60)
            budgets = [RollingBudget(root/(name+'-admission.yaml')) for name in ('provider','native')]
            def retained():
                with budgets[0]._database.connection() as db:
                    return list(db.execute('SELECT * FROM network_budgets ORDER BY scope'))
            before = retained()
            finished = threading.Event()
            with budgets[0]._database.connection(write=True) as held:
                guards(held, {'network-budget:native-admission.yaml'})
                thread = threading.Thread(target=lambda: (verify_budgets(root), finished.set()))
                thread.start()
                self.assertFalse(finished.wait(.1))
            thread.join(2)
            self.assertTrue(finished.is_set())
            self.assertEqual(retained(), before)

    def test_reader_probe_retains_stopped_proof_without_registry_exhaustion(self):
        from library.stage_runtime import StageRuntime
        from library.dependency_recovery import DependencyRecovery
        from library.decoder_ownership import DecoderOwnership
        from datetime import timedelta
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, reception, _, _ = setup_library(root,self)
            require_postgresql(catalog)
            StageTasks.upgrade(catalog.backend())
            Resources.create(catalog.directory/'stage-resources.yaml')
            SourcePolicy.create(catalog.directory/'task-policy.yaml')
            runtime = StageRuntime(root, reception, root/'collector', providers=Providers(root/'cache'), client=None, browser=None)
            observations = DependencyRecovery(catalog.directory/'dependency-readiness.yaml')
            observations.observe('reader-engine', available=False, at=AT.timestamp())
            with patch.dict('os.environ', {'BOOKSYS_READER_IMAGE': 'fixture'}), patch.object(DecoderOwnership, 'cleanup', return_value=True):
                with patch('library.stage_runtime.subprocess.run', return_value=subprocess.CompletedProcess([],125,b'',b'')):
                    for index in range(130):
                        runtime.recover_reader(now=AT+timedelta(seconds=index+1))
                with patch('library.stage_runtime.subprocess.run', return_value=subprocess.CompletedProcess([],0,b'',b'')):
                    runtime.recover_reader(now=AT+timedelta(seconds=131))
            self.assertTrue(observations.read()['dependencies']['reader-engine']['available'])
            self.assertEqual(len(DecoderOwnership(catalog.directory/'reader-dependency-children.yaml').names()), 1)
            self.assertEqual(len(list((root/'evidence/reader-probes').glob('*.yaml'))), 130)

    def test_exhausted_ocr_gets_one_episode_only_after_actual_engine_recovery(self):
        from library.stage_runtime import StageRuntime
        from library.stage_model import Obligation
        from library.readers import ReaderUnavailable
        from library.dependency_recovery import DependencyRecovery
        from datetime import timedelta
        import time
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, reception, _, aid = setup_library(root,self)
            require_postgresql(catalog)
            StageTasks.upgrade(catalog.backend())
            Resources.create(catalog.directory/'stage-resources.yaml')
            SourcePolicy.create(catalog.directory/'task-policy.yaml')
            runtime = StageRuntime(root, reception, root/'collector', providers=Providers(root/'cache'), client=None, browser=None)
            task = runtime.tasks.discover(Obligation('ocr', aid, 'fixture', 'v1'), now=AT)
            import subprocess
            with patch('library.printed.bounded_run', return_value=subprocess.CompletedProcess([], 125, b'', b'engine setup failed')):
                for offset in (0, 301, 1202):
                    stamp = AT+timedelta(seconds=offset)
                    with runtime.actor('ocr') as actor:
                        self.assertEqual(actor.step(now=stamp, free_bytes=100*1024**3, external_bytes=0), 'uncertain')
                    with runtime.actor('ocr') as actor:
                        self.assertEqual(actor.step(now=stamp, free_bytes=100*1024**3, external_bytes=0), 'absent')
            retained = runtime.tasks.get(task.key)
            self.assertEqual(retained.blocked, 'attempts_exhausted')
            self.assertEqual(retained.attempts, 3)
            later = datetime.now(timezone.utc)+timedelta(seconds=1)
            observations = DependencyRecovery(catalog.directory/'dependency-readiness.yaml')
            observations.observe('reader-probe', available=True, at=later.timestamp()-1)
            observations.observe('reader-engine', available=True, at=later.timestamp()-1)
            runtime.recover_reader(now=later)
            self.assertEqual(runtime.tasks.get(task.key).attempts, 3)
            observations.observe('reader-probe', available=False, at=later.timestamp())
            observations.observe('reader-probe', available=True, at=later.timestamp()+1)
            observations.observe('reader-engine', available=True, at=later.timestamp()+1)
            runtime.recover_reader(now=later+timedelta(seconds=2))
            recovered = runtime.tasks.get(task.key)
            self.assertEqual(recovered.attempts, 0)
            self.assertEqual(recovered.episode, 1)
            self.assertEqual(recovered.first_eligible_at, task.first_eligible_at)
            runtime.recover_reader(now=later+timedelta(seconds=1))
            self.assertEqual(runtime.tasks.get(task.key).episode, 1)

    def test_external_snapshot_counts_active_downloads_and_rejects_rate_drift(self):
        from library.stage_worker import snapshot
        from library.stage_runtime import external_capacity
        from library.primitives import atomic_yaml
        from types import SimpleNamespace
        import yaml
        import time
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            collector = root/'collector'
            configuration = dict(limit_speed=True, max_speed=10240, max_downloads=5, chunks=3)
            queue = [{'pid': 1, 'name': 'fixture', 'links': [{'status': 4, 'size': 0}, {'status': 12, 'size': 100}]}]
            def call(method, **kw):
                if method == 'get_queue_data': return queue
                if method == 'get_collector_data': return []
                if method == 'get_config_value': return configuration[kw['option']]
                raise AssertionError(method)
            from library.catalog import Catalog
            store = create_store(self, root/'state', empty())
            collector.mkdir()
            selector = collector/'postgresql.json'
            selector.write_bytes((root/'state/postgresql.json').read_bytes())
            selector.chmod(0o600)
            RuntimeObservation(store._database).activate(
                {'pyload_bound': {'configuration': dict(configuration), 'overshoot_bytes': 200}})
            runtime = SimpleNamespace(root=root, collector=collector, catalog=Catalog(root/'state'),
                                      client=SimpleNamespace(call=call))
            snapshot(runtime)
            now = datetime.now(timezone.utc)
            self.assertEqual(external_capacity(root/'snapshots/external-capacity.yaml', now=now), 300)
            configuration['limit_speed'] = False
            snapshot(runtime)
            self.assertIsNone(external_capacity(root/'snapshots/external-capacity.yaml', now=datetime.now(timezone.utc)))

    def test_refused_reader_start_defers_without_spending_attempt_or_failing_dependency(self):
        from library.stage_runtime import StageRuntime
        from library.stage_model import Obligation
        from library.reader_inputs import ReaderBusy
        from library.dependency_recovery import DependencyRecovery
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, reception, _, aid = setup_library(root,self)
            require_postgresql(catalog)
            StageTasks.upgrade(catalog.backend())
            Resources.create(catalog.directory/'stage-resources.yaml')
            SourcePolicy.create(catalog.directory/'task-policy.yaml')
            runtime = StageRuntime(root, reception, root/'collector', providers=Providers(root/'cache'), client=None, browser=None)
            task = runtime.tasks.discover(Obligation('ocr', aid, 'fixture', 'v1'), now=AT)
            with patch.object(runtime.business, 'execute', side_effect=ReaderBusy('Reader busy; request did not start')):
                with runtime.actor('ocr') as actor:
                    self.assertEqual(actor.step(now=AT, free_bytes=100*1024**3, external_bytes=0), 'defer')
            retained = runtime.tasks.get(task.key)
            self.assertEqual(retained.attempts, 0)
            self.assertEqual(retained.status, 'pending')
            self.assertEqual(retained.technical_dependency, '')
            self.assertEqual(runtime.resources.grants(), [])

    def test_retirement_lock_wait_preserves_attempt_and_has_no_native_effect(self):
        from datetime import timedelta
        from library.stage_runtime import StageRuntime
        from library.stage_model import Obligation
        from library.primitives import digest
        from test_retirement import fixture
        from test_library_intake import SOURCE
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, reception, collector, source, client = fixture(root,self)
            require_postgresql(catalog)
            StageTasks.upgrade(catalog.backend())
            Resources.create(catalog.directory/'stage-resources.yaml')
            SourcePolicy.create(catalog.directory/'task-policy.yaml')
            runtime = StageRuntime(root, reception, collector, providers=Providers(catalog.directory/'cache'),
                                   client=client, browser=None)
            task = runtime.tasks.discover(Obligation('retirement', 'source-'+digest(SOURCE), '1', 'v1'), now=AT)
            with patch.object(runtime.native, 'ready', return_value=''):
                with lock(collector/'pipeline.lock'), runtime.actor('retirement') as actor:
                    self.assertEqual(actor.step(now=AT, free_bytes=100*1024**3, external_bytes=0), 'defer')
                retained = runtime.tasks.get(task.key)
                self.assertEqual(retained.attempts, 0)
                self.assertEqual(retained.status, 'pending')
                from library.operational_state import NativeHistory
                self.assertEqual(NativeHistory(collector).retirements()['acquisitions'], {})
                self.assertEqual(client.removed, [])
                with runtime.actor('retirement') as actor:
                    self.assertEqual(actor.step(now=AT+timedelta(seconds=31), free_bytes=100*1024**3, external_bytes=0), 'done')
            self.assertTrue(source.exists())
            self.assertEqual(client.removed, [1])

    def test_native_allowance_wait_after_start_defers_without_spending_attempt(self):
        from library.stage_runtime import StageRuntime
        from library.stage_model import Obligation
        from booksys_core.network_admission import RollingBudget, BudgetWait
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, reception, _, _ = setup_library(root,self)
            require_postgresql(catalog)
            StageTasks.upgrade(catalog.backend())
            Resources.create(catalog.directory/'stage-resources.yaml')
            SourcePolicy.create(catalog.directory/'task-policy.yaml')
            budget = RollingBudget.create(catalog.directory/'native-admission.yaml', requests=30, period=60)
            runtime = StageRuntime(root, reception, root/'collector', providers=Providers(root/'cache'),
                client=SimpleNamespace(admission=budget), browser=None)
            task = runtime.tasks.discover(Obligation('collection', 'category', '1', 'v1'), now=AT)
            with patch.object(runtime.native, 'execute', side_effect=BudgetWait(AT.timestamp()+120)):
                with runtime.actor('collection') as actor:
                    self.assertEqual(actor.step(now=AT, free_bytes=100*1024**3, external_bytes=0), 'defer')
            retained = runtime.tasks.get(task.key)
            self.assertEqual(retained.attempts, 0)
            self.assertEqual(retained.status, 'pending')
            self.assertEqual(datetime.fromisoformat(retained.due_at).timestamp(), AT.timestamp()+120)

    def test_lost_copy_ack_recovers_current_output_without_another_conversion(self):
        from library.stage_runtime import StageRuntime
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, reception, source, aid = setup_library(root,self)
            require_postgresql(catalog)
            StageTasks.upgrade(catalog.backend())
            Resources.create(catalog.directory/'stage-resources.yaml')
            SourcePolicy.create(catalog.directory/'task-policy.yaml')
            runtime = StageRuntime(root, reception, root/'collector', providers=Providers(root/'cache'),
                client=None, browser=None, isolated=True, capacity=Capacity(reserve=0))
            runtime.discover_business(now=AT)
            with runtime.actor('copies') as actor:
                execute = actor.execute
                def lose_ack(task, stamp):
                    execute(task, stamp)
                    raise OSError('lost ack after committed publication')
                actor.execute = lose_ack
                self.assertIn(actor.step(now=AT, free_bytes=100*1024**3, external_bytes=0), ('uncertain', 'blocked_local'))
            from library.operational_state import CopyOperations
            self.assertIsNone(catalog.get('assets', aid).get('admission'))
            retained = CopyOperations(catalog.backend()._database, 'admission').records()
            self.assertEqual(len(retained), 1)
            operation = retained[0]['id']
            with runtime.actor('copies') as actor:
                self.assertEqual(actor.step(now=AT, free_bytes=100*1024**3, external_bytes=0), 'done')
            self.assertEqual(catalog.get('assets', aid)['admission']['operation'], operation)
            self.assertTrue(source.is_file())

    def test_native_budget_does_not_block_local_copy(self):
        from library.stage_runtime import StageRuntime
        from library.stage_model import Obligation
        from library.admission import projection
        from booksys_core.network_admission import RollingBudget
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, reception, _, aid = setup_library(root,self)
            require_postgresql(catalog)
            StageTasks.upgrade(catalog.backend())
            native = RollingBudget.create(catalog.directory/'native.yaml', requests=1, period=300)
            native.debit(timestamp=AT.timestamp())
            runtime = StageRuntime(root, reception, root/'collector',
                providers=Providers(root/'cache'), client=SimpleNamespace(admission=native), browser=None)
            state = catalog.working_set('admit', {'assets': [aid]}, media=False)
            task = runtime.tasks.discover(Obligation('copies', aid, projection(state,aid)['signature'], 'v1'), now=AT)
            self.assertEqual(runtime.ready(task, AT), '')

    def test_real_metadata_and_copy_steps_keep_intake_pending_and_serial_cycle_excluded(self):
        from library.stage_runtime import StageRuntime
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, reception, source, aid = setup_library(root,self)
            require_postgresql(catalog)
            StageTasks.upgrade(catalog.backend())
            Resources.create(catalog.directory/'stage-resources.yaml', external_policy='lab-reserve')
            SourcePolicy.create(catalog.directory/'task-policy.yaml')
            runtime = StageRuntime(root, reception, root/'collector', providers=Providers(root/'cache', fetch=lambda url: (200,'{"docs":[]}'), interval=0),
                client=None, browser=None, isolated=True, capacity=Capacity(reserve=0))
            from library.stage_model import Obligation
            runtime.tasks.discover(Obligation('intake', 'pending-source', 'input', 'v1'), now=AT)
            runtime.discover_business(now=AT)
            with lock(catalog.directory/'library-cycle.lock', blocking=False):
                with self.assertRaises(LibraryError):
                    with runtime.actor('copies'):
                        pass
            with runtime.actor('metadata') as actor:
                self.assertEqual(actor.step(now=AT, free_bytes=100*1024**3, external_bytes=None), 'done')
                with self.assertRaises(LibraryError):
                    with lock(catalog.directory/'library-cycle.lock', blocking=False):
                        pass
            runtime.discover_business(now=AT)
            with runtime.actor('copies') as actor:
                self.assertEqual(actor.step(now=AT, free_bytes=100*1024**3, external_bytes=None), 'done')
            self.assertEqual(catalog.get('assets',aid)['admission']['status'], 'classified')
            self.assertEqual(runtime.tasks.select('intake', now=AT).eligible, 1)
            self.assertTrue(source.is_file())

    def test_missing_or_stale_external_capacity_never_becomes_zero(self):
        from library.stage_runtime import external_capacity
        from library.primitives import atomic_yaml
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = create_store(self, root/'state', empty())
            path = root/'snapshots/external-capacity.yaml'
            self.assertIsNone(external_capacity(path, now=AT))
            observation = RuntimeObservation(store._database)
            observation.publish([], at=AT.timestamp()-91, remaining=100, overshoot=200)
            self.assertIsNone(external_capacity(path, now=AT))
            observation.publish([], at=AT.timestamp(), remaining=100, overshoot=200)
            self.assertEqual(external_capacity(path, now=AT),300)

    def test_every_postgresql_writer_respects_an_acting_entity_scope(self):
        from library.stage_execution import effect_scopes
        import threading
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, _, _, aid = setup_library(root,self)
            require_postgresql(catalog)
            state = catalog.result_set('identify', {'assets':[aid]})
            state['assets'][aid]['assertions'].append({'field':'summary','raw':'concurrent'})
            errors = []
            def write():
                try:
                    catalog.save(state, expected=state['revision'])
                except LibraryError as exc:
                    errors.append(str(exc))
            with effect_scopes(catalog.directory, ['asset:'+aid]):
                thread=threading.Thread(target=write)
                thread.start()
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(errors),1)
            self.assertNotIn({'field':'summary','raw':'concurrent'},catalog.get('assets',aid)['assertions'])

    def test_real_metadata_overlaps_real_copy_while_intake_has_a_backlog(self):
        from library.stage_runtime import StageRuntime
        from library.intake import receive
        from library.stage_model import Obligation
        from library.admission import projection
        from test_library_intake import SOURCE
        from test_library_readers import image_bytes
        import threading
        import zipfile
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            catalog,reception,source,aid=setup_library(root,self)
            second_source=SOURCE.replace('1-', '2-')
            second=reception/'second/other.cbz'
            second.parent.mkdir()
            with zipfile.ZipFile(second,'w') as archive:
                archive.writestr('001.png',image_bytes())
                archive.comment=b'independent source fixture'
            receive(catalog,reception,{'items':{second_source:{'title':'Another album'}}},
                {'submissions':{second_source:{'status':'queued','name':'other','package_id':2}}},
                [{'pid':2,'name':'other','folder':'second','links':[{'fid':2,'name':'other.cbz','status':0,'size':second.stat().st_size}]}],stable_seconds=.01)
            second_aid=next(key for key,_ in catalog.records('assets') if key != aid)
            require_postgresql(catalog)
            StageTasks.upgrade(catalog.backend())
            Resources.create(catalog.directory/'stage-resources.yaml')
            SourcePolicy.create(catalog.directory/'task-policy.yaml')
            entered, copied=threading.Event(),threading.Event()
            def fetch(url):
                entered.set()
                if not copied.wait(8):
                    raise AssertionError('Copy did not progress during metadata I/O')
                return 200,'{"docs":[]}'
            runtime=StageRuntime(root,reception,root/'collector',providers=Providers(root/'cache',fetch=fetch,interval=0),
                client=None,browser=None,isolated=True,capacity=Capacity(reserve=0))
            runtime.tasks.discover(Obligation('metadata',second_aid,'initial','metadata-step-v1'),now=AT)
            state=catalog.working_set('admit',{'assets':[aid]},media=False)
            runtime.tasks.discover(Obligation('copies',aid,projection(state,aid)['signature'],'admission-step-v1'),now=AT)
            for number in range(10):
                runtime.tasks.discover(Obligation('intake','pending-'+str(number),'initial','intake-step-v1'),now=AT)
            errors=[]
            def metadata():
                try:
                    with runtime.actor('metadata') as actor:
                        self.assertEqual(actor.step(now=AT,free_bytes=100*1024**3,external_bytes=0),'done')
                except BaseException as exc:
                    errors.append(exc)
            thread=threading.Thread(target=metadata)
            thread.start()
            try:
                self.assertTrue(entered.wait(8))
                with runtime.actor('copies') as actor:
                    self.assertEqual(actor.step(now=AT,free_bytes=100*1024**3,external_bytes=0),'done')
                self.assertEqual(catalog.get('assets',aid)['admission']['status'],'classified')
                self.assertTrue(thread.is_alive())
            finally:
                copied.set()
                thread.join(10)
            self.assertEqual(errors,[])
            self.assertFalse(thread.is_alive())
            self.assertEqual(runtime.tasks.select('intake',now=AT).eligible,10)
            self.assertTrue(source.is_file())
            self.assertTrue(second.is_file())
