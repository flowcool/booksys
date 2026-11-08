import json
import os
import subprocess
import sys
import tempfile
import unittest
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from library.catalog import atomic_yaml
from test_library_publish import setup_library


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
            self.assertEqual(calls, ['/api/get_queue_data'])
            self.assertNotIn('fixture-key', result.stdout)
    def test_tool_lifecycle_and_safe_preview(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store, reception, original, asset = setup_library(root)
            def cli(*arguments, code=0):
                process = subprocess.run([sys.executable, '-m', 'library.cli', '--state', str(store.directory),
                    *map(str, arguments)], capture_output=True, text=True)
                self.assertEqual(process.returncode, code, process.stdout + process.stderr)
                return json.loads(process.stdout)
            cli('status')
            cli('replay')
            cli('report', '--output', root / 'review.yaml')
            cases = yaml.safe_load((root / 'review.yaml').read_text())['cases']
            case = next(iter(cases.values()))
            atomic_yaml(root / 'batch.yaml', {'schema': 1, 'requests': [{'request_id': 'cli-override',
                'case': case['id'], 'version': case['version'], 'action': 'override',
                'values': {'summary': 'Operator correction'}}]})
            cli('decide', '--batch', root / 'batch.yaml', '--author', 'flowcool')
            common = ['--isolated', '--reception', reception, '--library', root / 'library']
            cli('preflight', *common)
            cli('activate', *common)
            cli('enrich', '--asset', asset, '--budget', '0')
            published = cli('publish', '--asset', asset, *common)
            cli('reconcile', *common)
            cli('withdraw', '--edition', 'edition', '--expected', str(store.read()['revision']), *common)
            withdrawn = [p for p in (store.directory / 'library-operations').glob('*.yaml')
                         if yaml.safe_load(p.read_text())['kind'] == 'withdraw'][0]
            cli('restore', '--operation', withdrawn.stem, '--expected', str(store.read()['revision']), *common)
            cli('preflight', '--reception', reception, '--library', root / 'library', code=2)
            atomic_yaml(root / 'captured.yaml', {'items': {}})
            atomic_yaml(root / 'submissions.yaml', {'submissions': {}})
            atomic_yaml(root / 'queue.yaml', [])
            inputs = ['--reception', reception, '--captured', root / 'captured.yaml',
                      '--submissions', root / 'submissions.yaml', '--queue', root / 'queue.yaml']
            before = store.path.read_bytes()
            cli('preview', '--output', root / 'preview', *inputs)
            self.assertEqual(store.path.read_bytes(), before)
            cli('intake', '--isolated', *inputs)
            self.assertTrue((root / 'preview/review.yaml').exists())
