import json
import os
import subprocess
import sys
import tempfile
import unittest
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from library.catalog import atomic_yaml
from test_library_publish import setup_library
from library.operational_state import CollectorState,RuntimeObservation

def runtime_fixture(store,root):
    (root/"postgresql.json").write_bytes((store.directory/"postgresql.json").read_bytes());(root/"postgresql.json").chmod(0o600)
    from booksys_core.network_admission import RollingBudget
    RollingBudget.create(store.directory/"provider-admission.yaml",requests=50,period=60)
    RuntimeObservation(store.backend()._database).activate({"status":"lab"},role="library")
    RuntimeObservation(store.backend()._database).publish([],at=0,remaining=None,overshoot=None)



class CLITests(unittest.TestCase):





    def test_snapshot_command_only_uses_queue_read(self):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                calls.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'[]')
            def log_message(self, *_):
                pass
        with tempfile.TemporaryDirectory() as folder, ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
            root = Path(folder)
            credentials = root / 'credentials.json'
            credentials.write_text(json.dumps({'api_key': 'fixture-key'}))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            result = subprocess.run([sys.executable, 'ops/library_snapshot.py', '--credentials', str(credentials),
                '--endpoint', f'http://127.0.0.1:{server.server_port}', '--output', str(root / 'queue.yaml')],
                capture_output=True, text=True)
            server.shutdown()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(calls, ['/api/get_queue_data', '/api/get_collector_data'])
            self.assertNotIn('fixture-key', result.stdout)
