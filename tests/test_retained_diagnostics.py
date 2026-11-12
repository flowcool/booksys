"""Trusted retained dependencies remain observable across Compose projects."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from library.log_sources import LiveSource


class RetainedDiagnosticTests(unittest.TestCase):
    def test_dependency_requires_exact_id_and_original_labels(self):
        identity = 'a'*64
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder)/'retained.json'
            config.write_text(json.dumps({'pyload': {'id': identity, 'project': 'old-lab', 'service': 'pyload'}}))
            for owner, observed_id, available in [('old-lab', identity, True), ('candidate', identity, False), ('old-lab', 'b'*64, False)]:
                with self.subTest(owner=owner, identity=observed_id):
                    calls = []
                    def run(argv, **kwargs):
                        calls.append(argv)
                        return subprocess.CompletedProcess(argv, 0, json.dumps({'Id': observed_id, 'Config': {'Labels': {'com.docker.compose.project': owner, 'com.docker.compose.service': 'pyload'}}}).encode())
                    with patch.dict(os.environ, {'BOOKSYS_RETAINED_SOURCES_FILE': str(config)}), patch('library.log_sources.subprocess.run', side_effect=run):
                        source = LiveSource('pyload', project='candidate')
                        source.refresh()
                    self.assertEqual(source.available, available)
                    self.assertEqual(calls, [['docker', 'inspect', '--format', '{{json .}}', identity]])

    def test_config_cannot_override_business_roles_or_accept_partial_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder)/'retained.json'
            for value in [{'controller': {'id': 'a'*64, 'project': 'old-lab', 'service': 'controller'}}, {'pyload': {'id': 'a'*12, 'project': 'old-lab', 'service': 'pyload'}}]:
                config.write_text(json.dumps(value))
                with patch.dict(os.environ, {'BOOKSYS_RETAINED_SOURCES_FILE': str(config)}):
                    with self.assertRaises(ValueError):
                        LiveSource('pyload', project='candidate')


class DiagnosticDeadlineTests(unittest.TestCase):
    def test_server_uses_one_deadline_for_sql_inspect_and_logs(self):
        from library.compose_diagnostics import read_request
        from library.locking import acquisition_deadline
        clock=[10.]
        value={'role':'pyload','start':100,'end':200,'position':None,'lines':1,
               'byte_limit':100,'generation':'a'*64}
        observed=[]
        def sql_request(reference):
            observed.append(acquisition_deadline.get())
            clock[0]+=.12
            return value
        def refresh(*,deadline):
            observed.append(deadline)
            clock[0]+=.1
        def scan(*args,deadline,**kwargs):
            observed.append(deadline)
            self.assertAlmostEqual(deadline-clock[0],.03)
            return {'done':True}
        with patch.dict(os.environ,{'BOOKSYS_LAUNCHER_DATABASE_DIR':'/unused'}), patch(
                'library.compose_diagnostics.time.monotonic',side_effect=lambda:clock[0]), patch(
                'library.compose_diagnostics.time.time',return_value=200), patch(
                'library.postgres.Database.configured'), patch(
                'library.operational_state.ComposeDiagnostics.request',side_effect=sql_request), patch(
                'library.compose_diagnostics.LiveSource') as source:
            source.return_value.refresh.side_effect=refresh
            source.return_value.available=True
            source.return_value.generation='a'*64
            source.return_value.scan.side_effect=scan
            self.assertEqual(read_request(Path('/unused'),'b'*32,'candidate'),{'done':True})
        self.assertEqual(observed,[10.25]*3)

    def test_refresh_budget_expiry_is_not_source_loss(self):
        import time
        with patch('library.log_sources.subprocess.run') as engine:
            source=LiveSource('pyload',project='isolated')
            with self.assertRaises(TimeoutError):
                source.refresh(deadline=time.monotonic()-1)
            engine.assert_not_called()

    def test_eof_before_process_exit_preserves_budget_timeout(self):
        import time
        from unittest.mock import Mock
        process=Mock(stdout=None,stderr=None)
        process.poll.return_value=0
        process.wait.side_effect=[subprocess.TimeoutExpired('docker logs',.01),0]
        source=LiveSource('pyload',project='isolated')
        source.available=True
        source.container='a'*64
        with patch('library.log_sources.subprocess.Popen',return_value=process):
            with self.assertRaises(TimeoutError):
                source.scan(100,200,None,lines=1,byte_limit=100,deadline=time.monotonic()+.01)
        self.assertEqual(process.wait.call_count,2)

    def test_sql_busy_before_fractional_deadline_preserves_continuation(self):
        from library.compose_diagnostics import RemoteSource,read_request
        from library.primitives import LibraryError
        clock=[10.]
        def busy(*args):
            clock[0]=10.2495
            raise LibraryError('storage_busy')
        with patch.dict(os.environ,{'BOOKSYS_LAUNCHER_DATABASE_DIR':'/unused'}), patch(
                'library.compose_diagnostics.time.monotonic',side_effect=lambda:clock[0]), patch(
                'library.postgres.Database.configured'), patch('library.compose_diagnostics.request') as ipc, patch(
                'library.operational_state.ComposeDiagnostics.retain',side_effect=busy), patch(
                'library.operational_state.ComposeDiagnostics.request',side_effect=busy), patch(
                'library.compose_diagnostics.LiveSource') as live:
            for seam in ('client','server'):
                clock[0]=10.
                with self.subTest(seam=seam),self.assertRaises(TimeoutError):
                    if seam=='client':
                        RemoteSource('pyload',Path('/unused')).scan(100,200,None,lines=1,byte_limit=100,deadline=10.25)
                    else:
                        read_request(Path('/unused'),'b'*32,'candidate')
            ipc.assert_not_called()
            live.assert_not_called()

    def test_real_sql_statement_budget_is_a_continuation(self):
        import time
        from postgres_fixture import create_store
        from library.primitives import empty
        from library.compose_diagnostics import read_request
        with tempfile.TemporaryDirectory() as folder:
            state=Path(folder)
            store=create_store(self,state,empty())
            def slow(reference):
                with store._database.connection() as db:
                    db.execute('SELECT pg_sleep(1)')
            with patch.dict(os.environ,{'BOOKSYS_LAUNCHER_DATABASE_DIR':str(state)}), patch(
                    'library.operational_state.ComposeDiagnostics.request',side_effect=slow):
                started=time.monotonic()
                with self.assertRaises(TimeoutError):read_request(state,'b'*32,'candidate')
                self.assertLess(time.monotonic()-started,.5)

    def test_uncertain_commit_is_not_reclassified_as_budget_exhaustion(self):
        from library.compose_diagnostics import RemoteSource,read_request
        from library.postgres import CommitUnknown
        clock=[10.]
        def uncertain(*args):
            clock[0]=11.
            raise CommitUnknown('commit_unresolved')
        with patch.dict(os.environ,{'BOOKSYS_LAUNCHER_DATABASE_DIR':'/unused'}), patch(
                'library.compose_diagnostics.time.monotonic',side_effect=lambda:clock[0]), patch(
                'library.postgres.Database.configured'), patch(
                'library.operational_state.ComposeDiagnostics.retain',side_effect=uncertain), patch(
                'library.operational_state.ComposeDiagnostics.request',side_effect=uncertain):
            with self.assertRaises(OSError) as result:
                RemoteSource('pyload',Path('/unused')).scan(100,200,None,lines=1,byte_limit=100,deadline=10.25)
            self.assertIsInstance(result.exception.__cause__,CommitUnknown)
            clock[0]=10.
            with self.assertRaises(CommitUnknown):read_request(Path('/unused'),'b'*32,'candidate')
