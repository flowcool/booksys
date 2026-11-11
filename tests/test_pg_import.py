"""Frozen-source import and current-state recovery at the migration seam."""
import os
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import yaml
from uuid import uuid4
from contextlib import closing

from library.primitives import LibraryError
from library.postgres import Database


def source_fixture(root):
    """A source, not a reverse export from the candidate implementation."""
    (root/'files').mkdir(parents=True)
    (root/'collector').mkdir()
    with closing(sqlite3.connect(root/'catalogue.sqlite')) as db:
        db.execute('CREATE TABLE meta(singleton,authority_id,schema_version,global_revision,imported_source_digest,ledger,case_scope_revision,ready,source_semantic,required_runtime,created_at)')
        db.execute("INSERT INTO meta VALUES(1,'source-test',1,7,'original','[]',0,1,'old','python3.14','before')")
        for kind in ('works','editions','variants','assets','artifacts','origins','cases','operations'):
            db.execute(f'CREATE TABLE {kind}(id TEXT PRIMARY KEY,revision INTEGER,payload TEXT)')
        db.execute('INSERT INTO works VALUES(?,?,?)',('work-kept',3,'{"record":{"title":"Retained"}}'))
        db.commit()
    return root


class ImportTests(unittest.TestCase):
    def test_manifest_accepts_literal_legacy_provider_cache_identity(self):
        from library.pg_import import prepare_manifest
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=source_fixture(root/'source')
            cache=source/'files/library-provider-cache';cache.mkdir()
            # Recorded legacy JSON-string digest, not plain UTF8 URL hashing.
            path=cache/'4ace2b6a2860ba56b6a992b5d4a2f03dbaa136a749f1e3e2a725f62ba0fe4ffb.yaml'
            raw=b'url: https://fixture.invalid/metadata\nexpires: 1794311222.125\nbody: retained\nstatus: ok\n'
            path.write_bytes(raw)
            manifest=prepare_manifest(source,candidate='literal-cache',origins={'library':str(root/'target')})
            self.assertEqual(manifest['source']['families']['provider_cache']['target_counts'],{'provider_cache':1})
            self.assertEqual(path.read_bytes(),raw)
            path.rename(cache/('0'*64+'.yaml'))
            with self.assertRaises(LibraryError):
                prepare_manifest(source,candidate='wrong-cache',origins={'library':str(root/'target')})

    def target(self):
        import psycopg
        from psycopg import sql
        database=Database(Path(os.environ['BOOKSYS_TEST_PG_DSN_FILE']).read_text().strip(),'test_'+uuid4().hex)
        def cleanup():
            with psycopg.connect(database.dsn,autocommit=True) as db:
                db.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(database.schema)))
        self.addCleanup(cleanup)
        return database

    def test_manifest_binds_bytes_and_unknown_mutable_input_is_rejected(self):
        from library.pg_import import prepare_manifest, read_manifest
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            source=source_fixture(root/'source')
            manifest=prepare_manifest(source,candidate='candidate-test')
            self.assertEqual(manifest['source']['authority_id'],'source-test')
            self.assertEqual(manifest['source']['revision'],7)
            self.assertEqual(len(manifest['source']['families']),38)
            self.assertEqual(read_manifest(manifest)['source']['revision'],7)
            (source/'files'/'unclassified-progress.yaml').write_text('phase: creating\n')
            with self.assertRaises(LibraryError):
                prepare_manifest(source,candidate='candidate-test')
            with self.assertRaises(LibraryError):
                read_manifest(manifest)

    def test_interrupted_import_stays_unselected_and_resumes_same_bound_target(self):
        from library.pg_import import prepare_manifest, import_snapshot, compare, select
        from library.catalog import Catalog
        from library.store import Change
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            manifest=prepare_manifest(source_fixture(root/'source'),candidate='candidate-test')
            database=self.target()
            def interrupt(phase):
                if phase=='catalogue':raise InterruptedError('crash after durable catalogue phase')
            with self.assertRaises(InterruptedError):
                import_snapshot(manifest,root/'target',database=database,checkpoint=interrupt)
            with self.assertRaises((LibraryError,OSError)):
                Catalog(root/'target')
            other=self.target()
            with self.assertRaises(LibraryError):
                import_snapshot(manifest,root/'target',database=other)
            import_snapshot(manifest,root/'target',database=database)
            report=compare(manifest,root/'target',database=database)
            self.assertEqual(report['status'],'equal')
            secret=root/'target'/'test.dsn';secret.write_text(database.dsn);secret.chmod(0o600)
            config=root/'target'/'postgresql.json'
            config.write_text(json.dumps({'schema':database.schema,'dsn_file':str(secret)}));config.chmod(0o600)
            select(manifest,root/'target',database=database)
            catalog=Catalog(root/'target')
            self.assertEqual(catalog.get('works','work-kept'),{'title':'Retained'})
            catalog.backend().commit(Change(upserts={'works':{'new-work':{'title':'After import'}}}),expected_revision=7)
            with self.assertRaises(LibraryError):
                import_snapshot(manifest,root/'target',database=database)
            restarted=Catalog(root/'target')
            self.assertEqual(restarted.get('works','new-work'),{'title':'After import'})
            self.assertEqual(restarted.revision,8)

    def test_exact_task_policy_ordered_links_and_unfinished_effects_survive_import(self):
        from library.pg_import import prepare_manifest, import_snapshot, compare
        from library.operational_state import CollectorState, NativeHistory
        from library.stage_model import Obligation
        from library.stage_tasks import StageTasks
        from library.store import Store
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=source_fixture(root/'source')
            task={'key':Obligation('routing','source-test','old-input','routing-step-v1').key,
                'stage':'routing','entity':'source-test','input_revision':'old-input','method':'routing-step-v1',
                'first_eligible_at':'2026-11-01T00:00:00+00:00','due_at':'2026-11-02T00:00:00+00:00',
                'blocked':'source_changed_rescan_required','generation':4,'attempts':2,'prestart_failures':1,
                'status':'active','episode':3,'policy_sequence':11}
            def save(path,value):path.write_text(yaml.safe_dump(value,sort_keys=False))
            save(source/'files'/'task-policy.yaml',{'schema':1,'tasks':{task['key']:task},'sources':{}})
            capture={'title':'Kept','downloads':{'host':{'urls':['first','first','last'],'absent':None}}}
            save(source/'collector'/'state.yaml',{'version':1,'items':{'source':capture}})
            routing={'phase':'create_intent','pid':280,'original_files':[3,3,2],'fallback_links':['x','x']}
            save(source/'collector'/'mirror-routing.yaml',{'schema':1,'packages':{'280':routing}})
            link={'fid':3,'url':'a','name':'a.cbz','status':0}
            retirement={'phase':'deleting','source':'source','submission':{'package_id':280},
                'packages':{'280':{'pid':280,'name':'Booksys-kept','folder':'kept','links':[link,link]}},
                'origins':[],'routing':{},'files':[],'unrelated':[],'created_at':'before','diagnostic_operation':'kept'}
            from retirement_history import evidence_digest
            retirement['evidence_digest']=evidence_digest(retirement)
            save(source/'collector'/'package-retirement.yaml',{'schema':1,'acquisitions':{'source':retirement},'cursor':'source'})
            database=self.target();manifest=prepare_manifest(source,candidate='candidate-test')
            import_snapshot(manifest,root/'target',database=database)
            store=Store(root/'target',authority='source-test',database=database)
            # Catalogue remains inaccessible before selection. Operational observations
            # here exercise the public adapters without activating an effect runner.
            collector=CollectorState(root/'target',database=database)
            self.assertEqual(collector.captured()['items']['source'],capture)
            native=NativeHistory(root/'target',database=database)
            self.assertEqual(native.routing()['packages']['280'],routing)
            self.assertEqual(native.retirements()['acquisitions']['source'],retirement)
            self.assertEqual(compare(manifest,root/'target',database=database)['status'],'equal')

    def test_activity_keeps_exact_ids_sessions_times_gap_and_payload(self):
        from library.pg_import import prepare_manifest, import_snapshot, compare
        from library.activity_store import ActivityStore
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=source_fixture(root/'source')
            activity_selector=source/'files'/'activity'/'activity-authority.yaml'
            activity_selector.parent.mkdir();legacy=yaml.safe_dump({'schema':1,'authority_id':'activity-test'})
            activity_selector.write_text(legacy)
            payload={'session':'retained-session','observed_at':1794311222.125,'dropped_count':4,'extension':None}
            with closing(sqlite3.connect(source/'activity.sqlite')) as db:
                db.execute('CREATE TABLE meta(singleton,schema,authority_id,history_started_at,gap_before)')
                db.execute("INSERT INTO meta VALUES(1,1,'activity-test',1794300000.125,1794300111.875)")
                db.execute('CREATE TABLE producers(stage,producer_scope,session,observed_at,progress_at,state,operation_code,reason_code,payload)')
                db.execute('CREATE TABLE events(id,at,stage,session,code,payload)')
                db.execute('INSERT INTO producers VALUES(?,?,?,?,?,?,?,?,?)',('collection','collector','retained-session',1794311222.125,1794310000.875,'waiting','progress','unknown',json.dumps(payload)))
                db.execute('INSERT INTO events VALUES(?,?,?,?,?,?)',('d82bc854-c0c1-46ab-af48-eb91c1021cb5',1794311222.125,'collection','retained-session','progress',json.dumps(payload)))
                db.commit()
            database=self.target();manifest=prepare_manifest(source,candidate='candidate-test',origins={'collector':str(root/'collector-live')})
            import_snapshot(manifest,root/'target',database=database)
            activity=ActivityStore(root/'target',authority='activity-test',database=database)
            self.assertEqual(activity.producers(),{'collection:collector':payload})
            result=activity.events(after=None)
            self.assertEqual(result['gap_before'],1794300111.875)
            self.assertEqual(result['items'],[{**payload,'id':'d82bc854-c0c1-46ab-af48-eb91c1021cb5','at':1794311222.125,'stage':'collection'}])
            self.assertEqual(compare(manifest,root/'target',database=database)['status'],'equal')
            target=root/'target';secret=target/'test.dsn';secret.write_text(database.dsn);secret.chmod(0o600)
            config=target/'postgresql.json';config.write_text(json.dumps({'schema':database.schema,'dsn_file':str(secret)}));config.chmod(0o600)
            from library.pg_import import select
            collector=root/'collector-live'
            live_activity=target/'activity';live_activity.mkdir();(live_activity/'activity-authority.yaml').write_text(legacy)
            for bad in (root/'wrong-collector',source/'collector',root/'other'/'..'/'source'/'collector'):
                with self.assertRaises(LibraryError):select(manifest,target,database=database,collector_directory=bad)
                self.assertFalse((bad/'postgresql.json').exists())
                with database.connection() as db:self.assertEqual(db.execute('SELECT ready FROM meta').fetchone(),(0,))
            alias=root/'collector-alias';alias.symlink_to(collector,target_is_directory=True)
            with self.assertRaises(LibraryError):select(manifest,target,database=database,collector_directory=alias)
            def interrupted(phase):raise InterruptedError('before auxiliary selectors')
            with self.assertRaises(InterruptedError):
                select(manifest,target,database=database,collector_directory=collector,checkpoint=interrupted)
            self.assertEqual((live_activity/'activity-authority.yaml').read_text(),legacy)
            select(manifest,target,database=database,collector_directory=collector)
            import activity as public_activity
            from unittest.mock import patch
            with patch.dict(os.environ,{'BOOKSYS_ACTIVITY_DIR':str(target/'activity')}):
                self.assertEqual(public_activity.selected_store(collector).events(after=None),result)
            self.assertEqual(Database.configured(collector),database)
            self.assertEqual(yaml.safe_load((target/'activity'/'activity-authority.yaml').read_text())['authority_id'],'activity-test')

    def test_nonempty_applied_decision_chain_starts_without_reindexing_revision(self):
        from library.pg_import import prepare_manifest, import_snapshot, select
        from library.primitives import digest
        from library.catalog import Catalog
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=source_fixture(root/'source')
            payload={'schema':1,'sequence':1,'previous':None,'action':'override','target':'a'*64,
                     'values':{'title':'Retained decision'},'author':'operator','time':'2026-11-01T00:00:00+00:00'}
            record={**payload,'id':digest(payload)}
            decisions=source/'files'/'library-decisions';decisions.mkdir()
            (decisions/('000000001-'+record['id']+'.yaml')).write_text(yaml.safe_dump(record))
            with closing(sqlite3.connect(source/'catalogue.sqlite')) as db:
                db.execute('UPDATE meta SET ledger=?',(json.dumps([record['id']]),))
                db.execute('CREATE TABLE ledger_records(sequence INTEGER,id TEXT,target TEXT,action TEXT,request_id TEXT,payload TEXT)')
                db.execute('INSERT INTO ledger_records VALUES(?,?,?,?,?,?)',(1,record['id'],record['target'],'override',None,json.dumps(record)))
                db.commit()
            database=self.target();manifest=prepare_manifest(source,candidate='candidate-test')
            import_snapshot(manifest,root/'target',database=database)
            target=root/'target'
            secret=target/'test.dsn';secret.write_text(database.dsn);secret.chmod(0o600)
            config=target/'postgresql.json';config.write_text(json.dumps({'schema':database.schema,'dsn_file':str(secret)}));config.chmod(0o600)
            import shutil
            shutil.copytree(decisions,target/'library-decisions')
            select(manifest,target,database=database)
            catalog=Catalog(target);catalog.project_pending()
            self.assertEqual(catalog.ledger(),[record])
            self.assertEqual(catalog.revision,7)

    def test_pending_indexed_tail_and_immutable_suffix_project_once_after_selection(self):
        from library.pg_import import prepare_manifest, import_snapshot, select, compare
        from library.primitives import digest
        from library.catalog import Catalog
        import shutil
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=source_fixture(root/'source');asset='a'*64
            decisions=source/'files'/'library-decisions';decisions.mkdir()
            chain=[]
            for sequence in (1,2,3):
                payload={'schema':1,'sequence':sequence,'previous':chain[-1]['id'] if chain else None,
                    'action':'override','target':asset,'values':{'title':str(sequence)},
                    'author':'operator','time':'2026-11-01T00:00:00+00:00'}
                record={**payload,'id':digest(payload)};chain.append(record)
                (decisions/(f"{sequence:09d}-"+record['id']+'.yaml')).write_text(yaml.safe_dump(record))
            with closing(sqlite3.connect(source/'catalogue.sqlite')) as db:
                db.execute('INSERT INTO assets VALUES(?,?,?)',(asset,4,json.dumps({'record':{'sha256':asset,'size':1,'format':'cbz','origins':[],'overrides':{'title':'1'}}})))
                db.execute('UPDATE meta SET ledger=?',(json.dumps([chain[0]['id']]),))
                db.execute('CREATE TABLE ledger_records(sequence INTEGER,id TEXT,target TEXT,action TEXT,request_id TEXT,payload TEXT)')
                for record in chain[:2]:
                    db.execute('INSERT INTO ledger_records VALUES(?,?,?,?,?,?)',(record['sequence'],record['id'],asset,'override',None,json.dumps(record)))
                db.commit()
            database=self.target();manifest=prepare_manifest(source,candidate='candidate-test');target=root/'target'
            import_snapshot(manifest,target,database=database)
            self.assertEqual(compare(manifest,target,database=database)['status'],'equal')
            with database.connection() as db:
                self.assertEqual(db.execute('SELECT global_revision,ledger FROM meta').fetchone(),(7,json.dumps([chain[0]['id']])))
                self.assertEqual(db.execute('SELECT status FROM decision_reservations ORDER BY sequence').fetchall(),[('projected',),('indexed',),('reserved',)])
            secret=target/'test.dsn';secret.write_text(database.dsn);secret.chmod(0o600)
            config=target/'postgresql.json';config.write_text(json.dumps({'schema':database.schema,'dsn_file':str(secret)}));config.chmod(0o600)
            shutil.copytree(decisions,target/'library-decisions');before={p.name:p.read_bytes() for p in decisions.iterdir()}
            select(manifest,target,database=database)
            catalog=Catalog(target)
            from library.store import Change, _projection
            with self.assertRaises(LibraryError):
                catalog.backend().commit(Change(upserts={'assets':{asset:{'forged':True}}},
                    ledger=[r['id'] for r in chain[:2]]),expected_revision=7,
                    commit_key='decision-project:'+chain[1]['id'])
            self.assertIsNone(_projection.get())
            catalog.project_pending()
            self.assertIsNone(_projection.get())
            self.assertEqual(catalog.ledger(),chain)
            self.assertEqual(catalog.get('assets',asset)['overrides'],{'title':'3'})
            self.assertEqual(catalog.revision,10)
            catalog.project_pending();self.assertEqual(catalog.revision,10)
            self.assertEqual({p.name:p.read_bytes() for p in (target/'library-decisions').iterdir()},before)
            import subprocess,sys
            startup=subprocess.run([sys.executable,'-c',
                'from pathlib import Path; from library.catalog import Catalog; import sys; c=Catalog(Path(sys.argv[1])); c.project_pending(); print(c.revision)',str(target)],
                capture_output=True,text=True,check=True)
            self.assertEqual(startup.stdout.strip(),'10')

    def test_seeded_store_prepares_cover_outside_write_transaction(self):
        from contextlib import contextmanager
        from unittest.mock import patch
        from library.store import Store
        from library.primitives import empty
        with tempfile.TemporaryDirectory() as folder:
            database=self.target();state=empty();cover='original cover\nbytes'
            state['works']['seed']={'title':'Seed','cover':cover}
            connection,prepare=Database.connection,Store.prepare;active=[]
            @contextmanager
            def observed(database,**kwargs):
                with connection(database,**kwargs) as db:
                    active.append(kwargs.get('write',False))
                    try:yield db
                    finally:active.pop()
            def checked(store,record):
                self.assertFalse(any(active),'cover prepared inside SQL write')
                return prepare(store,record)
            with patch.object(Database,'connection',observed),patch.object(Store,'prepare',checked):
                store=Store.create(Path(folder),database=database,authority='seed',state=state)
            self.assertEqual(store.get('works','seed')['cover'],cover)

    def test_imported_identity_positions_allow_ordered_public_appends(self):
        from library.pg_import import prepare_manifest,import_snapshot,select
        from library.decoder_ownership import DecoderOwnership
        from booksys_core.network_admission import RollingBudget
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=source_fixture(root/'source');target=root/'target'
            children=source/'files'/'stage-children';children.mkdir()
            names=['booksys-reader-'+c*32 for c in ('a','b','c')]
            (children/'owner.yaml').write_text(yaml.safe_dump({'schema':1,'names':names[:2]}))
            (source/'files'/'provider-admission.yaml').write_text(yaml.safe_dump({'schema':1,
                'requests':3,'period':100,'spacing':0,'review_block':False,'last':20,'not_before':0,'starts':[10,20]}))
            database=self.target();manifest=prepare_manifest(source,candidate='fixture')
            import_snapshot(manifest,target,database=database)
            import_snapshot(manifest,target,database=database)
            secret=target/'test.dsn';secret.write_text(database.dsn);secret.chmod(0o600)
            config=target/'postgresql.json';config.write_text(json.dumps({'schema':database.schema,'dsn_file':str(secret)}));config.chmod(0o600)
            select(manifest,target,database=database)
            owner=DecoderOwnership(target/'stage-children'/'owner.yaml')
            self.assertEqual(owner.names(),names[:2]);owner.register(names[2]);self.assertEqual(owner.names(),names)
            budget=RollingBudget(target/'provider-admission.yaml');budget.debit(timestamp=30)
            self.assertEqual(budget.next_due(timestamp=30),110)
            with database.connection() as db:
                self.assertEqual([r[0] for r in db.execute('SELECT at FROM network_starts ORDER BY position')],[10,20,30])
                self.assertGreater(db.execute('SELECT min(position) FROM network_starts').fetchone()[0],2)

    def test_absent_historical_families_import_literal_sources_through_public_reads(self):
        from library.pg_import import prepare_manifest,import_snapshot,select,compare
        from library.operational_state import (ManualProofs,StatusObservations,RuntimeObservation,
            ProviderCache,ResolutionPreviews,OperationalState,ComposeDiagnostics)
        import hashlib
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=source_fixture(root/'source');target=root/'target'
            def save(name,value):
                path=source/'files'/name;path.parent.mkdir(exist_ok=True,parents=True);path.write_text(yaml.safe_dump(value))
            request={'operation':'inspect','source':'retained','extension':None};result={'phase':'done','proof':['kept','kept']}
            save('proof-requests/request.yaml',request);save('proof-requests/request.result.yaml',result)
            preview={'asset':'a'*64,'id':'preview-kept','fields':{'title':'Kept'},'absence':None}
            save('library-resolution/previews/preview-kept.yaml',preview)
            url='https://fixture.invalid/metadata';cached={'url':url,'data':{'title':'Kept'},'at':1794311222.125}
            save('provider-cache/4ace2b6a2860ba56b6a992b5d4a2f03dbaa136a749f1e3e2a725f62ba0fe4ffb.yaml',cached)
            save('stage-selection-metadata.yaml',{'schema':1,'after':['2026-11-01','task-kept'],'lookback_done':True})
            save('stage-discovery.yaml',{'after':'business-kept'});save('stage-intake-discovery.yaml',{'after':'intake-kept'})
            status={'state':'waiting','reason':'retained','extra':None}
            save('evidence/cycle.yaml',status);save('stage-actors/identity.yaml',{'observed_at':1794311222.125})
            packages=[{'pid':280,'name':'kept','links':[{'fid':3,'name':'a.cbz'},{'fid':3,'name':'a.cbz'}]}]
            save('snapshots/queue.yaml',packages)
            capacity={'observed_at':1794311222.125,'remaining_bytes':400,'overshoot_bytes':None}
            save('snapshots/external-capacity.yaml',capacity)
            compose={'schema':1,'observed_at':1794311222.125,'sources':{'collection':'exact-generation'}}
            save('compose-diagnostic-sources.yaml',compose)
            save('relocation.json',{'source':'retained'});save('decisions.lock',{})
            save('.retained-temporary',{'phase':'interrupted'})
            database=self.target();manifest=prepare_manifest(source,candidate='fixture',origins={'library':str(target)})
            import_snapshot(manifest,target,database=database)
            self.assertEqual(compare(manifest,target,database=database)['status'],'equal')
            secret=target/'test.dsn';secret.write_text(database.dsn);secret.chmod(0o600)
            config=target/'postgresql.json';config.write_text(json.dumps({'schema':database.schema,'dsn_file':str(secret)}));config.chmod(0o600)
            select(manifest,target,database=database)
            proofs=ManualProofs(database);self.assertEqual(proofs.request('request'),request);self.assertEqual(proofs.result('request'),result)
            self.assertEqual(ResolutionPreviews(database).get('preview-kept'),preview)
            self.assertEqual(ProviderCache(target,str(target/'provider-cache')).get(url),cached)
            state=OperationalState(database);self.assertEqual(state.selection('metadata'),(('2026-11-01','task-kept'),True))
            self.assertEqual(state.discovery(),'business-kept');self.assertEqual(state.discovery('intake'),'intake-kept')
            self.assertEqual(StatusObservations(database).cycle(),status)
            observations=RuntimeObservation(database)
            self.assertEqual(observations.queue(),(packages,1794311222.125))
            self.assertEqual(observations.capacity(),{'schema':1,**capacity})
            self.assertEqual(observations.heartbeats(),{'identity':1794311222.125})
            self.assertEqual(ComposeDiagnostics(database).sources(),compose)
            for family in ('configuration','process_locks','retained_temporary_files'):
                self.assertEqual(manifest['source']['families'][family]['presence'],'present')

    def test_unclassified_legacy_library_retirement_refuses_before_target_initialization(self):
        from library.pg_import import prepare_manifest
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=source_fixture(root/'source')
            (source/'files'/'library-retirement.yaml').write_text('phase: deleting\n')
            with self.assertRaises(LibraryError):prepare_manifest(source,candidate='fixture')
            self.assertFalse((root/'target').exists())

    def test_bound_legacy_selector_is_replaced_and_interrupted_selection_resumes(self):
        from library.pg_import import prepare_manifest, import_snapshot, select
        from library.catalog import Catalog
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=source_fixture(root/'source')
            legacy=yaml.safe_dump({'backend':'sqlite','authority_id':'source-test','retained':'original'})
            (source/'files'/'storage-authority.yaml').write_text(legacy)
            with closing(sqlite3.connect(source/'catalogue.sqlite')) as db:
                db.execute('ALTER TABLE works ADD COLUMN next_attempt REAL')
                db.execute('UPDATE works SET next_attempt=?,payload=?',(1794311222.125,json.dumps({'record':{'title':'Retained','automation':{'next_attempt':1794311222.125}}})))
                db.commit()
            target=root/'target';target.mkdir()
            selector=target/'storage-authority.yaml';selector.write_text(legacy);selector.chmod(0o400)
            database=self.target();manifest=prepare_manifest(source,candidate='candidate-test')
            import_snapshot(manifest,target,database=database)
            secret=target/'test.dsn';secret.write_text(database.dsn+' application_name=runtime-selector');secret.chmod(0o600)
            config=target/'postgresql.json';config.write_text(json.dumps({'schema':database.schema,'dsn_file':str(secret)}));config.chmod(0o600)
            def interrupt(phase):raise InterruptedError('selection interrupted after database readiness')
            with self.assertRaises(InterruptedError):select(manifest,target,database=database,checkpoint=interrupt)
            self.assertEqual(selector.read_text(),legacy)
            select(manifest,target,database=database)
            catalog=Catalog(target)
            page=catalog.backend().page('works',filters={'next_attempt':1794311222.125})
            self.assertEqual([item['id'] for item in page['items']],['work-kept'])
            self.assertEqual(page['items'][0]['record']['automation']['next_attempt'],1794311222.125)
            self.assertEqual((source/'files'/'storage-authority.yaml').read_text(),legacy)

    def test_decision_index_columns_cannot_disagree_with_immutable_payload(self):
        from library.pg_import import prepare_manifest
        from library.primitives import digest
        for column in ('id','target','action','request_id'):
            with self.subTest(column=column), tempfile.TemporaryDirectory() as folder:
                source=source_fixture(Path(folder)/'source')
                payload={'schema':1,'sequence':1,'previous':None,'action':'override','target':'a'*64,'values':{}}
                record={**payload,'id':digest(payload)}
                decisions=source/'files'/'library-decisions';decisions.mkdir()
                (decisions/('000000001-'+record['id']+'.yaml')).write_text(yaml.safe_dump(record))
                with closing(sqlite3.connect(source/'catalogue.sqlite')) as db:
                    db.execute('UPDATE meta SET ledger=?',(json.dumps([record['id']]),))
                    db.execute('CREATE TABLE ledger_records(sequence INTEGER,id TEXT,target TEXT,action TEXT,request_id TEXT,payload TEXT)')
                    db.execute('INSERT INTO ledger_records VALUES(?,?,?,?,?,?)',(1,record['id'],record['target'],'override',None,json.dumps(record)))
                    db.execute('UPDATE ledger_records SET '+column+'=?',('different',));db.commit()
                with self.assertRaises(LibraryError):prepare_manifest(source,candidate='candidate-test')

    def test_resume_refuses_a_changed_empty_phase_before_any_new_phase(self):
        from library.pg_import import prepare_manifest, import_snapshot
        from library.operational_state import NativeHistory
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=source_fixture(root/'source');database=self.target()
            manifest=prepare_manifest(source,candidate='candidate-test')
            def interrupt(phase):
                if phase=='routing_packages':raise InterruptedError('after empty routing phase')
            with self.assertRaises(InterruptedError):import_snapshot(manifest,root/'target',database=database,checkpoint=interrupt)
            history=NativeHistory(root/'target',database=database)
            history.route('unexpected',{'phase':'prepared','source':'kept-for-inspection'})
            new_phases=[]
            with self.assertRaises(LibraryError):import_snapshot(manifest,root/'target',database=database,checkpoint=new_phases.append)
            self.assertEqual(new_phases,[])
            self.assertEqual(history.routing()['packages'],{'unexpected':{'phase':'prepared','source':'kept-for-inspection'}})

    def test_reader_cleanup_import_keeps_original_proof_and_exact_result(self):
        from library.pg_import import prepare_manifest, import_snapshot
        from library.operational_state import ReaderJobs
        from library.reader_jobs import JobLauncher
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=source_fixture(root/'source');job='a'*32
            request=source/'files'/'reader-requests'/job;request.mkdir(parents=True)
            manifest={'operation':'validate','source':'kept'}
            (request/'manifest.json').write_text(json.dumps(manifest));(request/'grant.json').write_text(json.dumps({'key':'kept:4'}))
            phases=source/'files'/'reader-jobs'/job;phases.mkdir(parents=True)
            origin=root/'jobs';actual=origin/job;actual.mkdir(parents=True)
            intent={'schema':1,'name':'booksys-reader-'+job,'image':'sha256:'+'0'*64,'manifest_sha256':'1'*64}
            values={'intent':intent,'container':{**intent,'container':'2'*64},'cleanup':{'container':'2'*64,'removed':False}}
            for phase,value in values.items():
                raw=yaml.safe_dump(value);(phases/(phase+'.yaml')).write_text(raw);(actual/(phase+'.yaml')).write_text(raw)
            database=self.target();bound=prepare_manifest(source,candidate='candidate-test',origins={'reader_jobs':str(origin)})
            import_snapshot(bound,root/'target',database=database)
            inputs=root/'inputs';inputs.mkdir()
            launcher=JobLauncher(inputs,origin,'sha256:'+'0'*64,remove_stopped=False)
            launcher.retained=ReaderJobs(database)
            self.assertEqual(launcher.retained_read(actual,'cleanup'),{'container':'2'*64,'removed':False})
            # This records an already observed result. It executes no Docker command.
            launcher.retained_write(actual,'cleanup',{**values['container'],'removed':False})
            self.assertEqual(launcher.retained_read(actual,'cleanup'),{**values['container'],'removed':False})
            self.assertEqual(yaml.safe_load((actual/'cleanup.yaml').read_text()),values['cleanup'])


if __name__=='__main__':
    unittest.main()
