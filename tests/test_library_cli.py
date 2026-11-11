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
    def test_preview_bundle_ignores_shared_media_root_and_preserves_source(self):
        from unittest.mock import patch
        from test_library_bundles import comic
        from test_library_intake import SOURCE
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            store,reception,original,asset=setup_library(root,self)
            runtime_fixture(store,root)
            shared=root/'shared-media';shared.mkdir();(shared/'retained').write_bytes(b'unchanged')
            bundle=reception/'package/bundle.zip'
            with zipfile.ZipFile(bundle,'w') as archive:archive.writestr('Album.cbz',comic())
            atomic_yaml(root/'captured.yaml',{'items':{SOURCE:{'title':'Bundle'}}})
            atomic_yaml(root/'submissions.yaml',{'submissions':{SOURCE:{'status':'queued','package_id':1,'name':'tracked'}}})
            atomic_yaml(root/'queue.yaml',[{'pid':1,'name':'tracked','folder':'package','links':[
                {'fid':2,'name':bundle.name,'status':0,'size':bundle.stat().st_size}]}])
            before=store.backend().export()
            files={str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}
            with patch.dict(os.environ,BOOKSYS_BUNDLE_INPUT_ROOT=str(shared)):
                process=subprocess.run([sys.executable,'-m','library.cli','--state',str(store.directory),
                    'preview','--output',str(root/'preview'),'--reception',str(reception),
                    '--captured',str(root/'captured.yaml'),'--submissions',str(root/'submissions.yaml'),
                    '--queue',str(root/'queue.yaml')],capture_output=True,text=True)
            self.assertEqual(process.returncode,0,process.stdout+process.stderr)
            self.assertEqual(store.backend().export(),before)
            self.assertEqual({str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*')
                if p.is_file() and not p.is_relative_to(root/'preview')},files)
            self.assertTrue(any((root/'preview/state/bundle-inputs').glob('*/manifest.yaml')))
            preview=yaml.safe_load((root/'preview/state/library.yaml').read_text())
            self.assertGreater(len(preview['origins']),len(store.read()['origins']))

    def test_preview_projects_indexed_decision_without_committing(self):
        from library.cli import parser,run
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);store,reception,_,asset=setup_library(root,self)
            record=store.append_decision({'action':'override','target':asset,
                'values':{'title':'Accepted pending title'},'author':'fixture','time':'2026-11-09T00:00:00Z'})
            for name,value in [('captured',{'items':{}}),('submissions',{'submissions':{}}),('queue',[])]:
                atomic_yaml(root/(name+'.yaml'),value)
            before=store.backend().export()
            files={str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}
            args=parser().parse_args(['--state',str(store.directory),'preview','--output',str(root/'preview'),
                '--reception',str(reception),'--captured',str(root/'captured.yaml'),
                '--submissions',str(root/'submissions.yaml'),'--queue',str(root/'queue.yaml')])
            self.assertEqual(run(args)['status'],'preview_only')
            preview=yaml.safe_load((root/'preview/state/library.yaml').read_text())
            self.assertEqual(preview['assets'][asset]['overrides']['title'],'Accepted pending title')
            self.assertEqual(preview['ledger'],[record['id']])
            self.assertEqual(store.backend().export(),before)
            self.assertEqual({str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*')
                if p.is_file() and not p.is_relative_to(root/'preview')},files)

    def test_library_activation_preserves_stage_activation(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);store,_,_,_=setup_library(root,self)
            observed=RuntimeObservation(store.backend()._database)
            observed.activate({'qualification':'stage'})
            observed.activate({'qualification':'library'},role='library')
            self.assertEqual(observed.activation(),{'qualification':'stage'})
            self.assertEqual(observed.activation(role='library'),{'qualification':'library'})

    def test_explicit_lab_lifecycle_and_path_guards(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertTrue(root.is_relative_to('/data'))
            store, reception, original, asset = setup_library(root,self)
            runtime_fixture(store,root)
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
            store, reception, original, asset = setup_library(root,self)
            runtime_fixture(store,root)
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
                    CollectorState(root).capture(SOURCE,{'title':'Album'})
                    CollectorState(root).submission(SOURCE,{'status':'queued','name':'tracked','package_id':1})
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
            root = Path(folder);store, reception, original, asset = setup_library(root,self)
            runtime_fixture(store,root)
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
            store, reception, original, asset = setup_library(root,self)
            runtime_fixture(store,root)
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
            from library.operational_state import CopyOperations
            withdrawn=next(op for op in CopyOperations(store.backend()._database,'publication').records() if op['kind']=='withdraw')
            cli('restore','--operation',withdrawn['id'],'--expected',str(store.revision),*common)
            cli('preflight', '--reception', reception, '--library', root / 'library', code=2)
            atomic_yaml(root / 'captured.yaml', {'items': {}})
            atomic_yaml(root / 'submissions.yaml', {'submissions': {}})
            atomic_yaml(root / 'queue.yaml', [])
            inputs = ['--reception', reception, '--captured', root / 'captured.yaml',
                      '--submissions', root / 'submissions.yaml', '--queue', root / 'queue.yaml']
            before = store.backend().export()
            cli('preview', '--output', root / 'preview', *inputs)
            self.assertEqual(store.backend().export(),before)
            cli('intake', '--isolated', *inputs)
            self.assertTrue((root / 'preview/review.yaml').exists())
