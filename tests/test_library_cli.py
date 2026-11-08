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


class CLITests(unittest.TestCase):
    def test_explicit_lab_lifecycle_and_path_guards(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertTrue(root.is_relative_to('/data'))
            store, reception, original, asset = setup_library(root)
            original_bytes = original.read_bytes()
            def cli(*args, state=None, code=0):
                result = subprocess.run([sys.executable, '-m', 'library.cli', '--state',
                    str(state or store.directory), *map(str, args)], capture_output=True, text=True)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                return json.loads(result.stdout) if result.stdout else None
            common = ['--lab', '--reception', reception, '--library', root / 'library']
            qualification = cli('activate', *common)
            self.assertEqual(qualification['status'], 'lab')
            self.assertNotIn('backup_verified', qualification)
            cli('publish', '--asset', asset, *common)
            cli('reconcile', *common)
            cli('publish', '--asset', asset, *common)
            self.assertEqual(original.read_bytes(), original_bytes)
            cli('preflight', *common, state=reception / 'state', code=2)
            for private in ('state', 'secrets', 'downloads', ''):
                cli('preflight', *common, state='/data/booksys/' + private, code=2)
            cli('preflight', *common, state=root / 'library/state', code=2)
            cli('preflight', *common, state='/home/operator/forbidden-lab-state', code=2)
            link = root / 'linked'; link.symlink_to(root / 'state', target_is_directory=True)
            cli('preflight', *common, state=link, code=2)
            cli('preflight', '--isolated', '--reception', '/data/booksys/downloads',
                '--library', root / 'library', code=2)
            cli('preflight', '--isolated', *common, code=2)
            for name, value in [('captured', {'items': {}}), ('submissions', {'submissions': {}}), ('queue', [])]:
                atomic_yaml(root / (name + '.yaml'), value)
            inputs = ['--captured', root / 'captured.yaml', '--submissions', root / 'submissions.yaml',
                '--queue', root / 'queue.yaml']
            cli('intake', *common, *inputs)
            cli('intake', *common, *inputs, state=root / 'captured.yaml/state', code=2)
            cli('intake', '--lab', '--reception', reception, '--library', root / 'captured.yaml/library',
                *inputs, code=2)


    def test_reusable_lab_cycle_accounts_for_pending_inputs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store, reception, original, asset = setup_library(root)
            for name, value in [('captured', {'items': {}}), ('submissions', {'submissions': {}}), ('queue', [])]:
                atomic_yaml(root / (name + '.yaml'), value)
            command = [sys.executable, 'ops/library_cycle.py', '--root', str(root),
                '--reception', str(reception), '--captured', str(root / 'captured.yaml'),
                '--submissions', str(root / 'submissions.yaml'), '--queue', str(root / 'queue.yaml'),
                '--asset', asset, '--budget', '0']
            before = original.read_bytes()
            first_operation = None
            for iteration in range(2):
                if iteration:
                    from test_library_intake import SOURCE
                    second = reception / 'package/pending.cbz'
                    with zipfile.ZipFile(second, 'w') as archive, zipfile.ZipFile(original) as first:
                        archive.writestr('001.png', first.read('001.png'))
                        archive.writestr('002.png', first.read('001.png'))
                    atomic_yaml(root / 'captured.yaml', {'items': {SOURCE: {'title': 'Album'}}})
                    atomic_yaml(root / 'submissions.yaml', {'submissions': {SOURCE: {
                        'status': 'queued', 'name': 'tracked', 'package_id': 1}}})
                    atomic_yaml(root / 'queue.yaml', [{'pid': 1, 'name': 'tracked', 'folder': 'package',
                        'links': [{'fid': 1, 'name': original.name, 'status': 0, 'size': original.stat().st_size},
                                  {'fid': 2, 'name': second.name, 'status': 0, 'size': second.stat().st_size}]}])
                result = subprocess.run(command, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                summary = json.loads(result.stdout)
                self.assertEqual(summary['published'], 1)
                self.assertEqual(summary['pending_assets'], iteration)
                operation = store.read()['editions']['edition']['publication']['operation']
                if first_operation is None:
                    first_operation = operation
                self.assertEqual(operation, first_operation)
            from library.catalog import apply_effect
            current = store.read()
            other = next(key for key in current['assets'] if key != asset)
            apply_effect(current, {'action': 'identify', 'target': other, 'values': {
                'work_id': 'work', 'edition_id': 'edition', 'work': {'title': 'Album'},
                'edition': {'publisher': 'Publisher', 'language': 'fr',
                            'evidence': {'operator': 'fixture', 'edition': 'labelled'}}}})
            store.save(current, expected=current['revision'])
            result = subprocess.run(command + ['--asset', other], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)['pending_assets'], 1)
            self.assertEqual(store.read()['editions']['edition']['publication']['operation'], first_operation)
            self.assertEqual(original.read_bytes(), before)
            self.assertTrue((root / 'evidence/review.yaml').exists())

    def test_automatic_entrypoint_and_nonoverlapping_cycle(self):
        import importlib.util
        import contextlib
        import io
        from unittest.mock import patch
        from library.catalog import lock
        from test_library_intake import SOURCE
        spec = importlib.util.spec_from_file_location('cycle_test', 'ops/library_cycle.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder);store, reception, original, asset = setup_library(root)
            state = store.read();state['assets'][asset]['identity_result'] = {'status': 'identified', 'reason': 'Fixture proved edition'}
            store.save(state, expected=state['revision'])
            atomic_yaml(root / 'captured.yaml', {'items': {SOURCE: {'title': 'Album'}}})
            atomic_yaml(root / 'submissions.yaml', {'submissions': {SOURCE: {'status': 'queued', 'name': 'tracked', 'package_id': 1}}})
            atomic_yaml(root / 'queue.yaml', [{'pid': 1, 'name': 'tracked', 'folder': 'package', 'links': [
                {'fid': 1, 'name': original.name, 'status': 0, 'size': original.stat().st_size},
                {'fid': 2, 'name': 'not-finished.cbz', 'status': 2, 'size': 10}]}])
            argv = ['cycle', '--auto', '--root', str(root), '--reception', str(reception),
                '--captured', str(root / 'captured.yaml'), '--submissions', str(root / 'submissions.yaml'),
                '--queue', str(root / 'queue.yaml'), '--budget', '50']
            with patch('sys.argv', argv), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(module.main(), 0)
                self.assertEqual(json.loads(output.getvalue())['published'], 1)
                operation = store.read()['assets'][asset]['admission']['operation']
                with patch('library.intake.inspect_file', side_effect=AssertionError('repeated validation')):
                    self.assertEqual(module.main(), 0)
                self.assertEqual(store.read()['assets'][asset]['admission']['operation'], operation)
                with lock(store.directory / 'library-cycle.lock'):
                    with contextlib.redirect_stdout(io.StringIO()) as skipped:
                        self.assertEqual(module.main(), 0)
                    self.assertEqual(json.loads(skipped.getvalue())['status'], 'skipped')
            self.assertEqual(len(store.read()['assets']), 1)
            self.assertTrue((root / 'evidence/dashboard.yaml').exists())

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
