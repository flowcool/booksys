"""Work confirmation preserves sourced enrichment through the public interfaces."""
import copy
from pathlib import Path
import tempfile
import unittest
import zipfile
from xml.etree import ElementTree as ET

from library.admission import AdmissionPublisher, work_fields
from library.metadata import Providers, enrich
from library.publication import Capacity
from library.readers import sha256
from library.review import decide, report
from library.views import build_projection, fiche_html
from library.xmlmetadata import work_comicinfo
from test_library_publish import setup_library
from postgres_fixture import require_postgresql

TITLE = "Hacker vaillant rien d'impossible"


class WorkDecisionEnrichmentTests(unittest.TestCase):
    def decision(self, catalog, key, action, values, request_id):
        case = next(c for c in report(catalog)['cases'].values() if c.get('asset') == key)
        result = decide(catalog, [{'request_id': request_id, 'case': case['id'],
            'version': case['version'], 'action': action, 'values': values}], author='flowcool')[0]
        self.assertEqual(result['status'], 'applied', result)
        return result

    def setup(self, root, sqlite, work=None):
        catalog, reception, source, key = setup_library(root,self)
        state = catalog.read()
        state['assets'][key].pop('identity')
        state['assets'][key]['embedded'] = {'Series': 'Les Geeks', 'Number': '4'}
        for table in ('works', 'editions', 'variants'):
            state[table].clear()
        catalog.save(state, expected=state['revision'])
        if sqlite:
            require_postgresql(catalog)
        self.decision(catalog, key, 'identify_work', {'work': {'title': TITLE, 'author': '', **(work or {})},
            'evidence': {'basis': 'Operator read the title page', 'asset': key}}, 'confirm')
        initial = catalog.get('assets', key)
        self.assertEqual(initial['work_result']['evidence'], initial['work_decision']['evidence'])
        return catalog, source, key

    def providers(self, root, *, conflict=False):
        album = (Path(__file__).parent/'fixtures/french-metadata/album-95136.html').read_text()
        album = album.replace('</body>', '<span itemprop="description">Fresh sourced summary</span></body>')
        def fetch(url):
            if url.endswith('/robots.txt'):
                return 200, 'User-agent: *\nAllow: /'
            if 'bandes_dessinees_' in url:
                return 200, '<a href="/serie-123-Les-Geeks.html">Les Geeks</a>'
            if '/serie-' in url:
                numbers = ('95136', '95137') if conflict else ('95136',)
                return 200, '<ul class="bdt-box-albums-liste">'+''.join(
                    '<li><a href="/BD-Les-Geeks-Tome-4-'+number+'.html">'
                    '<span class="bdt-box-albums-titre">'+TITLE+'</span></a></li>' for number in numbers)+'</ul>'
            if '/BD-' in url:
                return 200, album.replace('Soleil Productions', 'Other publisher') if '95137' in url else album
            self.fail('Unexpected reference request: '+url)
        return Providers(root/('conflict-cache' if conflict else 'cache'), fetch=fetch, interval=0, budget=30)

    def test_confirmation_only_keeps_fresh_metadata_and_exports_after_replay(self):
        for sqlite in (False, True):
            with self.subTest(sqlite=sqlite), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                catalog, source, key = self.setup(root, sqlite)
                original_decisions = [path.read_bytes() for path in sorted(catalog.decisions.glob('*.yaml'))]
                state = enrich(catalog, key, self.providers(root), [])
                asset = state['assets'][key]
                self.assertEqual(asset['work_result']['work']['author'], "Collectif ''Gang''")
                self.assertEqual(asset['work_result']['work']['summary'], 'Fresh sourced summary')
                self.assertEqual(asset['work_result']['work']['genres'], ['Humour'])
                before = copy.deepcopy(asset['work_result'])
                replayed = catalog.replay()
                # Legacy YAML first projection can precede the assigned ledger ID.
                self.assertEqual(replayed['assets'][key]['work_result'],
                                 {**before, 'decision': catalog.ledger()[0]['id']})
                self.assertEqual(original_decisions, [path.read_bytes() for path in sorted(catalog.decisions.glob('*.yaml'))])
                fiche = next(iter(build_projection(root, state=replayed, captured={}, write_covers=False)['fiches'].values()))
                self.assertIn('Fresh sourced summary', fiche_html(fiche))
                self.assertIn('Humour', fiche_html(fiche))
                xml = ET.fromstring(work_comicinfo(replayed['assets'][key], work_fields(replayed, key)))
                self.assertEqual(xml.findtext('Writer'), "Collectif ''Gang''")
                self.assertEqual(xml.findtext('Summary'), 'Fresh sourced summary')
                self.assertEqual(xml.findtext('Genre'), 'Humour')
                operation = AdmissionPublisher(catalog, root/'reception', root/'library',
                    isolated=True, local_recovery=sqlite, capacity=Capacity(reserve=0)).publish_asset(key)
                with zipfile.ZipFile(root/'library'/operation['target']/'book.cbz') as archive:
                    exported = ET.fromstring(archive.read('ComicInfo.xml'))
                self.assertEqual(exported.findtext('Writer'), "Collectif ''Gang''")
                self.assertEqual(exported.findtext('Summary'), 'Fresh sourced summary')
                self.assertEqual(exported.findtext('Genre'), 'Humour')
                self.assertEqual(sha256(source), key)

    def test_supplied_confirmation_fields_win_but_empty_fields_do_not_erase_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, _, key = self.setup(root, True, {'author': 'Operator writer', 'summary': ''})
            result = enrich(catalog, key, self.providers(root), [])['assets'][key]['work_result']
            self.assertEqual(result['work']['author'], 'Operator writer')
            self.assertEqual(result['work']['summary'], 'Fresh sourced summary')
            replayed = catalog.replay()
            self.assertEqual(replayed['assets'][key]['work_result'], result)
            xml = ET.fromstring(work_comicinfo(replayed['assets'][key], work_fields(replayed, key)))
            self.assertEqual(xml.findtext('Writer'), 'Operator writer')
            self.assertEqual(xml.findtext('Summary'), 'Fresh sourced summary')

    def test_explicit_override_survives_conflicts_enrichment_and_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, _, key = self.setup(root, True)
            self.decision(catalog, key, 'override', {'summary': 'Operator summary', 'author': None}, 'override')
            state = enrich(catalog, key, self.providers(root, conflict=True), [])
            result = state['assets'][key]['work_result']
            self.assertIn('publisher', result['field_conflicts'])
            self.assertNotIn('publisher', result['work'])
            self.assertEqual(len(result['candidates']), 2)
            replayed = catalog.replay()
            self.assertEqual(replayed['assets'][key]['work_result'], result)
            self.assertEqual(replayed['assets'][key]['overrides'], {'summary': 'Operator summary', 'author': None})
            xml = ET.fromstring(work_comicinfo(replayed['assets'][key], work_fields(replayed, key)))
            self.assertEqual(xml.findtext('Title'), TITLE)
            self.assertEqual(xml.findtext('Summary'), 'Operator summary')
            self.assertIsNone(xml.find('Writer'))
            self.assertIsNone(xml.find('Publisher'))
            fiche = next(iter(build_projection(root, state=replayed, captured={}, write_covers=False)['fiches'].values()))
            self.assertIn('publisher', fiche_html(fiche))
            self.assertEqual(fiche['summary'], 'Operator summary')

    def test_fresh_identity_contradiction_does_not_replace_confirmation_or_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, _, key = self.setup(root, True)
            state = catalog.working_set('identify', {'assets': [key]})
            state['assets'][key]['embedded']['Title'] = 'A contradictory title'
            catalog.save(state, expected=state['revision'])
            result = enrich(catalog, key, self.providers(root), [])['assets'][key]['work_result']
            self.assertEqual(result['work']['title'], TITLE)
            self.assertEqual(result['evidence']['title'], 'A contradictory title')
            self.assertIn('provenance', result['evidence'])
            self.assertEqual(catalog.replay()['assets'][key]['work_result'], result)

    def test_confirmation_does_not_borrow_fields_from_a_different_automatic_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, _, key = self.setup(root, True, {'title': 'Operator distinct work'})
            result = enrich(catalog, key, self.providers(root), [])['assets'][key]['work_result']
            self.assertEqual(result['work']['title'], 'Operator distinct work')
            self.assertNotIn('author', result['work'])
            self.assertNotIn('summary', result['work'])
            self.assertEqual(result['candidates'][0]['title'], TITLE)
