"""Operator search shares fixed filters and reports partial history explicitly."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from booksys_core.diagnostics import event
from library.diagnostic_reader import DiagnosticReader, MemorySource


class DiagnosticCLITests(unittest.TestCase):
    def test_compose_mode_uses_only_fixed_remote_sources(self):
        from library.diagnostic_cli import main
        from library.log_sources import COMPOSE_ROLES
        from unittest.mock import patch
        from datetime import datetime,timezone,timedelta
        now=datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as folder:
            def source(name,state):
                self.assertEqual(state,Path(folder)/'state')
                value=MemorySource(name,'retained',[])
                value.refresh=lambda:None
                return value
            with patch('library.compose_diagnostics.RemoteSource',side_effect=source) as remote, patch('library.compose_diagnostics.refresh_sources') as refresh, patch('library.log_sources.LiveSource',side_effect=AssertionError('No host Docker/journal access')),redirect_stdout(io.StringIO()):
                code=main(['--root',folder,'--compose','--from',(now-timedelta(seconds=10)).isoformat(),'--to',now.isoformat()])
            self.assertEqual(code,0)
            refresh.assert_called_once()
            self.assertEqual([call.args[0] for call in remote.call_args_list],list(COMPOSE_ROLES))

    def test_bounded_utc_search_exports_only_jsonl_and_rejects_local_time(self):
        from library.diagnostic_cli import main
        with tempfile.TemporaryDirectory() as folder:
            reader = DiagnosticReader([MemorySource('worker', 'one', [(100, event(
                service='collector', stage='collection', code='completed', reason='none'))])],
                Path(folder) / 'reader.lock', clock=lambda: 200)
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(['--from', '1970-01-01T00:01:30Z', '--to', '1970-01-01T00:02:00Z'], reader=reader)
            self.assertEqual(code, 0)
            rows = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(rows[0]['state'], 'complete')
            self.assertEqual(rows[1]['code'], 'completed')
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--from', '1970-01-01T00:01:30', '--to', '1970-01-01T00:02:00'], reader=reader), 1)
