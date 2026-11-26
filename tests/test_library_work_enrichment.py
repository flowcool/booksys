"""Reference transport to catalogue seam; no hoster or live state."""
from pathlib import Path
import tempfile
import unittest

from library.metadata import Providers, enrich
from test_library_publish import setup_library


class WorkEnrichmentTests(unittest.TestCase):

    def match_naruto(self, label, *, embedded=None, number='1', ambiguous=False):
        from library.work_enrichment import enrich_work

        key = 'a' * 64
        url = 'https://www.bedetheque.com/BD-Naruto-Tome-1-Naruto-Uzumaki-24065.html'
        fixture = Path(__file__).parent / 'fixtures/french-metadata/album-24065.html'
        state = {'assets': {key: {'sha256': key, 'origins': ['origin'], 'embedded': embedded or {}}},
                 'origins': {'origin': {'sha256': key, 'path': label}}}
        candidate = {'source': 'bedetheque', 'url': url, 'edition_ref': '24065',
                     'series': 'Naruto', 'number': number, 'title': 'Naruto',
                     'provenance': {'method': 'bdnex-sitemap-1'}}
        candidates = [candidate]
        if ambiguous:
            candidates.append({**candidate, 'title': 'Uzumaki',
                               'url': url.replace('24065', '22222'), 'edition_ref': '22222'})

        class RetainedPages:
            def get(self, requested_url):
                if requested_url.endswith('/robots.txt'):
                    return {'status': 'ok', 'body': 'User-agent: *'}
                if requested_url == url:
                    return {'status': 'ok', 'body': fixture.read_text(),
                            'retrieved_at': '2026-11-20T00:00:00+00:00'}
                raise AssertionError(requested_url)

        enrich_work(state, key, RetainedPages(), candidates, discovery=False, hydrate=True)
        return state['assets'][key]['work_result']

    def test_exact_bdnex_page_can_supply_the_album_title(self):
        result = self.match_naruto('Naruto - 1 - Naruto Uzumaki.cbz')
        self.assertEqual(result['work'].get('title'), 'Naruto Uzumaki')

    def test_exact_page_author_can_explain_a_filename_suffix(self):
        result = self.match_naruto('Naruto - 1 - Masashi Kishimoto.cbz')
        self.assertEqual(result['status'], 'no_match')
        self.assertEqual(result['work'], {})
        self.assertEqual(result['candidate_concordance']['mode'], 'none')
        self.assertTrue(any(entry['value'] == 'Masashi Kishimoto'
                            for entry in result['evidence']['provenance']['title']))

    def test_exact_page_does_not_override_other_title_evidence_or_a_wrong_volume(self):
        cases = (
            ('Naruto - 1 - Other Caption.cbz', None, '1', 'no_match'),
            ('Naruto - 1 - Masashi Kishimoto.cbz', {'Title': 'Other Caption'}, '1', 'review'),
            ('Naruto - 2 - Naruto Uzumaki.cbz', None, '2', 'review'),
        )
        for label, embedded, number, status in cases:
            with self.subTest(label=label, embedded=embedded):
                self.assertEqual(self.match_naruto(label, embedded=embedded, number=number)['status'], status)

    def test_exact_page_does_not_choose_between_two_numbered_proposals(self):
        result = self.match_naruto('Naruto - 1 - Naruto Uzumaki.cbz', ambiguous=True)
        self.assertEqual(result['status'], 'no_match')
        self.assertEqual(result['candidate_concordance']['mode'], 'ambiguous')

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
        asset['printed']['pages'] = [{'text': ''}]*4 + asset['printed']['pages']
        self.assertEqual(evaluate_work(printed_concordance(evidence, asset, [row]), [row])['status'], 'review')
