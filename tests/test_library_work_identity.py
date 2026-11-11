import unittest
from library.work_identity import evaluate_work


def reference(**values):
    return {'source': 'bedetheque', 'upstream': 'bedetheque',
            'url': 'https://www.bedetheque.com/BD-Fixture-1.html',
            'title': 'Le Grand Voyage', **values}


class WorkIdentityTests(unittest.TestCase):
    def test_standalone_title_bound_to_file_identifies_without_an_edition(self):
        result = evaluate_work({'title': 'Grand Voyage (Le)', 'asset_bound': True}, [reference()])
        self.assertEqual(result['status'], 'identified')
        self.assertEqual(result['edition_status'], 'unknown')
        self.assertNotIn('series', result['work'])
        self.assertNotIn('number', result['work'])
        self.assertNotIn('isbn', result['work'])


class ConcordanceSafetyTests(unittest.TestCase):
    def test_homonymous_works_with_different_authors_abstain_and_keep_candidates(self):
        candidates = [reference(author='Alice', work_ref='work-a', edition_ref='edition-a'),
                      reference(author='Bob', work_ref='work-b', edition_ref='edition-b')]
        result = evaluate_work({'title': 'Le Grand Voyage', 'asset_bound': True}, candidates)
        self.assertEqual(result['status'], 'review')
        self.assertIn('creator', result['reason'])
        self.assertEqual(result['candidates'], candidates)
        self.assertEqual(result['work'], {})

    def test_conflicting_primary_contributors_abstain_without_a_flat_author(self):
        candidates = [reference(contributors=[{'role': '(Scénario)', 'name': name}]) for name in ('Alice', 'Bob')]
        result = evaluate_work({'title': 'Le Grand Voyage', 'asset_bound': True}, candidates)
        self.assertEqual(result['status'], 'review')
        self.assertIn('creator', result['reason'])

    def test_duplicate_ids_and_missing_creators_do_not_invent_identity_conflicts(self):
        for candidates in ([reference(work_ref='a'), reference(work_ref='b')],
                           [reference(author='Alice', work_ref='a'), reference(author='Alice', work_ref='b')],
                           [reference(author='Alice'), reference(author=None)]):
            with self.subTest(candidates=candidates):
                result = evaluate_work({'title': 'Le Grand Voyage', 'asset_bound': True}, candidates)
                self.assertEqual(result['status'], 'identified')
                self.assertEqual(result['edition_status'], 'unknown')
                self.assertEqual(len(result['concordant_candidates']), 2)

    def test_same_work_editions_keep_secondary_conflicts_and_partial_creator_credits(self):
        candidates = [reference(author='Alice', work_ref='same', publisher='A', date='2000', isbn='123',
                                contributors={'writer': ['Alice', 'Bob'], 'colorist': ['C']}),
                      reference(author='Alice', work_ref='same', publisher='B', date='2001', isbn='456',
                                contributors={'writer': ['Alice'], 'colorist': ['D']})]
        result = evaluate_work({'title': 'Le Grand Voyage', 'asset_bound': True}, candidates)
        self.assertEqual(result['status'], 'identified')
        self.assertEqual(result['work']['author'], 'Alice')
        self.assertEqual(result['edition_status'], 'unknown')
        for field in ('publisher', 'date', 'isbn'):
            self.assertNotIn(field, result['work'])
            self.assertIn(field, result['field_conflicts'])

    def test_parent_and_generic_titles_cannot_identify(self):
        for evidence in ({'title': 'Le Grand Voyage'}, {'title': 'Intégrale', 'asset_bound': True},
                         {'labels': ['Le Grand Voyage'], 'asset_bound': True}):
            self.assertEqual(evaluate_work(evidence, [reference()])['status'], 'review')

    def test_series_number_filters_unrelated_results(self):
        result = evaluate_work({'series': 'Voyages', 'number': '01', 'asset_bound': True},
            [reference(series='Voyages', number=1), reference(title='Un autre album', series='Autres', number=1)])
        self.assertEqual(result['status'], 'identified')
        self.assertEqual(len(result['concordant_candidates']), 1)

    def test_conflicting_file_assertions_block_even_with_one_matching_result(self):
        evidence = {'title': 'Le Grand Voyage', 'asset_bound': True,
                    'provenance': {'title': [{'value': 'Un autre album', 'scope': 'asset', 'source': 'embedded'}]}}
        self.assertEqual(evaluate_work(evidence, [reference()])['status'], 'review')
        self.assertEqual(evaluate_work({'identity_conflicts': {'title': ['A', 'B']}}, [reference()])['status'], 'review')

    def test_multiple_editions_keep_common_fields_and_omit_secondary_conflicts(self):
        result = evaluate_work({'title': 'Le Grand Voyage', 'asset_bound': True},
            [reference(publisher='A', summary='Shared', isbn='123'), reference(publisher='B', summary='Shared', isbn='456')])
        self.assertEqual(result['status'], 'identified')
        self.assertEqual(result['work']['summary'], 'Shared')
        self.assertNotIn('publisher', result['work'])
        self.assertIn('isbn', result['field_conflicts'])

    def test_ambiguous_identity_and_incomplete_discovery_abstain(self):
        evidence = {'title': 'Le Grand Voyage', 'asset_bound': True}
        self.assertEqual(evaluate_work(evidence, [reference(series='A'), reference(series='B')])['status'], 'review')
        self.assertEqual(evaluate_work({**evidence, 'discovery_truncated': True}, [reference()])['status'], 'review')

    def test_classification_requires_sources_and_never_infers_age(self):
        from library.work_identity import classify_work
        assertions = [{'field': 'family', 'value': 'BD', 'source': 'bedetheque', 'url': 'https://example.test/1'},
                      {'field': 'genres', 'value': ['Aventure', 'Érotique'], 'source': 'bedetheque', 'url': 'https://example.test/1'},
                      {'field': 'recommended_age', 'value': 18}]
        result = classify_work({}, assertions)
        self.assertEqual(result['family'], 'BD')
        self.assertIs(result['adult'], True)
        self.assertIsNone(result['recommended_age'])

    def test_common_genres_survive_edition_specific_tags(self):
        result = evaluate_work({'title': 'Le Grand Voyage', 'asset_bound': True},
            [reference(genres=['Aventure', 'Fantastique']), reference(genres=['Aventure', 'Historique'])])
        self.assertEqual(result['work']['genres'], ['Aventure'])
        self.assertIn('genres', result['field_conflicts'])

    def test_integral_identity_conflict_blocks(self):
        result = evaluate_work({'title': 'Le Grand Voyage', 'asset_bound': True},
            [reference(integral=True), reference(integral=False)])
        self.assertEqual(result['status'], 'review')

    def test_classification_conflict_does_not_choose_a_source(self):
        from library.work_identity import classify_work
        assertions = [{'field': 'family', 'value': value, 'source': 'reference', 'url': 'https://example.test/' + value}
                      for value in ['BD', 'Manga']]
        result = classify_work({}, assertions)
        self.assertIsNone(result['family'])
        self.assertIn('family', result['field_conflicts'])
