"""Pinned engine boundary; run inside Dockerfile.bdnex, without network."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest

from bdnex_service.engine import Engine, SitemapCache, validate_xml


@unittest.skipUnless(importlib.util.find_spec('bdnex'), 'Pinned engine lives in the separate container')
class EngineTests(unittest.TestCase):
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
            finally:
                server.shutdown()
                server.server_close()


class SnapshotTests(unittest.TestCase):
    def test_html_and_entity_documents_are_not_accepted_as_terminal_sitemaps(self) -> None:
        for body in (b'<html/>', b'<!DOCTYPE urlset><urlset/>', b'<urlset/>'):
            with self.assertRaises(ValueError):
                validate_xml(body)
