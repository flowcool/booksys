from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import URLError

from library.mangaupdates import decide_series, lookup, main, read_references, request_series
from library.primitives import LibraryError


def hit(series_id, title, kind='Manhwa', alias=''):
    return {'record': {'series_id': series_id, 'title': title, 'type': kind,
                       'url': f'https://www.mangaupdates.com/series/{series_id}/test'}, 'hit_title': alias}


class MangaUpdatesTests(unittest.TestCase):
    def test_decision_uses_unique_exact_manhwa_title_or_alias(self):
        # The comparison contains same-title hits, translated aliases, unrelated
        # near matches and Manga homonyms. All data here is synthetic.
        outcomes = (
            ('Same title', [hit(1, 'Same title')], 'accepted', 1),
            ('Cours et été !', [hit(2, 'Summer Lessons', alias='Cours et Ete'),
                                 hit(3, 'Cours et été', 'Manga')], 'accepted', 2),
            ('Summer Lessons', [hit(2, 'Summer Lessons', 'Manga')], 'review', None),
            ('Summer Lessons', [hit(2, 'Special Summer Lessons')], 'review', None),
            ('Summer Lessons', [hit(2, 'Summer Lessons'), hit(3, 'Summer school', alias='Summer Lessons')],
             'review', None),
            ('Summer Lessons', [], 'review', None),
        )
        for title, rows, decision, chosen in outcomes:
            with self.subTest(title=title, rows=rows):
                result = decide_series(title, rows)
                self.assertEqual((result['decision'], result['mu_id']), (decision, chosen))
                self.assertEqual([row['mu_id'] for row in result['candidates']],
                                 [row['record']['series_id'] for row in rows])

    def test_series_lookup_and_review_round_trip_yaml(self):
        queries = []

        def request(path, payload):
            queries.append((path, payload))
            if path == 'search':
                if payload['search'] == 'Cours et été !':
                    return {'results': [hit(2, 'Summer Lessons', alias='Cours et Ete')]}
                return {'results': [hit(4, 'Spring Lessons')]}
            return {**hit(2, 'Summer Lessons')['record'], 'associated': [{'title': 'Cours et Ete'}],
                    'authors': [{'name': 'Sample Writer', 'type': 'Author'},
                                {'name': 'Sample Artist', 'type': 'Artist'}]}

        with TemporaryDirectory() as folder, redirect_stdout(StringIO()):
            directory = Path(folder)
            lookup(directory, ['Cours et été !', 'cours et ete', 'Other'], request=request)
            lookup(directory, ['Cours et été !', 'Other'], request=request)
            self.assertEqual(queries, [('search', {'search': 'Cours et été !'}), ('2', None),
                                       ('search', {'search': 'Other'})])
            saved = read_references(directory)
            self.assertEqual(set(saved), {'cours et ete', 'other'})
            self.assertEqual(saved['cours et ete']['authors'], [{'name': 'Sample Writer', 'type': 'Author'},
                                                                {'name': 'Sample Artist', 'type': 'Artist'}])
            self.assertEqual(saved['other']['decision'], 'review')
            main(['--state-dir', folder, 'reject', 'Other', '4', '--by', 'operator'])
            rejected = read_references(directory)['other']
            self.assertEqual((rejected['decision'], rejected['mu_id'], rejected['decided_by']),
                             ('rejected', 4, 'operator'))
            with patch('library.mangaupdates.request_series', side_effect=AssertionError('Unexpected request')):
                main(['--state-dir', folder, 'accept', 'Cours et été !', '2', '--by', 'operator'])
            accepted = read_references(directory)['cours et ete']
            self.assertEqual((accepted['decision'], accepted['mu_id'], accepted['decided_by']),
                             ('accepted', 2, 'operator'))
            self.assertEqual(accepted['authors'], saved['cours et ete']['authors'])

            with patch('library.mangaupdates.request_series', return_value={**hit(4, 'Spring Lessons')['record'],
                       'authors': [{'name': 'Another Writer', 'type': 'Author'}]}) as detail:
                main(['--state-dir', folder, 'accept', 'Other', '4', '--by', 'operator'])
                detail.assert_called_once_with('4', None)
            self.assertEqual(read_references(directory)['other']['authors'],
                             [{'name': 'Another Writer', 'type': 'Author'}])

    def test_request_failures_have_bounded_attempts_and_spacing(self):
        with patch('library.mangaupdates.build_opener') as opener, patch('library.mangaupdates.time.sleep') as sleep:
            opener.return_value.open.side_effect = URLError('test connection failure')
            with self.assertRaisesRegex(LibraryError, 'three attempts'):
                request_series('search', {'search': 'Sample Series'})
            self.assertEqual(opener.return_value.open.call_count, 3)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.0, 1.0, 2.0])
