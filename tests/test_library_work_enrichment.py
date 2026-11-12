"""Reference transport to catalogue seam; no hoster or live state."""
from pathlib import Path
import tempfile
import unittest

from library.metadata import Providers, enrich
from library.automatic import process_assets, ready_assets
from test_library_publish import setup_library


class WorkEnrichmentTests(unittest.TestCase):

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

    def test_new_origin_triggers_retry_before_weekly_deadline(self):
        from library.automatic import METHOD, evidence_version, ready_asset
        asset = {'status': 'validated', 'origins': ['old']}
        asset['automation'] = {'method': METHOD, 'evidence': evidence_version(asset),
            'outcome': 'pending', 'next_attempt': 604800}
        state = {'origins': {'old': {'availability': 'validated'}, 'new': {'availability': 'validated'}}}
        self.assertFalse(ready_asset(asset, state, 1))
        asset['origins'].append('new')
        self.assertTrue(ready_asset(asset, state, 1))


