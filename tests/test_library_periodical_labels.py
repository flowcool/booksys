import unittest

from library.asset_evidence import bind_evidence
from library.names import parse_name
from library.periodicals import periodical_result


class PeriodicalLabelTests(unittest.TestCase):
    def test_approved_labels_bind_one_title_and_one_issue(self) -> None:
        cases = (
            ('ZzLes amis de Jacobs - 17', 'Les Amis de Jacobs', '17'),
            ('zzLes Amis de Jacobs - T19', 'Les Amis de Jacobs', '19'),
            ('ZzSchtroumpf.fanzine.-.31', 'Schtroumpf Fanzine', '31'),
            ('zzLa semaine de Charlie - 03', 'La Semaine de Charlie', '3'),
            ('ZzPsikopat - Série 2 - 309', 'Psikopat', '309'),
            ('ZzTcho ! Le mégazine - 57', 'Tchô ! Le mégazine', '57'),
            ('Vécu - Tome 18', 'Vécu', '18'),
            ('ZzPif le Mag - 05', 'Pif le Mag', '5'),
        )
        asset_id = 'a' * 64
        for label, title, number in cases:
            with self.subTest(label=label):
                state = {
                    'assets': {asset_id: {'sha256': asset_id, 'origins': ['file']}},
                    'origins': {'file': {'sha256': asset_id, 'path': f'{label}.cbz', 'source': 'external:test'}},
                }
                result = periodical_result(bind_evidence(state, asset_id))
                self.assertIsNotNone(result)
                if result is not None:
                    self.assertEqual(result['status'], 'periodical')
                    self.assertEqual(result['work'], {'series': title, 'number': number})
                    self.assertEqual([(claim['field'], claim['source']) for claim in result['provenance']],
                                     [('series', 'origin:file'), ('number', 'origin:file')])

    def test_series_marker_stays_unbound_on_other_titles(self) -> None:
        self.assertEqual(parse_name('ZzUn autre titre - Série 2 - 309.cbz'), {})
