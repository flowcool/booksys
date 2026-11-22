"""Asset-bound Web proposals share page checks and the final confirmation gate."""
import unittest
from pathlib import Path
import tempfile
from unittest.mock import Mock, patch

import test_library_cover_routes as routes
from library.work_enrichment import apply_bdnex_decision


class WebIdentityTests(unittest.TestCase):
    def route(self, web=None):
        state, row, providers = routes.CoverRouteTests().setup_route()
        if web is not None:
            state['assets']['a'*64]['embedded']['Web'] = web
        providers.get_binary = Mock(side_effect=AssertionError('No cover spend'))
        response = {'candidates': [row], 'decision': {
            'status': 'selected', 'reason': 'strong', 'edition_ref': '123'}}
        return state, row, providers, response

    def test_web_agreement_exempts_before_title_creator_and_cover(self):
        state, row, providers, response = self.route('https://www.bedetheque.com/BD-Series-Tome-2-123.html')
        state['assets']['a'*64]['embedded']['Title'] = 'Informative Album'
        apply_bdnex_decision(state, 'a'*64, providers, response)
        result = state['assets']['a'*64]['work_result']
        self.assertEqual(result['status'], 'identified', result)
        self.assertEqual(result['evidence']['cover_confirmation']['basis'], 'web')
        self.assertEqual(result['evidence']['provenance']['web'][0]['source'], 'embedded:' + 'a'*64)
        providers.get_binary.assert_not_called()

    def test_page_failure_blocks_even_a_concordant_title(self):
        state, row, providers, response = self.route()
        state['assets']['a'*64]['embedded']['Title'] = 'Informative Album'
        original = providers.get
        providers.get = lambda url: original(url) if url.endswith('robots.txt') else {'status': 'unavailable'}
        apply_bdnex_decision(state, 'a'*64, providers, response)
        result = state['assets']['a'*64]['work_result']
        self.assertEqual(result['status'], 'review', result)
        self.assertEqual(result['reason_code'], 'cover_page_unavailable')

    def test_lady_travel_web_and_title_cannot_override_number(self):
        state, row, providers, response = self.route('https://www.bedetheque.com/BD-Series-Tome-2-123.html')
        state['assets']['a'*64]['embedded'].update(Number='1', Title='Informative Album')
        apply_bdnex_decision(state, 'a'*64, providers, response)
        self.assertEqual(state['assets']['a'*64]['work_result']['status'], 'review')
        providers.get_binary.assert_not_called()

    def test_every_structured_conflict_wins_over_web_and_title(self):
        for field, value in (('Title', 'Other Album'), ('Series', 'Other Series'),
                             ('Number', '1'), ('Writer', 'Other Writer')):
            with self.subTest(field=field):
                state, row, providers, response = self.route('https://www.bedetheque.com/BD-Series-Tome-2-123.html')
                state['assets']['a'*64]['embedded'].update({'Title': 'Informative Album', field: value})
                apply_bdnex_decision(state, 'a'*64, providers, response)
                result = state['assets']['a'*64]['work_result']
                self.assertEqual(result['status'], 'review', result)
                self.assertIsNone(result['evidence']['cover_confirmation']['basis'])
                providers.get_binary.assert_not_called()

    def test_dated_series_mismatch_wins_over_web(self):
        state, row, providers, response = self.route('https://www.bedetheque.com/BD-Series-Tome-2-123.html')
        row['series'] = 'Series 1955'
        original = providers.get
        providers.get = lambda url: {**original(url), 'body': original(url)['body'].replace('Series', 'Series 1955')}
        state['assets']['a'*64]['origins'] = ['o']
        state['origins']['o'] = {'sha256': 'a'*64, 'path': 'Series (2011) - T02.cbz', 'source': 'offline:series'}
        apply_bdnex_decision(state, 'a'*64, providers, response)
        result = state['assets']['a'*64]['work_result']
        self.assertEqual(result['reason_code'], 'cover_identity_conflict', result)
        providers.get_binary.assert_not_called()

    def test_web_mismatch_and_canonical_mismatch_block_all_exemptions(self):
        for mode in ('web', 'canonical'):
            state, row, providers, response = self.route('https://www.bedetheque.com/BD-Series-Tome-2-123.html')
            state['assets']['a'*64]['embedded']['Title'] = 'Informative Album'
            if mode == 'web':
                state['assets']['a'*64]['embedded']['Web'] = row['url'].replace('123.html', '999.html')
            else:
                original = providers.get
                providers.get = lambda url: {**original(url), 'body': original(url)['body'].replace('123.html', '999.html')}
            apply_bdnex_decision(state, 'a'*64, providers, response)
            result = state['assets']['a'*64]['work_result']
            self.assertEqual(result['status'], 'review', result)
            self.assertEqual(result['reason_code'], 'cover_web_conflict' if mode == 'web' else 'cover_page_reference_mismatch')
            providers.get_binary.assert_not_called()

    def test_unknown_slug_title_needs_confirmation_instead_of_a_title_veto(self):
        state, row, providers, response = self.route()
        state['assets']['a'*64]['embedded']['Title'] = 'Informative Album'
        row['title'] = row['series']
        original = providers.get
        providers.get = lambda url: {**original(url), 'body': original(url)['body'].replace('Series - Informative Album', 'Series')}
        # The slug/page has no album title. A real file title is still unconfirmed.
        apply_bdnex_decision(state, 'a'*64, providers, response)
        result = state['assets']['a'*64]['work_result']
        self.assertEqual(result['status'], 'review', result)
        self.assertEqual(result['reason_code'], 'cover_archive_unavailable')

    def test_decorated_series_proposal_reaches_cover_guard(self):
        state, row, providers, response = self.route()
        state['assets']['a'*64]['embedded']['Series'] = 'Series'
        row['series'] = 'Series Imperia'
        original = providers.get
        providers.get = lambda url: {**original(url), 'body': original(url)['body'].replace('Series', 'Series Imperia')}
        apply_bdnex_decision(state, 'a'*64, providers, response)
        result = state['assets']['a'*64]['work_result']
        self.assertEqual(result['reason_code'], 'cover_archive_unavailable', result)

    def test_web_exemption_precedes_passing_or_failing_cover(self):
        from library import decode
        canonical = 'https://www.bedetheque.com/BD-Series-Tome-2-123.html'
        for web, score, expected, basis in (
                (canonical, 0, 'identified', 'web'), (canonical, 100, 'identified', 'web'),
                (canonical.replace('bedetheque.com', 'foreign.example'), 100, 'identified', 'cover'),
                (canonical + '?x=1', 0, 'review', None),
                (canonical.replace('123.html', 'bad.html'), 100, 'identified', 'cover'),
                (canonical.replace('123.html', '999.html'), 100, 'review', None)):
            with self.subTest(web=web, score=score), tempfile.TemporaryDirectory() as directory:
                state, row, providers, response = self.route(web)
                providers.get_binary = Mock(return_value={'status': 'ok', 'body': b'reference'})
                def extract(operation, source, payload, destination):
                    (destination/'first-page.img').write_bytes(b'original')
                    return {'status': 'extracted', 'input_sha256': 'a'*64}
                receipt = {'score': score, 'result': 'pass' if score >= 40 else 'review',
                           'reason': 'cover_agreement' if score >= 40 else 'below_threshold'}
                with patch.object(decode, 'WORK_ROOT', directory), patch.object(decode, 'run', side_effect=extract), \
                        patch('library.cover_confirmation.compare', return_value=receipt):
                    apply_bdnex_decision(state, 'a'*64, providers, response, cover_source=lambda: Path('/unused'))
                result = state['assets']['a'*64]['work_result']
                self.assertEqual(result['status'], expected, result)
                self.assertEqual(result['evidence']['cover_confirmation']['basis'], basis)
                if basis == 'web' or web.endswith('999.html'):
                    providers.get_binary.assert_not_called()
