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
