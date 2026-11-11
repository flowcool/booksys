import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from http.server import ThreadingHTTPServer

from library.catalog import atomic_yaml
from library.web import build_projection, handler
from test_library_publish import setup_library


class WebTests(unittest.TestCase):
    def test_operator_reason_explains_scheduling_without_changing_backing_reason(self):
        from library.web import operator_reason
        text = operator_reason('Intake budget deferred')
        self.assertIn('prochain cycle', text)
        self.assertIn('travaux lourds', text)
        self.assertNotIn('5 minutes', text)
        self.assertIn('Téléchargement', operator_reason('Transfer is not confirmed finished'))

    def test_provisional_copy_is_available_without_confirmed_publication(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder);store, _, _, asset = setup_library(root,self)
            state = store.read();state['assets'][asset].pop('identity', None)
            state['assets'][asset]['provisional'] = {'status': 'published', 'edition_status': 'unknown',
                'target': asset, 'artifact_format': 'cbz', 'operation': 'a'*32,
                'manifest': {'book.cbz': 'b'*64, 'fiche.md': 'c'*64}}
            store.save(state, expected=state['revision'])
            projection = build_projection(root)
            row = projection['rows'][0]
            self.assertEqual(row['publication'], 'available_edition_unknown')
            self.assertEqual(row['identity'], 'pending')
            self.assertEqual(projection['counts']['confirmed_assets'], 0)
            self.assertEqual(projection['counts']['provisional_assets'], 1)
            self.assertEqual(projection['counts']['sources'], 1)

    def test_description_uses_matching_captured_source_then_bibliography_or_unknown(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); store, reception, original, asset = setup_library(root,self)
            state = store.read(); source = next(iter(state['origins'].values()))['source']
            from library.operational_state import CollectorState
            captured=CollectorState(root/'state')
            captured.capture(source,{'page':{'description':'<p>Album &amp; story.</p><p>Second paragraph.</p><script>bad()</script>'}})
            captured.capture('https://other.example/',{'page':{'description':'Wrong album'}})
            fiche = next(iter(build_projection(root)['fiches'].values()))
            self.assertEqual(fiche['summary'], 'Album & story.\n\nSecond paragraph.')
            self.assertEqual(fiche['summary_provenance'], 'Captured Bookys page')
            state['assets'][asset]['overrides']['summary'] = 'Operator correction'
            store.save(state, expected=state['revision'])
            fiche = next(iter(build_projection(root)['fiches'].values()))
            self.assertEqual(fiche['summary'], 'Operator correction')
            self.assertEqual(fiche['summary_provenance'], 'Operator override')
            state = store.read();state['assets'][asset]['overrides'].pop('summary')
            captured.capture(source,{})
            state['works']['work']['summary'] = 'Bibliographic summary'
            store.save(state, expected=state['revision'])
            fiche = next(iter(build_projection(root)['fiches'].values()))
            self.assertEqual(fiche['summary'], 'Bibliographic summary')
            self.assertEqual(fiche['summary_provenance'], 'Collected bibliographic metadata')
            state = store.read();state['works']['work'].pop('summary')
            store.save(state, expected=state['revision'])
            self.assertEqual(next(iter(build_projection(root)['fiches'].values()))['summary'], 'Description non disponible')

    def test_read_only_projection_filters_private_evidence_and_escapes_fiche(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); store, reception, original, asset = setup_library(root,self)
            state = store.read();state['assets'][asset]['captured_hints'] = {'private-host': {'title': '<script>alert(1)</script>'}}
            state['assets'][asset]['assertions'] = [{'field': 'summary', 'raw': '<img src=x onerror=alert(2)>', 'source': 'fixture', 'url': 'https://evil.example/download'}]
            state['works']['work']['title'] = '<script>alert(2)</script>'
            state['editions']['edition']['publication'] = {'asset': asset}
            store.save(state, expected=state['revision'])
            projection = build_projection(root)
            atomic_yaml(root / 'evidence/dashboard.yaml', projection)
            body = json.dumps({key: value for key, value in projection.items() if key != 'fiches'})
            self.assertNotIn('PRIVATE_', body);self.assertNotIn('bookys-ebooks', body)
            self.assertEqual(len(projection['rows']), 1)
            self.assertTrue(projection['rows'][0]['fiche'])
            with ThreadingHTTPServer(('127.0.0.1', 0), handler(root)) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
                base = f'http://127.0.0.1:{server.server_port}'
                with urllib.request.urlopen(base + projection['rows'][0]['fiche']) as response:
                    page = response.read().decode()
                self.assertIn('&lt;script&gt;', page);self.assertNotIn('<script>alert(2)', page)
                with urllib.request.urlopen(urllib.request.Request(base + '/api/status', method='HEAD')) as response:
                    self.assertEqual(response.status, 200);self.assertEqual(response.read(), b'')
                pending_fiche = projection['rows'][0]['fiche']
                self.assertTrue(pending_fiche.startswith('/fiche/'))
                self.assertNotIn('evil.example', page)
                self.assertIn('Source assertions', page)
                for route in ('/../state/library.yaml', '/state/library.yaml', '/fiche/../../secrets', '/review.md'):
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        urllib.request.urlopen(base + route)
                    self.assertEqual(error.exception.code, 404)
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(urllib.request.Request(base + '/api/status', data=b'', method='POST'))
                self.assertEqual(error.exception.code, 405)
                server.shutdown()
