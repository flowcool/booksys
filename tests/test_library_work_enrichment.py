"""Reference transport to catalogue seam; no hoster or live state."""
from pathlib import Path
import tempfile
import unittest

from library.metadata import Providers, enrich
from test_library_publish import setup_library


class WorkEnrichmentTests(unittest.TestCase):

    def test_exact_bdnex_page_can_supply_the_album_title(self):
        from library.work_enrichment import enrich_work

        key = 'a' * 64
        url = 'https://www.bedetheque.com/BD-Naruto-Tome-1-Naruto-Uzumaki-24065.html'
        fixture = Path(__file__).parent / 'fixtures/french-metadata/album-24065.html'
        state = {'assets': {key: {'sha256': key, 'origins': ['origin']}},
                 'origins': {'origin': {'sha256': key, 'path': 'Naruto - 1 - Naruto Uzumaki.cbz'}}}
        candidate = {'source': 'bedetheque', 'url': url, 'edition_ref': '24065',
                     'series': 'Naruto', 'number': '1', 'title': 'Naruto',
                     'provenance': {'method': 'bdnex-sitemap-1'}}

        class RetainedPages:
            def get(self, requested_url):
                if requested_url.endswith('/robots.txt'):
                    return {'status': 'ok', 'body': 'User-agent: *'}
                if requested_url == url:
                    return {'status': 'ok', 'body': fixture.read_text(),
                            'retrieved_at': '2026-11-20T00:00:00+00:00'}
                raise AssertionError(requested_url)

        enrich_work(state, key, RetainedPages(), [candidate], discovery=False, hydrate=True)
        self.assertEqual(state['assets'][key]['work_result']['work']['title'], 'Naruto Uzumaki')

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
