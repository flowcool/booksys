"""Activity contracts: producer observations, persistence, projection and read-only HTTP."""
import json
import tempfile
import unittest
from pathlib import Path
from activity import Activity, read_activity
from postgres_fixture import create_store,create_activity
from library.primitives import empty
from library.operational_state import CollectorState,NativeHistory,StatusObservations
from library.codec import encode
from unittest.mock import patch

def setup(test,root):
    store=create_store(test,root/"state",empty())
    for directory in (root,root/"collector",root/"collector/activity",root/"evidence/activity"):
        directory.mkdir(parents=True,exist_ok=True)
        selector=directory/"postgresql.json"
        selector.write_bytes((root/"state/postgresql.json").read_bytes());selector.chmod(0o600)
    create_activity(test,root,authority="activity",timestamp=0)
    for directory in (root/"collector/activity",root/"evidence/activity"):
        (directory/"activity-authority.yaml").write_bytes((root/"activity-authority.yaml").read_bytes())
    return store

def catalog_fixture(store,value):
    with store._database.connection(write=True) as db:
        for kind in ("origins","assets"):
            for key,item in value.get(kind,{}).items():
                db.execute(f"INSERT INTO {kind}(id,revision,payload) VALUES (%s,1,%s)",(key,store.prepare(item)[0]))


class ActivityTests(unittest.TestCase):
    def test_restart_keeps_events_and_heartbeat_is_not_progress(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store=setup(self,root)
            a = Activity(root, 'collection', clock=lambda: 100000)
            a.report('working', 'backfill', item='source-1')
            a.report('waiting', 'lock', reason='Processing lock occupied')
            before = read_activity(root, 'collection')
            b = Activity(root, 'collection', clock=lambda: 100100)
            b.heartbeat()
            after = read_activity(root, 'collection')
            self.assertEqual(after['events'], before['events'])
            self.assertIsNone(after['current']['last_progress_at'])
            b.report('working', 'backfill', item='source-1', progressed=True)
            value = read_activity(root, 'collection')
            self.assertEqual(value['current']['last_progress_at'], 100100)
            self.assertEqual(len(value['events']), 3)
            b.heartbeat()
            self.assertEqual(len(read_activity(root, 'collection')['events']), 3)
            c = Activity(root, 'collection', clock=lambda: 200000)
            c.report('sleeping', 'interval')
            self.assertEqual(len(read_activity(root, 'collection')['events']), 1)

    def test_projection_preserves_stale_values_and_never_infers_failure(self):
        from library.activity import project
        snapshot = {'observed_at': 1000, 'stages': {'collection': {
            'observed_at': 1000, 'state': 'working', 'last_progress_at': 800,
            'counts': {'captured source entries': 8}, 'password': 'secret',
            'items': [{'id': 'abc', 'title': '<img onerror=evil>', 'url': 'https://private/secret'}]}}}
        current = project(snapshot, timestamp=1100)
        stage = current['stages']['collection']
        self.assertEqual(stage['freshness'], 'stale')
        self.assertEqual(stage['state'], 'unknown')
        self.assertEqual(stage['counts']['captured source entries'], 8)
        self.assertEqual(stage['no_progress_seconds'], 300)
        self.assertNotIn('secret', json.dumps(current))
        self.assertNotIn('url', stage['items'][0])
        self.assertIsNone(current['stages']['identity']['no_progress_seconds'])

    def test_shared_observer_reports_booksys_downloads_and_waits_only(self):
        from library.activity import Observer
        from activity import write_yaml
        class Client:
            calls = []
            def call(self, method):
                self.calls.append(method)
                if method == 'status_downloads':
                    return [{'fid': 11, 'wait_until': 1200}]
                return [{'pid': 1, 'links': [{'fid': 11, 'status': 5, 'name': 'file.cbz',
                    'url': 'https://host/private', 'password': 'secret'}]},
                    {'pid': 2, 'links': [{'fid': 12, 'status': 12, 'name': 'unowned'}]}]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store=setup(self,root)
            CollectorState(root/'collector').submission('bookys-source',{'package_id':1})
            CollectorState(root/'collector').capture('bookys-source',{'title':'Album'})
            observer = Observer(root, root/'collector', clock=lambda: 1000)
            client = Client()
            observer.sample_downloads(client)
            stage = observer.snapshot()['stages']['downloads']
            self.assertEqual(stage['counts']['waiting links'], 1)
            self.assertEqual(stage['items'][0]['next_attempt'], 1200)
            self.assertIsNone(stage['items'][0]['waiting_seconds'])
            self.assertEqual(client.calls, ['get_queue_data', 'status_downloads'])
            self.assertNotIn('secret', json.dumps(observer.snapshot()))
            self.assertNotIn('unowned', json.dumps(observer.snapshot()))
            observer.clock = lambda: 1100
            observer.downloads_unavailable()
            stage = observer.snapshot()['stages']['downloads']
            self.assertEqual(stage['freshness'], 'unavailable')
            self.assertEqual(stage['observed_at'], 1000)
            self.assertEqual(stage['counts']['waiting links'], 1)

    def test_http_activity_is_allowlisted_and_does_not_touch_business_locks(self):
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        from library.catalog import lock
        from library.web import handler, PAGE
        from activity import write_yaml
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store=setup(self,root)
            StatusObservations(store._database).save_activity( {'stages': {'collection': {
                'state': 'waiting', 'reason': 'Lock occupied', 'password': 'secret',
                'items': [{'title': '<script>evil()</script>', 'url': 'https://private/secret'}]}}})
            before = StatusObservations(store._database).activity()
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler(root))
            thread = threading.Thread(target=server.serve_forever);thread.start()
            try:
                with lock(root/'state/library-cycle.lock', blocking=False):
                    with lock(root/'state/pipeline.lock', blocking=False):
                        for _ in range(3):
                            with urllib.request.urlopen('http://127.0.0.1:'+str(server.server_port)+'/api/activity') as response:
                                value = response.read().decode()
                                self.assertNotIn('secret', value)
                                self.assertIn('<script>evil()</script>', value)
                self.assertEqual(before, StatusObservations(store._database).activity())
                self.assertIn('activity-tab', PAGE)
                self.assertNotIn('innerHTML', PAGE)
            finally:
                server.shutdown();thread.join();server.server_close()

    def test_stopped_and_restarted_producers_cannot_look_running(self):
        from library.activity import Observer
        from activity import write_yaml
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store=setup(self,root)
            Activity(root/'collector/activity','collection',clock=lambda:900).report('working','Backfill')
            observer = Observer(root, root/'collector', clock=lambda: 1001)
            observer.container_started['collection'] = 999
            self.assertEqual(observer.snapshot()['stages']['collection']['state'], 'unknown')
            observer.resources['services'] = {'observed_at': 1001, 'states': {'collector': 'exited'}}
            # A fresh explicit stopped observation supersedes a leftover running phase.
            stage = observer.snapshot()['stages']['collection']
            self.assertIn(stage['state'], ('stopped', 'unknown'))

    def test_pruning_retains_freshness_and_observation_errors_do_not_stop_work(self):
        from activity import prune
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store=setup(self,root)
            recorder = Activity(root, 'byparr', clock=lambda: 100000)
            recorder.report('blocked', 'Cloudflare', reason='Browser challenge')
            recorder.report('working', 'Recovery')
            before = read_activity(root, 'byparr')['current']['observed_at']
            prune(root, 'byparr', 200000)
            after = read_activity(root, 'byparr')
            self.assertEqual(after['events'], {})
            self.assertEqual(after['current']['observed_at'], before)
            bad = root/'not-a-directory';bad.write_text('fixture')
            Activity(bad, 'local').report('working', 'Validation')

    def test_missing_canonical_sources_preserve_counts_and_bundle_is_complete(self):
        from library.activity import Observer
        from activity import write_yaml
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store=setup(self,root)
            catalog_fixture(store, {'origins': {
                'bundle': {'availability': 'bundle'}, 'part': {'availability': 'multipart_member'},
                'retry': {'availability': 'pending', 'reason': 'Intake budget deferred', 'intake_next_attempt': 1400}}, 'assets': {}})
            observer = Observer(root, root/'collector', clock=lambda: 1000)
            observer.save()
            local = observer.snapshot()['stages']['local']
            self.assertEqual(local['counts']['waiting files'], 1)
            self.assertEqual(local['items'][0]['next_attempt'], 1400)
            # A failed canonical read must retain the last successful observation.
            from library.primitives import LibraryError
            broken=patch('library.store.Store.check',side_effect=LibraryError('storage_busy'))
            observer.clock = lambda: 1100
            with broken:
                local = observer.snapshot()['stages']['local']
            self.assertEqual(local['counts']['waiting files'], 1)
            self.assertEqual(local['queue_freshness'], 'unavailable')
            self.assertEqual(local['queue_observed_at'], 1000)

    def test_broken_activity_nested_data_cannot_abort_business(self):
        from activity import write_yaml
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store=setup(self,root)
            write_yaml(root/'local.yaml', {'current': [], 'events': []})
            Activity(root, 'local').report('working', 'Validation')

    def test_byte_progress_does_not_create_significant_events(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store=setup(self,root)
            a = Activity(root, 'downloads', clock=lambda: 100000)
            a.report('working', 'Downloading')
            for _ in range(600):
                a.report('working', 'Downloading', progressed=True)
            value = read_activity(root, 'downloads')
            self.assertEqual(len(value['events']), 1)
            self.assertEqual(value['current']['last_progress_at'], 100000)

    def test_missing_retirement_inspections_preserve_prior_counts(self):
        from library.activity import Observer
        from activity import write_yaml
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store=setup(self,root)
            # Empty canonical retirement history is valid.
            StatusObservations(store._database).save_retirement( {'results': [{'source': 'source', 'outcome': 'blocked'}]})
            observer = Observer(root, root/'collector', clock=lambda: 1000)
            observer.save()
            with store._database.connection(write=True) as db:db.execute('DELETE FROM retirement_observation')
            stage = observer.snapshot()['stages']['retirement']
            self.assertEqual(stage['counts']['retained acquisitions (last inspection)'], 1)
            self.assertEqual(stage['queue_freshness'], 'unavailable')

    def test_native_fallback_ownership_and_status_enum(self):
        from library.activity import Observer
        from activity import write_yaml
        class Client:
            def call(self, method):
                return [] if method == 'status_downloads' else [
                    {'pid': 99, 'links': [{'fid': 13, 'status': 13}, {'fid': 8, 'status': 8}, {'fid': 9, 'status': 9}]}]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store=setup(self,root)
            CollectorState(root/'collector').submission('source',{'package_id':1})
            CollectorState(root/'collector').capture('source',{'title':'Album'})
            NativeHistory(root/'collector').route('1',{'source':'source','pid':1,'attempt_pid':99})
            observer = Observer(root, root/'collector', clock=lambda: 1000)
            observer.sample_downloads(Client())
            counts = observer.snapshot()['stages']['downloads']['counts']
            self.assertEqual(counts['active links'], 1)
            self.assertEqual(counts['failed links'], 2)

    def test_long_phase_changes_before_cycle_end_and_manual_decision_is_visible(self):
        from library.activity import Observer
        from activity import write_yaml
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store=setup(self,root)
            catalog_fixture(store, {'origins': {'o': {'source': 's', 'sha256': 'asset', 'availability': 'validated'}},
                'assets': {'asset': {'sha256': 'asset', 'resolution': {'status': 'pending'}}}})
            a = Activity(root/'evidence/activity', 'identity', clock=lambda: 1000)
            a.report('working', 'Metadata request', item='asset')
            observer = Observer(root, root/'collector', clock=lambda: 1000)
            first = observer.snapshot()['stages']['identity']
            self.assertEqual(first['operation'], 'Metadata request')
            self.assertEqual(first['items'][0]['reason'], 'Operator decision pending')
            a.clock = lambda: 1010
            a.report('working', 'Publication', item='asset')
            observer.clock = lambda: 1010
            second = observer.snapshot()['stages']['identity']
            self.assertEqual(second['operation'], 'Publication')
            self.assertFalse((root/'evidence/cycle.yaml').exists())
            self.assertIsNone(second['last_progress_at'])

    def test_events_survive_a_real_process_restart(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as folder:
            setup(self,Path(folder))
            script = "from pathlib import Path; from activity import Activity; import sys; Activity(Path(sys.argv[1]), 'collection', clock=lambda: 100000).report('waiting', 'Lock', reason='Processing lock occupied')"
            subprocess.run([sys.executable, '-c', script, folder], check=True)
            before = read_activity(Path(folder), 'collection')['events']
            subprocess.run([sys.executable, '-c', script, folder], check=True)
            self.assertEqual(before, read_activity(Path(folder), 'collection')['events'])

    def test_large_canonical_catalogue_has_an_independent_read_bound(self):
        from library.activity import Observer
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store=setup(self,root)
            catalog_fixture(store,{'origins':{'one':{'availability':'pending'}}})
            with store._database.connection(write=True) as db:
                db.execute("UPDATE origins SET payload=%s WHERE id='one'",(store.prepare({'availability':'pending','cover':'a'*(33*1024*1024)})[0],))
            observer = Observer(root, root/'collector', clock=lambda: 1000)
            stage = observer.snapshot()['stages']['local']
            self.assertEqual(stage['queue_freshness'], 'current')
            self.assertEqual(stage['counts']['waiting files'], 1)
