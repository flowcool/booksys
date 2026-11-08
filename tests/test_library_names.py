"""Qualified filename shapes provide assertions, never a selected reference."""
import unittest

from library.names import parse_name


class NameTests(unittest.TestCase):
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
