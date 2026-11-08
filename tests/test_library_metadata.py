import copy
import tempfile
import unittest
from pathlib import Path
import yaml

from library.metadata import enrich, identify, parse_bdtheque, parse_mangadex, Providers


EMBEDDED = {'ISBN': '9781569319017', 'Title': 'One Piece, Vol. 1', 'Writer': 'Eiichiro Oda',
            'Number': '1', 'LanguageISO': 'en', 'Publisher': 'SHONEN JUMP'}
CANDIDATE = {'isbn': '9781569319017', 'title': 'One Piece, Vol. 1', 'author': 'Eiichiro Oda',
             'number': '1', 'language': 'en', 'publisher': 'SHONEN JUMP', 'date': '2003',
             'revision': '1 edition', 'work_ref': '/works/OL8215661W',
             'edition_ref': '/books/OL8692999M', 'source': 'openlibrary', 'upstream': 'openlibrary',
             'url': 'https://openlibrary.org/books/OL8692999M'}


class IdentityTests(unittest.TestCase):
    def test_validated_number_reaches_generated_comicinfo(self):
        from test_library_publish import setup_library
        from library.xmlmetadata import comicinfo
        from xml.etree import ElementTree
        class QualifiedFixtureProvider:
            def discover(self, title):
                return {'status': 'ok', 'candidates': []}
            def edition(self, code):
                return {'status': 'ok', 'candidate': CANDIDATE}
        with tempfile.TemporaryDirectory() as folder:
            store, _, _, asset = setup_library(Path(folder))
            state = store.read()
            state['works'], state['editions'], state['variants'] = {}, {}, {}
            state['assets'][asset].pop('identity')
            state['assets'][asset]['embedded'] = EMBEDDED
            store.save(state, expected=state['revision'])
            state = enrich(store, asset, QualifiedFixtureProvider(), [])
            self.assertEqual(ElementTree.fromstring(comicinfo(state, asset)).findtext('Number'), '1')

    def test_labelled_calibration_has_zero_false_automatic_admissions(self):
        fixture = yaml.safe_load((Path(__file__).parent / 'fixtures/library_identity_calibration.yaml').read_text())
        admissions = 0
        for label, case in fixture['cases'].items():
            embedded = {**fixture['base_embedded'], **case.get('embedded', {})}
            candidate = {**fixture['base_candidate'], **case.get('candidate', {})}
            candidates = [candidate]
            if 'second_candidate' in case:
                candidates.append({**candidate, **case['second_candidate']})
            with self.subTest(label=label):
                result = identify(embedded, candidates)
                self.assertEqual(result['status'], case['expected'])
                admissions += result['status'] == 'identified'
        self.assertEqual(admissions, 1)
    def test_non_isbn_requires_independent_concordant_edition_evidence(self):
        embedded = {k: v for k, v in EMBEDDED.items() if k != 'ISBN'}
        embedded.update(EditionDate='2003', EditionRevision='1 edition')
        second = {**CANDIDATE, 'source': 'independent', 'upstream': 'independent',
                  'url': 'https://example.org/edition', 'edition_ref': 'independent-edition'}
        self.assertEqual(identify(embedded, [CANDIDATE, second])['status'], 'identified')
        self.assertEqual(identify(embedded, [CANDIDATE, {**second, 'upstream': 'openlibrary'}])['status'], 'pending')
    def test_exact_bibliographic_identifier_requires_consistency(self):
        self.assertEqual(identify(EMBEDDED, [CANDIDATE])['status'], 'identified')
        for field, wrong in [('title', 'One Piece Integral'), ('language', 'fr'),
                             ('publisher', 'Other'), ('number', '2'), ('author', 'Other')]:
            candidate = {**CANDIDATE, field: wrong}
            with self.subTest(field=field):
                self.assertEqual(identify(EMBEDDED, [candidate])['status'], 'pending')
        self.assertEqual(identify({'Title': 'One Piece'}, [CANDIDATE])['status'], 'pending')
        self.assertEqual(identify(EMBEDDED, [{**CANDIDATE, 'language': ''}])['status'], 'pending')

    def test_conflicting_editions_and_correlated_evidence_remain_pending(self):
        conflict = {**CANDIDATE, 'edition_ref': '/books/OTHER', 'revision': 'revised'}
        self.assertEqual(identify(EMBEDDED, [CANDIDATE, conflict])['status'], 'pending')
        evidence = identify({}, [CANDIDATE, {**CANDIDATE, 'source': 'mirror'}])
        self.assertEqual(evidence['independent_sources'], 1)


class ProviderTests(unittest.TestCase):
    def test_public_is_separate_from_genre_and_missing_stays_unknown(self):
        page = '<h1>Album</h1><table class="table-sm"><tr><td>Genre / Public / Type</td><td><a href="?genre=3">Western</a><a href="?public=2">Adultes</a></td></tr></table>'
        result = parse_bdtheque(page)
        self.assertEqual(result['genres'], ['Western'])
        self.assertEqual(result['public'], 'Adultes')
        self.assertEqual(parse_bdtheque('<h1>Album</h1>')['genres'], [])
        result = parse_mangadex({'id': 'id', 'attributes': {'title': {'en': 'Manga'}, 'contentRating': 'erotica'}})
        self.assertEqual(result['public'], 'erotica')
        self.assertEqual(result['genres'], [])

    def test_cache_request_budget_and_source_failures(self):
        calls = []
        def fetch(url):
            calls.append(url)
            return 429, 'rate limit'
        with tempfile.TemporaryDirectory() as folder:
            providers = Providers(Path(folder), fetch=fetch, interval=0, budget=1)
            first = providers.discover('Album')
            self.assertEqual(first['status'], 'unavailable')
            self.assertNotIn('public', first)
            self.assertEqual(providers.discover('Album')['status'], 'unavailable')
            self.assertEqual(len(calls), 1)
            self.assertEqual(providers.discover('Other')['status'], 'unavailable')
            self.assertEqual(len(calls), 1)
