"""A fixed operation still needs its existing queue reservation and ownership."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import os
import tempfile
import unittest
import uuid
from unittest.mock import patch
import subprocess
from library.primitives import atomic_yaml,empty,LibraryError
from postgres_fixture import create_store
from library.codec import encode

from library.decoder_ownership import DecoderOwnership
from library.reader_jobs import JobError
from library.reader_service import GrantedLauncher
from library.stage_resources import Resources, Demand


class GrantedReaderTests(unittest.TestCase):
    def database(self,root):
        store=create_store(self,root/'state',empty())
        self.enterContext(patch.dict(os.environ,{'BOOKSYS_LAUNCHER_DATABASE_DIR':str(root/'state')}))
        return store

    def retain_fixture(self,store,reference,kind,value):
        # Deliberately seed/corrupt only this test's historical phase records.
        path=store.directory.parent/'jobs'/reference/(kind+'.yaml')
        atomic_yaml(path,value)
        retained={**value,'_evidence':{'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}}
        with store._database.connection(write=True) as db:
            db.execute('INSERT INTO reader_job_state VALUES (%s,%s,%s) '
                'ON CONFLICT(job,kind) DO UPDATE SET payload=excluded.payload',(reference,kind,encode(retained)))

    def request_fixture(self,store,reference):
        from library.operational_state import ReaderJobs
        ReaderJobs(store._database).request(reference,'historical:1',
            {'operation':'validate','input':'downloads/book.cbz','sha256':'0'*64,'payload':{}})

    def test_missing_worker_requires_exact_terminal_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('inputs', 'jobs', 'state'):
                (root/name).mkdir()
            store=self.database(root)
            reference = uuid.uuid4().hex
            job = root/'jobs'/reference
            job.mkdir(mode=0o700)
            intent = {'schema': 1, 'name': 'booksys-reader-'+reference,
                      'image': 'sha256:'+'0'*64, 'manifest_sha256': '1'*64}
            self.request_fixture(store,reference)
            self.retain_fixture(store,reference,'intent',intent)
            self.retain_fixture(store,reference,'container',{**intent,'container':'2'*64})
            launcher = GrantedLauncher(root/'inputs', root/'jobs', 'sha256:'+'3'*64,
                                       root/'state', job_uid=os.getuid(), cgroup_parent='owned-test.slice')
            self.assertFalse(launcher.remove_stopped)
            receipt = {**intent, 'container': '2'*64, 'stopped': True,
                       'status': 'completed', 'worker_state': {'Running': False}}
            missing = subprocess.CompletedProcess([], 1, b'', b'Error: No such object: worker')
            with patch('library.reader_service.subprocess.run', return_value=missing) as engine:
                with self.assertRaises(JobError):
                    launcher.reconcile(reference)
                for status in ('completed', 'worker_failed', 'blocked'):
                    self.retain_fixture(store,reference,'receipt',{**receipt,'status':status})
                    self.assertEqual(launcher.reconcile(reference),
                                     {'stopped': True, 'status': 'retained_terminal'})
                for change in ({'container': '4'*64}, {'image': 'sha256:'+'5'*64},
                               {'manifest_sha256': '6'*64}, {'stopped': False},
                               {'worker_state': {'Running': True}}, {'status': 'unknown'}):
                    self.retain_fixture(store,reference,'receipt',{**receipt,**change})
                    with self.subTest(change=change), self.assertRaises(JobError):
                        launcher.reconcile(reference)
                self.retain_fixture(store,reference,'receipt',receipt)
                engine.return_value = subprocess.CompletedProcess([], 1, b'', b'Cannot connect to daemon')
                with self.assertRaises(JobError):
                    launcher.reconcile(reference)
                self.assertTrue(all(c.args[0][1] == 'inspect' for c in engine.call_args_list))

    def test_reconcile_retains_exact_previous_image_after_upgrade(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('inputs', 'jobs', 'state'):
                (root/name).mkdir()
            store=self.database(root)
            reference = uuid.uuid4().hex
            job = root/'jobs'/reference
            job.mkdir(mode=0o700)
            image = 'sha256:'+'0'*64
            name = 'booksys-reader-'+reference
            intent={'name':name,'image':image,'manifest_sha256':'1'*64}
            self.request_fixture(store,reference)
            self.retain_fixture(store,reference,'intent',intent)
            self.retain_fixture(store,reference,'container',{**intent,'container':'2'*64})
            launcher = GrantedLauncher(root/'inputs', root/'jobs', 'sha256:'+'3'*64, root/'state', job_uid=os.getuid(), cgroup_parent='owned-test.slice')
            worker = {'Image': image, 'Id': '2'*64, 'Config': {'Labels': {'booksys.reader.job': reference, 'booksys.reader.manifest': '1'*64}}}
            results = [subprocess.CompletedProcess([],0,json.dumps(worker).encode(),b''), subprocess.CompletedProcess([],0,b'',b''), subprocess.CompletedProcess([],0,b'{"Running":false}',b'')]
            with patch('library.reader_service.subprocess.run', side_effect=results) as engine:
                self.assertTrue(launcher.reconcile(reference)['stopped'])
                self.assertEqual(engine.call_args_list[1].args[0][-1], '2'*64)

    def test_reconcile_refuses_replaced_worker_before_sending_stop(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('inputs', 'jobs', 'state'):
                (root/name).mkdir()
            store=self.database(root)
            reference = uuid.uuid4().hex
            job = root/'jobs'/reference
            job.mkdir(mode=0o700)
            image = 'sha256:'+'0'*64
            self.request_fixture(store,reference)
            self.retain_fixture(store,reference,'intent',{'name':'booksys-reader-'+reference,
                'image':image,'manifest_sha256':'1'*64})
            launcher = GrantedLauncher(root/'inputs', root/'jobs', image, root/'state',
                job_uid=os.getuid(), cgroup_parent='owned-test.slice')
            replaced = {'Image': image, 'Id': '2'*64, 'Config': {'Labels': {}}}
            with patch('library.reader_service.subprocess.run', return_value=subprocess.CompletedProcess(
                    [], 0, json.dumps(replaced).encode(), b'')) as engine:
                with self.assertRaises(JobError):
                    launcher.reconcile(reference)
            self.assertEqual(engine.call_count, 1)
            self.assertEqual(engine.call_args.args[0][1], 'inspect')

    def test_reader_cannot_start_without_complete_grant_and_exact_registration(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            inputs, jobs, state = (root/name for name in ('inputs', 'jobs', 'state'))
            for directory in (inputs, jobs, state/'stage-children', state/'reader-requests'):
                directory.mkdir(parents=True, mode=0o700)
            store=self.database(root)
            reference = uuid.uuid4().hex
            pending = state/'reader-requests'/reference
            pending.mkdir(mode=0o700)
            from library.stage_tasks import StageTasks,Obligation
            from library.operational_state import ReaderJobs
            tasks=StageTasks(store);stamp=datetime(2026,11,9,tzinfo=timezone.utc)
            task=tasks.discover(Obligation('ocr','fixture','input','method'),now=stamp)
            task=tasks.start(task.key,task.generation,now=stamp,resources=True)
            key=task.key+':'+str(task.generation)
            ReaderJobs(store._database).request(reference,key,
                {'operation':'validate','input':'downloads/book.cbz','sha256':'0'*64,'payload':{}})
            (pending/'grant.json').write_text(json.dumps({'key': key}))
            resource = Resources.create(state/'stage-resources.yaml')
            owner = DecoderOwnership.create(state/'stage-children'/(key+'.yaml'))
            launcher = GrantedLauncher(inputs, jobs, 'sha256:'+'0'*64, state,
                                       job_uid=os.getuid(), cgroup_parent='owned-test.slice')
            request = {'operation': 'validate', 'job': reference}
            for stage in ('absent', 'insufficient', 'unregistered'):
                if stage != 'absent':
                    resource.admit(key, age=datetime(2026, 11, 9, tzinfo=timezone.utc),
                        demand=Demand(memory_mib=256 if stage == 'insufficient' else 1792,
                            cpu_millis=1250, disk=1, output_bytes=4*1024**3),
                        free_bytes=100*1024**3, external_bytes=0)
                with self.subTest(stage=stage), self.assertRaises((JobError,LibraryError)):
                    launcher.execute(request)
                resource.release(key, stopped=True, children_stopped=True)
                self.assertFalse((jobs/reference).exists())
            self.assertEqual(owner.names(), [])

    def test_reconcile_unknown_job_cannot_select_a_container_or_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('inputs', 'jobs', 'state'):
                (root/name).mkdir()
            store=self.database(root)
            launcher = GrantedLauncher(root/'inputs', root/'jobs', 'sha256:'+'0'*64, root/'state',
                                       job_uid=os.getuid(), cgroup_parent='owned-test.slice')
            for reference in ('../other', 'booksys-worker-1', '/var/run/docker.sock'):
                with self.subTest(reference=reference), self.assertRaises(JobError):
                    launcher.reconcile(reference)


class ReaderIPCAdmissionTests(unittest.TestCase):
    def test_busy_reader_refuses_only_an_unstarted_distinct_job(self):
        import socket
        import threading
        from types import SimpleNamespace
        from library.ipc import Channel
        from library.reader_service import ReaderHandler
        entered, release = threading.Event(), threading.Event()
        calls = []
        def execute(request):
            calls.append(request['job'])
            entered.set()
            if not release.wait(2):
                raise RuntimeError('fixture did not release worker')
            return {'stopped': True, 'status': 'completed'}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            launcher = SimpleNamespace(execute=execute, jobs=root)
            handler = ReaderHandler(launcher, root, 'sha256:'+'0'*64)
            def dispatch(reference, operation='validate'):
                client, server = socket.socketpair()
                thread = threading.Thread(target=handler.handle, args=(server,))
                thread.start()
                channel = Channel(client)
                channel.send({'operation': operation, 'job': reference})
                return client, channel, thread
            (root/('c'*32)).mkdir()
            client, first, thread = dispatch('a'*32)
            self.assertTrue(entered.wait(1))
            try:
                for reference, expected in (('b'*32, 'reader_busy'),
                        ('a'*32, 'Reader unavailable; reconcile retained job before retry'),
                        ('c'*32, 'Reader unavailable; reconcile retained job before retry')):
                    with self.subTest(reference=reference):
                        other, channel, other_thread = dispatch(reference)
                        with other:
                            self.assertEqual(channel.receive()['error'], expected)
                        other_thread.join(1)
                other, channel, other_thread = dispatch('b'*32, 'reconcile')
                with other:
                    response = channel.receive()
                    self.assertIsNone(response['error'])
                    self.assertEqual(response['result'], {'stopped': True, 'status': 'not_started'})
                other_thread.join(1)
                self.assertEqual(calls, ['a'*32])
            finally:
                release.set()
                with client:
                    self.assertIsNone(first.receive()['error'])
                thread.join(1)

    def test_busy_response_is_retryable_but_unknown_reply_stays_uncertain(self):
        import socket
        import threading
        from library.ipc import Channel
        from library.reader_client import request
        from library.reader_inputs import ReaderUnavailable
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'reader.sock'
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(str(path))
                listener.listen(1)
                def reply(error):
                    connection, _ = listener.accept()
                    with connection:
                        channel = Channel(connection)
                        channel.receive()
                        channel.send({'result': None, 'error': error})
                for code, message in (('reader_busy', 'Reader busy; request did not start'),
                        ('reader error', 'Reader unavailable; retain job and grant for reconciliation')):
                    thread = threading.Thread(target=reply, args=(code,))
                    thread.start()
                    try:
                        with patch.dict(os.environ, {'BOOKSYS_READER_SOCKET': str(path)}):
                            with self.assertRaises(ReaderUnavailable) as raised:
                                request({'operation': 'validate', 'job': 'b'*32})
                        self.assertEqual(str(raised.exception), message)
                        self.assertEqual(type(raised.exception).__name__, 'ReaderBusy' if code == 'reader_busy' else 'ReaderUnavailable')
                    finally:
                        thread.join(1)

    def test_busy_readiness_does_not_report_engine_recovery(self):
        from library.reader_client import available
        with patch('library.reader_client.request', return_value={'available': True, 'idle': False}):
            self.assertTrue(available())
            self.assertFalse(available(idle=True))

    def test_refused_public_execute_retains_registration_and_proves_cleanup_while_other_runs(self):
        import socket
        import threading
        from types import SimpleNamespace
        from library.reader_service import ReaderHandler
        from library.reader_client import execute
        from library.reader_inputs import ReaderBusy
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            inputs, jobs, requests = (root/name for name in ('inputs', 'jobs', 'requests'))
            for directory in (inputs, jobs, requests):
                directory.mkdir()
            source = inputs/'book.cbz'
            source.write_bytes(b'reader input')
            state=root/'state'
            create_store(self,state,empty())
            owner = DecoderOwnership.create(state/'stage-children'/('b'*64+':1.yaml'))
            handler = ReaderHandler(SimpleNamespace(jobs=jobs), root, 'sha256:'+'0'*64)
            handler.active_job = 'a'*32
            path = root/'reader.sock'
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(str(path))
                listener.listen(2)
                def serve():
                    for _ in range(2):
                        connection, _ = listener.accept()
                        handler.handle(connection)
                thread = threading.Thread(target=serve)
                thread.start()
                try:
                    with patch.dict(os.environ, {'BOOKSYS_READER_SOCKET': str(path),
                            'BOOKSYS_READER_INPUTS': str(inputs), 'BOOKSYS_READER_JOBS': str(jobs),
                            'BOOKSYS_READER_REQUESTS': str(requests)}):
                        with owner.track(), self.assertRaises(ReaderBusy):
                            execute('validate', source)
                        self.assertEqual(len(owner.names()), 1)
                        self.assertEqual(len(list(requests.iterdir())), 1)
                        self.assertEqual(list(jobs.iterdir()), [])
                        self.assertTrue(owner.cleanup())
                    self.assertEqual(handler.active_job, 'a'*32)
                finally:
                    thread.join(2)
                self.assertFalse(thread.is_alive())


class DiagnosticIPCDeadlineTests(unittest.TestCase):
    def test_server_budget_exhaustion_stays_a_continuation_through_actual_ipc(self):
        import socket
        import threading
        from types import SimpleNamespace
        from library.reader_service import ReaderHandler
        from library.reader_client import request
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);path=root/'reader.sock'
            handler=ReaderHandler(SimpleNamespace(),root,'sha256:'+'0'*64)
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(str(path));listener.listen(1)
                def serve():
                    connection,_=listener.accept()
                    handler.handle(connection)
                with patch.dict(os.environ,{'BOOKSYS_READER_SOCKET':str(path),'BOOKSYS_COMPOSE_PROJECT':'isolated'}), patch(
                        'library.compose_diagnostics.read_request',side_effect=TimeoutError('server budget')):
                    thread=threading.Thread(target=serve);thread.start()
                    try:
                        with self.assertRaises(TimeoutError):
                            request({'operation':'diagnostics','job':'a'*32},timeout=1)
                    finally:thread.join(2)
                self.assertFalse(thread.is_alive())
