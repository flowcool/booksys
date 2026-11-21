"""Private recognition protocol and Booksys identity decisions; no live services."""
from __future__ import annotations

import json
import threading
import time
import unittest
from http.server import HTTPServer
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from library.bdnex import BDneXClient
from bdnex_service.server import handler
from library.metadata import prepare_enrichment
from library.asset_evidence import bind_evidence
from library.work_identity import evaluate_work


class ProtocolTests(unittest.TestCase):
    def test_scan_prefix_fallback_uses_spare_slots_only_after_an_empty_match(self) -> None:
        hint = {'Series': 'Navy', 'Number': '149'}
        original = {'path': 'Navy/ZzNavy - 149.cbz', 'embedded': hint}
        alternate = {'path': 'Navy/Navy - 149.cbz', 'embedded': hint}
        mirror = {'path': 'ZzFolder/Mirror.cbz', 'embedded': hint}
        other = {'path': 'Other.cbz', 'embedded': hint}
        candidate = {'source': 'bedetheque', 'edition_ref': '123',
                     'url': 'https://www.bedetheque.com/BD-Navy-123.html',
                     'title': 'Prise de guerre', 'series': 'Navy', 'number': '149', 'score': 80}
        received: list[dict[str, Any]] = []
        original_matches = False

        def match(request: dict[str, Any]) -> dict[str, Any]:
            received.append(request)
            found = original_matches or request['inputs'] == [alternate]
            return {'index': 'b'*64, 'candidates': [candidate] if found else []}

        server = HTTPServer(('127.0.0.1', 0), handler('test-secret', match))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = BDneXClient(f'http://127.0.0.1:{server.server_port}', 'test-secret')

        fallback = client.match('a'*64, [original, mirror])
        self.assertEqual([r['inputs'] for r in received], [[original, mirror], [alternate]])
        self.assertEqual([r['edition_ref'] for r in fallback['candidates']], ['123'])
        from library.bdnex_protocol import digest
        self.assertEqual(fallback['request'], digest(received[1]))
        self.assertEqual(fallback['prefix_fallback']['original_request'], digest(received[0]))

        for inputs in ([original, alternate], [original, mirror, other]):
            with self.subTest(inputs=inputs):
                received.clear()
                self.assertEqual(client.match('a'*64, inputs)['candidates'], [])
                self.assertEqual([r['inputs'] for r in received], [inputs])

        original_matches = True
        received.clear()
        successful = client.match('a'*64, [original])
        self.assertEqual([r['edition_ref'] for r in successful['candidates']], ['123'])
        self.assertEqual([r['inputs'] for r in received], [[original]])
        self.assertNotIn('prefix_fallback', successful)

    def test_invalid_logical_path_does_not_block_the_next_asset(self) -> None:
        received: list[dict[str, Any]] = []

        def match(request: dict[str, Any]) -> dict[str, Any]:
            received.append(request)
            return {'index': 'b'*64, 'candidates': []}

        server = HTTPServer(('127.0.0.1', 0), handler('test-secret', match))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        for path in ('Nains\\Redwin.cbz', 'Nains/Redwin\n.cbz', 'Nains//Redwin.cbz', 'x'*1025):
            with self.subTest(path=repr(path)):
                client = BDneXClient(f'http://127.0.0.1:{server.server_port}', 'test-secret')
                result = client.match('a'*64, [{'path': path, 'embedded': {}}])
                self.assertEqual(result, {'status': 'unavailable', 'reason': 'BDneX request invalid',
                                          'dependency': 'bdnex'})
                self.assertEqual(client.retry_at, 0)
                before = len(received)
                self.assertEqual(client.match('c'*64, [{'path': 'Redwin.cbz', 'embedded': {}}])['status'], 'ok')
                self.assertEqual(len(received), before + 1)

    def test_transport_error_keeps_the_cooldown(self) -> None:
        client = BDneXClient('http://127.0.0.1:1', 'test-secret')
        inputs = [{'path': 'Redwin.cbz', 'embedded': {}}]
        with patch('library.bdnex.build_opener') as opener:
            opener.return_value.open.side_effect = TimeoutError('Timed out')
            before = time.monotonic()
            self.assertEqual(client.match('a'*64, inputs), {
                'status': 'unavailable', 'reason': 'BDneX unavailable or invalid response', 'dependency': 'bdnex',
                'exception_type': 'TimeoutError'})
            self.assertGreaterEqual(client.retry_at, before + 60)
            self.assertEqual(client.match('c'*64, inputs), {
                'status': 'unavailable', 'reason': 'BDneX cooldown', 'dependency': 'bdnex'})
            opener.return_value.open.assert_called_once()

    def test_http_400_does_not_cool_down_but_http_401_does(self) -> None:
        received: list[dict[str, Any]] = []

        def match(request: dict[str, Any]) -> dict[str, Any]:
            received.append(request)
            return {'index': 'b'*64, 'candidates': []}

        server = HTTPServer(('127.0.0.1', 0), handler('test-secret', match))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        inputs = [{'path': 'Redwin.cbz', 'embedded': {}}]
        client = BDneXClient(f'http://127.0.0.1:{server.server_port}', 'test-secret')
        # Bypass only client validation so the handler receives an invalid path.
        with patch('library.bdnex.validate_request', side_effect=lambda value: value):
            self.assertEqual(client.match('a'*64, [{'path': 'Nains//Redwin.cbz', 'embedded': {}}]), {
                'status': 'unavailable', 'reason': 'BDneX request invalid', 'dependency': 'bdnex',
                'exception_type': 'HTTPError', 'http_status': 400})
        self.assertEqual(received, [])
        self.assertEqual(client.retry_at, 0)
        self.assertEqual(client.match('c'*64, inputs)['status'], 'ok')
        self.assertEqual(len(received), 1)
        denied = BDneXClient(client.endpoint, 'wrong')
        before = time.monotonic()
        self.assertEqual(denied.match('a'*64, inputs), {
            'status': 'unavailable', 'reason': 'BDneX unavailable or invalid response', 'dependency': 'bdnex',
            'exception_type': 'HTTPError', 'http_status': 401})
        self.assertGreaterEqual(denied.retry_at, before + 60)
        with patch('library.bdnex.build_opener') as opener:
            self.assertEqual(denied.match('c'*64, inputs), {
                'status': 'unavailable', 'reason': 'BDneX cooldown', 'dependency': 'bdnex'})
            opener.assert_not_called()

    def test_invalid_engine_score_returns_503_and_cools_down(self) -> None:
        received: list[dict[str, Any]] = []

        def match(request: dict[str, Any]) -> dict[str, Any]:
            received.append(request)
            return {'index': 'b'*64, 'candidates': [{'source': 'bedetheque',
                'edition_ref': '245127', 'url': 'https://www.bedetheque.com/BD-Nains-245127.html',
                'title': 'Redwin de la Forge', 'score': 101}]}

        server = HTTPServer(('127.0.0.1', 0), handler('test-secret', match))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = BDneXClient(f'http://127.0.0.1:{server.server_port}', 'test-secret')
        inputs = [{'path': 'Redwin.cbz', 'embedded': {}}]
        request = Request(client.endpoint + '/v1/match',
            data=json.dumps({'schema': 1, 'asset': 'a'*64, 'inputs': inputs}).encode(),
            headers={'Authorization': 'Bearer test-secret', 'Content-Type': 'application/json'})
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 503)
        caught.exception.close()
        before = time.monotonic()
        self.assertEqual(client.match('a'*64, inputs), {
            'status': 'unavailable', 'reason': 'BDneX unavailable or invalid response', 'dependency': 'bdnex',
            'exception_type': 'HTTPError', 'http_status': 503})
        self.assertGreaterEqual(client.retry_at, before + 60)
        with patch('library.bdnex.build_opener') as opener:
            self.assertEqual(client.match('c'*64, inputs), {
                'status': 'unavailable', 'reason': 'BDneX cooldown', 'dependency': 'bdnex'})
            opener.assert_not_called()
        self.assertEqual(len(received), 2)

    def test_logical_name_and_embedded_hints_round_trip_without_archive_access(self) -> None:
        received: list[dict[str, Any]] = []

        def match(request: dict[str, Any]) -> dict[str, Any]:
            received.append(request)
            counts = {'native': 3, 'filtered': 2} if request['asset'] == 'a'*64 else {'native': -1, 'filtered': 2}
            return {'index': 'b'*64, 'match_counts': counts,
                'candidates': [{'source': 'bedetheque',
                'edition_ref': '245127', 'url': 'https://www.bedetheque.com/BD-Nains-Tome-1-Redwin-de-la-Forge-245127.html',
                'title': 'Redwin de la Forge', 'series': 'Nains', 'number': '1', 'score': 89.0}]}

        server = HTTPServer(('127.0.0.1', 0), handler('test-secret', match))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = BDneXClient(f'http://127.0.0.1:{server.server_port}', 'test-secret')
        result = client.match('a'*64, [{'path': 'Nains/Redwin.cbz', 'embedded': {'Series': 'Nains', 'Number': '1'}}])
        self.assertEqual(received[0]['inputs'][0]['path'], 'Nains/Redwin.cbz')
        self.assertEqual(result['candidates'][0]['number'], '1')
        self.assertEqual(result['match_counts'], {'native': 3, 'filtered': 2})
        self.assertEqual(result['candidates'][0]['provenance']['index'], 'b'*64)
        malformed = client.match('c'*64, [{'path': 'Redwin.cbz', 'embedded': {}}])
        self.assertEqual(malformed['status'], 'ok')
        self.assertEqual(malformed['candidates'][0]['number'], '1')
        self.assertNotIn('match_counts', malformed)
        denied = BDneXClient(f'http://127.0.0.1:{server.server_port}', 'wrong')
        self.assertEqual(denied.match('a'*64, [{'path': 'Redwin.cbz', 'embedded': {}}])['status'], 'unavailable')
        self.assertEqual(len(received), 2)


class IdentityTests(unittest.TestCase):
    def numbered_alias_result(self, filename: str, candidates: list[dict[str, Any]],
                              html: str | None) -> dict[str, Any]:
        from copy import deepcopy

        key = 'a'*64
        state = {'assets': {key: {'sha256': key, 'origins': ['o']}},
                 'origins': {'o': {'sha256': key, 'path': filename}}}

        class Snapshot:
            def result_set(self, *args: Any) -> dict[str, Any]:
                return deepcopy(state)

        class Local:
            def match(self, *args: Any) -> dict[str, Any]:
                return {'status': 'ok', 'candidates': candidates}

        class Public:
            bdnex = Local()

            def get(self, url: str) -> dict[str, Any]:
                if url.endswith('/robots.txt'):
                    return {'status': 'ok', 'body': 'User-agent: *\nAllow: /\n'}
                if url == candidates[0]['url'] and html is not None:
                    return {'status': 'ok', 'body': html, 'retrieved_at': '2026-11-21'}
                return {'status': 'unavailable', 'http_status': 403}

        result = prepare_enrichment(Snapshot(), key, Public(), [])  # type: ignore[arg-type]
        work_result: dict[str, Any] = result['assets'][key]['work_result']
        return work_result

    def test_numbered_series_aliases_use_verified_pages_at_the_normal_entry_point(self) -> None:
        from html import escape

        base = 'https://www.bedetheque.com/album/index/i/'
        cases = (
            ('Big Bull', 'Big Bull (Imperia)', '101', '406430', 'Un collier de coquillages'),
            ('Garry', 'Garry (Imperia) (2e série)', '418', '157436', "L'enfer de Tulagi"),
        )
        for alias, canonical, number, ref, title in cases:
            with self.subTest(alias=alias):
                url = base + ref
                row = {'source': 'bedetheque', 'edition_ref': ref, 'url': url, 'title': title,
                       'series': canonical, 'number': number, 'score': 90,
                       'provenance': {'method': 'bdnex-sitemap-1'}}
                html = (f'<link rel="canonical" href="{url}"><h1><a>{escape(canonical)}</a></h1>'
                        f'<meta itemprop="name" content="{escape(canonical + " - " + title)}">'
                        f'<meta itemprop="alternativeheadline" content="Tome {number}">'
                        '<meta itemprop="genre" content="Franco-Belge"><span itemprop="publisher">Imperia</span>')
                work = self.numbered_alias_result(f'{alias} - T{number}.cbz', [row], html)
                self.assertEqual(work['status'], 'identified')
                self.assertEqual(work['work']['series'], canonical)
                self.assertEqual(work['work']['title'], title)
                self.assertEqual(work['work']['publisher'], 'Imperia')
                self.assertEqual(work['evidence']['provenance']['series'][0]['value'], alias)
                self.assertEqual(work['exact_page_attempts'], [{'edition_ref': ref, 'status': 'parsed'}])

    def test_numbered_series_aliases_refuse_unverified_or_conflicting_candidates(self) -> None:
        base = 'https://www.bedetheque.com/album/index/i/'
        ref = '406430'
        url = base + ref
        row = {'source': 'bedetheque', 'edition_ref': ref, 'url': url, 'title': 'Un collier de coquillages',
               'series': 'Big Bull Imperia', 'number': '101', 'provenance': {'method': 'bdnex-sitemap-1'}}
        filename = 'Big Bull - T101.cbz'
        identity = ('<h1><a>Big Bull (Imperia)</a></h1>'
                    '<meta itemprop="name" content="Big Bull (Imperia) - Un collier de coquillages">'
                    '<meta itemprop="alternativeheadline" content="Tome 101">')
        html = f'<link rel="canonical" href="{url}">' + identity
        cases = (
            ('unknown alias', 'Akim - T101.cbz', [{**row, 'series': 'Akim Color'}], html, None),
            ('wrong issue', 'Big Bull - T102.cbz', [row], html, None),
            ('two candidates', filename, [row, {**row, 'edition_ref': '999', 'url': base + '999'}], html, None),
            ('competing Garry series', 'Garry - T101.cbz',
             [{**row, 'series': 'Garry Impéria 2e série'},
              {**row, 'series': 'Garry (Soleil)', 'edition_ref': '999', 'url': base + '999'}], html, None),
            ('403', filename, [row], None, 'unavailable'),
            ('no page reference', filename, [row], identity, 'reference_mismatch'),
            ('wrong page reference', filename, [row], html.replace(url, base + '999'), 'reference_mismatch'),
            ('wrong page series', filename, [row], html.replace('Big Bull (Imperia)', 'Big Bull Extra'), 'reference_mismatch'),
            ('wrong page issue', filename, [row], html.replace('Tome 101', 'Tome 102'), 'reference_mismatch'),
            ('conflicting title', 'Big Bull - T101 - Une autre histoire.cbz', [row], html, 'parsed'),
        )
        for label, name, candidates, body, attempt in cases:
            with self.subTest(label=label):
                work = self.numbered_alias_result(name, candidates, body)
                self.assertEqual(work['status'], 'no_match')
                self.assertEqual(work['exact_page_attempts'], [{'edition_ref': ref, 'status': attempt}] if attempt else [])

    def test_exact_page_contradiction_survives_public_discovery_fallback(self) -> None:
        from copy import deepcopy
        key = 'a'*64
        state = {'assets': {key: {'sha256': key, 'origins': ['o'], 'embedded': {'Title': 'Old title'}}},
                 'origins': {'o': {'sha256': key, 'path': 'Old title.cbz'}}}

        class Snapshot:
            def result_set(self, *args: Any) -> dict[str, Any]:
                return deepcopy(state)

        class Local:
            def match(self, *args: Any) -> dict[str, Any]:
                return {'status': 'ok', 'candidates': [{'source': 'bedetheque', 'edition_ref': '123',
                    'url': 'https://www.bedetheque.com/BD-Old-title-123.html', 'title': 'Old title', 'score': 100}]}

        class Public:
            bdnex = Local()
            events: list[dict[str, Any]] = []

            def get(self, url: str) -> dict[str, Any]:
                body = ('User-agent: *\nAllow: /\n' if url.endswith('robots.txt') else
                        '<meta itemprop="name" content="New title">' if '/BD-' in url else '<main/>')
                return {'status': 'ok', 'body': body, 'retrieved_at': '2026-11-17'}

            def discover(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                return {}

            def resolve_discovery(self, *args: Any) -> list[dict[str, Any]]:
                return []

        result = prepare_enrichment(Snapshot(), key, Public(), [])  # type: ignore[arg-type]
        self.assertNotEqual(result['assets'][key]['work_result']['status'], 'identified')
        work = result['assets'][key]['work_result']
        self.assertEqual(work['candidates'][0]['title'], 'New title')
        self.assertEqual(work['exact_page_attempts'], [{
            'edition_ref': '123', 'status': 'parsed',
        }])

    def test_scores_and_wrong_embedded_web_do_not_override_asset_identity(self) -> None:
        evidence = {'asset_bound': True, 'series': 'Nains', 'number': '1'}
        wrong = {'source': 'bedetheque', 'title': 'Wrong work', 'series': 'Another series', 'number': '1', 'score': 100}
        self.assertEqual(evaluate_work(evidence, [wrong])['status'], 'no_match')
        ambiguity = [dict(wrong, series='Nains', title=title) for title in ('First title', 'Second title')]
        self.assertEqual(evaluate_work(evidence, ambiguity)['status'], 'review')

    def test_bundle_inputs_use_member_name_and_exclude_parent_capture(self) -> None:
        from library.bdnex import inputs_for
        key = 'a'*64
        state = {'assets': {key: {'sha256': key, 'origins': ['o'], 'captured_hints': {'source': {'title': 'Parent'}}}},
            'origins': {'o': {'sha256': key, 'parent_origin': 'p', 'member': 'Series/T01.cbz',
                             'path': 'bundle-inputs/' + 'b'*64 + '/' + key}}}
        self.assertEqual(inputs_for(state, key), [{'path': 'Series/T01.cbz', 'embedded': {}}])
        self.assertNotIn('title', bind_evidence(state, key))

    def test_existing_identification_does_not_call_bdnex(self) -> None:
        from copy import deepcopy
        key = 'a'*64
        state = {'assets': {key: {'sha256': key, 'origins': [], 'work_result': {'status': 'identified'}}}, 'origins': {}}

        class Snapshot:
            def result_set(self, *args: Any) -> dict[str, Any]:
                return deepcopy(state)

        class Local:
            def match(self, *args: Any) -> dict[str, Any]:
                raise AssertionError('Identified assets are excluded from local replay')

        class Offline:
            bdnex = Local()
            events: list[dict[str, Any]] = []

        prepare_enrichment(Snapshot(), key, Offline(), [])  # type: ignore[arg-type]

    def test_outage_keeps_public_fallback_available(self) -> None:
        from copy import deepcopy
        key = 'a'*64
        state = {'assets': {key: {'sha256': key, 'origins': ['o'], 'embedded': {'Title': 'The exact title'}}},
                 'origins': {'o': {'sha256': key, 'path': 'The exact title.cbz'}}}

        class Snapshot:
            def result_set(self, *args: Any) -> dict[str, Any]:
                return deepcopy(state)

        class Local:
            def match(self, *args: Any) -> dict[str, Any]:
                return {'status': 'unavailable', 'dependency': 'bdnex', 'reason': 'Offline'}

        class Public:
            bdnex = Local()
            events: list[dict[str, Any]] = []
            called = False

            def get(self, url: str) -> dict[str, Any]:
                return {'status': 'unavailable'}

            def discover(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                self.called = True
                return {}

            def resolve_discovery(self, *args: Any) -> list[dict[str, Any]]:
                return [{'status': 'ok', 'candidate': {'source': 'openlibrary', 'edition_ref': 'x',
                          'title': 'The exact title', 'url': 'https://openlibrary.org/x'}}]

        providers = Public()
        result = prepare_enrichment(Snapshot(), key, providers, [])  # type: ignore[arg-type]
        self.assertTrue(providers.called)
        self.assertEqual(result['assets'][key]['work_result']['status'], 'identified')

    def test_local_candidate_uses_existing_identity_checks_before_public_discovery(self) -> None:
        from copy import deepcopy

        key = 'a'*64
        state = {'assets': {key: {'sha256': key, 'origins': ['o'],
            'embedded': {'Series': 'Nains', 'Number': '1'}}},
            'origins': {'o': {'sha256': key, 'path': '/storage/Nains T01.cbz'}}}

        class Snapshot:
            def result_set(self, *args: Any) -> dict[str, Any]:
                return deepcopy(state)

        class Local:
            def match(self, *args: Any) -> dict[str, Any]:
                return {'status': 'ok', 'candidates': [{'source': 'bedetheque', 'edition_ref': '245127',
                    'url': 'https://www.bedetheque.com/BD-Nains-Tome-1-Redwin-de-la-Forge-245127.html',
                    'title': 'Redwin de la Forge', 'series': 'Nains', 'number': '1', 'score': 89}]}

        class Offline:
            bdnex = Local()
            events: list[dict[str, Any]] = []

            def get(self, url: str) -> dict[str, Any]:
                self.events.append({'status': 'unavailable', 'dependency': 'bedetheque', 'reason': 'Offline'})
                return {'status': 'unavailable'}

            def discover(self, *args: Any, **kwargs: Any) -> None:
                raise AssertionError('A concordant local candidate must precede public discovery')

        result = prepare_enrichment(Snapshot(), key, Offline(), [])  # type: ignore[arg-type]
        work = result['assets'][key]['work_result']
        self.assertEqual(work['status'], 'identified')
        self.assertIsNone(work['work'].get('language'))
        self.assertIsNone(work['work']['family'])
        self.assertEqual(work['concordant_candidates'][0]['score'], 89)
