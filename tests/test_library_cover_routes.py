"""Actual automatic entry points must reach the common cover finalizer."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from library.metadata import prepare_enrichment
from library.work_enrichment import apply_bdnex_decision, enrich_work
from library.french_metadata import DiscoveryResult, ReferenceCandidate
import test_library_cover_guard as guard_tests


class CoverRouteTests(unittest.TestCase):
    def setup_route(self):
        state, row, proposal = guard_tests.CoverGuardTests().fixture()
        row['provenance'] = {'method': 'bdnex-sitemap-1'}
        row['score'] = 94
        row.pop('cover_binding')
        url = row['url']
        html = (f'<link rel="canonical" href="{url}"><h1><a>Series</a></h1>'
                '<meta itemprop="name" content="Series - Informative Album">'
                '<meta itemprop="alternativeheadline" content="Tome 2">'
                '<img itemprop="image" src="https://www.bedetheque.com/media/123.jpg">')
        class Public:
            bdnex = None
            def __init__(self):
                self.events = []
            def get(self, requested):
                return {'status': 'ok', 'body': 'User-agent: *\nAllow: /' if requested.endswith('robots.txt') else html,
                        'retrieved_at': '2026-11-22T00:00:00+00:00'}
            def discover(self, *a, **kw):
                return {}
            def resolve_discovery(self, *a):
                return []
        return state, row, Public()

    def test_numbered_comicinfo_title_requires_an_agreeing_issue(self):
        from bdnex_service.engine import Engine

        state, row, providers = self.setup_route()
        key = 'a'*64
        state['assets'][key]['embedded']['Title'] = 'Tome 02 - Informative Album'
        state['assets'][key]['origins'] = ['o']
        state['origins']['o'] = {'sha256': key, 'path': 'Series.cbz'}
        row['score'] = 83
        other = {**row, 'edition_ref': '124', 'number': '3', 'score': 85}
        engine = Engine.__new__(Engine)
        engine.numbered = {'2': [('Series', '123')], '3': [('Series', '124')]}
        candidates = [other, row]

        def match(*args, identity=None):
            response = {'status': 'ok', 'candidates': candidates}
            if identity is not None:
                response['decision'] = engine.decide(candidates, identity)
            return response

        providers.bdnex = SimpleNamespace(match=match)
        snapshot = SimpleNamespace(result_set=lambda *a: deepcopy(state))
        with patch('library.work_enrichment.discover', return_value=DiscoveryResult('no_candidate')):
            result = prepare_enrichment(snapshot, key, providers, [])['assets'][key]['work_result']
            self.assertEqual(result['exact_page_attempts'], [{'edition_ref': '123', 'status': 'parsed'}])
            self.assertEqual(result['status'], 'identified')
            self.assertEqual(result['work']['title'], 'Informative Album')
            self.assertEqual(result['evidence']['cover_confirmation']['reason'], 'asset_title')
            embedded = state['assets'][key]['embedded']
            self.assertEqual(embedded['Title'], 'Tome 02 - Informative Album')
            self.assertEqual(result['evidence']['provenance']['title'][0]['raw_value'], embedded['Title'])

            embedded['Title'] = 'Tome 03 - Informative Album'
            result = prepare_enrichment(snapshot, key, providers, [])['assets'][key]['work_result']
            self.assertEqual(result['exact_page_attempts'], [])
            self.assertEqual(result['status'], 'no_match')

            embedded['Title'] = 'Tome 02 - T03'
            result = prepare_enrichment(snapshot, key, providers, [])['assets'][key]['work_result']
            self.assertEqual(result['evidence']['title'], 'Tome 02 - T03')

            embedded['Title'] = 'Tome 02 - Informative Album'
            embedded.pop('Number')
            source = 'https://www7.bookys-ebooks.com/bandes-dessinees/1-series'
            state['origins']['o']['source'] = source
            state['assets'][key]['captured_hints'] = {source: {'title': 'Series T02'}}
            for hints in ({}, state['assets'][key]['captured_hints']):
                with self.subTest(captured_issue=bool(hints)):
                    state['assets'][key]['captured_hints'] = hints
                    result = prepare_enrichment(snapshot, key, providers, [])['assets'][key]['work_result']
                    self.assertEqual(result['exact_page_attempts'], [])
                    self.assertEqual(result['status'], 'no_match')

    def test_unique_numbered_proposal_hydrates_one_album_before_confirmation(self):
        from bdnex_service.engine import Engine
        from library.bdnex_protocol import validate_decision

        state, row, providers = self.setup_route()
        key = 'a'*64
        asset = state['assets'][key]
        asset['origins'] = ['o']
        state['origins']['o'] = {'sha256': key, 'path': 'Comic.cbz'}
        row.update(score=83, url='https://www.bedetheque.com/BD-Series-Number-2-123.html')
        engine = Engine.__new__(Engine)
        engine.numbered = {}
        other = {**row, 'edition_ref': '124', 'url': 'https://www.bedetheque.com/BD-Other-124.html',
                 'series': 'Other Series'}
        candidates = [row, other]
        pages = []
        title = 'Informative Album'
        reference_url = row['url']

        def get(url):
            if url.endswith('/robots.txt'):
                return {'status': 'ok', 'body': 'User-agent: *\nAllow: /'}
            pages.append(url)
            return {'status': 'ok', 'retrieved_at': '2026-11-22T00:00:00+00:00', 'body': (
                f'<link rel="canonical" href="{reference_url}"><h1><a>Series</a></h1>'
                f'<meta itemprop="name" content="Series - {title}">'
                '<meta itemprop="alternativeheadline" content="Tome 2">'
                '<img itemprop="image" src="https://www.bedetheque.com/media/123.jpg">')}

        def match(*args, identity=None):
            decision = engine.decide(candidates, identity or {})
            validate_decision(decision, candidates)
            return {'status': 'ok', 'candidates': candidates, 'decision': decision}

        providers.get = get
        providers.bdnex = SimpleNamespace(match=match)
        snapshot = SimpleNamespace(result_set=lambda *a: deepcopy(state))
        with patch('library.work_enrichment.discover', return_value=DiscoveryResult('no_candidate')):
            result = prepare_enrichment(snapshot, key, providers, [])['assets'][key]['work_result']
            self.assertEqual(pages, [row['url']])
            self.assertEqual(result['bdnex_decision'], {
                'status': 'proposed', 'reason': 'unique_agreement', 'edition_ref': '123',
            })
            self.assertEqual(result['exact_page_attempts'], [{'edition_ref': '123', 'status': 'parsed'}])
            self.assertEqual(result['status'], 'review')
            self.assertEqual(result['reason_code'], 'cover_archive_unavailable')

            asset['embedded'] = {'Title': title, 'Number': '2'}
            candidates[:] = [row, {**other, 'title': 'Other Album'}]
            pages.clear()
            result = prepare_enrichment(snapshot, key, providers, [])['assets'][key]['work_result']
            self.assertEqual(pages, [row['url']])
            self.assertEqual(result['status'], 'identified')
            self.assertEqual(result['evidence']['cover_confirmation']['reason'], 'asset_title')

            title = 'Conflicting Album'
            pages.clear()
            result = prepare_enrichment(snapshot, key, providers, [])['assets'][key]['work_result']
            self.assertEqual(pages, [row['url']])
            self.assertEqual(result['reason_code'], 'cover_identity_conflict')

            for label, embedded, row_changes, expected in (
                ('ambiguous', {'Series': 'Series', 'Number': '2'}, {'series': 'Series'}, 'review'),
                ('dated', {'Series': 'Series', 'Number': '2'}, {'series': 'Series 2011'}, 'no_match'),
                ('wrong number', {'Series': 'Series', 'Number': '3'}, {'series': 'Series'}, 'no_match'),
                ('title veto', {'Series': 'Series', 'Number': '2', 'Title': 'Other Title'},
                 {'series': 'Series'}, 'no_match'),
            ):
                with self.subTest(label=label):
                    asset['embedded'] = embedded
                    candidates[:] = [{**row, **row_changes}, {**other, **row_changes}] if label == 'ambiguous' else [
                        {**row, **row_changes}, other]
                    pages.clear()
                    result = prepare_enrichment(snapshot, key, providers, [])['assets'][key]['work_result']
                    self.assertEqual(pages, [])
                    self.assertEqual(result['status'], expected)

    def assert_guarded(self, state):
        result = state['assets']['a'*64]['work_result']
        self.assertEqual(result['status'], 'review', result)
        self.assertEqual(result['work'], {})
        self.assertIn('cover_confirmation', result['evidence'])
        self.assertNotIn('classification', result)
        return result

    def test_fresh_and_retained_strong_and_numbered_decisions(self):
        for reason in ('strong', 'numbered'):
            state, row, providers = self.setup_route()
            response = {'candidates': [row], 'decision': {'status': 'selected', 'reason': reason, 'edition_ref': '123'}}
            with self.subTest(reason=reason):
                self.assertTrue(apply_bdnex_decision(state, 'a'*64, providers, response))
                result = self.assert_guarded(state)
                # An old accepted result has no valid cover receipt.
                result.update(status='identified', work={'title': 'Informative Album'})
                result['evidence'].pop('cover_confirmation')
                apply_bdnex_decision(state, 'a'*64, providers, response)
                self.assert_guarded(state)

    def test_identified_asset_keeps_previous_evidence_after_method_bumps(self):
        from datetime import datetime, timezone
        from library.automatic import evidence_version
        from library.business_stages import BusinessStages
        from library.stage_model import METHODS, Obligation, Task

        state, row, providers = self.setup_route()
        key = 'a'*64
        asset = state['assets'][key]
        asset.update(status='validated', origins=['o'], bdnex={'status': 'ok', 'candidates': [row]})
        state['origins']['o'] = {'availability': 'validated'}
        previous = {'status': 'identified', 'work': {'series': 'Series', 'number': '2'},
                    'evidence': {'asset': key, 'previous_method': 'pre-cover-guard'}}
        asset['work_result'] = deepcopy(previous)
        asset['automation'] = {'method': 'work-metadata-2026-11-22-v5-web-cover', 'evidence': 'old'}
        asset['stage_metadata'] = {'method': 'metadata-step-v5-web-cover', 'evidence': 'old',
                                  'evaluation': 'prior', 'outcome': 'done',
                                  'next_at': '2026-11-01T00:00:00+00:00'}
        at = datetime(2026, 11, 22, tzinfo=timezone.utc)
        # The previous evidence revision differs. A method bump and an expired
        # schedule still must not enqueue an already accepted identity.
        self.assertNotEqual(asset['stage_metadata']['evidence'], evidence_version(asset))
        self.assertNotIn('metadata', [o.stage for o in BusinessStages.obligations(state, key, now=at)])
        # A task queued before the update also reaches the normal worker step.
        # Use a detached catalogue adapter; no DB or provider is allowed.
        snapshot = SimpleNamespace(result_set=lambda *a: deepcopy(state), prepare=lambda s: s)
        providers.get = lambda *a: self.fail('Accepted identity must not fetch a page')
        providers.bdnex = SimpleNamespace(match=lambda *a, **kw: self.fail('Must not query BDneX'))
        obligation = Obligation('metadata', key, 'prior', METHODS['metadata'])
        task = Task(obligation.key, 'metadata', key, 'prior', METHODS['metadata'], None, at.isoformat())
        stages = BusinessStages(snapshot, Path('/unused'), Path('/unused'), providers)
        completed = stages.execute(task, now=at)
        self.assertEqual(completed.prepared['assets'][key]['work_result'], previous)
        self.assertNotIn('metadata', [o.stage for o in completed.successors])

    def test_candidate_only_hydration_and_plain_series_number(self):
        for hydrate in (True, False):
            with self.subTest(hydrate=hydrate):
                state, row, providers = self.setup_route()
                enrich_work(state, 'a'*64, providers, [row], hydrate=hydrate, discovery=False)
                self.assert_guarded(state)

    def test_alias_and_dated_series_results(self):
        for file_series, reference_series in (('Big Bull', 'Big Bull Imperia'), ('Series', 'Series 2011')):
            state, row, providers = self.setup_route()
            state['assets']['a'*64]['embedded']['Series'] = file_series
            row['series'] = reference_series
            if file_series == 'Big Bull':
                row['field_provenance'] = {'series': {'file_series_alias': 'big bull'}}
            enrich_work(state, 'a'*64, providers, [row], discovery=False)
            with self.subTest(file_series=file_series):
                self.assert_guarded(state)

    def test_public_discovery_and_explicit_reference_fallback(self):
        state, row, providers = self.setup_route()
        found = DiscoveryResult('ok', [ReferenceCandidate('123', row['url'], 'Series', 'Informative Album', '2')])
        with patch('library.work_enrichment.discover', return_value=found):
            enrich_work(state, 'a'*64, providers)
        self.assert_guarded(state)
        # Final explicit-reference enrichment shares the same persistence seam.
        snapshot = SimpleNamespace(result_set=lambda *a: deepcopy(state))
        providers.source = lambda *a: {'status': 'ok', 'candidate': row}
        with patch('library.work_enrichment.discover', return_value=DiscoveryResult('no_candidate')):
            final = prepare_enrichment(snapshot, 'a'*64, providers, [('bedetheque', '123')])
        self.assert_guarded(final)

    def test_canonical_page_conflict_cannot_be_approved_by_a_cover_or_title_exemption(self):
        state, row, providers = self.setup_route()
        state['assets']['a'*64]['embedded']['Title'] = 'Informative Album'
        original_get = providers.get
        providers.get = lambda url: {**original_get(url), 'body': original_get(url)['body'].replace('Informative Album', 'Other Album')}
        response = {'candidates': [row], 'decision': {'status': 'selected', 'reason': 'strong', 'edition_ref': '123'}}
        apply_bdnex_decision(state, 'a'*64, providers, response)
        result = self.assert_guarded(state)
        self.assertEqual(result['reason_code'], 'cover_identity_conflict')

    def test_selected_page_binding_is_required_and_edition_card_cannot_supply_it(self):
        state, row, providers = self.setup_route()
        row['cover_url'] = 'https://www.bedetheque.com/media/123.jpg'
        # Old rows and inherited edition images have no checked canonical binding.
        row['provenance']['canonical_album_ref'] = '999'
        enrich_work(state, 'a'*64, providers, [row], discovery=False)
        self.assertEqual(self.assert_guarded(state)['reason_code'], 'cover_canonical_cover_unavailable')

    def test_heading_fallback_and_retained_candidate_at_prepare_entry_point(self):
        for retained in (False, True):
            state, row, providers = self.setup_route()
            key = 'a'*64
            source = 'https://www7.bookys-ebooks.com/bandes-dessinees/1-series'
            state['assets'][key]['origins'] = ['o']
            state['assets'][key]['captured_hints'] = {source: {'title': 'Series - T02 - Informative Album'}}
            state['origins']['o'] = {'sha256': key, 'source': source, 'path': 'Series T02.cbz'}
            class Local:
                def __init__(self):
                    self.calls = 0
                def match(self, *a, **kw):
                    self.calls += 1
                    return {'status': 'ok', 'candidates': [] if self.calls == 1 else [row]}
            providers.bdnex = Local()
            if retained:
                state['assets'][key]['work_result'] = {'status': 'identified', 'work': {'title': 'Informative Album'}}
                state['assets'][key]['bdnex'] = {'status': 'ok', 'candidates': [row]}
            snapshot = SimpleNamespace(result_set=lambda *a: deepcopy(state))
            with self.subTest(retained=retained), patch('library.work_enrichment.discover', return_value=DiscoveryResult('no_candidate')):
                result = prepare_enrichment(snapshot, key, providers, [])
                if retained:
                    self.assertEqual(result['assets'][key]['work_result'], state['assets'][key]['work_result'])
                    self.assertEqual(providers.bdnex.calls, 0)
                else:
                    self.assert_guarded(result)
                    self.assertEqual(providers.bdnex.calls, 2)
