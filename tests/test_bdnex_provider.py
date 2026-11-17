"""Private recognition protocol and Booksys identity decisions; no live services."""
from __future__ import annotations

import threading
import unittest
from http.server import HTTPServer
from typing import Any

from library.bdnex import BDneXClient
from bdnex_service.server import handler
from library.metadata import prepare_enrichment
from library.asset_evidence import bind_evidence
from library.work_identity import evaluate_work


class ProtocolTests(unittest.TestCase):
    def test_logical_name_and_embedded_hints_round_trip_without_archive_access(self) -> None:
        received: list[dict[str, Any]] = []

        def match(request: dict[str, Any]) -> dict[str, Any]:
            received.append(request)
            return {'index': 'b'*64, 'candidates': [{'source': 'bedetheque',
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
        self.assertEqual(result['candidates'][0]['provenance']['index'], 'b'*64)
        denied = BDneXClient(f'http://127.0.0.1:{server.server_port}', 'wrong')
        self.assertEqual(denied.match('a'*64, [{'path': 'Redwin.cbz', 'embedded': {}}])['status'], 'unavailable')
        self.assertEqual(len(received), 1)


class IdentityTests(unittest.TestCase):
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
        self.assertEqual(result['assets'][key]['work_result']['candidates'][0]['title'], 'New title')

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
