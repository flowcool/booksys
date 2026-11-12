"""Real queue ownership and process stderr; all state is isolated."""
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

from booksys_core import diagnostics as d
from library.catalog import empty
from postgres_fixture import create_store
from library.primitives import LibraryError
from library.stage_actor import StageActor
from library.stage_model import Obligation, StepResult, QUEUES
from library.stage_tasks import StageTasks
from library.stage_resources import Resources, Demand

AT = datetime(2026, 11, 10, tzinfo=timezone.utc)
CANARY = 'password=CANARY https://private.example/source'

@contextmanager
def capture(path):
    with path.open('wb') as stream:
        mirror = d.Emitter(2) if os.environ.get('BOOKSYS_PROOF_STDERR') == '1' else None
        class ProofEmitter(d.Emitter):
            def emit(self, value):
                if mirror is not None:
                    mirror.emit(value)
                return super().emit(value)
        emitter = ProofEmitter(stream.fileno())
        with patch.object(d, '_default', emitter), patch.object(d, '_conditions', {}):
            try:
                yield
            finally:
                emitter.close()
                if mirror is not None:
                    mirror.close()

class StageDiagnosticTests(unittest.TestCase):
    def test_all_roles_emit_real_attempt_wait_failure_recovery_and_correlate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root/'events.jsonl'
            with capture(log):
                for queue in QUEUES:
                    state = root/queue
                    store = create_store(self, state, empty(), authority='diagnostic')
                    StageTasks.upgrade(store)
                    tasks, resources = StageTasks(store), Resources.create(state/'resources.yaml')
                    with StageActor(tasks, resources, queue, lambda task, now:'done',
                                    cleanup=lambda task:True, demand=lambda task:Demand(memory_mib=256),
                                    receipt=lambda task:('done','retained-proof')) as actor:
                        self.assertEqual(actor.step(now=AT, free_bytes=100*1024**3, external_bytes=0), 'idle')
                        task = tasks.discover(Obligation(queue, 'a'*64, 'fixture-v1', 'v1'), now=AT)
                        actor.ready = lambda task, stamp:'reader_setup_unavailable'
                        self.assertEqual(actor.step(now=AT, free_bytes=100*1024**3, external_bytes=0), 'reader_setup_unavailable')
                        actor.ready = lambda task, stamp:''
                        self.assertEqual(actor.step(now=AT+timedelta(seconds=31), free_bytes=100*1024**3, external_bytes=0), 'done')
                        tasks.discover(Obligation(queue, 'a'*64, 'fixture-v2', 'v1'), now=AT)
                        def fail(task, now):
                            raise OSError(CANARY)
                        actor.execute = fail
                        self.assertEqual(actor.step(now=AT, free_bytes=100*1024**3, external_bytes=0), 'uncertain')
                        self.assertEqual(actor.step(now=AT, free_bytes=100*1024**3, external_bytes=0), 'done')
            raw = log.read_text()
            self.assertNotIn('CANARY', raw)
            self.assertNotIn('private.example', raw)
            events = [d.parse_event(line) for line in raw.splitlines()]
            for queue in QUEUES:
                rows = [row for row in events if row.get('queue') == queue]
                self.assertTrue({'started','completed','waiting','failed','recovered'} <= {row['code'] for row in rows})
                self.assertTrue(any(row['reason']=='idle' for row in rows))
                self.assertTrue(any(row['reason']=='missing_dependency' and row.get('next_attempt') for row in rows))
                for row in rows:
                    if row['code'] in ('started','completed','failed','recovered'):
                        self.assertIn('attempt', row)
                        self.assertTrue(any(ref['kind']=='task' for ref in row['refs']))
                    if row['code'] in ('completed','failed'):
                        self.assertGreaterEqual(row['duration_ms'], 0)
            traced = [row for row in events if row.get('queue') in ('intake','ocr','metadata','copies') and row['refs']]
            self.assertEqual(len({row['request_id'] for row in traced}), 1)
            self.assertTrue(all({'kind':'asset','id':'a'*64} in row['refs'] for row in traced))

    def test_error_classes_wait_dedup_and_reader_detail(self):
        import sqlite3
        from library.diagnostic_reader import DiagnosticReader, MemorySource, Filter
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root/'events.jsonl'
            with capture(log):
                errors = [(LibraryError('storage_busy'),'storage_busy'),
                          (FileNotFoundError(CANARY),'missing_dependency'),
                          (ValueError(CANARY),'invalid_content'),
                          (RuntimeError(CANARY),'unexpected_exception')]
                for exc, expected in errors:
                    try:
                        raise exc
                    except Exception as raised:
                        detail = d.exception_details(raised)
                        self.assertEqual(detail['reason'], expected)
                        d.emit(service='library', stage='local', code='failed', **detail)
                for _ in range(500):
                    d.emit(service='library', stage='identity', code='waiting', reason='provider_wait', next_attempt=AT.isoformat())
                d.emit(service='library', stage='identity', code='completed')
            events = [d.parse_event(line) for line in log.read_text().splitlines()]
            self.assertEqual(len([e for e in events if e['code']=='waiting']),1)
            self.assertEqual(events[-1]['repeat_count'],499)
            reader = DiagnosticReader([MemorySource('isolated','same',[(AT.timestamp(),e) for e in events])],root/'reader.lock',clock=lambda:AT.timestamp()+10)
            rows = reader.query(Filter(AT.timestamp()-1,AT.timestamp()+1),stacks=True)['events']
            self.assertEqual(len(rows),len(events))
            self.assertNotIn('CANARY',json.dumps(rows))
            self.assertTrue(all('exception_type' in row for row in rows if row['code']=='failed'))
            import io
            from contextlib import redirect_stdout
            from library.diagnostic_cli import main as cli
            output=io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(cli(['--from',(AT-timedelta(seconds=1)).isoformat(),'--to',(AT+timedelta(seconds=1)).isoformat(),'--stack'],reader=reader),0)
            self.assertNotIn('CANARY',output.getvalue())
            from library.api import PublicAPI
            create_store(self, root/'state', empty())
            api=PublicAPI(root,host='lab',reception=root/'unused',diagnostic_reader=reader)
            web=api.invoke('diagnostics',{'host':'lab','origin':'','token':'','payload':{'filter':{'start':AT.timestamp()-1,'end':AT.timestamp()+1}}})
            self.assertNotIn('CANARY',json.dumps(web))
            self.assertTrue(all('stack' not in row for row in web['events']))
            self.assertTrue(any(row.get('exception_type')=='ValueError' for row in web['events']))


    def test_actual_intake_ocr_metadata_publication_share_committed_asset(self):
        from library.stage_runtime import StageRuntime
        from library.source_tasks import SourcePolicy
        from library.publish import Capacity
        from library.metadata import Providers
        from library.primitives import atomic_yaml, digest
        from test_library_publish import setup_library
        from test_library_intake import SOURCE
        from postgres_fixture import require_postgresql
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, reception, source, aid = setup_library(root,self)
            require_postgresql(catalog)
            StageTasks.upgrade(catalog.backend())
            Resources.create(catalog.directory/'stage-resources.yaml')
            SourcePolicy.create(catalog.directory/'task-policy.yaml')
            collector = root/'collector'
            from library.operational_state import CollectorState, RuntimeObservation
            database = catalog.backend()._database
            state = CollectorState(collector, database=database)
            state.submission(SOURCE, {'status':'queued','name':'tracked','package_id':1})
            state.capture(SOURCE, {'title':'Album'})
            RuntimeObservation(database).publish(
                [{'pid':1,'name':'tracked','folder':'package','links':[{'fid':1,'name':'album.cbz',
                  'status':0,'size':source.stat().st_size}]}], at=AT.timestamp(), remaining=0, overshoot=0)
            runtime=StageRuntime(root,reception,collector,providers=Providers(root/'cache',fetch=lambda url:(200,'{"docs":[]}'),interval=0),client=None,browser=None,isolated=True,capacity=Capacity(reserve=0))
            runtime.native.discover_intake(runtime.tasks,now=AT)
            runtime.tasks.discover(Obligation('ocr',aid,'fixture','v1'),now=AT)
            with capture(root/'events.jsonl'):
                with runtime.actor('intake') as actor:
                    actor.ready=lambda task,stamp:''
                    self.assertEqual(actor.step(now=AT,free_bytes=100*1024**3,external_bytes=0),'done')
                with patch('library.printed.read_printed',return_value={'status':'ok','asset':aid,'pages':[]}):
                    with runtime.actor('ocr') as actor:
                        self.assertEqual(actor.step(now=AT,free_bytes=100*1024**3,external_bytes=0),'done')
                runtime.discover_business(now=AT)
                with runtime.actor('metadata') as actor:
                    self.assertEqual(actor.step(now=AT,free_bytes=100*1024**3,external_bytes=0),'done')
                runtime.discover_business(now=AT)
                with runtime.actor('copies') as actor:
                    self.assertEqual(actor.step(now=AT,free_bytes=100*1024**3,external_bytes=0),'done')
            rows=[d.parse_event(line) for line in (root/'events.jsonl').read_text().splitlines()]
            linked=[r for r in rows if r.get('queue') and {'kind':'asset','id':aid} in r['refs']]
            self.assertEqual({r['queue'] for r in linked},{'intake','ocr','metadata','copies'})
            self.assertEqual(len({r['request_id'] for r in linked}),1)
            self.assertTrue(any({'kind':'source','id':digest(SOURCE)} in r['refs'] for r in linked))
            self.assertEqual(catalog.get('assets',aid)['admission']['status'],'classified')
            self.assertTrue(source.is_file())

    def test_uncaught_startup_failure_is_safe_process_stderr(self):
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            result=subprocess.run([sys.executable,'-m','library.stage_worker','--root',directory,'--collector',directory,'--reception',directory,'--credentials',directory+'/CANARY','--queue','intake'],capture_output=True)
            self.assertNotEqual(result.returncode,0)
            self.assertEqual(result.stdout,b'')
            self.assertNotIn(b'CANARY',result.stderr)
            self.assertNotIn(b'Traceback',result.stderr)
            rows=[d.parse_event(line) for line in result.stderr.decode().splitlines()]
            self.assertEqual([r['code'] for r in rows],['started','failed'])
            self.assertEqual(rows[-1]['reason'],'unexpected_exception')
            self.assertTrue(rows[-1]['stack'])

    def test_invalid_archive_emits_safe_exception_before_reader(self):
        from library.catalog import Catalog
        from library.intake import receive
        from test_library_intake import SOURCE
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            create_store(self, root/'state', empty())
            source=root/'CANARY.cbz'
            source.write_bytes(b'PK\x03\x04invalid archive body CANARY')
            with capture(root/'events.jsonl'):
                receive(Catalog(root/'state'),root,{'items':{SOURCE:{'title':'Album'}}},
                    {'submissions':{SOURCE:{'status':'queued','name':'tracked','package_id':1}}},
                    [{'pid':1,'name':'tracked','folder':'','links':[{'fid':1,'name':source.name,'status':0,'size':source.stat().st_size}]}],stable_seconds=.01)
            raw=(root/'events.jsonl').read_text()
            self.assertNotIn('CANARY',raw)
            rows=[d.parse_event(line) for line in raw.splitlines()]
            self.assertTrue(any(row['reason']=='invalid_archive' for row in rows))

    def test_real_postgresql_contention_remains_a_safe_wait(self):
        from library.postgres import guards
        from library.locking import acquisition_deadline
        import time
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            store=create_store(self, root/'state',empty(),authority='contention')
            StageTasks.upgrade(store)
            tasks=StageTasks(store)
            task = tasks.discover(Obligation('metadata','fixture','v1','v1'),now=AT)
            with capture(root/'events.jsonl'), StageActor(tasks,Resources.create(root/'state/resources.yaml'),'metadata',lambda task,now:'done',cleanup=lambda task:True,demand=lambda task:Demand(memory_mib=256)) as actor:
                with store._database.connection(write=True) as held:
                    guards(held, {'task:'+task.key})
                    token = acquisition_deadline.set(time.monotonic()+1)
                    try:
                        self.assertEqual(actor.step(now=AT,free_bytes=100*1024**3,external_bytes=0),'storage_wait')
                    finally:
                        acquisition_deadline.reset(token)
                self.assertEqual(actor.step(now=AT,free_bytes=100*1024**3,external_bytes=0),'done')
            rows=[d.parse_event(line) for line in (root/'events.jsonl').read_text().splitlines()]
            self.assertTrue(any(r['reason']=='storage_busy' and r.get('exception_type')=='LibraryError' and r.get('stack') for r in rows))
            self.assertEqual(tasks.get(next(r['refs'][0]['id'] for r in rows if r['code']=='started')).attempts,1)

    def test_worker_lifecycle_and_graceful_signal(self):
        from library import stage_worker as worker
        from contextlib import nullcontext
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'library').mkdir()
            store = create_store(self, root/'state', empty())
            (root/'unused').write_text(json.dumps({'api_key':'isolated'}))
            actor = Mock()
            actor.queue = "metadata"
            actor.__enter__ = Mock(return_value=actor)
            actor.__exit__ = Mock(return_value=False)
            def step(**kwargs):
                os.kill(os.getpid(), signal.SIGTERM)
                return 'idle'
            actor.step.side_effect = step
            runtime = Mock()
            runtime.catalog.backend.return_value = store
            runtime.actor.return_value = actor
            argv=['worker','--root',str(root),'--collector',str(root),'--reception',str(root),'--credentials',str(root/'unused'),'--queue','metadata']
            old_term, old_int = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
            try:
                with patch.dict(os.environ), capture(root/'events.jsonl'), patch.object(sys,'argv',argv), patch.object(worker,'verify_activation',return_value={'mode':'independent'}), patch('library.runtime.lab_preflight'), patch.object(worker,'verify_startup'), patch.object(worker,'verify_budgets'), patch.object(worker,'StageRuntime',return_value=runtime), patch.object(worker,'Pyload'), patch.object(worker,'Providers'), patch.object(worker,'deadline',side_effect=lambda value:nullcontext()), patch.object(worker,'external_capacity',return_value=0), patch('booksys_core.diagnostics.install_logging'):
                    worker.main()
            finally:
                signal.signal(signal.SIGTERM, old_term)
                signal.signal(signal.SIGINT, old_int)
            rows=[d.parse_event(line) for line in (root/'events.jsonl').read_text().splitlines()]
            self.assertEqual([(r['code'],r.get('outcome')) for r in rows],[('started',None),('completed','stopped')])

if __name__ == '__main__':
    unittest.main()
