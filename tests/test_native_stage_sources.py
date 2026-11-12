"""Native monitor projections retain per-entry source conflict proof."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from library.catalog import empty
from postgres_fixture import create_store
from library.primitives import digest
from library.operational_state import CollectorState
from library.source_tasks import SourcePolicy
from library.stage_tasks import StageTasks

AT = datetime(2026, 11, 9, tzinfo=timezone.utc)
SOURCE = 'https://www.bookys-ebooks.com/123-album'

def seed_monitor(root, value):
    state=CollectorState(root)
    for source,record in value.get('categories',{}).items():state.category(source,record)
    for source,record in value.get('submissions',{}).items():state.submission(source,record)
    if 'schedule' in value:state.patch_monitor(schedule=value['schedule'])

class NativeSourceTests(unittest.TestCase):
    def test_collection_selection_keeps_native_validation_within_small_window(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=create_store(self,root,empty(),authority='small-collection')
            policy=SourcePolicy.create(root/'policy.yaml',store=store);tasks=StageTasks(store,policy=policy)
            seed_monitor(root,{'categories':{},'submissions':{
                SOURCE+str(i):{'status':'queued','package_id':i+1} for i in range(40)}})
            tasks.scan('collection',root/'monitor.yaml',now=AT)
            original=policy.validation_snapshot
            def bounded(rows,**kwargs):
                self.assertLessEqual(len(rows),16)
                return original(rows,**kwargs)
            with patch.object(policy,'validation_snapshot',side_effect=bounded):
                result=tasks.select('collection',now=AT)
            self.assertIsNotNone(result.task)
            self.assertEqual(result.task.attempts,0)

    def test_incomplete_source_pages_continue_before_the_full_scan_interval(self):
        from datetime import timedelta
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = create_store(self,root,empty(),authority='scan')
            StageTasks.upgrade(store)
            tasks = StageTasks(store, policy=SourcePolicy.create(root/'policy.yaml'))
            journal = root/'monitor.yaml'
            seed_monitor(root, {'version':1, 'categories':{}, 'submissions': {
                SOURCE+str(index): {'status':'queued', 'package_id':index} for index in range(257)}})
            tasks.policy.register('routing', journal)
            first = tasks.scan_next(now=AT)
            self.assertTrue(first.continued)
            second = tasks.scan_next(now=AT+timedelta(seconds=1))
            self.assertEqual(second.processed, 1)
            self.assertFalse(second.continued)
            self.assertEqual(tasks.scan_next(now=AT+timedelta(seconds=2)).processed, 0)

    def test_continuation_validates_native_candidate_beyond_first_256_rows(self):
        from time import monotonic
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=create_store(self,root,empty(),authority='continued-fence')
            state=CollectorState(root);policy=SourcePolicy.create(root/'policy.yaml',store=store);tasks=StageTasks(store,policy=policy)
            by_entity={}
            for index in range(257):
                url=SOURCE+str(index);state.submission(url,{'status':'queued','package_id':index})
                by_entity['source-'+digest(url)]=url
            journal=root/'monitor.yaml'
            started=monotonic();first=tasks.scan('routing',journal,now=AT);first_seconds=monotonic()-started
            started=monotonic();second=tasks.scan('routing',journal,now=AT);second_seconds=monotonic()-started
            self.assertEqual((first.processed,first.continued,first.blocked),(256,True,''))
            self.assertEqual((second.processed,second.continued,second.blocked),(1,False,''))
            records=sorted(policy.read()['tasks'].values(),key=lambda task:task['key'])
            self.assertEqual(len(records),257)
            final=records[-1];source=by_entity[final['entity'].split(':',1)[1]]
            state.submission(source,{'status':'queued','package_id':9999})
            started=monotonic()
            result=tasks.select('routing',now=AT,after=(records[-2]['first_eligible_at'],records[-2]['key']))
            selection_seconds=monotonic()-started
            print(f'Native routing 257: page_256={first_seconds:.3f}s page_1={second_seconds:.3f}s selection={selection_seconds:.3f}s')
            self.assertIsNone(result.task)
            retained=tasks.get(final['key'])
            self.assertEqual((retained.blocked,retained.attempts,retained.generation),
                ('source_changed_rescan_required',final['attempts'],final['generation']))

    def test_cached_selection_revalidates_native_signature_under_writer_guard(self):
        from unittest.mock import patch
        from library.primitives import LibraryError
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=create_store(self,root,empty(),authority='receipt-source-fence')
            state=CollectorState(root);state.submission(SOURCE,{'status':'queued','package_id':1})
            policy=SourcePolicy.create(root/'policy.yaml',store=store);tasks=StageTasks(store,policy=policy)
            tasks.scan('routing',root/'monitor.yaml',now=AT)
            selected=tasks.select('routing',now=AT).task
            self.assertIsNotNone(selected)
            snapshot=policy.validation_snapshot
            def after_detached(*args,**kwargs):
                value=snapshot(*args,**kwargs)
                if kwargs.get('db') is None:state.submission(SOURCE,{'status':'queued','package_id':2})
                return value
            with patch.object(policy,'validation_snapshot',side_effect=after_detached):
                with self.assertRaisesRegex(LibraryError,'storage_busy'):
                    tasks.select('routing',now=AT)
            self.assertIsNone(tasks.select('routing',now=AT).task)
            self.assertEqual(tasks.get(selected.key).attempts,0)

    def test_native_start_rejects_missing_or_mismatched_binding(self):
        from library.primitives import LibraryError
        from library.codec import decode,encode
        for missing in (True,False):
            with self.subTest(missing=missing),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);store=create_store(self,root,empty(),authority='binding-fence')
                CollectorState(root).submission(SOURCE,{'status':'queued','package_id':1})
                policy=SourcePolicy.create(root/'policy.yaml',store=store);tasks=StageTasks(store,policy=policy)
                tasks.scan('routing',root/'monitor.yaml',now=AT)
                task=tasks.select('routing',now=AT).task
                with store._database.connection(write=True) as db:
                    row=db.execute('SELECT binding FROM source_inputs WHERE source=%s AND entity=%s',(task.source,task.entity)).fetchone()
                    binding=decode(row[0]);binding['registration_kind']='backfill'
                    db.execute('UPDATE source_inputs SET binding=%s WHERE source=%s AND entity=%s',
                        (None if missing else encode(binding),task.source,task.entity))
                with self.assertRaisesRegex(LibraryError,'binding'):
                    tasks.start(task.key,task.generation,now=AT,resources=True)
                retained=tasks.get(task.key)
                self.assertEqual((retained.status,retained.attempts,retained.generation),
                    ('pending',task.attempts,task.generation))

    def test_detached_rescan_cannot_overwrite_newer_registration_cursor(self):
        from unittest.mock import patch
        from datetime import timedelta
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=create_store(self,root,empty(),authority='cursor-race')
            state=CollectorState(root);state.submission(SOURCE,{'status':'queued','package_id':1})
            policy=SourcePolicy.create(root/'policy.yaml',store=store);tasks=StageTasks(store,policy=policy)
            journal=root/'monitor.yaml';snapshot=policy.journal;newer=AT+timedelta(seconds=1)
            def stale_view(*args,**kwargs):
                old=snapshot(*args,**kwargs)
                concurrent=StageTasks(store,policy=SourcePolicy(root/'policy.yaml',store=store))
                self.assertEqual(concurrent.scan('routing',journal,now=newer).processed,1)
                return old
            with patch.object(policy,'journal',side_effect=stale_view):
                stale=tasks.scan('routing',journal,now=AT)
            self.assertEqual(stale.blocked,'source_cursor_changed')
            registration=next(iter(policy.read()['sources'].values()))
            self.assertEqual(registration['last_scan'],newer.isoformat())

    def test_native_routing_source_survives_unrelated_monitor_change_and_fences_own_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = create_store(self,root,empty(),authority='native')
            StageTasks.upgrade(store)
            tasks = StageTasks(store, policy=SourcePolicy.create(root/'policy.yaml'))
            journal = root/'monitor.yaml'
            data = {'version': 1, 'categories': {}, 'submissions': {SOURCE: {'status': 'queued', 'package_id': 1, 'name': 'Booksys-one'}}}
            seed_monitor(root, data)
            scanned = tasks.scan('routing', journal, now=AT)
            self.assertEqual(scanned.processed, 1)
            task = tasks.select('routing', now=AT).task
            self.assertIsNotNone(task)
            data['submissions']['https://www.bookys-ebooks.com/456-other'] = {'status': 'queued', 'package_id': 2, 'name': 'Booksys-two'}
            seed_monitor(root, data)
            self.assertEqual(tasks.select('routing', now=AT).task.key, task.key)
            data['submissions'][SOURCE]['package_id'] = 99
            seed_monitor(root, data)
            self.assertIsNone(tasks.select('routing', now=AT).task)
            self.assertEqual(tasks.get(task.key).blocked, 'source_changed_rescan_required')
            self.assertEqual((tasks.get(task.key).attempts,tasks.get(task.key).generation),(task.attempts,task.generation))

    def test_collection_has_separate_category_and_fiche_units_with_remote_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = create_store(self,root,empty(),authority='native')
            StageTasks.upgrade(store)
            tasks = StageTasks(store, policy=SourcePolicy.create(root/'policy.yaml'))
            journal = root/'monitor.yaml'
            seed_monitor(root, {'version': 1, 'schedule': {'not_before': '2026-11-10T00:00:00+00:00'},
                'categories': {'https://www.bookys-ebooks.com/bandes-dessinees': {'after_id': 100}},
                'submissions': {SOURCE: {'status': 'queued', 'package_id': 1, 'name': 'Booksys-one'}}})
            self.assertEqual(tasks.scan('collection', journal, now=AT).processed, 2)
            self.assertIsNone(tasks.select('collection', now=AT).task)

    def test_new_remote_deadline_fences_an_already_discovered_collection_task(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = create_store(self,root,empty(),authority='native')
            StageTasks.upgrade(store)
            tasks = StageTasks(store, policy=SourcePolicy.create(root/'policy.yaml'))
            journal = root/'monitor.yaml'
            data = {'version': 1, 'categories': {}, 'submissions': {SOURCE: {'status': 'queued', 'package_id': 1}}}
            seed_monitor(root, data)
            tasks.scan('collection', journal, now=AT)
            task = tasks.select('collection', now=AT).task
            self.assertIsNotNone(task)
            data['schedule'] = {'not_before': '2026-11-10T00:00:00+00:00'}
            seed_monitor(root, data)
            self.assertIsNone(tasks.select('collection', now=AT).task)
            tasks.scan('collection', journal, now=AT)
            retained = tasks.get(task.key)
            self.assertEqual(retained.attempts, task.attempts)
            self.assertEqual(retained.key, task.key)
            self.assertEqual(retained.generation,task.generation)
            self.assertEqual(retained.due_at, '2026-11-10T00:00:00+00:00')

    def test_active_source_and_deadline_changes_fence_result_without_new_allowance(self):
        from unittest.mock import patch
        from library.primitives import LibraryError
        for queue,change in [('routing','package'),('collection','deadline')]:
            with self.subTest(queue=queue),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);store=create_store(self,root,empty(),authority='active-fence')
                state=CollectorState(root);entry={'status':'queued','package_id':1}
                state.submission(SOURCE,entry)
                policy=SourcePolicy.create(root/'policy.yaml',store=store);tasks=StageTasks(store,policy=policy)
                tasks.scan(queue,root/'monitor.yaml',now=AT)
                task=tasks.select(queue,now=AT).task
                active=tasks.start(task.key,task.generation,now=AT,resources=True)
                progress=policy.prepare_progress(active,{'proof':'prepared-original-input'})
                state.submission(SOURCE+'-other',{'status':'queued','package_id':2})
                with patch.object(CollectorState,'monitor',side_effect=AssertionError('No full monitor read')):
                    policy.verify_task(active)
                    if change=='package':state.submission(SOURCE,{**entry,'package_id':99})
                    else:state.patch_monitor(schedule={'not_before':'2026-11-10T00:00:00+00:00'})
                    with self.assertRaises(LibraryError):
                        tasks.complete(active.key,active.generation,now=AT,source_progress=progress)
                retained=tasks.get(active.key)
                self.assertEqual((retained.status,retained.attempts,retained.generation),
                    ('active',active.attempts,active.generation))
                self.assertIsNone(policy.progress(active.source,active.entity))

    def test_slow_source_validation_does_not_hold_the_catalogue_write_transaction(self):
        from library.stage_model import Obligation
        from unittest.mock import patch
        import threading
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            store=create_store(self,root,empty(),authority='native')
            StageTasks.upgrade(store)
            tasks=StageTasks(store,policy=SourcePolicy.create(root/'policy.yaml'))
            journal=root/'monitor.yaml'
            seed_monitor(root,{'version':1,'categories':{},'submissions':{SOURCE:{'status':'queued','package_id':1}}})
            tasks.scan('routing',journal,now=AT)
            local=tasks.discover(Obligation('metadata','independent','1','v1'),now=AT)
            entered,release=threading.Event(),threading.Event()
            original=tasks.policy.validation_snapshot
            def slow(*args,**kwargs):
                entered.set()
                if not release.wait(5):
                    raise AssertionError('Independent claim could not progress')
                return original(*args,**kwargs)
            errors=[]
            def select():
                try: tasks.select('routing',now=AT)
                except BaseException as exc: errors.append(exc)
            with patch.object(SourcePolicy,'validation_snapshot',side_effect=slow):
                thread=threading.Thread(target=select)
                thread.start()
                try:
                    self.assertTrue(entered.wait(5))
                    started=tasks.start(local.key,local.generation,now=AT,resources=True)
                    self.assertEqual(started.status,'active')
                finally:
                    release.set()
                    thread.join(6)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors,[])
