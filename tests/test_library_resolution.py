import copy
import tempfile
import unittest
from pathlib import Path

from library.catalog import LibraryError
from library.resolution import Resolution
from test_library_publish import setup_library


class ResolutionTests(unittest.TestCase):
    def test_preview_confirm_is_ledger_only_idempotent_and_stale_on_new_assertion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, reception, original, asset = setup_library(root)
            service = Resolution(store, reception)
            page = service.page(asset, 1)
            values = {'edition_id': 'edition', 'work_id': 'work', 'work': {}, 'edition': {},
                      'select': True, 'proof': [{'page': page['id'], 'field': field, 'text': 'Checked visible ' + field} for field in ('title', 'publisher', 'language')],
                      'reason': 'Publisher and language checked', 'supersedes': None, 'restore': None}
            preview = service.preview(asset, values)
            before = store.path.read_bytes()
            result = service.confirm({'request_id': 'one', 'preview': preview['id']})
            self.assertEqual(result['status'], 'recorded')
            self.assertEqual(store.path.read_bytes(), before)
            self.assertEqual(service.confirm({'request_id': 'one', 'preview': preview['id']})['decision'], result['decision'])
            self.assertEqual(len(store.ledger()), 1)
            service.project()
            self.assertEqual(store.read()['assets'][asset]['resolution']['decision'], result['decision'])
            values['supersedes'] = result['decision']
            next_preview = service.preview(asset, values)
            state = store.read()
            state['assets'][asset]['assertions'] = [{'field': 'publisher', 'raw': 'Different'}]
            store.save(state, expected=state['revision'])
            with self.assertRaisesRegex(LibraryError, 'Stale'):
                service.confirm({'request_id': 'two', 'preview': next_preview['id']})

    def test_published_transfer_and_return_replay_and_gates(self):
        from library.publish import Publisher, Capacity
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, reception, original, asset = setup_library(root)
            publisher = Publisher(store, reception, root / 'library', isolated=True, capacity=Capacity(reserve=0))
            publisher.publish(asset)
            source_bytes = original.read_bytes()
            service = Resolution(store, reception)
            page = service.page(asset, 1)
            values = {'edition_id': None, 'work_id': 'work', 'work': {'title': 'Album'},
                      'edition': {'publisher': 'Other', 'language': 'fr', 'reference': 'local-label'},
                      'select': True, 'proof': [{'page': page['id'], 'field': field, 'text': 'Checked visible ' + field} for field in ('title', 'publisher', 'language')],
                      'reason': 'Changed printed publisher', 'supersedes': None, 'restore': None}
            preview = service.preview(asset, values)
            record = service.confirm({'request_id': 'transfer', 'preview': preview['id']})
            service.project()
            with self.assertRaisesRegex(LibraryError, 'gates publication'):
                publisher.publish(asset)
            service.apply(publisher)
            self.assertEqual(store.read()['assets'][asset]['resolution']['status'], 'applied')
            state = store.read()
            self.assertNotIn('publication', state['editions']['edition'])
            withdrawal = next(op for op in state['operations'] if state['operations'][op].get('role') == 'withdraw')
            current = state['assets'][asset]['identity']['edition']
            back = {**values, 'edition_id': 'edition', 'work': {}, 'edition': {}, 'restore': withdrawal}
            preview = service.preview(asset, back)
            service.confirm({'request_id': 'back', 'preview': preview['id']})
            service.apply(publisher)
            service.project()
            self.assertEqual(store.read()['assets'][asset]['resolution']['status'], 'applied')
            self.assertIn('publication', store.read()['editions']['edition'])
            self.assertEqual(original.read_bytes(), source_bytes)
            before = len(store.read()['operations'])
            service.apply(publisher)
            self.assertEqual(len(store.read()['operations']), before)

    def test_http_origin_token_and_closed_schema(self):
        import http.client
        import threading
        from http.server import ThreadingHTTPServer
        from library.web import handler
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, reception, _, asset = setup_library(root)
            service = Resolution(store, reception)
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler(root, resolution=service))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host = f'127.0.0.1:{server.server_port}'
                client = http.client.HTTPConnection('127.0.0.1', server.server_port)
                client.request('GET', '/api/resolution/token')
                response = client.getresponse()
                import json
                token = json.loads(response.read())['token']
                client.request('POST', '/api/resolution/case', json.dumps({'asset': asset}),
                               {'Content-Type': 'application/json', 'X-CSRF-Token': token, 'Origin': 'http://evil.invalid'})
                response = client.getresponse()
                self.assertEqual(response.status, 403)
                response.read()
                client.request('POST', '/api/resolution/case', json.dumps({'asset': asset}),
                               {'Content-Type': 'application/json', 'X-CSRF-Token': token, 'Origin': 'http://' + host})
                response = client.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read())['asset'], asset)
                self.assertEqual(store.ledger(), [])
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_missing_proof_blocks_replay_and_same_reference_is_not_duplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, reception, _, asset = setup_library(root)
            service = Resolution(store, reception)
            page = service.page(asset, 1)
            values = {'edition_id': None, 'work_id': 'work', 'work': {'title': 'Album'},
                      'edition': {'publisher': 'Other', 'language': 'fr', 'reference': 'notice-unique'},
                      'select': False, 'proof': [{'page': page['id'], 'field': field, 'text': 'Checked visible ' + field} for field in ('title', 'publisher', 'language')],
                      'reason': 'Printed statement', 'supersedes': None, 'restore': None}
            preview = service.preview(asset, values)
            service.confirm({'request_id': 'manual', 'preview': preview['id']})
            service.project()
            self.assertEqual(len(store.read()['editions']), 2)
            with self.assertRaisesRegex(LibraryError, 'already exists'):
                service.preview(asset, values)
            piece = service.pieces / (page['id'] + '.yaml')
            piece.chmod(0o600)
            piece.write_text('changed: true\n')
            with self.assertRaisesRegex(LibraryError, 'evidence missing or altered'):
                store.replay()

    def test_shared_request_budget_keeps_local_application_independent(self):
        from library.metadata import Providers
        from library.catalog import atomic_yaml
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []
            def fetch(url):
                calls.append(url)
                return 200, '{}'
            atomic_yaml(root / 'library-network-budget.yaml', {'remaining': 1})
            first = Providers(root / 'cache', fetch=fetch, interval=0, shared=root)
            second = Providers(root / 'cache', fetch=fetch, interval=0, shared=root)
            self.assertEqual(first.get('https://openlibrary.org/books/OL1M.json')['status'], 'ok')
            self.assertEqual(second.get('https://openlibrary.org/books/OL2M.json')['reason'], 'Request budget exhausted')
            self.assertEqual(len(calls), 1)
            # Cached evidence does not spend another request.
            self.assertEqual(second.get('https://openlibrary.org/books/OL1M.json')['status'], 'ok')

    def test_selecting_another_variant_of_same_published_edition_replaces_it(self):
        import zipfile
        from library.intake import receive
        from library.publish import Publisher, Capacity
        from test_library_intake import SOURCE
        from test_library_readers import image_bytes
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, reception, original, first_asset = setup_library(root)
            publisher = Publisher(store, reception, root / 'library', isolated=True, capacity=Capacity(reserve=0))
            publisher.publish(first_asset)
            second = original.with_name('scan2.cbz')
            with zipfile.ZipFile(second, 'w') as archive:
                archive.writestr('001.png', image_bytes())
                archive.comment = b'Other retained scan'
            state = receive(store, reception, {'items': {SOURCE: {'title': 'Album'}}},
                {'submissions': {SOURCE: {'status': 'queued', 'name': 'tracked', 'package_id': 1}}},
                [{'pid': 1, 'name': 'tracked', 'folder': 'package', 'links': [
                    {'fid': 1, 'name': original.name, 'status': 0, 'size': original.stat().st_size},
                    {'fid': 2, 'name': second.name, 'status': 0, 'size': second.stat().st_size}]}], stable_seconds=.01)
            asset = next(key for key in state['assets'] if key != first_asset)
            service = Resolution(store, reception)
            page = service.page(asset, 1)
            values = {'edition_id': 'edition', 'work_id': 'work', 'work': {}, 'edition': {},
                      'select': True, 'proof': [{'page': page['id'], 'field': field, 'text': 'Visible ' + field}
                                              for field in ('title', 'publisher', 'language')],
                      'reason': 'Explicit better scan choice', 'supersedes': None, 'restore': None}
            preview = service.preview(asset, values)
            self.assertEqual(preview['effect'], 'replace')
            service.confirm({'request_id': 'variant', 'preview': preview['id']})
            service.apply(publisher)
            self.assertEqual(store.read()['editions']['edition']['publication']['asset'], asset)
            self.assertTrue(original.exists())

    def test_shared_metadata_change_needs_all_field_proof_and_failed_replacement_is_not_unchanged(self):
        from library.publish import Publisher, Capacity
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, reception, _, asset = setup_library(root)
            publisher = Publisher(store, reception, root / 'library', isolated=True, capacity=Capacity(reserve=0))
            publisher.publish(asset)
            service = Resolution(store, reception)
            page = service.page(asset, 1)
            values = {'edition_id': 'edition', 'work_id': 'work', 'work': {'title': 'Corrected Album'},
                      'edition': {}, 'select': True, 'edit_shared': True,
                      'proof': [{'page': page['id'], 'field': 'title', 'text': 'Corrected Album'}],
                      'reason': 'Title page corrected', 'supersedes': None, 'restore': None}
            with self.assertRaisesRegex(LibraryError, 'Each distinguishing field'):
                service.preview(asset, values)
            values['proof'].extend({'page': page['id'], 'field': field, 'text': 'Visible ' + field}
                                   for field in ('publisher', 'language'))
            preview = service.preview(asset, values)
            result = service.confirm({'request_id': 'fix-title', 'preview': preview['id']})
            service.project()
            record = next(r for r in store.ledger() if r['id'] == result['decision'])
            service.outcome(record, 'blocked', 'Fixture pre-operation failure')
            values['supersedes'] = result['decision']
            successor = service.preview(asset, values)
            self.assertEqual(successor['effect'], 'replace')

    def test_changed_dependency_after_committed_withdrawal_blocks_creation(self):
        from library.publish import Publisher, Capacity
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, reception, _, asset = setup_library(root)
            publisher = Publisher(store, reception, root / 'library', isolated=True, capacity=Capacity(reserve=0))
            publisher.publish(asset)
            service = Resolution(store, reception)
            page = service.page(asset, 1)
            values = {'edition_id': None, 'work_id': 'work', 'work': {'title': 'Album'},
                      'edition': {'publisher': 'Other', 'language': 'fr'}, 'select': True,
                      'proof': [{'page': page['id'], 'field': f, 'text': 'Visible ' + f}
                                for f in ('title', 'publisher', 'language')],
                      'reason': 'Checked edition', 'supersedes': None, 'restore': None}
            preview = service.preview(asset, values)
            recorded = service.confirm({'request_id': 'crash-transfer', 'preview': preview['id']})
            service.project()
            publisher.withdraw('edition', expected=store.read()['revision'], decision=recorded['decision'], role='withdraw')
            state = store.read()
            state['assets'][asset]['assertions'] = [{'field': 'publisher', 'raw': 'new contradiction'}]
            store.save(state, expected=state['revision'])
            service.apply(publisher)
            self.assertEqual(store.read()['assets'][asset]['resolution']['status'], 'blocked')
            self.assertEqual(list(publisher.exposed.iterdir()), [])

    def test_real_form_candidate_and_manual_journeys_send_preview_before_confirmation(self):
        import json
        import re
        import shutil
        import subprocess
        from library.resolution_ui import PANEL
        if not shutil.which('node'):
            self.skipTest('Node is needed for actual form event execution')
        script = re.search(r'<script>(.*?)</script>', PANEL, re.S).group(1)
        driver = r'''
const vm=require('vm'),assert=require('assert');let input='';process.stdin.on('data',d=>input+=d);process.stdin.on('end',async()=>{
class E{constructor(tag=''){this.tag=tag;this.value='';this.children=[];this.textContent='';this.dataset={};this.style={}}append(...es){for(const e of es){this.children.push(e);if(this.tag==='select'&&this.children.length===1)this.value=e.value}}replaceChildren(...es){this.children=[];this.append(...es)}addEventListener(k,f){this['on'+k]=f}querySelector(s){return this.children.find(e=>e.className===s.slice(1))}}
const ids={},doc={getElementById:id=>ids[id]??(ids[id]=new E),createElement:tag=>new E(tag)};
for(const id of ['resolution-select','resolution-page-number','resolution-reason','resolution-edit-shared','resolution-supersedes','resolution-restore'])doc.getElementById(id);
const calls=[];const data={asset:'asset',identity:null,resolution:null,publications:{},unknowns:'unknown',editions:[],works:[],recovery:[],variants:{v:{edition:'e',format:'cbz',size:10,pages:1,origins:[],selected:false}},candidates:[{label:'Candidate Album',candidate:'candidate-id',work:{title:'Album'},edition:{publisher:'Publisher',language:'fr'}}]};
const ctx={document:doc,location:{pathname:'/fiche/origin',origin:'http://127.0.0.1'},crypto:{randomUUID:()=> 'unique'},fetch:async(url,options)=>{const body=options?JSON.parse(options.body):null;calls.push({url,body});let value={};if(url.endsWith('/token'))value={token:'csrf'};if(url.endsWith('/case'))value=data;if(url.endsWith('/page'))value={id:'page-id',page:1};if(url.endsWith('/preview'))value={id:'preview-id',effect:'create'};if(url.endsWith('/confirm'))value={decision:'durable'};return {ok:true,json:async()=>value}}};vm.runInNewContext(JSON.parse(input).script,ctx);
await ids['open-resolution'].onclick();ids['resolution-candidate'].value='1';ids['resolution-candidate'].onchange();assert.equal(ids['resolution-title'].value,'Album');assert(ids['resolution-variants'].textContent.includes('cbz'));
ids['resolution-select'].checked=true;ids['resolution-page-number'].value='1';await ids['resolution-page'].onclick();const proof=ids['resolution-proof'].children[0];proof.querySelector('.proof-text').value='Printed title';proof.querySelector('.proof-field').value='title';
ids['resolution-reason'].value='Checked printed statement';await ids['resolution-preview'].onclick();assert.equal(ids['resolution-confirm'].disabled,false);let p=calls.find(c=>c.url.endsWith('/preview'));assert.equal(p.body.values.candidate,'candidate-id');assert.equal(p.body.values.select,true);assert(!calls.some(c=>c.url.endsWith('/confirm')));await ids['resolution-confirm'].onclick();assert(calls.some(c=>c.url.endsWith('/confirm')&&c.body.preview==='preview-id'));
ids['resolution-candidate'].value='0';ids['resolution-candidate'].onchange();ids['resolution-title'].value='Manual Album';ids['resolution-publisher'].value='Local';ids['resolution-language'].value='fr';await ids['resolution-preview'].onclick();p=calls.filter(c=>c.url.endsWith('/preview')).pop();assert.equal(p.body.values.work.title,'Manual Album');assert.equal(p.body.values.candidate,null);ids['resolution-form'].oninput();assert.equal(ids['resolution-confirm'].disabled,true);process.stdout.write('candidate/manual preview and explicit confirmation passed');});'''
        result = subprocess.run(['node', '-e', driver], input=json.dumps({'script': script}), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_uncertain_publication_is_reconciled_before_outcome_changes_revision(self):
        from library.publish import Publisher, Capacity
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, reception, _, asset = setup_library(root)
            publisher = Publisher(store, reception, root / 'library', isolated=True, capacity=Capacity(reserve=0))
            service = Resolution(store, reception)
            page = service.page(asset, 1)
            values = {'edition_id': 'edition', 'work_id': 'work', 'work': {}, 'edition': {},
                      'select': True, 'proof': [{'page': page['id'], 'field': f, 'text': 'Visible ' + f}
                                              for f in ('title', 'publisher', 'language')],
                      'reason': 'Printed statement', 'supersedes': None, 'restore': None}
            preview = service.preview(asset, values)
            service.confirm({'request_id': 'uncertain', 'preview': preview['id']})
            def lost_response(phase, operation):
                if phase == 'published':
                    publisher.fault = lambda *_: None
                    raise OSError('Lost operation response')
            publisher.fault = lost_response
            service.apply(publisher)
            state = store.read()
            self.assertEqual(state['assets'][asset]['resolution']['status'], 'applied')
            self.assertEqual(len(state['operations']), 1)
            service.apply(publisher)
            self.assertEqual(len(store.read()['operations']), 1)

    def test_rejected_evidence_admission_leaves_no_new_file_and_preserves_existing_piece(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, reception, _, _ = setup_library(root)
            service = Resolution(store, reception)
            existing = service.piece({'fixture': 'retained'})
            with patch.object(service, 'trim_previews', side_effect=LibraryError('Temporary evidence budget exhausted')):
                with self.assertRaisesRegex(LibraryError, 'budget'):
                    service.piece({'fixture': 'new, not admitted'})
                with self.assertRaisesRegex(LibraryError, 'budget'):
                    service.piece({'fixture': 'retained'})
            self.assertEqual([p.stem for p in service.pieces.glob('*.yaml')], [existing])
            self.assertEqual(service.load_piece(existing), {'fixture': 'retained'})
