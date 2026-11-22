"""Pinned engine boundary; run inside Dockerfile.bdnex, without network."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest

from bdnex_service.engine import Engine, SitemapCache, validate_xml


@unittest.skipUnless(importlib.util.find_spec('bdnex'), 'Pinned engine lives in the separate container')
class EngineTests(unittest.TestCase):
    def test_decorated_lookup_and_web_proposal_without_native_hits(self) -> None:
        from bdnex.importer.bedetheque.sitemap import sitemap_urls
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'index.sqlite'
            cache = SitemapCache(path, writable=True)
            albums = ['Navy-Imperia-Tome-100-900100', 'Akim-1re-serie-Tome-296-900296',
                      'Bob-et-Bobette-3e-Serie-Rouge-Tome-8-900008', 'Jim-Canada-Imperia-Tome-36-900036',
                      'Archie-Editions-Tome-4-900004', 'Archie-Autre-serie-Tome-4-900014',
                      'Lady-Travel-Tome-2-Miss-Travel-215240']
            xml = '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + ''.join(
                f'<url><loc>https://www.bedetheque.com/BD-{album}.html</loc></url>' for album in albums) + '</urlset>'
            cache.cache_put('bedetheque', 'sitemap', sitemap_urls()[0], xml.encode())
            cache.cache_put('bedetheque', 'sitemap', sitemap_urls()[1],
                            b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"/>')
            cache.close()
            engine = Engine(path)
            for series, number, ref in [('navy', '100', '900100'), ('akim', '296', '900296'),
                    ('bob et bobette', '8', '900008'), ('jim canada', '36', '900036')]:
                with self.subTest(series=series):
                    result = engine.match({'inputs': [{'path': 'unrelated.cbz', 'embedded': {}}],
                        'identity': {'series': [series], 'number': [number]}})
                    self.assertEqual(result['decision'], {'status': 'selected', 'reason': 'numbered', 'edition_ref': ref})
            result = engine.match({'inputs': [{'path': 'unrelated.cbz', 'embedded': {}}],
                'identity': {'series': ['archie'], 'number': ['4']}})
            self.assertEqual(result['decision'], {'status': 'review', 'reason': 'ambiguous'})
            web = 'https://www.bedetheque.com/BD-Lady-Travel-Tome-2-Miss-Travel-215240.html'
            result = engine.match({'inputs': [{'path': 'Navy T100.cbz', 'embedded': {'Web': web}}],
                'identity': {'series': ['lady travel'], 'number': ['1'], 'title': ['miss travel']}})
            self.assertEqual(result['decision']['edition_ref'], '215240')
            for value in (web.replace('www.bedetheque.com', 'evil.example'), web + '?x=1',
                          web + '#x', web.replace('215240', 'not-an-id'), web.replace('215240', '999999')):
                result = engine.match({'inputs': [{'path': 'unrelated.cbz', 'embedded': {'Web': value}}], 'identity': {}})
                self.assertEqual(result['candidates'], [], value)

    def test_numbered_choice_uses_the_full_index_and_accepts_a_lower_score(self) -> None:
        from bdnex.importer.bedetheque.sitemap import sitemap_urls

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'index.sqlite'
            cache = SitemapCache(path, writable=True)
            urls = sitemap_urls()
            albums = [
                'Akim-1re-serie-aventures-voyages-Tome-296-La-colere-du-tigre-900296',
                'Akim-1re-serie-aventures-voyages-Tome-297-Titre-long-900297',
                'Archie-Editions-Tome-4-Anniversaire-900004',
                'Archie-Autre-serie-Tome-4-Aventure-900014',
                'Example-2016-Tome-1-Return-900201',
            ]
            xml = '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + ''.join(
                f'<url><loc>https://www.bedetheque.com/BD-{album}.html</loc></url>' for album in albums
            ) + '</urlset>'
            cache.cache_put('bedetheque', 'sitemap', urls[0], xml.encode())
            cache.cache_put('bedetheque', 'sitemap', urls[1], b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"/>')
            cache.close()
            engine = Engine(path)
            response = engine.match({
                'inputs': [{'path': 'Akim - 296.cbz', 'embedded': {}}],
                'identity': {'series': ['akim'], 'number': ['296']},
            })
            self.assertLess(response['candidates'][0]['score'], 70)
            self.assertEqual(response.get('decision'),
                             {'status': 'selected', 'edition_ref': '900296', 'reason': 'numbered'})
            response = engine.match({
                'inputs': [{'path': 'Archie - 4.cbz', 'embedded': {}}],
                'identity': {'series': ['archie'], 'number': ['4']},
            })
            self.assertEqual(response['decision'], {'status': 'review', 'reason': 'ambiguous'})
            response = engine.match({
                'inputs': [{'path': 'Example - T01 - Return.cbz', 'embedded': {}}],
                'identity': {'series': ['example'], 'number': ['1'], 'title': ['return'], 'year': ['1987']},
            })
            self.assertIn('900201', [row['edition_ref'] for row in response['candidates']])
            self.assertEqual(response['decision'], {'status': 'review', 'reason': 'insufficient'})
            dated = next(row for row in response['candidates'] if row['edition_ref'] == '900201')
            self.assertEqual(engine.decide([{**dated, 'score': 100}], {'series': ['example'], 'number': ['1']}),
                             {'status': 'review', 'reason': 'insufficient'})


    def test_high_score_without_volume_survives_many_wanted_albums(self) -> None:
        from bdnex.importer.bedetheque.sitemap import sitemap_urls
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'index.sqlite'
            cache = SitemapCache(path, writable=True)
            urls = sitemap_urls()
            albums = [f'Tueur-Tome-2-Album-{n}-90000{n}' for n in range(1, 8)]
            albums.append('Tueur-900100')
            xml = '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + ''.join(
                f'<url><loc>https://www.bedetheque.com/BD-{album}.html</loc></url>' for album in albums) + '</urlset>'
            cache.cache_put('bedetheque', 'sitemap', urls[0], xml.encode())
            cache.cache_put('bedetheque', 'sitemap', urls[1], b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"/>')
            cache.close()
            engine = Engine(path)
            candidates = engine.source.candidates('Tueur 2')
            self.assertEqual(len(candidates), 5)
            integral, score = next((c, s) for c, s in candidates if c.source_id == '900100')
            self.assertIsNone(integral.volume)
            self.assertGreater(score, max(s for c, s in candidates if c.volume == 2))

    def test_wanted_volume_survives_cut_with_long_slug(self) -> None:
        from bdnex.importer.bedetheque import Bedetheque  # type: ignore[import-not-found]
        from bdnex.importer.bedetheque.sitemap import sitemap_urls  # type: ignore[import-not-found]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'index.sqlite'
            cache = SitemapCache(path, writable=True)
            urls = sitemap_urls()
            albums = [f'Akim-Tome-{n}-90000{n}' for n in range(1, 8)]
            albums.append('Akim-1re-serie-aventures-voyages-Tome-296-La-colere-du-tigre-900296')
            xml = '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + ''.join(
                f'<url><loc>https://www.bedetheque.com/BD-{album}.html</loc></url>' for album in albums) + '</urlset>'
            cache.cache_put('bedetheque', 'sitemap', urls[0], xml.encode())
            cache.cache_put('bedetheque', 'sitemap', urls[1], b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"/>')
            upstream = Bedetheque(cache)
            self.assertNotIn('900296', [c.source_id for c, _ in upstream.candidates('Akim 296')])
            cache.close()
            engine = Engine(path)
            candidates = engine.source.candidates('Akim 296')
            self.assertEqual(len(candidates), 5)
            self.assertEqual(candidates[0][0].source_id, '900296')
            self.assertEqual(candidates[0][1], engine.source._score(['akim'], candidates[0][0]))

    def test_low_score_requires_volume_and_series_words(self) -> None:
        from bdnex.importer.bedetheque.sitemap import sitemap_urls  # type: ignore[import-not-found]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'index.sqlite'
            cache = SitemapCache(path, writable=True)
            urls = sitemap_urls()
            cache.cache_put('bedetheque', 'sitemap', urls[0], b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://www.bedetheque.com/BD-Akim-1re-serie-aventures-voyages-Tome-296-La-colere-du-tigre-900296.html</loc></url></urlset>')
            cache.cache_put('bedetheque', 'sitemap', urls[1], b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"/>')
            cache.close()
            engine = Engine(path)
            for name in ('Akim - 296.cbz', 'Akim - 296 - ScanPersonnel.cbz'):
                with self.subTest(name=name):
                    candidates = engine.match({'inputs': [{'path': name, 'embedded': {}}]})['candidates']
                    self.assertEqual([c['edition_ref'] for c in candidates], ['900296'])
                    self.assertGreaterEqual(candidates[0]['score'], 60)
                    self.assertLess(candidates[0]['score'], 70)
            for name in ('Akim - 295.cbz', 'Akim.cbz', 'Akim unknown - 296.cbz'):
                self.assertEqual(engine.match({'inputs': [{'path': name, 'embedded': {}}]})['candidates'], [])

    def test_index_and_supplied_hints_find_album_without_reading_logical_path(self) -> None:
        from bdnex.importer.bedetheque.sitemap import sitemap_urls  # type: ignore[import-not-found]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'index.sqlite'
            cache = SitemapCache(path, writable=True)
            urls = sitemap_urls()
            cache.cache_put('bedetheque', 'sitemap', urls[0], b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://www.bedetheque.com/BD-Nains-Tome-1-Redwin-de-la-Forge-245127.html</loc></url></urlset>')
            cache.cache_put('bedetheque', 'sitemap', urls[1], b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"/>')
            cache.close()
            engine = Engine(path)
            from bdnex_service.__main__ import refresh
            from unittest.mock import patch
            before = path.read_bytes()
            # External transport is the only replaced boundary. Refresh must
            # reject the response before publishing the staging database.
            with patch('bdnex.importer.bedetheque.http_fetch', return_value=b'<html>blocked</html>'):
                with self.assertRaises(ValueError):
                    refresh(path)
            self.assertEqual(path.read_bytes(), before)
            result = engine.match({'inputs': [{'path': 'does-not-exist.cbz', 'embedded': {
                'Web': 'https://www.bedetheque.com/BD-Nains-Tome-1-Redwin-de-la-Forge-245127.html'}}]})
            self.assertEqual(result['candidates'][0]['edition_ref'], '245127')
            self.assertEqual(result['candidates'][0]['score'], 100)
            self.assertNotIn('language', result['candidates'][0])
            self.assertNotIn('integral', result['candidates'][0])
            self.assertEqual(engine.match({'inputs': [{'path': 'does-not-exist.cbz', 'embedded': {}}]})['candidates'], [])
            # An invalid replacement must fail loading; the running engine and
            # its in-memory snapshot still answer with the previous revision.
            old_index = engine.index
            from bdnex_service.__main__ import SnapshotMatcher
            from bdnex_service.server import handler
            from library.bdnex import BDneXClient
            from http.server import HTTPServer
            import threading
            matcher = SnapshotMatcher(path)
            path.write_bytes(b'not sqlite')
            with self.assertRaises(Exception):
                Engine(path)
            self.assertEqual(engine.match({'inputs': [{'path': 'Nains T01.cbz', 'embedded': {}}]})['index'], old_index)
            server = HTTPServer(('127.0.0.1', 0), handler('secret', matcher))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                response = BDneXClient(f'http://127.0.0.1:{server.server_port}', 'secret').match('a'*64,
                    [{'path': 'Nains T01.cbz', 'embedded': {}}])
                self.assertEqual(response['status'], 'ok')
                self.assertEqual(response['index'], old_index)
                response = BDneXClient(f'http://127.0.0.1:{server.server_port}', 'secret').match(
                    'a'*64, [{'path': 'does-not-exist.cbz', 'embedded': {
                        'Web': 'https://www.bedetheque.com/BD-Nains-Tome-1-Redwin-de-la-Forge-245127.html'}}],
                    identity={},
                )
                self.assertEqual(response['decision'],
                                 {'status': 'selected', 'edition_ref': '245127', 'reason': 'strong'})
            finally:
                server.shutdown()
                server.server_close()


class SnapshotTests(unittest.TestCase):
    def test_candidate_score_floor_is_60(self) -> None:
        from library.bdnex_protocol import validate_candidates
        row = {'source': 'bedetheque', 'edition_ref': '900296',
               'url': 'https://www.bedetheque.com/BD-Akim-Tome-296-900296.html', 'score': 60}
        self.assertEqual(validate_candidates([row]), [row])
        with self.assertRaises(ValueError):
            validate_candidates([{**row, 'score': 59}])

    def test_html_and_entity_documents_are_not_accepted_as_terminal_sitemaps(self) -> None:
        for body in (b'<html/>', b'<!DOCTYPE urlset><urlset/>', b'<urlset/>'):
            with self.assertRaises(ValueError):
                validate_xml(body)
