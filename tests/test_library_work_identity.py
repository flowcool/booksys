import unittest
from library.work_identity import evaluate_work


def reference(**values):
    return {'source': 'bedetheque', 'upstream': 'bedetheque',
            'url': 'https://www.bedetheque.com/BD-Fixture-1.html',
            'title': 'Le Grand Voyage', **values}


class WorkIdentityTests(unittest.TestCase):
    def test_dotted_leading_article_agrees_with_album_title(self):
        result = evaluate_work({'title': 'Le.roi.assassin', 'asset_bound': True},
                               [reference(title='Le Roi assassin')])
        self.assertEqual(result['status'], 'identified')

    def test_standalone_title_bound_to_file_identifies_without_an_edition(self):
        result = evaluate_work({'title': 'Grand Voyage (Le)', 'asset_bound': True}, [reference()])
        self.assertEqual(result['status'], 'identified')
        self.assertNotIn('series', result['work'])
        self.assertNotIn('number', result['work'])
        self.assertNotIn('isbn', result['work'])

    def test_numbered_album_with_a_generic_title_keeps_its_series_identity(self):
        evidence = {'series': 'Joe Bar Team', 'number': '04', 'asset_bound': True}
        candidates = [reference(title=title, series='Joe Bar Team', number=4)
                      for title in ('Tome 4', 'Volume 4')]
        result = evaluate_work(evidence, candidates)
        self.assertEqual(result['status'], 'identified')
        self.assertEqual(result['work'], {'series': 'Joe Bar Team', 'number': 4})
        self.assertEqual(result['concordant_candidates'], candidates)
        self.assertEqual(result['field_conflicts'], {})

    def test_generic_title_does_not_override_conflicting_identity(self):
        evidence = {'series': 'Akim', 'number': 4, 'asset_bound': True}
        for asset, candidate in (
            (evidence, reference(title='Tome 4', series='Akim Color', number=4)),
            (evidence, reference(title='Tome 3', series='Akim', number=3)),
            (evidence, reference(title='Tome 3', series='Akim', number=4)),
            (evidence, reference(title='Intégrale', series='Akim', number=4)),
            ({**evidence, 'title': 'Le Grand Voyage'},
             reference(title='Tome 4', series='Akim', number=4)),
            ({**evidence, 'integral': False},
             reference(title='Tome 4', series='Akim', number=4, integral=True)),
        ):
            with self.subTest(asset=asset, candidate=candidate):
                self.assertEqual(evaluate_work(asset, [candidate])['status'], 'no_match')

    def test_reports_fields_that_prevent_candidate_concordance(self):
        evidence = {'title': 'Le Grand Voyage', 'series': 'Voyages', 'number': 4, 'asset_bound': True}
        candidates = [reference(title='Une autre aventure', series='Voyages', number=4),
                      reference(series='Voyages', number=3)]
        result = evaluate_work(evidence, candidates)
        self.assertEqual(result['status'], 'no_match')
        self.assertEqual(result['rejected_fields'], ['number', 'title'])
        self.assertNotIn('rejected_fields', evaluate_work(evidence, []))

    def test_generic_titles_keep_series_year_ambiguity_and_label_safeguards(self):
        evidence = {'series': 'Batman', 'number': 5, 'asset_bound': True}
        first = reference(title='Volume 5', series='Batman (1940)', number=5)
        second = reference(title='Tome 5', series='Batman 2011', number=5)
        self.assertEqual(evaluate_work(evidence, [first, second])['status'], 'review')
        self.assertEqual(evaluate_work({**evidence, 'labels': ['Batman (1941) T5']}, [first])['status'],
                         'no_match')
        self.assertEqual(evaluate_work({**evidence, 'labels': ['Batman (1940) T5']}, [first])['status'],
                         'identified')


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
                self.assertEqual(len(result['concordant_candidates']), 2)

    def test_same_work_editions_keep_secondary_conflicts_and_partial_creator_credits(self):
        candidates = [reference(author='Alice', work_ref='same', publisher='A', date='2000', isbn='123',
                                contributors={'writer': ['Alice', 'Bob'], 'colorist': ['C']}),
                      reference(author='Alice', work_ref='same', publisher='B', date='2001', isbn='456',
                                contributors={'writer': ['Alice'], 'colorist': ['D']})]
        result = evaluate_work({'title': 'Le Grand Voyage', 'asset_bound': True}, candidates)
        self.assertEqual(result['status'], 'identified')
        self.assertEqual(result['work']['author'], 'Alice')
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

    def test_series_trailing_year_requires_the_same_number(self):
        evidence = {'series': 'Housewives at Play', 'number': 12, 'asset_bound': True}
        for series in ('Housewives at Play 1999', 'Housewives at Play (1999)'):
            for number, status in ((12, 'identified'), (13, 'no_match')):
                with self.subTest(series=series, number=number):
                    result = evaluate_work(evidence, [reference(series=series, number=number)])
                    self.assertEqual(result['status'], status)

    def test_series_with_distinct_trailing_years_remain_in_conflict(self):
        result = evaluate_work({'series': 'Batman', 'number': 5, 'asset_bound': True},
            [reference(series='Batman 1940', number=5), reference(series='Batman 2011', number=5)])
        self.assertEqual(result['status'], 'review')
        self.assertEqual(result['reason'], 'Concordant references describe different works')
        self.assertIn('series', result['field_conflicts'])

    def test_exact_series_wins_over_a_same_name_series_with_a_year(self):
        evidence = {'series': 'Sunstone', 'number': 2, 'asset_bound': True}
        exact = reference(series='Sunstone', number=2)
        before = evaluate_work(evidence, [exact])
        self.assertEqual(before['status'], 'identified')
        self.assertEqual(evaluate_work(evidence,
            [reference(series='Sunstone 2011', number=2), exact])['concordant_candidates'],
            before['concordant_candidates'])
        after = evaluate_work(evidence, [exact, reference(series='Sunstone 2011', number=2)])
        self.assertEqual(after['status'], before['status'])
        self.assertEqual(after['work'], before['work'])

    def test_label_year_must_agree_with_the_relaxed_series_year(self):
        for labels, status in ((['Magenta (2006) Vol.2'], 'no_match'),
                               (['Magenta (2008) Vol.2'], 'identified'),
                               (['Magenta Vol.2'], 'identified'),
                               (['Magenta (2008) Vol.2', 'Magenta (2006) Vol.2'], 'no_match')):
            with self.subTest(labels=labels):
                result = evaluate_work({'series': 'Magenta', 'number': 2,
                    'asset_bound': True, 'labels': labels}, [reference(series='Magenta 2008', number=2)])
                self.assertEqual(result['status'], status)

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
