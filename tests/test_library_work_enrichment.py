"""Reference transport to catalogue seam; no hoster or live state."""
from pathlib import Path
import tempfile
import unittest

from library.metadata import Providers, enrich
from library.automatic import process_assets, ready_assets
from test_library_publish import setup_library


class WorkEnrichmentTests(unittest.TestCase):
    def test_creator_ambiguity_controls_real_holding_and_classified_admission(self):
        import zipfile
        from library.admission import AdmissionPublisher
        from library.publication import Capacity
        from library.readers import sha256
        for scenario, authors, expected in (
                ('homonyms', ('Alice', 'Bob'), 'review'),
                ('editions', ('Alice', 'Alice'), 'identified'),
                ('missing-creators', (None, None), 'identified'),
                ('duplicates', ('Alice', 'Alice'), 'identified')):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                store, reception, source, key = setup_library(root,self)
                state = store.read()
                state['assets'][key].pop('identity')
                state['assets'][key]['embedded'] = {'Series': 'Les Geeks', 'Number': '4'}
                for table in ('works', 'editions', 'variants'):
                    state[table].clear()
                store.save(state, expected=state['revision'])
                album = (Path(__file__).parent/'fixtures/french-metadata/album-95136.html').read_text()
                def fetch(url):
                    if url.endswith('/robots.txt'):
                        return 200, 'User-agent: *\nAllow: /'
                    if 'bandes_dessinees_' in url:
                        return 200, '<a href="/serie-123-Les-Geeks.html">Les Geeks</a>'
                    if '/serie-' in url:
                        return 200, '<ul class="bdt-box-albums-liste">'+''.join(
                            '<li><a href="/BD-Les-Geeks-Tome-4-'+number+'.html">'
                            '<span class="bdt-box-albums-titre">Hacker vaillant rien d’impossible</span></a></li>'
                            for number in ('95136', '95137'))+'</ul>'
                    if '/BD-' in url:
                        index = int('95137' in url)
                        body = album.replace("Collectif ''Gang''", authors[index] or '')
                        if scenario == 'editions' and index:
                            body = body.replace('Soleil Productions', 'Other publisher').replace('2009-09-23', '2010-01-01').replace('9782302008168', '9782302008175')
                        return 200, body
                    if url.startswith('https://openlibrary.org/search.json?'):
                        return 200, '{"docs":[]}'
                    self.fail('Unexpected reference request: '+url)
                providers = Providers(root/'cache', fetch=fetch, interval=0, budget=30)
                result = enrich(store, key, providers, [])['assets'][key]['work_result']
                self.assertEqual(result['status'], expected)
                self.assertEqual(len(result['candidates']), 2)
                if expected == 'review':
                    self.assertIn('creator', result['reason'])
                    self.assertEqual(result['work'], {})
                    self.assertNotIn('classification', result)
                if scenario == 'editions':
                    self.assertEqual(result['edition_status'], 'unknown')
                    for field in ('publisher', 'date', 'isbn'):
                        self.assertIn(field, result['field_conflicts'])
                        self.assertNotIn(field, result['work'])
                operation = AdmissionPublisher(store, reception, root/'library', isolated=True,
                    capacity=Capacity(reserve=0)).publish_asset(key)
                self.assertEqual(operation['status'], 'holding' if expected == 'review' else 'classified')
                self.assertEqual(sha256(source), key)
                with zipfile.ZipFile(root/'library'/operation['target']/'book.cbz') as archive:
                    self.assertIn('ComicInfo.xml', archive.namelist())

    def test_opening_ocr_identifies_work_without_isbn_but_not_rear_advertisement(self):
        from library.work_enrichment import printed_concordance
        from library.work_identity import evaluate_work
        key = 'a'*64
        evidence = {'asset_bound': True, 'provenance': {}}
        row = {'title': 'A reliable title', 'author': 'Jane Writer', 'series': 'Series', 'number': '2'}
        asset = {'sha256': key, 'printed': {'asset': key, 'status': 'ok',
            'pages': [{'name': '1.png', 'sha256': 'b'*64, 'text': 'A reliable title Jane Writer'}]}}
        result = evaluate_work(printed_concordance(evidence, asset, [row]), [row])
        self.assertEqual(result['status'], 'identified')
        self.assertEqual(result['edition_status'], 'unknown')
        asset['printed']['pages'] = [{'text': ''}]*4 + asset['printed']['pages']
        self.assertEqual(evaluate_work(printed_concordance(evidence, asset, [row]), [row])['status'], 'review')

    def test_new_origin_triggers_retry_before_weekly_deadline(self):
        from library.automatic import METHOD, evidence_version, ready_asset
        asset = {'status': 'validated', 'origins': ['old']}
        asset['automation'] = {'method': METHOD, 'evidence': evidence_version(asset),
            'outcome': 'pending', 'next_attempt': 604800}
        state = {'origins': {'old': {'availability': 'validated'}, 'new': {'availability': 'validated'}}}
        self.assertFalse(ready_asset(asset, state, 1))
        asset['origins'].append('new')
        self.assertTrue(ready_asset(asset, state, 1))

    def test_file_reference_concordance_identifies_without_edition(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store, reception, source, key = setup_library(root,self)
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
            # An ISBN-free opening title/creator can establish the same work
            # when filenames and embedded fields cannot anchor it.
            state = store.read()
            entry = state['assets'][key]
            entry['embedded'] = {}
            origin = state['origins'][entry['origins'][0]]
            entry['captured_hints'] = {origin['source']: {'title': 'Les Geeks'}}
            entry['printed'] = {'asset': key, 'status': 'ok', 'pages': [{'name': '001.png',
                'sha256': 'c'*64, 'text': value['work_result']['work']['title']+' '+value['work_result']['work']['author']}]}
            store.save(state, expected=state['revision'])
            value = enrich(store, key, providers, [])['assets'][key]
            self.assertEqual(value['work_result']['status'], 'identified')
            self.assertNotIn('identity', value)
            self.assertIn('printed:', str(value['work_result']['evidence']))

    def test_weekly_retry_reuses_unchanged_printed_evidence(self):
        from library.automatic import METHOD, evidence_version
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store, reception, source, key = setup_library(root,self)
            state = store.read()
            asset = state['assets'][key]
            asset.pop('identity')
            asset['printed'] = {'asset': key, 'status': 'ok', 'pages': []}
            asset['automation'] = {'method': METHOD, 'evidence': evidence_version(asset),
                'attempts': 1, 'outcome': 'pending', 'next_attempt': 604800}
            store.save(state, expected=state['revision'])
            self.assertEqual(ready_assets(store, clock=lambda: 604799), 0)
            self.assertEqual(ready_assets(store, clock=lambda: 604800), 1)
