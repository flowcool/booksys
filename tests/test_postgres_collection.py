"""Canonical source records and checkpoints do not depend on legacy files."""
from pathlib import Path
import tempfile
import unittest
from library.primitives import empty
from tests.postgres_fixture import create_store

class CollectionStateTests(unittest.TestCase):
    def test_source_capture_and_checkpoint_survive_reopen_without_yaml(self):
        from library.operational_state import CollectorState
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=create_store(self,root,empty())
            state=CollectorState(root)
            state.capture('source',{'title':'Original','downloads':{}},source_checkpoint=None,
                          collection_checkpoint={'item_index':1})
            (root/'state.yaml').write_text('corrupt frozen historical source')
            again=CollectorState(root)
            self.assertEqual(again.captured()['items']['source']['title'],'Original')
            self.assertEqual(again.monitor()['collection_checkpoint'],{'item_index':1})
            self.assertNotIn('source_checkpoint',again.monitor())

    def test_submission_save_preserves_newer_schedule_and_distinct_source_records(self):
        from library.operational_state import CollectorState
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);create_store(self,root,empty());state=CollectorState(root)
            stale=state.monitor();state.patch_monitor(schedule={'not_before':42})
            stale['submissions']['a']={'status':'creating','operation_id':'exact'}
            state.submission('a',stale['submissions']['a'])
            state.submission('b',{'status':'queued','package_id':9})
            again=CollectorState(root).monitor()
            self.assertEqual(again['schedule'],{'not_before':42})
            self.assertEqual(set(again['submissions']),{'a','b'})

    def test_unrelated_source_capture_does_not_overwrite_stale_other_record(self):
        from library.operational_state import CollectorState
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);create_store(self,root,empty());state=CollectorState(root)
            state.capture('a',{'title':'Old A'});state.capture('b',{'title':'Old B'})
            stale=state.captured();state.capture('b',{'title':'New B'})
            stale['items']['a']['title']='New A';state.capture('a',stale['items']['a'])
            self.assertEqual(state.captured()['items'],{'a':{'title':'New A'},'b':{'title':'New B'}})

    def test_capture_keeps_ordered_duplicate_links_and_absent_fields(self):
        from library.operational_state import CollectorState
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=create_store(self,root,empty());state=CollectorState(root)
            record={'title':'Exact','downloads':{'target':{'hoster':'h','urls':['a','a','b']},'unresolved':{'hoster':'other'}}}
            state.capture('source',record)
            self.assertEqual(state.captured()['items']['source'],record)
            with store._database.connection() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM captured_download_links').fetchone()[0],3)
            state.capture('source',{'title':'No invented downloads'})
            self.assertEqual(state.captured()['items']['source'],{'title':'No invented downloads'})

    def test_backfill_page_and_continuation_preserve_order_and_memberships(self):
        from library.operational_state import CollectorState
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=create_store(self,root,empty());state=CollectorState(root)
            job={'version':1,'status':'collecting','next_page':'page1','pages':{},'items':{}}
            state.start_backfill(job)
            for url,items,next_page in [('page1',{'b':'B','a':'A'},'page2'),('page2',{'b':'B2','c':'C'},None)]:
                job['pages'][url]=list(items);job['items'].update(items);job['next_page']=next_page
                state.backfill_page(job,url,items)
            restored=state.backfill()
            self.assertEqual(list(restored['pages']),['page1','page2'])
            self.assertEqual(restored['pages'],job['pages'])
            self.assertEqual(restored['items'],{'a':'A','b':'B2','c':'C'})
            self.assertIsNone(restored['next_page'])
            with store._database.connection() as db:
                self.assertNotIn('pages',db.execute('SELECT payload FROM collection_backfill').fetchone()[0])

    def test_native_source_discovery_uses_canonical_rows_after_frozen_source_corruption(self):
        from library.operational_state import CollectorState
        from library.source_tasks import SourcePolicy
        from library.stage_tasks import StageTasks
        from datetime import datetime,timezone
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=create_store(self,root,empty());state=CollectorState(root)
            state.category('https://category.test',{'after_id':4})
            state.submission('https://source.test',{'status':'queued','package_id':1})
            frozen=root/'monitor.yaml';frozen.write_text('invalid: frozen bytes')
            policy=SourcePolicy.create(root/'task-policy.yaml',store=store)
            tasks=StageTasks(store,policy=policy);stamp=datetime(2026,11,10,tzinfo=timezone.utc)
            result=tasks.scan('collection',frozen,now=stamp)
            self.assertEqual((result.processed,result.blocked),(2,''))
            state.fiche('https://source.test',{'status':'complete'})
            changed=policy.journal(frozen,queue='collection')
            self.assertEqual(len(changed['items']),1)
            self.assertTrue(next(iter(changed['items'])).startswith('category-'))

    def test_native_collection_callthrough_finishes_with_exact_source_progress(self):
        from library.operational_state import CollectorState
        from library.source_tasks import SourcePolicy
        from library.stage_tasks import StageTasks
        from library.native_stages import NativeStages
        from library.catalog import Catalog
        from test_pipeline import MemoryDownloader,FIRST
        from booksys_core.parsing import DEFAULT_CATEGORY
        from datetime import datetime,timezone
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=create_store(self,root,empty());state=CollectorState(root)
            state.category(DEFAULT_CATEGORY,{'after_id':10})
            policy=SourcePolicy.create(root/'task-policy.yaml',store=store)
            tasks=StageTasks(store,policy=policy);stamp=datetime(2026,11,10,tzinfo=timezone.utc)
            self.assertEqual(tasks.scan('collection',root/'monitor.yaml',now=stamp).processed,1)
            task=tasks.select('collection',now=stamp).task
            active=tasks.start(task.key,task.generation,now=stamp,resources=True)
            def fetch(url):
                self.assertEqual(url,DEFAULT_CATEGORY)
                return f'<h1>Catalogue</h1><div class="bys-items-container"><a class="bys-item" href="{FIRST}">Old</a></div>'
            native=NativeStages(Catalog(root,store=store),root/'reception',root/'library',root,browser=fetch,client=MemoryDownloader())
            result=native.execute(active,now=stamp)
            self.assertEqual(result.outcome,'done')
            self.assertIsNotNone(result.source_progress)
            self.assertEqual(tasks.get(task.key).status,'active')
            self.assertNotIn('last_success',state.monitor()['categories'][DEFAULT_CATEGORY])
            # Discovery observes this owner's bounded checkpoint without granting
            # a new attempt or superseding its original accepted input.
            tasks.scan('collection',root/'monitor.yaml',now=stamp)
            retained=tasks.get(active.key)
            self.assertEqual((retained.status,retained.attempts,retained.generation,retained.superseded_by),
                ('active',active.attempts,active.generation,''))
            from unittest.mock import patch
            from library.operational_state import apply_completion
            from library.primitives import LibraryError
            def fail_after_domain(db,result):
                apply_completion(db,result)
                raise LibraryError('injected after canonical domain write')
            with patch('library.operational_state.apply_completion',fail_after_domain):
                with self.assertRaises(LibraryError):
                    tasks.complete(active.key,active.generation,now=stamp,outcome=result.outcome,proof=result.proof,source_progress=result.source_progress)
            self.assertEqual(tasks.get(task.key).status,'active')
            self.assertIsNone(policy.progress(active.source,active.entity))
            self.assertNotIn('last_success',state.monitor()['categories'][DEFAULT_CATEGORY])
            self.assertTrue(state.monitor()['collection_checkpoint']['complete'])
            tasks.complete(active.key,active.generation,now=stamp,outcome=result.outcome,proof=result.proof,source_progress=result.source_progress)
            self.assertEqual(tasks.get(task.key).status,'done')
            self.assertEqual(policy.progress(active.source,active.entity)['proof'],result.proof)
            self.assertEqual(state.monitor()['categories'][DEFAULT_CATEGORY]['after_id'],10)

    def test_active_backfill_checkpoint_rescan_keeps_original_attempt_and_binding(self):
        from library.operational_state import CollectorState
        from library.source_tasks import SourcePolicy
        from library.stage_tasks import StageTasks
        from datetime import datetime,timezone
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=create_store(self,root,empty());state=CollectorState(root)
            state.start_backfill({'version':1,'pages_requested':2,'next_page':'https://catalogue/1',
                'pages':{},'items':{},'submitted':0,'status':'collecting','created_at':'retained'})
            policy=SourcePolicy.create(root/'task-policy.yaml',store=store);tasks=StageTasks(store,policy=policy)
            stamp=datetime(2026,11,10,tzinfo=timezone.utc)
            tasks.scan('collection',root/'backfill.yaml',now=stamp,kind='backfill')
            task=tasks.select('collection',now=stamp).task
            active=tasks.start(task.key,task.generation,now=stamp,resources=True)
            job=state.backfill();job['pages']['https://catalogue/1']=['https://source/1'];job['items']['https://source/1']='Retained'
            job['next_page']='https://catalogue/2';state.backfill_page(job,'https://catalogue/1',{'https://source/1':'Retained'})
            tasks.scan('collection',root/'backfill.yaml',now=stamp,kind='backfill')
            retained=tasks.get(active.key)
            self.assertEqual((retained.status,retained.attempts,retained.generation,retained.superseded_by),
                ('active',active.attempts,active.generation,''))
            terminal={k:v for k,v in job.items() if k not in ('pages','items')};terminal['status']='complete'
            completion=state.prepare_completion('backfill','current',terminal)
            progress=policy.prepare_progress(active,{'proof':'same-original-backfill'},completions=[completion])
            tasks.complete(active.key,active.generation,now=stamp,source_progress=progress)
            done=tasks.get(active.key)
            self.assertEqual((done.status,done.attempts,done.generation),('done',active.attempts,active.generation))
            self.assertEqual(state.backfill()['pages'],job['pages'])
            self.assertEqual(policy.progress(active.source,active.entity),{'proof':'same-original-backfill'})
            with store._database.connection() as db:
                import json
                raw=json.loads(db.execute('SELECT payload FROM source_registry WHERE id=%s',(active.source,)).fetchone()[0])
            self.assertNotIn('inputs',raw)
            self.assertEqual(raw['native'],{'queue':'collection'})
            public=policy.read()['sources'][active.source]
            self.assertEqual(public['inputs'][active.entity],active.input_revision)

    def test_native_observation_preserves_order_capacity_and_independent_heartbeats(self):
        from library.operational_state import RuntimeObservation
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=create_store(self,root,empty());state=RuntimeObservation(store._database)
            packages=[{'pid':2,'dest':1,'links':[{'fid':9,'status':12},{'fid':8,'status':0}]},{'pid':1,'dest':0,'links':[]}]
            state.publish(packages,at=123,remaining=None,overshoot=32)
            self.assertEqual(state.queue(),(packages,123))
            self.assertIsNone(state.capacity()['remaining_bytes'])
            state.heartbeat('intake',at=120);state.heartbeat('metadata',at=121)
            state.heartbeat('intake',at=123)
            self.assertEqual(state.heartbeats(),{'intake':123,'metadata':121})

    def test_retirement_terminal_readback_waits_for_atomic_native_task_completion(self):
        from test_retirement import fixture
        from library.source_tasks import SourcePolicy
        from library.stage_tasks import StageTasks
        from library.native_stages import NativeStages
        from library.operational_state import NativeHistory
        from datetime import datetime,timezone
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);catalog,reception,collector,original,client=fixture(root,self)
            store=catalog.backend();policy=SourcePolicy.create(root/'state/task-policy.yaml',store=store)
            tasks=StageTasks(store,policy=policy);stamp=datetime(2026,11,10,tzinfo=timezone.utc)
            self.assertEqual(tasks.scan('retirement',collector/'monitor.yaml',now=stamp).processed,1)
            task=tasks.select('retirement',now=stamp).task
            active=tasks.start(task.key,task.generation,now=stamp,resources=True)
            native=NativeStages(catalog,reception,root/'library',collector,browser=None,client=client)
            scripts=root/'scripts';scripts.mkdir()
            with patch('library.runtime.configured_scripts',return_value=scripts):
                result=native.execute(active,now=stamp)
            self.assertEqual(result.outcome,'done')
            self.assertEqual(client.removed,[1])
            history=NativeHistory(collector).retirements()['acquisitions']
            source=next(iter(history));self.assertEqual(history[source]['phase'],'deleting')
            self.assertEqual(result.source_progress.completions[0].kind,'retirement')
            tasks.complete(active.key,active.generation,now=stamp,outcome=result.outcome,proof=result.proof,source_progress=result.source_progress)
            self.assertEqual(NativeHistory(collector).retirements()['acquisitions'][source]['phase'],'retired')
            self.assertEqual(tasks.get(task.key).status,'done')
            self.assertTrue(original.is_file())

    def test_resolved_native_pointer_recovers_deleting_task_without_new_attempt(self):
        from test_retirement import fixture
        from library.source_tasks import SourcePolicy
        from library.stage_tasks import StageTasks
        from library.native_stages import NativeStages
        from library.operational_state import NativeHistory
        from booksys_core.network_admission import NativeMutation,RollingBudget
        from datetime import datetime,timezone
        from unittest.mock import patch
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);catalog,reception,collector,original,client=fixture(root,self)
            store=catalog.backend();policy=SourcePolicy.create(root/'state/task-policy.yaml',store=store)
            tasks=StageTasks(store,policy=policy);stamp=datetime(2026,11,10,tzinfo=timezone.utc)
            tasks.scan('retirement',collector/'monitor.yaml',now=stamp)
            task=tasks.select('retirement',now=stamp).task
            active=tasks.start(task.key,task.generation,now=stamp,resources=True)
            RollingBudget.create(collector/'native-admission.yaml',requests=30,period=60)
            client.admission=SimpleNamespace(path=collector/'native-admission.yaml')
            native=NativeStages(catalog,reception,root/'library',collector,browser=None,client=client)
            scripts=root/'scripts';scripts.mkdir()
            journal=collector/'package-retirement.yaml'
            from test_library_intake import SOURCE
            with patch('library.runtime.configured_scripts',return_value=scripts):
                result=native.execute(active,now=stamp)
                mutation=NativeMutation(collector,'retirement:'+SOURCE,journal)
                mutation.resolve('exact-external-readback-before-crash')
                self.assertEqual(mutation.read()['state'],'resolved')
                self.assertEqual(NativeHistory(collector).retirements()['acquisitions'][SOURCE]['phase'],'deleting')
                receipt=native.receipt(active)
            self.assertEqual(receipt.outcome,'done')
            done=tasks.reconcile(active.key,active.generation,now=stamp,owner_stopped=True,outcome=receipt.outcome,proof=receipt.proof,source_progress=receipt.source_progress)
            self.assertEqual((done.status,done.attempts,done.generation),('done',active.attempts,active.generation))
            self.assertEqual(client.removed,[1])
            self.assertEqual(NativeHistory(collector).retirements()['acquisitions'][SOURCE]['phase'],'retired')
            self.assertTrue(original.is_file())

    def test_reader_sql_functions_reject_missing_and_stale_grants(self):
        from library.operational_state import ReaderJobs
        from library.decoder_ownership import DecoderOwnership
        from library.stage_tasks import StageTasks,Obligation
        from library.stage_resources import Resources,Demand
        from datetime import datetime,timezone
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=create_store(self,root,empty());jobs=ReaderJobs(store._database)
            stamp=datetime(2026,11,10,tzinfo=timezone.utc);tasks=StageTasks(store)
            task=tasks.discover(Obligation('ocr','asset','1','v1'),now=stamp)
            grant=task.key+':1';reference='a'*32
            manifest={'operation':'proof','input':'downloads/a.cbz','sha256':'b'*64,'payload':{'page':1}}
            jobs.request(reference,grant,manifest)
            from library.primitives import LibraryError
            with self.assertRaises(LibraryError):jobs.accepted(reference)
            resources=Resources.create(root/'stage-resources.yaml')
            self.assertEqual(resources.admit(grant,age=stamp,demand=Demand(memory_mib=1792,cpu_millis=1250,disk=1,output_bytes=4*1024**3),free_bytes=100*1024**3,external_bytes=0),'')
            children=DecoderOwnership.create(root/'stage-children'/(grant+'.yaml'))
            children.register('booksys-reader-'+reference)
            active=tasks.start(task.key,task.generation,now=stamp,resources=True)
            self.assertEqual(jobs.accepted(reference)['grant'],grant)
            intent={'name':'booksys-reader-'+reference,'image':'sha256:'+'c'*64,'manifest_sha256':__import__('hashlib').sha256(__import__('json').dumps(manifest).encode()).hexdigest()}
            jobs.retain(reference,'intent',intent)
            self.assertEqual(jobs.read(reference,'intent'),intent)
            tasks.complete(active.key,active.generation,now=stamp)
            with self.assertRaises(LibraryError):jobs.accepted(reference)
            self.assertEqual(jobs.read(reference,'intent'),intent)

    def test_launcher_role_has_only_closed_function_capabilities(self):
        from library.launcher_access import grant
        from library.postgres import Database
        from uuid import uuid4
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import conninfo_to_dict,make_conninfo
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=create_store(self,root,empty());database=store._database
            launcher='launcher_'+uuid4().hex;definer='definer_'+uuid4().hex;application='application_'+uuid4().hex
            password=uuid4().hex
            def cleanup_roles():
                with psycopg.connect(database.dsn,autocommit=True) as db:
                    db.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(database.schema)))
                    db.execute(sql.SQL('REVOKE EXECUTE ON FUNCTION pg_catalog.pg_control_system() FROM {}').format(sql.Identifier(application)))
                    for role in (launcher,definer,application):db.execute(sql.SQL('DROP ROLE IF EXISTS {}').format(sql.Identifier(role)))
            self.addCleanup(cleanup_roles)
            with psycopg.connect(database.dsn,autocommit=True) as db:
                db.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {}').format(sql.Identifier(launcher),sql.Literal(password)))
                db.execute(sql.SQL('CREATE ROLE {} NOLOGIN').format(sql.Identifier(definer)))
            from ops.postgres_roles import provision
            provision(database,application=application,launcher=launcher,definer=definer,
                app_password=password,launcher_password=password)
            app_params=conninfo_to_dict(database.dsn);app_params.update(user=application,password=password)
            app_database=Database(make_conninfo(**app_params),database.schema)
            with app_database.connection(write=True) as db:
                db.execute("SELECT lock_conflict_guards(ARRAY['application-proof'])")
                self.assertIsNotNone(db.execute('SELECT system_identifier FROM pg_catalog.pg_control_system()').fetchone())
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                with app_database.connection(write=True) as db:db.execute('CREATE TABLE forbidden(id int)')
            params=conninfo_to_dict(database.dsn);params.update(user=launcher,password=password)
            restricted=Database(make_conninfo(**params),database.schema)
            with restricted.connection() as db:
                self.assertIsNone(db.execute('SELECT reader_accept(%s)',('f'*32,)).fetchone()[0])
            from library.operational_state import ComposeDiagnostics
            full=ComposeDiagnostics(database);limited=ComposeDiagnostics(restricted)
            query={'role':'pyload','generation':'exact','start':1,'end':2,'position':None,'lines':10,'byte_limit':100}
            full.retain('a'*32,query)
            limited.publish({'schema':1,'observed_at':123,'sources':{'pyload':'exact'}})
            self.assertEqual(limited.request('a'*32),query)
            self.assertEqual(full.sources()['sources'],{'pyload':'exact'})
            with self.assertRaises(psycopg.errors.RaiseException):limited.publish({'schema':1,'observed_at':123,'sources':{'arbitrary':'bad'}})
            for command in ('SELECT * FROM diagnostic_requests','SELECT * FROM compose_sources','SELECT * FROM assets','SELECT * FROM stage_tasks','SELECT * FROM reader_requests','SELECT * FROM resource_grants','UPDATE stage_tasks SET status=status WHERE false','DELETE FROM assets WHERE false','INSERT INTO resource_grants SELECT * FROM resource_grants WHERE false','CREATE TABLE forbidden(id int)'):
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    with restricted.connection(write=True) as db:db.execute(command)
            with self.assertRaises(psycopg.errors.RaiseException):
                with restricted.connection(write=True) as db:
                    db.execute('SELECT reader_retain(%s,%s,%s)',('f'*32,'intent','{}'))
            from library.stage_tasks import StageTasks,Obligation
            from library.stage_resources import Resources,Demand
            from library.decoder_ownership import DecoderOwnership
            from library.operational_state import ReaderJobs
            from datetime import datetime,timezone
            import hashlib,json
            stamp=datetime(2026,11,10,tzinfo=timezone.utc);tasks=StageTasks(store)
            task=tasks.discover(Obligation('ocr','restricted-proof','1','v1'),now=stamp)
            key=task.key+':1';reference='e'*32
            Resources.create(root/'stage-resources.yaml').admit(key,age=stamp,demand=Demand(memory_mib=1792,cpu_millis=1250,disk=1,output_bytes=4*1024**3),free_bytes=100*1024**3,external_bytes=0)
            DecoderOwnership.create(root/'stage-children'/(key+'.yaml')).register('booksys-reader-'+reference)
            tasks.start(task.key,task.generation,now=stamp,resources=True)
            manifest={'operation':'proof','input':'downloads/a.cbz','sha256':'b'*64,'payload':{'page':1}}
            ReaderJobs(database).request(reference,key,manifest)
            limited_jobs=ReaderJobs(restricted)
            intent={'name':'booksys-reader-'+reference,'image':'sha256:'+'c'*64,'manifest_sha256':hashlib.sha256(json.dumps(manifest).encode()).hexdigest()}
            limited_jobs.retain(reference,'intent',intent)
            limited_jobs.retain(reference,'intent',intent)
            container={**intent,'container':'d'*64}
            limited_jobs.retain(reference,'container',container)
            limited_jobs.retain(reference,'cleanup',{**container,'removed':False})
            limited_jobs.retain(reference,'cleanup',{**container,'removed':True})
            self.assertEqual(limited_jobs.read(reference,'cleanup'),{**container,'removed':True})
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                with restricted.connection(write=True) as db:db.execute("SELECT lock_conflict_guards(ARRAY['forbidden'])")

    def test_reader_runtime_keeps_immutable_phase_bytes_bound_in_postgres(self):
        from library.reader_jobs import JobLauncher,JobError
        from library.operational_state import ReaderJobs
        from library.stage_tasks import StageTasks,Obligation
        from library.decoder_ownership import DecoderOwnership
        from library.stage_resources import Resources,Demand
        from datetime import datetime,timezone
        import json,hashlib
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=create_store(self,root,empty());inputs=root/'inputs';inputs.mkdir();jobs=root/'jobs';jobs.mkdir()
            stamp=datetime(2026,11,10,tzinfo=timezone.utc);tasks=StageTasks(store)
            task=tasks.discover(Obligation('ocr','asset','1','v1'),now=stamp);grant=task.key+':1';reference='a'*32
            Resources.create(root/'stage-resources.yaml').admit(grant,age=stamp,demand=Demand(memory_mib=1792,cpu_millis=1250,disk=1,output_bytes=4*1024**3),free_bytes=100*1024**3,external_bytes=0)
            owner=DecoderOwnership.create(root/'stage-children'/(grant+'.yaml'));owner.register('booksys-reader-'+reference)
            tasks.start(task.key,task.generation,now=stamp,resources=True)
            manifest={'operation':'proof','input':'a.cbz','sha256':'b'*64,'payload':{'page':1}}
            backend=ReaderJobs(store._database);backend.request(reference,grant,manifest)
            launcher=JobLauncher(inputs,jobs,'sha256:'+'c'*64,remove_stopped=False);launcher.retained=backend
            job=jobs/reference;job.mkdir()
            intent={'name':'booksys-reader-'+reference,'image':launcher.image,'manifest_sha256':hashlib.sha256(json.dumps(manifest).encode()).hexdigest()}
            launcher.retained_write(job,'intent',intent)
            raw=(job/'intent.yaml').read_bytes()
            self.assertEqual(backend.read(reference,'intent')['_evidence']['sha256'],hashlib.sha256(raw).hexdigest())
            self.assertEqual(launcher.retained_read(job,'intent'),intent)
            launcher.retained_write(job,'intent',intent)
            self.assertEqual((job/'intent.yaml').read_bytes(),raw)
            from library.reader_service import GrantedLauncher
            from unittest.mock import patch
            import subprocess,os
            with patch.dict(os.environ,{'BOOKSYS_LAUNCHER_DATABASE_DIR':str(root)}):
                service=GrantedLauncher(inputs,jobs,launcher.image,root,job_uid=os.getuid(),cgroup_parent='test.slice')
            worker={'Image':launcher.image,'Id':'d'*64,'Config':{'Labels':{'booksys.reader.job':reference,'booksys.reader.manifest':intent['manifest_sha256']}}}
            responses=[subprocess.CompletedProcess([],0,json.dumps(worker).encode(),b''),subprocess.CompletedProcess([],0,b'',b''),subprocess.CompletedProcess([],0,b'{"Running":false}',b'')]
            for _ in range(2):
                with patch('library.reader_service.subprocess.run',side_effect=responses) as engine:
                    self.assertEqual(service.reconcile(reference),{'stopped':True,'status':'reconciled'})
                    self.assertFalse(any('create' in call.args[0] or 'rm' in call.args[0] for call in engine.call_args_list))
            self.assertEqual(launcher.retained_read(job,'container')['container'],'d'*64)
            (job/'intent.yaml').chmod(0o600)
            (job/'intent.yaml').write_text('changed: bytes')
            with self.assertRaises(JobError):launcher.retained_read(job,'intent')

    def test_admission_result_and_operation_phase_commit_with_task(self):
        from test_library_publish import setup_library
        from library.admission import AdmissionPublisher
        from library.publication import Capacity
        from library.stage_tasks import StageTasks,Obligation
        from datetime import datetime,timezone
        from unittest.mock import patch
        from library.operational_state import apply_completion
        from library.primitives import LibraryError
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);catalog,reception,original,asset=setup_library(root,self)
            tasks=StageTasks(catalog.backend());stamp=datetime(2026,11,10,tzinfo=timezone.utc)
            task=tasks.discover(Obligation('copies',asset,'1','v1'),now=stamp)
            active=tasks.start(task.key,task.generation,now=stamp,resources=True)
            prepared=[];writer=AdmissionPublisher(catalog,reception,root/'library',isolated=True,capacity=Capacity(reserve=0),prepared=prepared)
            operation=writer.publish_asset(asset)
            self.assertEqual(len(prepared),1)
            self.assertNotIn('admission',catalog.get('assets',asset))
            self.assertNotEqual(writer.load_operation(operation['id'])['phase'],'committed')
            def fail(db,result):
                apply_completion(db,result)
                raise LibraryError('injected terminal gap')
            with patch('library.operational_state.apply_completion',fail):
                with self.assertRaises(LibraryError):tasks.complete(active.key,active.generation,now=stamp,prepared=prepared[0])
            self.assertNotIn('admission',catalog.get('assets',asset))
            self.assertEqual(tasks.get(task.key).status,'active')
            tasks.complete(active.key,active.generation,now=stamp,prepared=prepared[0])
            self.assertEqual(catalog.get('assets',asset)['admission']['operation'],operation['id'])
            self.assertEqual(writer.load_operation(operation['id'])['phase'],'committed')
            self.assertEqual(tasks.get(task.key).status,'done')
            self.assertTrue(original.is_file())


    def test_legacy_terminal_routing_readback_finishes_same_attempt_without_effect(self):
        from library.source_tasks import SourcePolicy
        from library.stage_tasks import StageTasks
        from library.native_stages import NativeStages
        from library.catalog import Catalog
        from library.operational_state import CollectorState,NativeHistory
        from booksys_core.network_admission import NativeMutation,RollingBudget
        from datetime import datetime,timezone
        from types import SimpleNamespace
        from mirror_routing import package_signature
        from library.primitives import digest
        for phase in ('active','stopped'):
            with self.subTest(phase=phase),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);store=create_store(self,root,empty());catalog=Catalog(root,store=store)
                source='https://bookys.invalid/source';collector=CollectorState(root)
                entry={'status':'queued','package_id':1,'name':'Booksys-Owned'};collector.submission(source,entry)
                package={'pid':1,'name':'Booksys-Owned','dest':0,'folder':'original','links':[{'fid':2,'url':'https://host.invalid/original'}]}
                attempt={'pid':3,'name':'Attempt','dest':1,'folder':'attempt','links':[{'fid':4,'url':'https://host.invalid/fallback'}]}
                operation={'source':source,'pid':1,'name':'Booksys-Owned','phase':phase,'kind':'park' if phase=='stopped' else 'fallback',
                    'original_files':package_signature(package),'original_folder':'original','attempt_name':'Attempt',
                    'fallback_links':['https://host.invalid/fallback'],'attempt_pid':3,'attempt_folder':'attempt','attempt_files':package_signature(attempt)}
                NativeHistory(root).route('1',operation)
                policy=SourcePolicy.create(root/'task-policy.yaml',store=store);tasks=StageTasks(store,policy=policy)
                stamp=datetime(2026,11,10,tzinfo=timezone.utc);tasks.scan('routing',root/'monitor.yaml',now=stamp)
                task=tasks.select('routing',now=stamp).task
                self.assertIsNotNone(task)
                active=tasks.start(task.key,task.generation,now=stamp,resources=True)
                budget=RollingBudget.create(root/'native-admission.yaml',requests=30,period=60)
                mutation=NativeMutation(root,'routing:'+source,root/'mirror-routing.yaml');mutation.resolve('retained-exact-readback')
                calls=[]
                def call(method,*,write=False,**parameters):
                    self.assertFalse(write);calls.append(method)
                    return package if method=='get_package_data' else [attempt] if method=='get_queue_data' else []
                adapter=NativeStages(catalog,root/'reception',root/'library',root,browser=None,client=SimpleNamespace(admission=budget,call=call))
                receipt=adapter.receipt(active)
                outcome,proof=(receipt.outcome,receipt.proof) if hasattr(receipt,'outcome') else receipt
                self.assertEqual(outcome,'done')
                done=tasks.reconcile(active.key,active.generation,now=stamp,owner_stopped=True,outcome=outcome,proof=proof,source_progress=getattr(receipt,'source_progress',None))
                self.assertEqual((done.status,done.attempts,done.generation),('done',active.attempts,active.generation))
                self.assertNotIn('add_package',calls);self.assertNotIn('delete_packages',calls)


    def test_superseded_routing_readback_only_completes_original_operation(self):
        from booksys_core.network_admission import NativeMutation, RollingBudget
        from datetime import datetime, timezone, timedelta
        from library.catalog import Catalog
        from library.native_stages import NativeStages
        from library.operational_state import CollectorState, NativeHistory
        from library.primitives import LibraryError
        from library.source_tasks import SourcePolicy
        from library.stage_tasks import StageTasks
        from mirror_routing import package_signature
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = create_store(self, root, empty())
            catalog = Catalog(root, store=store)
            collector, history = CollectorState(root), NativeHistory(root)
            source = 'https://bookys.invalid/original'
            old_entry = {'status':'queued', 'package_id':1, 'name':'Booksys-Old'}
            new_entry = {'status':'queued', 'package_id':2, 'name':'Booksys-New'}
            collector.submission(source, old_entry)
            stamp = datetime(2026,11,10,tzinfo=timezone.utc)
            policy = SourcePolicy(root/'task-policy.yaml', store=store)
            tasks = StageTasks(store, policy=policy)
            tasks.scan('routing', root/'monitor.yaml', now=stamp)
            task = tasks.select('routing', now=stamp).task
            active = tasks.start(task.key, task.generation, now=stamp, resources=True)
            package = {'pid':1, 'name':old_entry['name'], 'dest':0, 'folder':'old',
                       'links':[{'fid':11,'url':'https://host.invalid/old'}]}
            operation = {'source':source, 'pid':1, 'name':old_entry['name'], 'kind':'park',
                         'phase':'parked', 'original_files':package_signature(package),
                         'task_attempt':[active.key, active.generation]}
            history.route('1', operation)
            replacement = {**operation, 'pid':2, 'name':new_entry['name'], 'phase':'stopped',
                           'task_attempt':['not-this-task', active.generation]}
            history.route('2', replacement)
            collector.submission(source, new_entry)
            tasks.scan('routing', root/'monitor.yaml', now=stamp+timedelta(seconds=1))
            superseded = tasks.get(active.key)
            self.assertTrue(superseded.superseded_by)
            with self.assertRaises(LibraryError):
                tasks.complete(active.key, active.generation, now=stamp, outcome='done', proof='stale')
            with self.assertRaises(LibraryError):
                tasks.reconcile(active.key, active.generation, now=stamp, owner_stopped=True,
                                outcome='done', proof='forged')
            budget = RollingBudget.create(root/'native-admission.yaml', requests=30, period=60)
            NativeMutation(root, 'routing:'+source, root/'mirror-routing.yaml').resolve('old-move-ack')
            calls = []
            def call(method, *, write=False, **parameters):
                self.assertFalse(write)
                calls.append((method, parameters))
                return package if method=='get_package_data' else []
            adapter = NativeStages(catalog, root/'downloads', root/'library', root, browser=None,
                                   client=SimpleNamespace(admission=budget, call=call))
            from dataclasses import replace
            self.assertEqual(adapter.receipt(replace(superseded, native_attempt=None)), ('uncertain', ''))
            history.route('1', {**operation, 'task_attempt':[active.key, active.generation+1]})
            self.assertEqual(adapter.receipt(superseded), ('uncertain', ''))
            self.assertEqual(calls, [])
            history.route('1', operation)
            receipt = adapter.receipt(superseded)
            self.assertEqual(receipt.outcome, 'done')
            retained = tasks.reconcile(active.key, active.generation, now=stamp, owner_stopped=True,
                outcome=receipt.outcome, proof=receipt.proof, source_progress=receipt.source_progress)
            self.assertEqual((retained.status,retained.generation,retained.attempts),
                             ('superseded',active.generation,active.attempts))
            self.assertEqual(history.routing()['packages']['1']['phase'], 'stopped')
            self.assertEqual(history.routing()['packages']['2'], replacement)
            self.assertEqual(collector.monitor()['submissions'][source], new_entry)
            self.assertIsNone(policy.progress(active.source, active.entity))
            self.assertEqual(calls, [('get_package_data', {'package_id':1}), ('status_downloads', {})])

    def test_superseded_retirement_readback_preserves_replacement(self):
        from booksys_core.network_admission import NativeMutation, RollingBudget
        from datetime import datetime, timezone, timedelta
        from library.native_stages import NativeStages
        from library.operational_state import CollectorState, NativeHistory
        from library.source_tasks import SourcePolicy
        from library.stage_tasks import StageTasks
        from test_library_intake import SOURCE
        from test_retirement import fixture
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, reception, collector, original, client = fixture(root, self)
            state, history = CollectorState(collector), NativeHistory(collector)
            stamp = datetime(2026,11,10,tzinfo=timezone.utc)
            policy = SourcePolicy(root/'state/task-policy.yaml', store=catalog.backend())
            tasks = StageTasks(catalog.backend(), policy=policy)
            tasks.scan('retirement', collector/'monitor.yaml', now=stamp)
            task = tasks.select('retirement', now=stamp).task
            active = tasks.start(task.key, task.generation, now=stamp, resources=True)
            client.admission = RollingBudget.create(collector/'native-admission.yaml', requests=30, period=60)
            adapter = NativeStages(catalog, reception, root/'library', collector, browser=None, client=client)
            replacement = {**client.packages[1], 'pid':2, 'name':'Booksys-Replacement'}
            next_entry = {**state.monitor()['submissions'][SOURCE],
                          'package_id':replacement['pid'], 'name':replacement['name']}
            scripts = root/'scripts'
            scripts.mkdir()
            with patch('library.runtime.configured_scripts', return_value=scripts):
                adapter.execute(active, now=stamp)
                self.assertEqual(history.retirements()['acquisitions'][SOURCE]['task_attempt'],
                                 [active.key, active.generation])
                client.packages[2] = replacement
                state.submission(SOURCE, next_entry)
                tasks.scan('retirement', collector/'monitor.yaml', now=stamp+timedelta(seconds=1))
                mutation = NativeMutation(collector, 'retirement:'+SOURCE, collector/'package-retirement.yaml')
                mutation.resolve('old-removal-ack')
                receipt = adapter.receipt(tasks.get(task.key))
            self.assertEqual(receipt.outcome, 'done')
            done = tasks.reconcile(task.key, active.generation, now=stamp, owner_stopped=True,
                outcome=receipt.outcome, proof=receipt.proof, source_progress=receipt.source_progress)
            self.assertEqual((done.status,done.generation,done.attempts),
                             ('superseded',active.generation,active.attempts))
            self.assertEqual(history.retirements()['acquisitions'][SOURCE]['phase'], 'retired')
            self.assertEqual(state.monitor()['submissions'][SOURCE], next_entry)
            self.assertEqual(client.packages, {2:replacement})
            self.assertEqual(client.removed, [1])
            self.assertIsNone(policy.progress(active.source, active.entity))
            self.assertTrue(original.is_file())

    def test_compose_bindings_and_requests_use_closed_postgres_capabilities(self):
        from library.operational_state import ComposeDiagnostics
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=create_store(self,root,empty());state=ComposeDiagnostics(store._database)
            state.publish({'schema':1,'observed_at':123,'sources':{'pyload':'exact-container-id','collection':'other-id'}})
            self.assertEqual(state.sources()['sources'],{'pyload':'exact-container-id','collection':'other-id'})
            request={'role':'pyload','position':None,'start':1,'end':2,'lines':10,'byte_limit':100,'generation':'exact-container-id'}
            state.retain('a'*32,request)
            self.assertEqual(state.request('a'*32),request)
            with self.assertRaises(Exception):state.request('b'*32)

    def test_intake_catalogue_result_waits_for_same_task_completion(self):
        from test_library_publish import setup_library
        from test_library_intake import SOURCE
        from library.operational_state import CollectorState,RuntimeObservation
        from library.native_stages import NativeStages
        from library.stage_tasks import StageTasks
        from library.stage_model import Obligation
        from library.primitives import digest
        from datetime import datetime,timezone
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);catalog,reception,original,asset=setup_library(root,self)
            collector=CollectorState(root/'collector',database=catalog.backend()._database)
            collector.capture(SOURCE,{'title':'Album'})
            collector.submission(SOURCE,{'status':'queued','package_id':1,'name':'tracked'})
            observation=RuntimeObservation(catalog.backend()._database)
            observation.publish([{'pid':1,'name':'tracked','folder':'package','links':[
                {'fid':2,'name':'album.cbz','status':0,'size':original.stat().st_size},
                {'fid':3,'name':'album.cbz','status':0,'size':original.stat().st_size}]}],at=123,remaining=None,overshoot=None)
            tasks=StageTasks(catalog.backend());stamp=datetime(2026,11,10,tzinfo=timezone.utc)
            task=tasks.discover(Obligation('intake','source-'+digest(SOURCE),'exact','v1'),now=stamp)
            active=tasks.start(task.key,task.generation,now=stamp,resources=True)
            before=catalog.get('assets',asset)
            original_origin=digest({'source':SOURCE,'pid':1,'fid':2})
            self.assertIsNone(catalog.get('origins',original_origin))
            native=NativeStages(catalog,reception,root/'library',root/'collector',browser=None,client=None,snapshots=root/'snapshots')
            from unittest.mock import patch
            from library.intake import receive
            from ops.replay_selected_intake import filtered_receiver
            with patch('library.intake.receive',filtered_receiver(receive,{(1,2)})):
                result=native.execute(active,now=stamp)
            self.assertEqual(len(observation.queue()[0][0]['links']),2)
            self.assertIsNotNone(result.prepared)
            self.assertEqual(catalog.get('assets',asset),before)
            self.assertEqual(tasks.get(task.key).status,'active')
            self.assertIsNone(catalog.get('origins',original_origin))
            from unittest.mock import patch
            from library.stage_tasks import StageTasks as Tasks
            from library.primitives import LibraryError
            finish=Tasks._finish
            def fail(tasks_self,db,*args,**kwargs):
                finish(tasks_self,db,*args,**kwargs)
                raise LibraryError('Injected before catalogue result')
            with patch.object(Tasks,'_finish',fail):
                with self.assertRaises(LibraryError):tasks.complete(active.key,active.generation,now=stamp,outcome=result.outcome,proof=result.proof,prepared=result.prepared)
            self.assertIsNone(catalog.get('origins',original_origin))
            self.assertEqual(tasks.get(task.key).status,'active')
            tasks.complete(active.key,active.generation,now=stamp,outcome=result.outcome,proof=result.proof,prepared=result.prepared)
            self.assertEqual(tasks.get(task.key).status,'done')
            self.assertIsNotNone(catalog.get('origins',original_origin))
            self.assertIsNone(catalog.get('origins',digest({'source':SOURCE,'pid':1,'fid':3})))


    def test_checkpoints_keep_order_duplicates_null_and_atomic_capture(self):
        from library.operational_state import CollectorState
        from library.postgres import DeadlineConnection
        from library.primitives import LibraryError
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=create_store(self,root,empty());state=CollectorState(root)
            collection={'scope':{'categories':['c','c'],'hosters':None},'visited':['p2','p1','p2'],'items':None,'seen_ids':[9,8,9],'item_index':0}
            source={'source':'source','hosters':[],'plan':{'title':'Exact','targets':{'second':{'hoster':'h2'},'first':{'hoster':'h1'}}},
                'downloads':{'first':{'hoster':'h1','urls':['u','u','v']},'second':{'hoster':'h2'}}}
            state.patch_monitor(collection_checkpoint=collection,source_checkpoint=source)
            value=state.monitor()
            self.assertEqual(value['collection_checkpoint'],collection)
            self.assertEqual(value['source_checkpoint'],source)
            self.assertEqual(list(value['source_checkpoint']['plan']['targets']),['second','first'])
            with store._database.connection() as db:
                from library.codec import decode
                header=decode(db.execute("SELECT payload FROM collection_checkpoints WHERE kind='source_checkpoint'").fetchone()[0])
                self.assertEqual(header['plan']['targets'],{})
                self.assertEqual(header['downloads'],{})
                self.assertEqual(db.execute('SELECT count(*) FROM checkpoint_links').fetchone()[0],3)
            execute=DeadlineConnection.execute
            def fail(connection,query,*args,**kwargs):
                if isinstance(query,str) and query.startswith('INSERT INTO checkpoint_links'):raise LibraryError('Injected checkpoint child failure')
                return execute(connection,query,*args,**kwargs)
            with patch.object(DeadlineConnection,'execute',fail):
                with self.assertRaises(LibraryError):state.capture('source',{'title':'New'},source_checkpoint=source,collection_checkpoint={**collection,'items':[]})
            self.assertEqual(state.monitor(),value)
            self.assertNotIn('source',state.captured()['items'])
            state.capture('source',{'title':'New'},source_checkpoint=None,collection_checkpoint={**collection,'items':[]})
            self.assertNotIn('source_checkpoint',state.monitor())
            self.assertEqual(state.monitor()['collection_checkpoint']['items'],[])
