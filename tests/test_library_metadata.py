import copy
import tempfile
import unittest
from pathlib import Path
import yaml

from postgres_fixture import create_store
from library.primitives import empty

from library.metadata import enrich, Providers


EMBEDDED = {'ISBN': '9781569319017', 'Title': 'One Piece, Vol. 1', 'Writer': 'Eiichiro Oda',
            'Number': '1', 'LanguageISO': 'en', 'Publisher': 'SHONEN JUMP'}
CANDIDATE = {'isbn': '9781569319017', 'title': 'One Piece, Vol. 1', 'author': 'Eiichiro Oda',
             'number': '1', 'language': 'en', 'publisher': 'SHONEN JUMP', 'date': '2003',
             'revision': '1 edition', 'work_ref': '/works/OL8215661W',
             'edition_ref': '/books/OL8692999M', 'source': 'openlibrary', 'upstream': 'openlibrary',
             'url': 'https://openlibrary.org/books/OL8692999M'}




class ProviderTests(unittest.TestCase):
    def test_malformed_edition_does_not_hide_later_discovery_candidates(self):
        import json
        def fetch(url):
            if '/books/OL1M.json' in url:
                return 200, json.dumps({'type': None, 'authors': [None]})
            if '/books/OL2M.json' in url:
                return 200, json.dumps({'type': {'key': '/type/edition'}, 'key': '/books/OL2M',
                                       'title': 'Album', 'authors': [None]})
            if '/books/OL3M.json' in url:
                return 200, json.dumps({'type': {'key': '/type/edition'}, 'key': '/books/OL3M', 'title': 'Album'})
            self.fail(url)
        with tempfile.TemporaryDirectory() as folder:
            create_store(self, Path(folder), empty())
            providers = Providers(Path(folder)/"cache", fetch=fetch, interval=0)
            results = providers.resolve_discovery({'candidates': [
                {'edition_key': ['OL1M']}, {'edition_key': ['OL2M']}, {'edition_key': ['OL3M']}]})
            self.assertEqual([item['status'] for item in results], ['unavailable', 'unavailable', 'ok'])

    def test_cache_request_budget_and_source_failures(self):
        calls = []
        def fetch(url):
            calls.append(url)
            return 429, 'rate limit'
        with tempfile.TemporaryDirectory() as folder:
            create_store(self, Path(folder), empty())
            providers = Providers(Path(folder)/"cache", fetch=fetch, interval=0, budget=1)
            first = providers.discover('Album')
            self.assertEqual(first['status'], 'unavailable')
            self.assertNotIn('public', first)
            self.assertEqual(providers.discover('Album')['status'], 'unavailable')
            self.assertEqual(len(calls), 1)
            self.assertEqual(providers.discover('Other')['status'], 'unavailable')
            self.assertEqual(len(calls), 1)
