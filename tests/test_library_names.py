"""Qualified filename shapes provide assertions, never a selected reference."""
import unittest

from library.names import parse_name


class NameTests(unittest.TestCase):
    def test_single_word_standalone_requires_file_and_capture_agreement(self):
        from library.asset_evidence import bind_evidence
        from library.work_identity import evaluate_work
        key = 'a' * 64
        asset = {'sha256': key, 'origins': ['o'], 'embedded': {'Series': 'Avila'},
                 'captured_hints': {'source': {'title': 'Avila (2026)'}}}
        state = {'assets': {key: asset}, 'origins': {'o': {
            'sha256': key, 'path': 'package/ZzAvila.cbz', 'source': 'source'}}}
        candidate = {'title': 'Avila'}
        self.assertEqual(evaluate_work(bind_evidence(state, key), [candidate])['status'], 'identified')
        asset['captured_hints']['source']['title'] = 'Another work'
        self.assertEqual(evaluate_work(bind_evidence(state, key), [candidate])['status'], 'review')

    def test_issue_annotations_do_not_become_part_of_the_series(self):
        self.assertEqual(parse_name('Train De Nuit (1992) #02 - Tome 2.cbz'),
                         {'series': 'Train De Nuit', 'number': '2'})
        self.assertEqual(parse_name('Girls bravo - 4. Tome 4'),
                         {'series': 'Girls bravo', 'number': '4'})
        self.assertEqual(parse_name('Girls bravo - 4. Tome 5'), {})
        self.assertEqual(parse_name('Horde Du Contrevent (La) (2017) Vol.2 #01 - Le Cosmos Est Mon Campement'), {})
        self.assertEqual(parse_name('Série Jaune (Elvifrance) (1983) #100 - Jeux de miroir'),
                         {'series': 'Série Jaune (Elvifrance)', 'number': '100', 'title': 'Jeux de miroir'})

    def test_source_prefix_does_not_create_a_false_series_conflict(self):
        from library.asset_evidence import bind_evidence
        from library.work_identity import evaluate_work
        key = 'a' * 64
        state = {'assets': {key: {'sha256': key, 'origins': ['o'],
            'embedded': {'Series': 'Dépisteur (Le)', 'Number': '2', 'Title': 'Sous terre'}}},
            'origins': {'o': {'sha256': key, 'source': 'source',
                'path': 'zzdépisteur (le) - t02 - sous terre.cbz'}}}
        candidate = {'title': 'Sous terre', 'series': 'Depisteur', 'number': '2'}
        self.assertEqual(evaluate_work(bind_evidence(state, key), [candidate])['status'], 'identified')
        state['assets'][key]['embedded']['Number'] = '3'
        self.assertEqual(evaluate_work(bind_evidence(state, key), [candidate])['status'], 'review')
        self.assertEqual(parse_name('ZZ Top - T01.cbz')['series'], 'ZZ Top')

    def test_explicit_french_volume_and_title(self):
        self.assertEqual(parse_name('Les Geeks - T03 - Le peuple des geeks.cbz'),
                         {'series': 'Les Geeks', 'number': '3', 'title': 'Le peuple des geeks'})

    def test_unqualified_and_bundle_labels_abstain(self):
        for label in ('chapter.pdf', '000123.cbz', 'Les Geeks Intégrale 11 Tomes + 2 Best Of',
                      'Out There 5 page 01.jpg', 'random title.cbz', 'a' * 64 + '.pdf'):
            self.assertEqual(parse_name(label), {}, label)

    def test_numbered_album_without_subtitle(self):
        self.assertEqual(parse_name('Les Geeks - T03.pdf'), {'series': 'Les Geeks', 'number': '3'})

    def test_reception_separator_and_scan_annotations(self):
        for label, series, number in [('ZzRacaille Blues - 42.cbr', 'Racaille Blues', '42'),
                ('Les_Geeks_-_T03_-_Si__231_a_rate_formate__33__Collectif_Gang_Labourot.pdf', 'Les Geeks', '3'),
                ('Prince du Tennis_Tome 02@BD_Fr.cbr', 'Prince du Tennis', '2'),
                ('Chopperman T04.cbz', 'Chopperman', '4')]:
            fields = parse_name(label)
            self.assertEqual((fields.get('series'), fields.get('number')), (series, number), label)

    def test_integral_is_one_book_without_invented_number(self):
        self.assertEqual(parse_name('Le Grand Pouvoir du Chninkel - Intégrale.cbz'),
                         {'title': 'Le Grand Pouvoir du Chninkel', 'integral': True})
