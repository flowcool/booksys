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

    def test_other_edition_of_same_work_uses_web_exemption(self):
        state, row, providers, response = self.route(
            'https://www.bedetheque.com/BD-Series-Tome-02-999.html')
        apply_bdnex_decision(state, 'a'*64, providers, response)
        result = state['assets']['a'*64]['work_result']
        self.assertEqual(result['status'], 'identified', result)
        receipt = result['evidence']['cover_confirmation']
        self.assertEqual(receipt['basis'], 'web')
        self.assertEqual(receipt['web_comparisons'][0]['web_album_id'], '999')
        self.assertEqual(receipt['web_comparisons'][0]['candidate_album_id'], '123')
        self.assertEqual(receipt['web_comparisons'][0]['result'], 'same_work')
        providers.get_binary.assert_not_called()

    def test_same_album_resolves_stale_web_slug_to_current_candidate_slug(self):
        state, row, providers, response = self.route(
            'https://www.bedetheque.com/BD-Old-Series-Name-Tome-2-123.html')
        apply_bdnex_decision(state, 'a'*64, providers, response)
        result = state['assets']['a'*64]['work_result']
        self.assertEqual(result['status'], 'identified', result)
        self.assertEqual(result['candidate_concordance']['mode'], 'web')
        self.assertEqual(result['evidence']['cover_confirmation']['basis'], 'web')
        providers.get_binary.assert_not_called()

    def test_work_differences_win_over_title_creator_and_cover(self):
        for slug in ('Other-Series-Tome-2', 'Series-Tome-3'):
            with self.subTest(slug=slug):
                state, row, providers, response = self.route(
                    'https://www.bedetheque.com/BD-' + slug + '-999.html')
                state['assets']['a'*64]['embedded'].update(Title='Informative Album', Writer='Jane Writer')
                apply_bdnex_decision(state, 'a'*64, providers, response)
                result = state['assets']['a'*64]['work_result']
                self.assertEqual(result['reason_code'], 'cover_web_conflict', result)
                providers.get_binary.assert_not_called()

    def test_different_explicit_slug_titles_are_a_work_conflict(self):
        state, row, providers, response = self.route(
            'https://www.bedetheque.com/BD-Series-Tome-2-Other-Album-999.html')
        row['url'] = 'https://www.bedetheque.com/BD-Series-Tome-2-Informative-Album-123.html'
        original = providers.get
        providers.get = lambda url: {**original(url), 'body': original(url)['body'].replace(
            'BD-Series-Tome-2-123.html', 'BD-Series-Tome-2-Informative-Album-123.html')}
        apply_bdnex_decision(state, 'a'*64, providers, response)
        self.assertEqual(state['assets']['a'*64]['work_result']['reason_code'], 'cover_web_conflict')
        providers.get_binary.assert_not_called()

    def test_missing_slug_title_is_unresolved_and_cannot_use_title_exemption(self):
        state, row, providers, response = self.route(
            'https://www.bedetheque.com/BD-Series-Tome-2-Other-Album-999.html')
        state['assets']['a'*64]['embedded']['Title'] = 'Informative Album'
        apply_bdnex_decision(state, 'a'*64, providers, response)
        result = state['assets']['a'*64]['work_result']
        self.assertEqual(result['reason_code'], 'cover_web_unresolved', result)
        providers.get_binary.assert_not_called()

    def test_edition_record_resolves_offline_or_stays_unresolved(self):
        from library.asset_evidence import bind_evidence
        from library.work_enrichment import save_work_result
        from copy import deepcopy
        for resolved in (True, False):
            with self.subTest(resolved=resolved):
                state, row, providers, response = self.route(
                    'https://www.bedetheque.com/BD-Series-Tome-2-999.html')
                state['assets']['a'*64]['embedded']['Title'] = 'Informative Album'
                row.update(url='https://www.bedetheque.com/album/index/i/123', edition_record=True)
                if resolved:
                    row['provenance'].update(edition_record_id='123',
                        url='https://www.bedetheque.com/BD-Series-Tome-2-456.html')
                result = {'status': 'identified', 'work': {}, 'field_conflicts': {},
                    'concordant_candidates': [deepcopy(row)],
                    'evidence': bind_evidence(state, 'a'*64),
                    'exact_page_attempts': [{'edition_ref': '123', 'status': 'parsed'}]}
                save_work_result(state['assets']['a'*64], result, providers)
                receipt = result['evidence']['cover_confirmation']
                self.assertEqual(result['status'], 'identified' if resolved else 'review', result)
                self.assertEqual(receipt['web_comparisons'][0]['result'],
                                 'same_work' if resolved else 'unresolved')
                if not resolved:
                    self.assertEqual(result['reason_code'], 'cover_web_unresolved')
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

    def test_own_web_ignores_noisy_fields_but_never_a_wrong_number(self):
        for field, value in (('Title', 'Other Album'), ('Series', 'Other Series'),
                             ('Number', '1'), ('Writer', 'Other Writer')):
            with self.subTest(field=field):
                state, row, providers, response = self.route('https://www.bedetheque.com/BD-Series-Tome-2-123.html')
                state['assets']['a'*64]['embedded'].update({'Title': 'Informative Album', field: value})
                apply_bdnex_decision(state, 'a'*64, providers, response)
                result = state['assets']['a'*64]['work_result']
                if field == 'Number':
                    self.assertEqual(result['status'], 'review', result)
                    self.assertIsNone(result['evidence']['cover_confirmation']['basis'])
                else:
                    self.assertEqual(result['status'], 'identified', result)
                    self.assertEqual(result['candidate_concordance']['mode'], 'web')
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
        self.assertEqual(result['status'], 'no_match', result)
        self.assertEqual(result['candidate_concordance']['mode'], 'none')
        providers.get_binary.assert_not_called()

    def test_own_web_identifies_without_hydration_but_legacy_web_conflict_blocks(self):
        for mode in ('web', 'canonical'):
            state, row, providers, response = self.route('https://www.bedetheque.com/BD-Series-Tome-2-123.html')
            state['assets']['a'*64]['embedded']['Title'] = 'Informative Album'
            if mode == 'web':
                state['assets']['a'*64]['embedded']['Web'] = row['url'].replace('Series-Tome-2-123', 'Other-Series-Tome-2-999')
            else:
                original = providers.get
                providers.get = lambda url: {**original(url), 'body': original(url)['body'].replace('123.html', '999.html')}
            apply_bdnex_decision(state, 'a'*64, providers, response)
            result = state['assets']['a'*64]['work_result']
            if mode == 'web':
                self.assertEqual(result['status'], 'review', result)
                self.assertEqual(result['reason_code'], 'cover_web_conflict')
            else:
                self.assertEqual(result['status'], 'identified', result)
                self.assertEqual(result['candidate_concordance']['mode'], 'web')
                self.assertEqual(result['exact_page_attempts'], [])
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
                (canonical.replace('123.html', '999.html'), 100, 'identified', 'web')):
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
