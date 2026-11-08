"""Reference transport to catalogue seam; no hoster or live state."""
from pathlib import Path
import tempfile
import unittest

from library.metadata import Providers, enrich
from library.automatic import process_assets, ready_assets
from test_library_publish import setup_library


class WorkEnrichmentTests(unittest.TestCase):
    def test_file_reference_concordance_identifies_without_edition(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store, reception, source, key = setup_library(root)
            state = store.read()
            state['assets'][key].pop('identity')
            state['assets'][key]['embedded'] = {'Series': 'Les Geeks', 'Number': '4'}
            store.save(state, expected=state['revision'])
            album = (Path(__file__).parent/'fixtures/french-metadata/album-95136.html').read_text()
            def fetch(url):
                if url.endswith('/robots.txt'):
                    return 200, 'User-agent: *\nAllow: /'
                if 'bandes_dessinees_' in url:
                    return 200, '<a href="/serie-123-Les-Geeks.html">Les Geeks</a>'
                if '/serie-' in url:
                    return 200, '<ul class="bdt-box-albums-liste"><li><a href="/BD-Les-Geeks-Tome-4-95136.html"><span class="bdt-box-albums-titre">Hacker vaillant rien d’impossible</span></a></li></ul>'
                if '/BD-' in url:
                    return 200, album
                self.fail('Unexpected reference request: ' + url)
            providers = Providers(root/'cache', fetch=fetch, interval=0, budget=20)
            value = enrich(store, key, providers, [])['assets'][key]
            self.assertEqual(value['work_result']['status'], 'identified')
            self.assertEqual(value['work_result']['work']['title'], "Hacker vaillant rien d'impossible")
            self.assertNotIn('identity', value)
            self.assertEqual(value['work_result']['edition_status'], 'unknown')
            self.assertEqual(value['work_result']['work']['family'], 'BD')

    def test_weekly_retry_reuses_unchanged_printed_evidence(self):
        from library.automatic import METHOD, evidence_version
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store, reception, source, key = setup_library(root)
            state = store.read()
            asset = state['assets'][key]
            asset.pop('identity')
            asset['printed'] = {'asset': key, 'status': 'ok', 'pages': []}
            asset['automation'] = {'method': METHOD, 'evidence': evidence_version(asset),
                'attempts': 1, 'outcome': 'pending', 'next_attempt': 604800}
            store.save(state, expected=state['revision'])
            self.assertEqual(ready_assets(store, clock=lambda: 604799), 0)
            self.assertEqual(ready_assets(store, clock=lambda: 604800), 1)
