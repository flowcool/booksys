"""Synthetic asset labels at the candidate decision boundary."""
import unittest

from library.candidate_concordance import decide


def evidence(*labels, embedded=None):
    return {'asset': 'a'*64, 'asset_bound': True, 'embedded': embedded or {},
            'labels': list(labels), 'provenance': {'labels': [
                {'value': label, 'scope': 'asset', 'source': 'origin:synthetic'} for label in labels]}}


def candidate(series='Sky Patrol', number='3', title='The Silent Island', ref='123', **extra):
    return {'source': 'bedetheque', 'series': series, 'number': number, 'title': title,
            'edition_ref': ref, 'url': f'https://www.bedetheque.com/BD-Fixture-{ref}.html', **extra}


class CandidateConcordanceTests(unittest.TestCase):
    def test_complete_label_words_identify_despite_parsed_field_noise(self):
        asset = evidence('zzSky Patrol - tome 03 : The Silent Island @source [Digital]')
        asset.update(title='Scanner Name', identity_conflicts={'title': ['noise']})
        result = decide(asset, [candidate()])
        self.assertEqual(result.candidate['edition_ref'], '123')
        self.assertEqual(result.mode, 'cov')
        self.assertFalse(result.needs_cover)
        self.assertIn('island', result.matched_words)

    def test_web_uses_embedded_id_despite_noisy_label_title(self):
        row = candidate(series='Imperial City', number='8', title='The Last Gate')
        asset = evidence('Imperial City T8 - Scanner Person', embedded={
            'Number': '08', 'Web': row['url']})
        self.assertEqual(decide(asset, [row]).mode, 'web')
        asset['embedded']['Number'] = '7'
        self.assertIsNone(decide(asset, [row]).candidate)

    def test_number_first_title_is_sufficient(self):
        self.assertEqual(decide(evidence('03 The Silent Island'), [candidate()]).mode, 'title_number')

    def test_generic_and_undated_file_need_cover(self):
        for row in (candidate(title='Sky Patrol'), candidate(series='Sky Patrol 1988')):
            with self.subTest(row=row):
                result = decide(evidence('Sky Patrol T3'), [row])
                self.assertTrue(result.needs_cover)

    def test_conflicting_label_title_number_digit_and_extra_series_words_abstain(self):
        cases = [(['Sky Patrol T3 - The Crimson Forest'], candidate()),
                 (['Sky Patrol T4 - The Silent Island'], candidate()),
                 (['Sky Patrol T3 - The Silent Island', 'Sky Patrol T4'], candidate()),
                 (['Sky Patrol T3 T4 - The Silent Island'], candidate()),
                 (["L'As T3"], candidate(series='4 As', title='As')),
                 (['Sky Patrol Junior T3'], candidate(title='Sky Patrol'))]
        for labels, row in cases:
            with self.subTest(labels=labels):
                self.assertIsNone(decide(evidence(*labels), [row]).candidate)

    def test_two_works_abstain_regardless_of_scores_or_matching_mode(self):
        rows = [candidate(score=99), candidate(title='Silent Island', ref='124', score=60)]
        self.assertEqual(decide(evidence('Sky Patrol T3 The Silent Island'), rows).mode, 'ambiguous')

    def test_editions_collapse_without_selecting_the_highest_score(self):
        rows = [candidate(series='Sky Patrol Special Edition', ref='124', score=99), candidate(score=60)]
        result = decide(evidence('Sky Patrol T3 The Silent Island',
                                 'Sky Patrol Special Edition T3 The Silent Island'), rows)
        self.assertEqual(result.candidate['edition_ref'], '123')

    def test_normalization_preserves_identity_words(self):
        row = candidate(series='Les Cités IXe', title='Éclats (Le)')
        asset = evidence('zzCitÃ© IXème - T03 - E\u0301clat @scanner')
        self.assertEqual(decide(asset, [row]).mode, 'cov')

    def test_parent_labels_and_unbound_embedded_data_cannot_identify(self):
        asset = evidence('Sky Patrol T3 The Silent Island')
        asset['provenance']['labels'][0]['scope'] = 'discovery'
        self.assertIsNone(decide(asset, [candidate()]).candidate)

    def test_runtime_evaluation_records_r3_before_parsed_conflicts(self):
        from library.work_identity import evaluate_work
        from library.cover_confirmation import finalize
        asset = evidence('Sky Patrol tome 3 : The Silent Island')
        asset.update(title='Noise', identity_conflicts={'title': ['Noise', 'Other']})
        result = evaluate_work(asset, [candidate()])
        result['evidence'] = asset
        finalize({'sha256': 'a'*64}, result, None)
        self.assertEqual(result['status'], 'identified', result)
        self.assertEqual(result['candidate_concordance']['method'], 'candidate-concordance-r3-v1')
        self.assertEqual(result['evidence']['cover_confirmation']['basis'], 'cov')

    def test_exact_series_alias_applies_when_r3_has_no_candidate(self):
        from library.work_identity import evaluate_work
        asset = evidence('Big Bull T3 The Silent Island')
        asset.update(series='Big Bull', number='3', title='The Silent Island')
        row = candidate(series='Big Bull (Impéria)', field_provenance={
            'series': {'file_series_alias': 'big bull'}})
        result = evaluate_work(asset, [row])
        self.assertEqual(result['status'], 'identified')
        self.assertEqual(result['concordant_candidates'], [row])
        self.assertEqual(result['candidate_concordance']['mode'], 'none')

    def test_exact_dated_series_reaches_cover_when_r3_has_no_candidate(self):
        from library.cover_confirmation import finalize
        from library.work_identity import evaluate_work
        asset = evidence('Sky Patrol Archive T3')
        asset.update(series='Sky Patrol', number='3')
        row = candidate(series='Sky Patrol 1988')
        result = evaluate_work(asset, [row])
        self.assertEqual(result['status'], 'identified')
        self.assertEqual(result['concordant_candidates'], [row])
        self.assertEqual(result['candidate_concordance']['mode'], 'none')
        result['evidence'] = asset
        finalize({'sha256': asset['asset']}, result, None)
        self.assertEqual(result['reason_code'], 'cover_canonical_cover_unavailable')

    def test_exact_series_and_number_with_volume_title_reach_cover(self):
        from library.cover_confirmation import finalize
        from library.work_identity import evaluate_work
        asset = evidence('Sky Patrol Archive T3')
        asset.update(series='Sky Patrol', number='3', title='Volume 3')
        row = candidate(title='Volume 3')
        result = evaluate_work(asset, [row])
        self.assertEqual(result['status'], 'identified')
        self.assertEqual(result['concordant_candidates'], [row])
        self.assertEqual(result['candidate_concordance']['mode'], 'none')
        self.assertNotIn('title', result['work'])
        result['evidence'] = asset
        finalize({'sha256': asset['asset']}, result, None)
        self.assertEqual(result['reason_code'], 'cover_canonical_cover_unavailable')

    def test_exact_title_resolves_r3_ambiguity(self):
        from library.cover_confirmation import finalize
        from library.work_identity import evaluate_work
        asset = evidence('Sky Patrol T3 The Silent Island', 'Sky Patrol T3 Silent Island Returns')
        asset.update(series='Sky Patrol', number='3')
        asset['provenance']['title'] = [{'value': 'The Silent Island', 'scope': 'asset',
                                        'source': 'origin:synthetic'}]
        row = candidate(score=75)
        other = candidate(title='Silent Island Returns', ref='124', score=99)
        result = evaluate_work(asset, [row, other])
        self.assertEqual(result['concordant_candidates'], [row])
        self.assertEqual(result['candidate_concordance']['mode'], 'ambiguous')
        result['evidence'] = asset
        finalize({'sha256': asset['asset']}, result, None)
        self.assertEqual(result['status'], 'identified')
        self.assertEqual(result['evidence']['cover_confirmation']['reason'], 'asset_title')

    def test_exact_path_vetoes_a_conflicting_bound_title(self):
        from library.work_identity import evaluate_work
        asset = evidence('Sky Patrol T3 The Crimson Forest')
        asset.update(series='Sky Patrol', number='3', title='The Crimson Forest')
        self.assertEqual(evaluate_work(asset, [candidate()])['status'], 'no_match')

    def test_r3_rejects_unnumbered_homonym_for_numbered_file(self):
        asset = evidence('Sky Patrol #03 The Silent Island', embedded={'Number': '3'})
        self.assertIsNone(decide(asset, [candidate(series=None, number=None)]).candidate)

    def test_scan_resolution_and_series_ordinal_are_not_issue_conflicts(self):
        asset = evidence('Sky Patrol (2e series) T3 (2024) [Digital-1200] The Silent Island')
        self.assertEqual(decide(asset, [candidate(series='Sky Patrol 2e series')]).mode, 'cov')

    def test_unsupported_series_year_keeps_cover_even_with_web(self):
        row = candidate(series='Sky Patrol 1988')
        asset = evidence('Sky Patrol T3 The Silent Island', embedded={'Web': row['url']})
        self.assertTrue(decide(asset, [row]).needs_cover)
        asset['embedded']['Year'] = '1989'
        self.assertIsNone(decide(asset, [row]).candidate)

    def test_embedded_numbered_title_cannot_contradict_the_issue(self):
        asset = evidence('Sky Patrol T3 The Silent Island', embedded={
            'Title': 'Tome 4 - The Silent Island', 'Number': '3'})
        self.assertIsNone(decide(asset, [candidate()]).candidate)

    def test_edition_differences_do_not_create_edition_metadata(self):
        from library.work_identity import evaluate_work
        rows = [candidate(ref='124', publisher='A'), candidate(publisher='B')]
        result = evaluate_work(evidence('Sky Patrol T3 The Silent Island'), rows)
        self.assertEqual(result['status'], 'identified')
        self.assertNotIn('publisher', result['work'])
        self.assertIn('publisher', result['field_conflicts'])

    def test_recorded_strong_cover_disagreement_blocks_direct_r3(self):
        from library.work_identity import evaluate_work
        from library.cover_confirmation import finalize, METHOD
        asset = evidence('Sky Patrol T3 The Silent Island')
        result = evaluate_work(asset, [candidate()])
        result['evidence'] = asset
        prior = {'evidence': {'cover_confirmation': {'asset_sha256': 'a'*64,
                 'album_id': '123', 'method': METHOD, 'score': 9.99}}}
        finalize({'sha256': 'a'*64, 'work_result': prior}, result, None)
        self.assertEqual(result['status'], 'review')
        self.assertEqual(result['reason_code'], 'cover_strong_cover_disagreement')

    def test_metadata_accepts_r3_before_service_insufficient_or_conflicting_fields(self):
        from copy import deepcopy
        from library.metadata import prepare_enrichment
        key = 'a'*64
        class Snapshot:
            def result_set(self, *_):
                return {'assets': {key: {'sha256': key, 'origins': ['file'], 'embedded': {}}},
                        'origins': {'file': {'sha256': key, 'source': 'external:fixture',
                                           'path': 'Sky Patrol tome 3 The Silent Island.cbz'}}}
        class Provider:
            events = []
            def __init__(self, reason):
                self.bdnex = self
                self.reason = reason
            def match(self, *_, **kw):
                return {'status': 'ok', 'decision': {'status': 'review', 'reason': self.reason},
                        'candidates': [deepcopy(candidate())]}
            def get(self, *_):
                raise AssertionError('Direct R3 must not require a page request')
        for reason in ('insufficient', 'conflicting_file_evidence'):
            with self.subTest(reason=reason):
                result = prepare_enrichment(Snapshot(), key, Provider(reason), [])['assets'][key]['work_result']
                self.assertEqual(result['status'], 'identified', result)
                self.assertEqual(result['candidate_concordance']['mode'], 'cov')

    def test_cover_route_survives_parsed_title_conflicts_until_cover_gate(self):
        from library.work_enrichment import apply_bdnex_decision
        key = 'a'*64
        state = {'assets': {key: {'sha256': key, 'origins': ['file'],
                                  'embedded': {'Title': 'Parser Noise'}}},
                 'origins': {'file': {'sha256': key, 'source': 'external:fixture',
                                     'path': 'Sky Patrol T3.cbz'}}}
        class Provider:
            events = []
            def get(self, *_):
                return {'status': 'unavailable', 'reason': 'Offline page boundary'}
        response = {'status': 'ok', 'decision': {'status': 'review', 'reason': 'insufficient'},
                    'candidates': [candidate(title='Sky Patrol')]}
        self.assertTrue(apply_bdnex_decision(state, key, Provider(), response))
        result = state['assets'][key]['work_result']
        self.assertEqual(result['status'], 'review')
        self.assertEqual(result['candidate_concordance']['mode'], 'cov:cover')
        self.assertEqual(result['reason_code'], 'cover_page_unavailable')
