"""Public operational-state contracts on the qualified isolated PostgreSQL."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch, Mock
from uuid import uuid4

import psycopg

from library.primitives import empty


@unittest.skipUnless(os.environ.get('BOOKSYS_TEST_PG_DSN_FILE'), 'isolated PostgreSQL required')
class PostgreSQLStateTests(unittest.TestCase):
    def setUp(self) -> None:
        from library.postgres import Database
        from library.store import Store
        self.temp = tempfile.TemporaryDirectory(prefix='booksys-pg-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dsn = Path(os.environ['BOOKSYS_TEST_PG_DSN_FILE']).read_text().strip()
        self.schema = 'test_' + uuid4().hex
        self.database = Database(self.dsn, self.schema)
        self.store = Store.create(self.root, database=self.database, authority='test', state=empty())
        from import_fixture import private_selector
        private_selector(self.root,self.database)
        self.addCleanup(self.drop_schema)
        self.now = datetime(2026, 11, 10, tzinfo=timezone.utc)
        from library.catalog import Catalog
        self.catalog = Catalog(self.root, store=self.store)

    def drop_schema(self) -> None:
        from psycopg import sql
        with psycopg.connect(self.dsn, autocommit=True) as db:
            db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))

    def test_startup_audit_validates_slow_attachments_outside_database_budget(self) -> None:
        from library.store import Change
        from library.locking import bounded_acquisition
        from library.primitives import LibraryError
        import time
        self.store.commit(Change(upserts={'works': {'w': {'title': 'W', 'cover': 'retained cover'}}}),
                          expected_revision=0)
        original = Path.read_bytes
        reads = []
        def slow_attachment(path):
            if path.parent == self.store.payloads:
                reads.append(path)
                time.sleep(.6)
            return original(path)
        with patch.object(Path, 'read_bytes', slow_attachment):
            with bounded_acquisition(time.monotonic() + .5):
                self.store.audit()
        self.assertEqual(len(reads), 1)
        reads[0].write_bytes(b'corrupt attachment')
        with self.assertRaisesRegex(LibraryError, 'Attachment digest mismatch'):
            self.store.audit()

    def test_startup_audit_keeps_one_snapshot_during_local_validation(self) -> None:
        from library.store import Change
        from library.primitives import LibraryError
        self.store.commit(Change(upserts={'works': {'w': {'title': 'W', 'cover': 'retained'}},
            'editions': {'e': {'work': 'w', 'language': 'fr', 'publisher': '', 'evidence': []}}}),
            expected_revision=0)
        original = Path.read_bytes
        changed = []
        def change_after_capture(path):
            if path.parent == self.store.payloads and not changed:
                with self.database.connection(write=True) as writer:
                    writer.execute("DELETE FROM edition_work WHERE source='e'")
                changed.append(True)
            return original(path)
        with patch.object(Path, 'read_bytes', change_after_capture):
            self.store.audit()
        self.assertEqual(changed, [True])
        with self.assertRaisesRegex(LibraryError, 'relation index differs'):
            self.store.audit()

    def test_startup_audit_checks_large_relations_and_rejects_corruption(self) -> None:
        from library.codec import encode
        from library.primitives import LibraryError
        count = 12000
        parent_hash = f'{0:064x}'
        origins = []
        assets = []
        for index in range(count):
            aid = f'{index:064x}'
            origin = {'source': 'b'*64, 'pid': 1, 'fid': index,
                      'path': f'album-{index}.cbz', 'sha256': aid}
            if index:
                origin.update(parent_origin='origin-0', parent_sha256=parent_hash,
                              member_sha256=aid, member=f'{index}.cbz',
                              path=f'bundle-inputs/{parent_hash}/{index}.cbz')
            origins.append((f'origin-{index}', encode({'record': origin})))
            assets.append((aid, encode({'record': {'sha256': aid, 'size': 1,
                'format': 'cbz', 'origins': [f'origin-{index}']}})))
        with self.database.connection(write=True) as db:
            with db.cursor() as cursor:
                cursor.executemany('INSERT INTO origins(id,revision,payload) VALUES (%s,0,%s)', origins)
                cursor.executemany('INSERT INTO assets(id,revision,payload) VALUES (%s,0,%s)', assets)
                cursor.executemany('INSERT INTO asset_origin VALUES (%s,%s)',
                                   [(f'{i:064x}', f'origin-{i}') for i in range(count)])
                cursor.executemany('INSERT INTO origin_parent VALUES (%s,%s)',
                                   [(f'origin-{i}', 'origin-0') for i in range(1, count)])
        # The snapshot keeps its existing ten-second database deadline.
        self.store.audit()
        with self.database.connection(write=True) as db:
            db.execute('DELETE FROM asset_origin WHERE source=%s', (parent_hash,))
        with self.assertRaisesRegex(LibraryError, 'relation index differs'):
            self.store.audit()
        with self.database.connection(write=True) as db:
            db.execute('INSERT INTO asset_origin VALUES (%s,%s)', (parent_hash, 'origin-0'))
            invalid = self.store.restore(origins[1][1])
            invalid['parent_sha256'] = 'f'*64
            db.execute('UPDATE origins SET payload=%s WHERE id=%s',
                       (encode({'record': invalid}), 'origin-1'))
        with self.assertRaisesRegex(LibraryError, 'Invalid bundle child provenance'):
            self.store.audit()
        with self.database.connection(write=True) as db:
            db.execute('UPDATE origins SET payload=%s WHERE id=%s', (origins[1][1], 'origin-1'))
            invalid = self.store.restore(assets[0][1])
            invalid['identity'] = {'work': 'missing-work', 'edition': 'missing-edition',
                                   'variant': 'missing-variant'}
            db.execute('UPDATE assets SET payload=%s WHERE id=%s',
                       (encode({'record': invalid}), parent_hash))
        with self.assertRaisesRegex(LibraryError, 'identity references missing or conflicting'):
            self.store.audit()

    def test_result_is_durable_and_visible_through_public_queries(self) -> None:
        from library.store import Change
        self.store.commit(Change(upserts={'works': {'work-a': {'title': 'A'}}}), expected_revision=0,
                          commit_key='create-work-a')
        self.assertEqual(self.store.get('works', 'work-a'), {'title': 'A'})
        self.assertEqual(self.store.page('works')['items'], [{'id': 'work-a', 'record': {'title': 'A'}}])

    def test_unrelated_prepared_results_both_commit(self) -> None:
        from library.store import Change
        self.store.commit(Change(upserts={'works': {'a': {'title': 'A'}, 'b': {'title': 'B'}}}), expected_revision=0)
        a = self.catalog.result_set('identify', {'works': ['a']})
        b = self.catalog.result_set('identify', {'works': ['b']})
        a['works']['a']['title'] = 'A2'
        b['works']['b']['title'] = 'B2'
        self.catalog.save(a, expected=a['revision'])
        self.catalog.save(b, expected=b['revision'])
        self.assertEqual(self.store.get('works', 'b'), {'title': 'B2'})

    def test_guard_order_does_not_bind_later_absent_key_while_earlier_key_waits(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        from library.postgres import guards
        from threading import Event
        import time
        waiting=Event();worker_pid=[]
        def ordered_writer() -> None:
            with self.database.connection(write=True) as db:
                worker_pid.append(db.info.backend_pid);waiting.set()
                guards(db,{'a-existing','z-absent'})
        with self.database.connection(write=True) as seed:
            guards(seed,{'a-existing'})
        with ThreadPoolExecutor(max_workers=1) as executor:
            with self.database.connection(write=True) as first:
                guards(first,{'a-existing'})
                pending=executor.submit(ordered_writer)
                self.assertTrue(waiting.wait(3))
                deadline=time.monotonic()+3
                while True:
                    with self.database.connection() as observer:
                        blocked=observer.execute('SELECT cardinality(pg_blocking_pids(%s))',(worker_pid[0],)).fetchone()[0]
                    if blocked:break
                    self.assertLess(time.monotonic(),deadline)
                    time.sleep(.01)
                with self.database.connection(write=True,seconds=1) as later:
                    guards(later,{'z-absent'})
                self.assertFalse(pending.done())
            pending.result(timeout=3)

    def test_new_relation_member_invalidates_prepared_parent(self) -> None:
        from library.store import Change
        from library.primitives import LibraryError
        self.store.commit(Change(upserts={'works': {'a': {'title': 'A'}}}), expected_revision=0)
        prepared = self.catalog.result_set('identify', {'works': ['a']})
        self.store.commit(Change(upserts={'editions': {'e': {
            'work': 'a', 'language': '', 'publisher': '', 'evidence': []}}}), expected_revision=1)
        prepared['works']['a']['title'] = 'A2'
        with self.assertRaisesRegex(LibraryError, 'Result conflict'):
            self.catalog.save(prepared, expected=prepared['revision'])

    def test_duplicate_key_retains_receipt_and_rejects_changed_input(self) -> None:
        from library.store import Change
        from library.primitives import LibraryError
        change = Change(upserts={'works': {'a': {'title': 'A'}}})
        first = self.store.commit(change, expected_revision=0, commit_key='same')
        self.assertEqual(self.store.commit(change, expected_revision=0, commit_key='same'), first)
        with self.assertRaisesRegex(LibraryError, 'input mismatch'):
            self.store.commit(Change(upserts={'works': {'a': {'title': 'B'}}}),
                              expected_revision=0, commit_key='same')

    def test_atomic_source_task_result_and_successor(self) -> None:
        from library.stage_tasks import StageTasks, Obligation
        from library.store import Change, PreparedResult
        tasks = StageTasks(self.store)
        from library.source_tasks import SourcePolicy
        policy = SourcePolicy(self.root/'source-policy.yaml', store=self.store)
        source, _ = policy.register('collection', self.root/'source.yaml')
        policy.accept_snapshot(source, {'_source_sha256': 'test', 'items': {'item': {
            'input_revision': 'r1', 'method': 'm1'}}}, self.now.isoformat())
        task = tasks.discover(Obligation('collection', source+':item', 'r1', 'm1', source=source), now=self.now)
        active = tasks.start(task.key, task.generation, now=self.now, resources=True)
        result = PreparedResult(Change(upserts={'works': {'a': {'title': 'A'}}}), None, 0)
        successor = Obligation('metadata', 'asset-a', 'r1', 'm1')
        progress = policy.prepare_progress(active, {'status': 'submitted', 'receipt': 'exact-pid'})
        done = tasks.complete(active.key, active.generation, now=self.now, prepared=result, successors=[successor], source_progress=progress)
        self.assertEqual(done.status, 'done')
        self.assertEqual(self.store.get('works', 'a'), {'title': 'A'})
        self.assertEqual(tasks.get(successor.key).status, 'pending')
        self.assertEqual(policy.progress(source, task.entity), {'status': 'submitted', 'receipt': 'exact-pid'})
        again = tasks.complete(active.key, active.generation, now=self.now, prepared=result, successors=[successor], source_progress=progress)
        self.assertEqual(again.attempts, 1)

    def test_failed_successor_rolls_back_result_and_completion(self) -> None:
        from library.stage_tasks import StageTasks, Obligation
        from library.store import Change, PreparedResult
        tasks = StageTasks(self.store)
        task = tasks.discover(Obligation('metadata', 'asset-a', 'r1', 'm1'), now=self.now)
        active = tasks.start(task.key, task.generation, now=self.now, resources=True)
        with patch.object(StageTasks, '_discover', side_effect=RuntimeError('interrupted successor')):
            with self.assertRaisesRegex(RuntimeError, 'interrupted successor'):
                tasks.complete(active.key, active.generation, now=self.now,
                    prepared=PreparedResult(Change(upserts={'works': {'a': {'title': 'A'}}}), None, 0),
                    successors=[Obligation('copies', 'asset-a', 'r1', 'm1')])
        self.assertIsNone(self.store.get('works', 'a'))
        self.assertEqual(tasks.get(active.key).status, 'active')

    def test_independent_public_commit_finishes_while_first_transaction_is_open(self) -> None:
        from library.store import Store, Change
        entered, release = Event(), Event()
        original = Store._refresh_rows
        def hold(store, db, affected):
            original(store, db, affected)
            if affected['works'] == {'a'}:
                entered.set()
                self.assertTrue(release.wait(5))
        with ThreadPoolExecutor(max_workers=2) as pool, patch.object(Store, '_refresh_rows', hold):
            first = pool.submit(self.store.commit, Change(upserts={'works': {'a': {'title': 'A'}}}), expected_revision=0)
            self.assertTrue(entered.wait(5))
            try:
                second = pool.submit(self.store.commit, Change(upserts={'works': {'b': {'title': 'B'}}}), expected_revision=0)
                second.result(timeout=3)
            finally:
                release.set()
            first.result(timeout=3)
        self.assertEqual(self.store.get('works', 'b'), {'title': 'B'})

    def test_absent_outcome_fences_delayed_original(self) -> None:
        from library.store import Change
        from library.primitives import LibraryError
        change = Change(upserts={'works': {'a': {'title': 'A'}}})
        identity = self.store.transition_digest(change, expected_revision=0)
        self.assertEqual(self.store.outcome('delayed', input_digest=identity), {'state': 'aborted'})
        with self.assertRaisesRegex(LibraryError, 'fenced aborted'):
            self.store.commit(change, expected_revision=0, commit_key='delayed')
        self.assertIsNone(self.store.get('works', 'a'))

    def test_lost_complete_ack_resolves_same_key_without_second_transition(self) -> None:
        from library.store import Store
        from library.stage_tasks import StageTasks, Obligation
        from library.postgres import CommitUnknown
        tasks = StageTasks(self.store)
        task = tasks.discover(Obligation('metadata', 'asset-a', 'r1', 'm1'), now=self.now)
        active = tasks.start(task.key, task.generation, now=self.now, resources=True)
        commit = Store.commit
        def lost_ack(store, *args, **kwargs):
            commit(store, *args, **kwargs)
            raise CommitUnknown('commit_unresolved')
        with patch.object(Store, 'commit', lost_ack):
            finished = tasks.complete(active.key, active.generation, now=self.now)
        self.assertEqual(finished.status, 'done')
        self.assertEqual(finished.attempts, 1)

    def test_task_only_complete_preserves_catalogue_cursor(self) -> None:
        from library.store import Change
        from library.stage_tasks import StageTasks, Obligation
        self.store.commit(Change(upserts={'works': {'a': {'title': 'A'}, 'b': {'title': 'B'}}}), expected_revision=0)
        first = self.store.page('works', limit=1)
        tasks = StageTasks(self.store)
        task = tasks.discover(Obligation('metadata', 'asset-a', 'r1', 'm1'), now=self.now)
        active = tasks.start(task.key, task.generation, now=self.now, resources=True)
        tasks.complete(active.key, active.generation, now=self.now)
        second = self.store.page('works', limit=1, cursor=first['cursor'])
        self.assertEqual(second['items'][0]['id'], 'b')
        self.assertEqual(second['revision'], first['revision'])

    def test_operator_deadline_bounds_wait_for_relevant_guard(self) -> None:
        import time
        from library.store import Change
        from library.postgres import guards
        from library.locking import bounded_acquisition
        from library.primitives import LibraryError
        with self.database.connection(write=True) as db:
            guards(db, {'entity:works:a'})
            started = time.monotonic()
            with bounded_acquisition(started + .5):
                with self.assertRaisesRegex(LibraryError, 'storage_busy'):
                    self.store.commit(Change(upserts={'works': {'a': {'title': 'A'}}}), expected_revision=0)
            self.assertLess(time.monotonic() - started, .8)
        self.assertIsNone(self.store.get('works', 'a'))

    def test_process_owner_survives_database_disconnect(self) -> None:
        from library.stage_execution import QueueOwner
        from library.primitives import LibraryError
        with QueueOwner(self.root, 'metadata'):
            with self.database.connection() as db:
                db.close()
            with self.assertRaisesRegex(LibraryError, 'owns exclusion'):
                with QueueOwner(self.root, 'metadata'):
                    self.fail('duplicate acting owner')

    def asset_fixture(self) -> str:
        import hashlib
        from library.store import Change
        self.reception = self.root/'reception'
        self.reception.mkdir()
        raw = b'isolated printed-reader boundary fixture'
        (self.reception/'album.cbz').write_bytes(raw)
        aid = hashlib.sha256(raw).hexdigest()
        self.store.commit(Change(upserts={
            'assets': {aid: {'sha256':aid,'size':len(raw),'format':'cbz','origins':['origin-a'],
                             'status':'validated','embedded':{}}},
            'origins': {'origin-a': {'source':'b'*64,'pid':1,'fid':1,'path':'album.cbz',
                                    'sha256':aid,'availability':'validated'}}}),expected_revision=0)
        return aid

    def test_real_metadata_actor_commits_result_and_actual_successors_after_lost_ack(self) -> None:
        from library.business_stages import BusinessStages
        from library.metadata import Providers
        from library.stage_actor import StageActor
        from library.stage_tasks import StageTasks
        from library.stage_resources import Demand
        from library.store import Store
        from library.postgres import CommitUnknown
        aid = self.asset_fixture()
        tasks = StageTasks(self.store)
        fetch = Mock(side_effect=AssertionError('unexpected external request'))
        stages = BusinessStages(self.catalog,self.reception,self.root/'library',
                                Providers(self.root/'cache',fetch=fetch,interval=0),isolated=True)
        stages.discover(tasks,now=self.now)
        task = tasks.select('metadata',now=self.now).task
        executed = []
        def execute(task, stamp):
            executed.append(task.key)
            return stages.execute(task,now=stamp)
        actor = StageActor(tasks,Mock(),'metadata',execute,demand=lambda task:Demand(memory_mib=256),
                           cleanup=lambda task:True,scopes=stages.scopes)
        commit = Store.commit
        def lost_ack(store,*args,**kwargs):
            result = commit(store,*args,**kwargs)
            if kwargs.get('commit_key','').startswith('task:'):
                raise CommitUnknown('commit_unresolved')
            return result
        with actor.owner, patch.object(actor,'report'), patch.object(Store,'commit',lost_ack):
            self.assertEqual(actor._run(task,'fixture-grant',now=self.now),'done')
        self.assertEqual(executed,[task.key])
        self.assertEqual(tasks.get(task.key).status,'done')
        self.assertIn('stage_metadata',self.catalog.get('assets',aid))
        self.assertIsNotNone(tasks.select('ocr',now=self.now).task)
        self.assertIsNotNone(tasks.select('copies',now=self.now).task)
        self.assertFalse(fetch.called)

    def test_real_ocr_prepares_detached_result_and_commits_metadata_successor(self) -> None:
        from library.business_stages import BusinessStages
        from library.metadata import Providers
        from library.stage_tasks import StageTasks, Obligation
        from library.stage_model import METHODS
        aid = self.asset_fixture()
        stages = BusinessStages(self.catalog,self.reception,self.root/'library',
                                Providers(self.root/'cache',fetch=Mock(),interval=0),isolated=True)
        tasks = StageTasks(self.store)
        task = tasks.discover(Obligation('ocr',aid,'r1',METHODS['ocr']),now=self.now)
        active = tasks.start(task.key,task.generation,now=self.now,resources=True)
        with patch('library.printed.read_printed',return_value={
                'status':'ok','asset':aid,'pages':[],'tool':'fixture'}) as reader:
            result = stages.execute(active,now=self.now)
        self.assertNotIn('printed',self.catalog.get('assets',aid))
        tasks.complete(active.key,active.generation,now=self.now,outcome=result.outcome,
                       prepared=result.prepared,successors=result.successors)
        self.assertEqual(reader.call_count,1)
        self.assertEqual(self.catalog.get('assets',aid)['printed']['status'],'ok')
        self.assertIsNotNone(tasks.select('metadata',now=self.now).task)

    def test_start_duplicate_does_not_charge_a_second_attempt(self) -> None:
        from library.stage_tasks import StageTasks, Obligation
        tasks = StageTasks(self.store)
        task = tasks.discover(Obligation('metadata','asset-a','r1','m1'),now=self.now)
        first = tasks.start(task.key,task.generation,now=self.now,resources=True)
        second = tasks.start(task.key,task.generation,now=self.now,resources=True)
        self.assertEqual(first,second)
        self.assertEqual(tasks.get(task.key).attempts,1)

    def test_source_method_change_rejects_start_without_consuming_allowance(self) -> None:
        from library.source_tasks import SourcePolicy
        from library.stage_tasks import StageTasks, Obligation
        from library.primitives import LibraryError
        policy = SourcePolicy(self.root/'source-policy.yaml',store=self.store)
        source,_ = policy.register('collection',self.root/'source.yaml')
        view = {'_source_sha256':'test','items':{'item':{'input_revision':'r1','method':'m1'}}}
        policy.accept_snapshot(source,view,self.now.isoformat())
        tasks = StageTasks(self.store)
        task = tasks.discover(Obligation('collection',source+':item','r1','m1',source=source),now=self.now)
        view['items']['item']['method'] = 'm2'
        policy.accept_snapshot(source,view,self.now.isoformat())
        with self.assertRaisesRegex(LibraryError,'source_input_missing_or_changed'):
            tasks.start(task.key,task.generation,now=self.now,resources=True)
        self.assertEqual(tasks.get(task.key).attempts,0)

    def test_new_claim_owner_invalidates_prepared_result(self) -> None:
        from library.store import Change
        from library.primitives import LibraryError
        self.store.commit(Change(upserts={'works':{'w':{'title':'W'}}}),expected_revision=0)
        preview = self.catalog.result_set('identify',{'works':['w']},claims={'isbn':['9780000000002']})
        self.store.commit(Change(upserts={'editions':{'e':{'work':'w','language':'fr','publisher':'P',
            'evidence':{},'isbn':'9780000000002'}}}),expected_revision=1)
        preview['works']['w']['title'] = 'Changed'
        with self.assertRaisesRegex(LibraryError,'Result conflict'):
            self.catalog.save(preview,expected=preview['revision'])

    def test_same_entity_overlap_rejects_stale_result_after_first_commit(self) -> None:
        from library.store import Store,Change
        from library.primitives import LibraryError
        self.store.commit(Change(upserts={'works':{'w':{'title':'W'}}}),expected_revision=0)
        a = self.catalog.result_set('identify',{'works':['w']})
        b = self.catalog.result_set('identify',{'works':['w']})
        a['works']['w']['title'] = 'First'
        b['works']['w']['title'] = 'Second'
        entered,release = Event(),Event()
        original = Store._refresh_rows
        def hold(store,db,affected):
            original(store,db,affected)
            if affected['works'] == {'w'}:
                entered.set()
                self.assertTrue(release.wait(5))
        with ThreadPoolExecutor(max_workers=2) as pool,patch.object(Store,'_refresh_rows',hold):
            first = pool.submit(self.catalog.save,a,expected=a['revision'])
            self.assertTrue(entered.wait(5))
            second = pool.submit(self.catalog.save,b,expected=b['revision'])
            release.set()
            first.result(timeout=3)
            with self.assertRaisesRegex(LibraryError,'Result conflict|Another library operation'):
                second.result(timeout=3)
        self.assertEqual(self.store.get('works','w')['title'],'First')

    def test_interrupted_decision_append_and_projection_resume_without_duplicate(self) -> None:
        from library.store import Store
        from library.primitives import lock
        aid = self.asset_fixture()
        payload = {'action':'exclude','target':aid,'values':{},'author':'fixture','at':self.now.isoformat()}
        with lock(self.root/'decisions.lock'):
            with patch.object(Store,'index_decision',side_effect=RuntimeError('after immutable append')):
                with self.assertRaisesRegex(RuntimeError,'after immutable'):
                    self.catalog.append_decision(payload)
            record = self.catalog.append_decision(payload)
            self.assertEqual(record['sequence'],1)
            self.assertEqual(len(self.catalog.ledger()),1)
            self.catalog.project_pending(locked=True)
            self.catalog.project_pending(locked=True)
        self.assertEqual(self.catalog.get('assets',aid)['disposition'],'exclude')
        self.assertEqual(self.catalog.read()['ledger'],[record['id']])

    def test_new_decision_on_target_invalidates_prepared_result(self) -> None:
        from library.primitives import LibraryError,lock
        aid = self.asset_fixture()
        preview = self.catalog.result_set('identify',{'assets':[aid]})
        with lock(self.root/'decisions.lock'):
            self.catalog.append_decision({'action':'exclude','target':aid,'values':{},
                                         'author':'fixture','at':self.now.isoformat()})
        preview['assets'][aid]['new_result'] = 'stale'
        with self.assertRaisesRegex(LibraryError,'Result conflict'):
            self.catalog.save(preview,expected=preview['revision'])

    def test_new_acquisition_member_invalidates_prepared_result(self) -> None:
        from library.store import Change
        from library.primitives import LibraryError
        aid = self.asset_fixture()
        preview = self.catalog.result_set('identify',{'assets':[aid]})
        self.store.commit(Change(upserts={'origins':{'new-origin':{'source':'b'*64,'pid':2,'fid':2,
            'path':'other.cbz','sha256':None,'availability':'waiting'}}}),expected_revision=self.store.revision)
        preview['assets'][aid]['new_result'] = 'stale'
        with self.assertRaisesRegex(LibraryError,'Result conflict'):
            self.catalog.save(preview,expected=preview['revision'])

    def test_connection_handshake_uses_the_same_operator_deadline(self) -> None:
        import socket,time
        from library.postgres import Database
        from library.locking import bounded_acquisition
        from library.primitives import LibraryError
        listener = socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        listener.bind(str(self.root/'.s.PGSQL.15432'))
        listener.listen(1)
        self.addCleanup(listener.close)
        database = Database(f'host={self.root} port=15432 user=deadline-probe dbname=deadline-probe',self.schema)
        started = time.monotonic()
        with bounded_acquisition(started+.5),self.assertRaisesRegex(LibraryError,'storage_busy'):
            with database.connection():
                self.fail('unanswered handshake succeeded')
        self.assertLess(time.monotonic()-started,.8)

    def test_unresolved_outcome_does_not_claim_a_safe_abort(self) -> None:
        import time
        from library.store import Change
        from library.locking import bounded_acquisition
        change = Change(upserts={'works':{'a':{'title':'A'}}})
        identity = self.store.transition_digest(change,expected_revision=0)
        with self.database.connection(write=True) as db:
            db.execute("INSERT INTO commit_receipts(key,digest,state) VALUES (%s,%s,'registered')",('inflight',identity))
            with bounded_acquisition(time.monotonic()+.5):
                self.assertEqual(self.store.outcome('inflight',input_digest=identity),{'state':'unresolved'})
        self.assertEqual(self.store.outcome('inflight',input_digest=identity),{'state':'aborted'})

    def test_shared_edition_delete_and_transfer_invalidate_old_membership(self) -> None:
        from library.store import Change
        from library.primitives import LibraryError
        self.store.commit(Change(upserts={'works':{'w1':{'title':'W1'},'w2':{'title':'W2'}},
            'editions':{'e':{'work':'w1','language':'fr','publisher':'P','evidence':{}}}}),expected_revision=0)
        before = self.catalog.result_set('identify',{'works':['w1']})
        self.store.commit(Change(upserts={'editions':{'e':{'work':'w2','language':'fr','publisher':'P','evidence':{}}}}),
                          expected_revision=self.store.revision)
        before['works']['w1']['title'] = 'stale'
        with self.assertRaisesRegex(LibraryError,'Result conflict'):
            self.catalog.save(before,expected=before['revision'])
        after = self.catalog.result_set('identify',{'works':['w2']})
        self.store.commit(Change(deletes={'editions':['e']}),expected_revision=self.store.revision)
        after['works']['w2']['title'] = 'stale'
        with self.assertRaisesRegex(LibraryError,'Result conflict'):
            self.catalog.save(after,expected=after['revision'])

    def test_destination_snapshot_meets_operator_budget_at_observed_payload_size(self) -> None:
        """Unrelated 844-asset/47 MB payloads must not exhaust preparation."""
        import time
        from library.codec import encode
        from library.locking import bounded_acquisition
        from postgres_fixture import create_catalog
        state = empty()
        for index in range(844):
            aid = f'{index:064x}'
            state['assets'][aid] = {'sha256': aid, 'size': 1, 'format': 'cbz',
                'origins': [], 'embedded': {'text': 'x' * 56000}}
        total = sum(len(encode({'record': row}).encode()) for row in state['assets'].values())
        state['assets'][f'{0:064x}']['embedded']['text'] += 'x' * (47_544_171 - total)
        state['operations']['copy'] = {'destination': 'managed/selected.cbz'}
        from library.store import Change
        catalog = create_catalog(self, self.root/'observed-shape')
        store = catalog.backend()
        assets = list(state['assets'].items())
        for offset in range(0, len(assets), 32):
            store.commit(Change(upserts={'assets': dict(assets[offset:offset+32])}),
                         expected_revision=store.revision)
        store.commit(Change(upserts={'operations': state['operations']}), expected_revision=store.revision)
        with bounded_acquisition(time.monotonic() + .5):
            result = catalog.result_set('retire', {'operations': ['copy']})
            catalog.verify_result(result.conflict)
        self.assertEqual(result['operations']['copy']['destination'], 'managed/selected.cbz')

    def test_destination_membership_rejects_a_new_owner(self) -> None:
        from library.store import Change
        from library.primitives import LibraryError
        shapes = [lambda target: {'target':target},
                  lambda target: {'target':target,'$booksys':'literal'},
                  lambda target: {'destination':target},
                  lambda target: {'publication':{'target':target}},
                  lambda target: {'admission':{'target':target}},
                  lambda target: {'provisional':{'target':target}},
                  lambda target: {'target':target,'destination':target,'publication':{'target':target}},
                  lambda target: {'destination':target,'$booksys':'literal'},
                  lambda target: {'publication':{'target':target,'$booksys':'nested'}},
                  lambda target: {'admission':{'target':target,'$booksys':'nested'},'$booksys':'outer'},
                  lambda target: {'provisional':{'target':target,'$booksys':'nested'}}]
        for index, shape in enumerate(shapes):
            with self.subTest(shape=index):
                target='managed/book'+str(index);a='a'+str(index);b='b'+str(index)
                self.store.commit(Change(upserts={'operations':{a:{'destination':target}}}),expected_revision=self.store.revision)
                before = self.catalog.result_set('publish',{'operations':[a]})
                self.store.commit(Change(upserts={'operations':{b:shape(target)}}),expected_revision=self.store.revision)
                before['operations'][a]['phase'] = 'stale'
                with self.assertRaisesRegex(LibraryError,'Result conflict'):
                    self.catalog.save(before,expected=before['revision'])

    def test_destination_membership_covers_every_entity_kind(self) -> None:
        from library.store import Change
        from library.primitives import LibraryError
        aid = 'a' * 64
        asset = {'sha256': aid, 'size': 1, 'format': 'cbz', 'origins': []}
        self.store.commit(Change(upserts={'works': {'w': {'title': 'W'}},
            'editions': {'e': {'work': 'w', 'language': 'fr', 'publisher': '', 'evidence': []}},
            'assets': {aid: asset}}), expected_revision=0)
        records = {'works': {'title': 'Competitor'},
            'editions': {'work': 'w', 'language': 'fr', 'publisher': '', 'evidence': []},
            'variants': {'edition': 'e', 'asset': aid},
            'assets': {**asset, 'sha256': 'b' * 64}, 'artifacts': {},
            'origins': {'source': 'source', 'pid': 1, 'fid': 2, 'path': 'book.cbz', 'sha256': None, 'availability': 'available'},
            'cases': {}, 'operations': {}}
        for kind, record in records.items():
            with self.subTest(kind=kind):
                target = 'managed/' + kind
                self.store.commit(Change(upserts={'operations': {'anchor': {'target': target}}}),
                                  expected_revision=self.store.revision)
                before = self.catalog.result_set('publish', {'operations': ['anchor']})
                ident = 'b' * 64 if kind == 'assets' else 'competitor-' + kind
                self.store.commit(Change(upserts={kind: {ident: {**record,
                    'publication': {'target': target, '$booksys': 'nested'}, '$booksys': 'outer'}}}),
                    expected_revision=self.store.revision)
                with self.assertRaisesRegex(LibraryError, 'Result conflict'):
                    self.catalog.verify_result(before.conflict)

    def test_destination_membership_tracks_moves_deletes_and_absent_values(self) -> None:
        from library.store import Change
        from library.primitives import LibraryError
        self.store.commit(Change(upserts={'operations': {'a': {'target': 'managed/a'},
            'b': {'target': 'managed/a', 'destination': 'managed/a'}}}), expected_revision=0)
        before = self.catalog.result_set('publish', {'operations': ['a']})
        self.store.commit(Change(upserts={'operations': {'b': {'target': 'managed/b'}}}),
                          expected_revision=self.store.revision)
        with self.assertRaisesRegex(LibraryError, 'Result conflict'):
            self.catalog.verify_result(before.conflict)
        absent = self.catalog.result_set('publish', {'operations': ['a']})
        self.store.commit(Change(upserts={'operations': {'b': {'publication': None,
            'admission': {'target': None}, 'provisional': {'$booksys': 'literal'}}}}),
                          expected_revision=self.store.revision)
        self.catalog.verify_result(absent.conflict)
        self.store.commit(Change(upserts={'operations': {'b': {'destination': 'managed/a'}}}),
                          expected_revision=self.store.revision)
        before_delete = self.catalog.result_set('publish', {'operations': ['a']})
        self.store.commit(Change(deletes={'operations': ['b']}), expected_revision=self.store.revision)
        with self.assertRaisesRegex(LibraryError, 'Result conflict'):
            self.catalog.verify_result(before_delete.conflict)
        self.catalog.verify_result(absent.conflict)

    def test_destination_membership_rejects_a_concurrent_new_owner(self) -> None:
        from library.postgres import guards
        from library.store import Change
        from library.primitives import LibraryError
        self.store.commit(Change(upserts={'operations': {'a': {'target': 'managed/shared'}}}),
                          expected_revision=0)
        before = self.catalog.result_set('publish', {'operations': ['a']})
        started = Event()
        def competing_writer() -> None:
            started.set()
            self.store.commit(Change(upserts={'operations': {'b': {
                'publication': {'target': 'managed/shared', '$booksys': 'literal'}}}}),
                expected_revision=self.store.revision)
        with ThreadPoolExecutor(max_workers=1) as executor:
            with self.database.connection(write=True) as blocker:
                guards(blocker, {'destination:managed/shared'})
                pending = executor.submit(competing_writer)
                self.assertTrue(started.wait(3))
                import time
                deadline = time.monotonic() + 3
                with self.database.connection() as observer:
                    while not observer.execute('SELECT 1 FROM pg_stat_activity WHERE '
                            '%s=ANY(pg_blocking_pids(pid))', (blocker.info.backend_pid,)).fetchone():
                        self.assertLess(time.monotonic(), deadline, 'competing writer did not reach guard')
                        observer.execute('SELECT pg_stat_clear_snapshot()')
                        time.sleep(.01)
                # Read-only preparation can still see the previous membership.
                self.catalog.verify_result(before.conflict)
                self.assertFalse(pending.done())
            pending.result(timeout=5)
        before['operations']['a']['phase'] = 'stale'
        with self.assertRaisesRegex(LibraryError, 'Result conflict'):
            self.catalog.save(before, expected=before['revision'])
        self.assertNotIn('phase', self.store.get('operations', 'a'))

    def test_decision_reservation_survives_failure_before_file_serialization(self) -> None:
        from library.primitives import LibraryError,lock
        aid = self.asset_fixture()
        preview = self.catalog.result_set('identify',{'assets':[aid]})
        payload = {'action':'exclude','target':aid,'values':{},'author':'fixture','at':self.now.isoformat()}
        with lock(self.root/'decisions.lock'):
            with patch('library.catalog.atomic_yaml',side_effect=RuntimeError('before evidence bytes')):
                with self.assertRaisesRegex(RuntimeError,'before evidence'):
                    self.catalog.append_decision(payload)
            with self.assertRaisesRegex(LibraryError,'interrupted decision reservation'):
                self.catalog.append_decision({**payload,'action':'defer'})
            preview['assets'][aid]['new_result'] = 'stale'
            with self.assertRaisesRegex(LibraryError,'Result conflict'):
                self.catalog.save(preview,expected=preview['revision'])
            record = self.catalog.append_decision(payload)
        self.assertEqual(record['sequence'],1)
        self.assertEqual(len(self.catalog.ledger()),1)

    def test_prepared_cover_preserves_bytes_without_file_reads_in_write_transaction(self) -> None:
        from contextlib import contextmanager
        from library.postgres import Database
        from library.store import Change,Store
        self.store.commit(Change(upserts={'works':{'w':{'title':'W','cover':'retained cover\nbytes'}}}),expected_revision=0)
        connection,restore = Database.connection,Store.restore
        active = []
        @contextmanager
        def observed(database,**kwargs):
            with connection(database,**kwargs) as db:
                active.append(kwargs.get('write',False))
                try:
                    yield db
                finally:
                    active.pop()
        def checked(store,payload,*,media=True):
            if media and any(active):
                self.fail('media read inside write transaction')
            return restore(store,payload,media=media)
        with patch.object(Database,'connection',observed),patch.object(Store,'restore',checked):
            state = self.catalog.result_set('identify',{'works':['w']})
            self.assertEqual(state['works']['w']['cover'],'retained cover\nbytes')
            state['works']['w']['title'] = 'Updated'
            self.catalog.save(state,expected=state['revision'])
        self.assertEqual(self.store.get('works','w')['cover'],'retained cover\nbytes')

    def test_source_page_cursor_survives_registration_readback(self) -> None:
        from datetime import timedelta
        from library.source_tasks import SourcePolicy
        from library.stage_tasks import StageTasks
        from library.operational_state import CollectorState
        policy = SourcePolicy(self.root/'source-policy.yaml',store=self.store)
        journal = self.root/'monitor.yaml'
        state=CollectorState(self.root,database=self.database)
        for key in ('a','b','c'):state.category('https://fixture/'+key,{'after_id':1})
        tasks = StageTasks(self.store,policy=policy)
        with patch('library.stage_model.SOURCE_PAGE',2):
            first = tasks.scan('collection',journal,now=self.now)
            second = tasks.scan('collection',journal,now=self.now+timedelta(seconds=1))
        self.assertEqual((first.processed,first.continued,first.blocked),(2,True,''))
        self.assertEqual((second.processed,second.continued,second.blocked),(1,False,''))
        self.assertEqual(len(policy.read()['tasks']),3)

    def test_key_pagination_preserves_binary_order_under_non_c_database_locale(self) -> None:
        from library.store import Change
        keys = ['a2','a10','A','Z','_','a','z','é']
        self.store.commit(Change(upserts={'works':{key:{'title':key} for key in keys}}),expected_revision=0)
        found,cursor = [],None
        while True:
            page = self.store.page('works',limit=2,cursor=cursor)
            found.extend(item['id'] for item in page['items'])
            cursor = page['cursor']
            if cursor is None:
                break
        self.assertEqual(found,sorted(keys))

    def test_independent_file_results_commit_while_first_file_transaction_remains_open(self) -> None:
        from library.store import Store,Change
        first_id,second_id = 'a'*64,'b'*64
        self.store.commit(Change(upserts={'assets':{key:{'sha256':key,'size':1,'format':'cbz','origins':[]}
            for key in (first_id,second_id)}}),expected_revision=0)
        a = self.catalog.result_set('identify',{'assets':[first_id]})
        b = self.catalog.result_set('identify',{'assets':[second_id]})
        a['assets'][first_id]['metadata_result'] = 'first'
        b['assets'][second_id]['metadata_result'] = 'second'
        entered,release = Event(),Event()
        original = Store._refresh_rows
        def hold(store,db,affected):
            original(store,db,affected)
            if affected['assets'] == {first_id}:
                entered.set()
                self.assertTrue(release.wait(5))
        with ThreadPoolExecutor(max_workers=2) as pool,patch.object(Store,'_refresh_rows',hold):
            first = pool.submit(self.catalog.save,a,expected=a['revision'])
            self.assertTrue(entered.wait(5))
            try:
                second = pool.submit(self.catalog.save,b,expected=b['revision'])
                second.result(timeout=3)
            finally:
                release.set()
            first.result(timeout=3)
        self.assertEqual(self.store.get('assets',second_id)['metadata_result'],'second')

    def test_committed_completion_readback_failure_preserves_real_outcome(self) -> None:
        from library.stage_tasks import StageTasks,Obligation
        from library.store import Store
        from library.postgres import CommitUnknown
        from library.primitives import LibraryError
        tasks = StageTasks(self.store)
        task = tasks.discover(Obligation('metadata','asset-a','r1','m1'),now=self.now)
        active = tasks.start(task.key,task.generation,now=self.now,resources=True)
        commit = Store.commit
        unavailable = []
        def committed(store,*args,**kwargs):
            result = commit(store,*args,**kwargs)
            unavailable.append(True)
            return result
        get = tasks.get
        def readback(key):
            if unavailable:
                raise LibraryError('storage_busy')
            return get(key)
        with patch.object(Store,'commit',committed),patch.object(tasks,'get',readback):
            with self.assertRaises(CommitUnknown):
                tasks.complete(active.key,active.generation,now=self.now)
        self.assertEqual(tasks.get(task.key).status,'done')
        self.assertEqual(tasks.get(task.key).attempts,1)

    def test_duplicate_start_survives_lost_registration_acknowledgement(self) -> None:
        from library.store import Store
        from library.stage_tasks import StageTasks,Obligation
        from library.postgres import CommitUnknown
        tasks = StageTasks(self.store)
        pending = tasks.discover(Obligation('metadata','asset-a','r1','m1'),now=self.now)
        active = tasks.start(pending.key,pending.generation,now=self.now,resources=True)
        register = Store._register
        def lost(store,*args,**kwargs):
            register(store,*args,**kwargs)
            raise CommitUnknown('registration_ack_lost')
        with patch.object(Store,'_register',lost):
            duplicate = tasks.start(pending.key,pending.generation,now=self.now,resources=True)
        self.assertEqual(duplicate,active)
        self.assertEqual(tasks.get(pending.key).attempts,1)

    def test_startup_projection_recovers_reservation_without_immutable_file(self) -> None:
        from library.primitives import lock
        aid = self.asset_fixture()
        payload = {'action':'exclude','target':aid,'values':{},'author':'fixture','at':self.now.isoformat()}
        record = self.store.reserve_decision(payload)
        self.assertFalse(list(self.catalog.decisions.glob('*.yaml')))
        self.catalog.project_pending()
        self.catalog.project_pending()
        self.assertEqual(self.catalog.ledger(),[record])
        self.assertEqual(self.catalog.read()['ledger'],[record['id']])
        self.assertEqual(self.catalog.get('assets',aid)['disposition'],'exclude')

    def test_reserved_scope_blocks_results_prepared_before_and_after_reservation(self) -> None:
        from library.store import Change
        from library.primitives import LibraryError
        aid = self.asset_fixture()
        other = 'c'*64
        self.store.commit(Change(upserts={'works':{'w':{'title':'W'}},'editions':{'e':{
            'work':'w','language':'fr','publisher':'P','evidence':{}}},'assets':{other:{
            'sha256':other,'size':1,'format':'cbz','origins':[]}}}),expected_revision=self.store.revision)
        before = self.catalog.result_set('identify',{'assets':[aid]})
        self.store.reserve_decision({'action':'exclude','target':aid,'affected_editions':['e'],
            'values':{'edition':{'isbn':'9780000000002'}},'author':'fixture','at':self.now.isoformat()})
        after = self.catalog.result_set('identify',{'assets':[aid]})
        edition = self.catalog.result_set('identify',{'editions':['e']})
        claim = self.catalog.result_set('identify',{'works':[]},claims={'isbn':['9780000000002']})
        for state,kind,ident in ((before,'assets',aid),(after,'assets',aid),(edition,'editions','e')):
            state[kind][ident]['new_result'] = 'blocked'
            with self.assertRaisesRegex(LibraryError,'Result conflict|reserved'):
                self.catalog.save(state,expected=state['revision'])
        claim['works']['new-work'] = {'title':'Claimed'}
        with self.assertRaisesRegex(LibraryError,'reserved'):
            self.catalog.save(claim,expected=claim['revision'])
        with self.assertRaisesRegex(LibraryError,'reserved'):
            self.store.commit(Change(upserts={'variants':{'new-member':{'edition':'e','asset':other}}}),
                              expected_revision=self.store.revision)
        with self.assertRaisesRegex(LibraryError,'reserved'):
            self.store.commit(Change(upserts={'editions':{'new-edition':{'work':'w','language':'fr',
                'publisher':'P','evidence':{},'isbn':'9780000000002'}}}),expected_revision=self.store.revision)
        current = self.store.get('assets',aid)
        current['new_result'] = 'unscoped'
        with self.assertRaisesRegex(LibraryError,'reserved'):
            self.store.commit(Change(upserts={'assets':{aid:current}}),expected_revision=self.store.revision)
        unrelated = self.catalog.result_set('identify',{'works':['unrelated']})
        unrelated['works']['unrelated'] = {'title':'Unaffected'}
        self.catalog.save(unrelated,expected=unrelated['revision'])
        self.assertEqual(self.store.get('works','unrelated'),{'title':'Unaffected'})

    def manual_request(self, aid):
        case = self.store.case(aid)
        return {'request_id':'manual-exclude','case':case['id'],'version':case['version'],
                'action':'exclude','values':{}}

    def test_public_manual_confirmation_applies_and_replays_exact_decision(self) -> None:
        from library.review import decide
        aid = self.asset_fixture()
        request = self.manual_request(aid)
        first = decide(self.catalog,[request],author='flowcool')[0]
        self.assertEqual(first['status'],'applied',first)
        again = decide(self.catalog,[request],author='flowcool')[0]
        self.assertEqual(again['status'],'applied',again)
        self.assertEqual(again['decision'],first['decision'])
        self.assertEqual(len(self.catalog.ledger()),1)
        self.assertEqual(self.catalog.get('assets',aid)['disposition'],'exclude')

    def test_public_manual_confirmation_checks_original_inputs_inside_reservation(self) -> None:
        from library.review import decide
        from library.store import Store,Change
        aid = self.asset_fixture()
        request = self.manual_request(aid)
        reserve = Store.reserve_decision
        def competitor(store,*args,**kwargs):
            store.commit(Change(upserts={'origins':{'concurrent-origin':{'source':'b'*64,'pid':3,'fid':3,
                'path':'new.cbz','sha256':None,'availability':'validated'}}}),expected_revision=store.revision)
            return reserve(store,*args,**kwargs)
        with patch.object(Store,'reserve_decision',competitor):
            result = decide(self.catalog,[request],author='flowcool')[0]
        self.assertEqual(result['status'],'rejected',result)
        self.assertFalse(self.catalog.ledger())

    def test_public_manual_confirmation_allows_unrelated_concurrent_change(self) -> None:
        from library.review import decide
        from library.store import Store,Change
        aid = self.asset_fixture()
        request = self.manual_request(aid)
        reserve = Store.reserve_decision
        def unrelated(store,*args,**kwargs):
            store.commit(Change(upserts={'works':{'unrelated':{'title':'Independent'}}}),expected_revision=store.revision)
            return reserve(store,*args,**kwargs)
        with patch.object(Store,'reserve_decision',unrelated):
            result = decide(self.catalog,[request],author='flowcool')[0]
        self.assertEqual(result['status'],'applied',result)
        self.assertEqual(self.store.get('works','unrelated'),{'title':'Independent'})

    def test_public_manual_confirmation_recovers_its_reserved_decision_exactly(self) -> None:
        from library.review import decide
        aid = self.asset_fixture()
        request = self.manual_request(aid)
        with patch('library.catalog.atomic_yaml',side_effect=RuntimeError('before immutable evidence')):
            with self.assertRaisesRegex(RuntimeError,'before immutable'):
                decide(self.catalog,[request],author='flowcool')
        retained = self.store.pending_decisions()[0]
        result = decide(self.catalog,[request],author='flowcool')[0]
        self.assertEqual(result['status'],'applied',result)
        self.assertEqual(result['decision'],retained['id'])
        self.assertEqual(self.catalog.ledger(),[retained])

    def test_reserved_original_closure_stays_excluded_until_projection(self) -> None:
        from library.primitives import LibraryError
        from library.store import Change
        aid = self.asset_fixture()
        original = self.catalog.result_set('exclude',{'assets':[aid]})
        payload = {'action':'exclude','target':aid,'values':{},'author':'fixture','at':self.now.isoformat()}
        record = self.store.reserve_decision(payload,conflict=original.conflict)
        for indexed in (False,True):
            if indexed:
                from library.primitives import atomic_yaml
                atomic_yaml(self.catalog.decisions/f"{record['sequence']:09d}-{record['id']}.yaml",record,exclusive=True)
                self.store.index_decision(record)
            origin = self.catalog.result_set('retire',{'origins':['origin-a']})
            origin['origins']['origin-a']['availability'] = 'changed'
            with self.assertRaisesRegex(LibraryError,'reserved'):
                self.catalog.save(origin,expected=origin['revision'])
            with self.assertRaisesRegex(LibraryError,'reserved'):
                self.store.commit(Change(upserts={'origins':{'new-origin':{'source':'b'*64,'pid':9,'fid':9,
                    'path':'new.cbz','sha256':None,'availability':'validated'}}}),expected_revision=self.store.revision)
            self.store.commit(Change(upserts={'works':{'unrelated':{'title':str(indexed)}}}),
                              expected_revision=self.store.revision,commit_key='unrelated:'+str(indexed))
        self.catalog.project_pending()
        origin = self.catalog.result_set('retire',{'origins':['origin-a']})
        origin['origins']['origin-a']['availability'] = 'changed-after-projection'
        self.catalog.save(origin,expected=origin['revision'])
        self.assertEqual(self.catalog.read()['ledger'],[record['id']])


if __name__ == '__main__':
    unittest.main()
