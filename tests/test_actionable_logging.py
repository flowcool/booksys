"""Provider causes survive real metadata execution, atomic work completion and events."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


AT = datetime(2026, 11, 19, tzinfo=timezone.utc)

@unittest.skipUnless(os.environ.get('BOOKSYS_TEST_PG_DSN_FILE'), 'isolated PostgreSQL required')
class ActionableLoggingTests(unittest.TestCase):
    def test_provider_causes_reach_persisted_work_and_events(self):
        from library.business_stages import BusinessStages
        from library.work_actor import WorkActor
        from library.work_items import WorkItems
        from library.primitives import empty
        from postgres_fixture import create_store
        cases = [
            ({'exception_type': 'TimeoutError'}, 'provider_timeout', 'retry'),
            ({'http_status': 429}, 'remote_rate_limited', 'retry'),
            ({'http_status': 503}, 'provider_http_failure', 'retry'),
            ({'reason': 'Request budget exhausted'}, 'provider_budget', 'defer'),
        ]
        for failure, reason, outcome, limit in [(failure, reason, outcome, limit)
                for failure, reason, outcome in cases for limit in (1, 3)]:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                store = create_store(self, root/'state', empty())
                WorkItems.upgrade(store._database)
                work = WorkItems(store._database, max_attempts={'metadata': limit})
                self.addCleanup(work.close)
                work.add('metadata', 'asset', input_rev='revision', method='method', now=AT)
                from library.metadata import Providers
                def fetch(url):
                    if failure.get('exception_type') == 'TimeoutError':
                        raise TimeoutError('private URL https://secret.invalid/path token=private')
                    return failure.get('http_status', 200), ''
                providers = Providers(root/'state/cache', fetch=fetch, interval=0,
                                      budget=0 if outcome == 'defer' else 100)
                catalog = SimpleNamespace(prepare=lambda state: None)
                stages = BusinessStages(catalog, root, root, providers)
                def enrich(*args):
                    providers.get('https://www.bedetheque.com/synthetic')
                    return {'assets': {'asset': {}}}
                events = []
                with patch('library.metadata.prepare_enrichment', side_effect=enrich), \
                     patch('library.business_stages.evidence_version', return_value='evidence'), \
                     patch.object(stages, 'obligations', return_value=[]), \
                     patch('booksys_core.diagnostics._default') as sink:
                    sink.emit.side_effect = lambda event: events.append(event) or True
                    actor = WorkActor(store, work, 'metadata', lambda task, now: stages.execute(task, now=now), clock=lambda: AT)
                    self.assertEqual(actor.step(now=AT), outcome)
                with store._database.connection() as db:
                    row = db.execute("SELECT state,wait_reason,last_error,next_at,attempts FROM work_items WHERE stage='metadata'").fetchone()
                expected_state = 'waiting' if outcome == 'defer' or limit == 3 else 'failed'
                self.assertEqual(row[0], expected_state)
                self.assertIn(reason, row[2] or row[1])
                self.assertEqual(row[4], 0 if outcome == 'defer' else 1)
                event = events[-1]
                self.assertEqual(event['reason'], reason)
                self.assertEqual(event['dependency'], 'www.bedetheque.com')
                self.assertEqual(event['task_state'], expected_state)
                if expected_state == 'waiting':
                    self.assertEqual(event['next_attempt'], row[3].isoformat())
                    if outcome == 'retry':
                        self.assertEqual(row[3], AT + timedelta(minutes=5))
                else:
                    self.assertNotIn('next_attempt', event)
                if 'http_status' in failure:
                    self.assertEqual(event['http_status'], failure['http_status'])
                self.assertNotIn('https://secret.invalid/path', json.dumps(event))
                self.assertNotIn('token=private', json.dumps(event))


    def test_repeated_error_keeps_the_terminal_failure_event(self):
        from library.work_items import WorkItems, retry_delay
        from library.primitives import empty
        from postgres_fixture import create_store
        with tempfile.TemporaryDirectory() as temporary:
            store = create_store(self, Path(temporary)/'state', empty())
            WorkItems.upgrade(store._database)
            work = WorkItems(store._database, max_attempts={'metadata': 3})
            self.addCleanup(work.close)
            work.add('metadata', 'asset', input_rev='r', method='m', now=AT)
            stamp = AT
            for attempt in (1, 2, 3):
                item = work.claim('metadata', 'worker', now=stamp)
                self.assertEqual(item.attempts, attempt)
                self.assertTrue(work.retry(item, 'provider_timeout', retry_delay(attempt), now=stamp))
                stamp += retry_delay(attempt)
            with store._database.connection() as db:
                state, attempts = db.execute("SELECT state,attempts FROM work_items WHERE stage='metadata'").fetchone()
            self.assertEqual((state, attempts), ('failed', 3))
            events = work.events('metadata', 'asset')
            self.assertEqual(sum(row[2] == 'failed' for row in events), 1)
            self.assertTrue(any(row[2] == 'failed' and 'provider_timeout' in row[3] for row in events))

    def test_exception_retry_keeps_filtered_cause_and_real_state(self):
        from library.work_actor import WorkActor
        from library.work_items import WorkItems
        from library.primitives import empty
        from postgres_fixture import create_store
        with tempfile.TemporaryDirectory() as temporary:
            store = create_store(self, Path(temporary)/'state', empty())
            WorkItems.upgrade(store._database)
            work = WorkItems(store._database, max_attempts={'copies': 3})
            self.addCleanup(work.close)
            work.add('copies', 'asset', input_rev='r', method='m', now=AT)
            def execute(task, now):
                raise RuntimeError('Failure token=secret at https://private.invalid /home/private/file')
            events = []
            with patch('booksys_core.diagnostics._default') as sink:
                sink.emit.side_effect = lambda event: events.append(event) or True
                self.assertEqual(WorkActor(store, work, 'copies', execute, clock=lambda: AT).step(now=AT), 'retry')
            with store._database.connection() as db:
                row = db.execute("SELECT state,last_error,next_at,attempts FROM work_items WHERE stage='copies'").fetchone()
            self.assertEqual((row[0], row[2], row[3]), ('waiting', AT+timedelta(minutes=5), 1))
            self.assertIn('RuntimeError', row[1])
            self.assertNotIn('secret', row[1])
            self.assertNotIn('/home/private', row[1])
            self.assertEqual(events[-1]['code'], 'waiting')
            self.assertEqual(events[-1]['task_state'], 'waiting')


class DiagnosticBoundaryTests(unittest.TestCase):
    def test_changed_provider_status_is_not_suppressed_as_the_same_wait(self):
        from booksys_core.diagnostics import emit
        events = []
        with patch('booksys_core.diagnostics._conditions', {}), patch('booksys_core.diagnostics._default') as sink:
            sink.emit.side_effect = lambda event: events.append(event) or True
            for status in (503, 503, 502):
                self.assertTrue(emit(service='library', stage='identity', code='waiting',
                                     reason='provider_http_failure', dependency='bdnex', http_status=status))
        self.assertEqual([row['http_status'] for row in events], [503, 502])
        self.assertEqual(events[-1]['repeat_count'], 1)

    def test_fiche_failure_preserves_cause_without_guessing_provider_outage(self):
        from library.native_stages import NativeStages
        source = 'synthetic-source'
        catalog = SimpleNamespace(backend=lambda: SimpleNamespace(_database=None))
        stages = NativeStages(catalog, Path('.'), Path('.'), Path('.'), browser=None, client=None)
        task = SimpleNamespace(stage='collection', entity='fiche-fixture', source='')
        with patch.object(stages, 'binding', return_value=(source, {}, {})), \
             patch('booksys_core.collection.run_fiche_once', return_value=False), \
             patch('library.native_stages.CollectorState') as state:
            state.return_value.fiches.return_value = {'items': {source: {'status': 'retry', 'error': 'Permission denied'}}}
            result = stages.execute(task, now=AT)
        self.assertEqual(result.outcome, 'retry')
        self.assertEqual(result.diagnostic, {'reason': 'unknown', 'detail': 'Permission denied'})

    def test_provider_dns_failure_keeps_the_sanitized_underlying_cause(self):
        import socket
        from urllib.error import URLError
        from booksys_core.diagnostics import provider_diagnostic, diagnostic_text, event
        for code, message in ((socket.EAI_AGAIN, 'Temporary failure in name resolution'),
                              (socket.EAI_NODATA, 'No address associated with hostname')):
            failure = URLError(socket.gaierror(code, message))
            fields = provider_diagnostic([{'status': 'unavailable', 'dependency': 'catalogue.bnf.fr',
                'exception_type': 'URLError', 'reason': str(failure)+' token=secret https://private.invalid /home/private/file'}])
            self.assertEqual(fields['reason'], 'provider_unavailable')
            self.assertIn(message, fields['detail'])
            self.assertIn(message, diagnostic_text(fields))
            self.assertNotIn('secret', fields['detail'])
            self.assertNotIn('https://private', fields['detail'])
            event(service='library', stage='identity', code='waiting', **fields)

    def test_diagnostic_fields_drop_untrusted_provider_values(self):
        from booksys_core.diagnostics import diagnostic_fields, diagnostic_text, event
        fields = diagnostic_fields({'reason': 'provider_unavailable', 'dependency': 'secret.invalid/private',
                    'http_status': 999, 'exception_type': 'UntrustedError',
                    'detail': 'token=secret https://private.invalid /home/private/file'})
        self.assertNotIn('dependency', fields)
        self.assertNotIn('http_status', fields)
        self.assertNotIn('exception_type', fields)
        self.assertNotIn('secret', diagnostic_text(fields))
        self.assertNotIn('/home/private', diagnostic_text(fields))
        event(service='library', stage='identity', code='waiting', **fields)

    def test_browser_and_decoder_causes_are_controlled(self):
        from booksys_core.diagnostics import exception_details, event
        from booksys_core.browser_outcomes import BrowserOutcome, Outcome
        from library.primitives import LibraryError
        for exc, reason in [(BrowserOutcome(Outcome('browser_timeout', 'local')), 'browser_timeout'),
                            (LibraryError('Decoder timed out'), 'decoder_timeout')]:
            details = exception_details(exc)
            self.assertEqual(details['reason'], reason)
            event(service='library', stage='diagnostics', code='failed', **details)

    def test_runtime_emit_literals_match_the_strict_event_schema(self):
        import ast
        from booksys_core.diagnostics import CODES, STAGES, FIELDS
        base = Path(__file__).parents[1]
        for package in ('booksys_core', 'library', 'ops', 'bdnex_service'):
            for path in (base/package).rglob('*.py'):
                for node in ast.walk(ast.parse(path.read_text())):
                    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != 'emit':
                        continue
                    for keyword in node.keywords:
                        with self.subTest(path=path.name, line=node.lineno, field=keyword.arg):
                            if keyword.arg is not None:
                                self.assertIn(keyword.arg, FIELDS)
                            if keyword.arg in ('code', 'stage') and isinstance(keyword.value, ast.Constant):
                                self.assertIn(keyword.value.value, CODES if keyword.arg == 'code' else STAGES)

    def test_cli_preserves_exception_diagnostics_and_exit_status(self):
        import pipeline
        from booksys_core.browser_outcomes import BrowserOutcome, Outcome
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            credentials = root/'credentials.json'
            credentials.write_text('{"api_key":"fixture"}')
            with patch.dict(os.environ, {'BOOKSYS_COLLECTION_DIR': str(root)}), \
                 patch('sys.argv', ['pipeline', '--directory', str(root), '--credentials', str(credentials), '--once']), \
                 patch('pipeline.install_logging'), patch('pipeline.Pyload'), patch('pipeline.Byparr'), \
                 patch('pipeline.run_once', side_effect=BrowserOutcome(Outcome('browser_timeout', 'local'))), \
                 patch('pipeline.emit') as emitted:
                self.assertEqual(pipeline.main(), 1)
                self.assertEqual(emitted.call_args.kwargs['reason'], 'browser_timeout')
                self.assertEqual(emitted.call_args.kwargs['exception_type'], 'BrowserOutcome')

class ByparrOutcomeTests(unittest.TestCase):
    def test_request_failure_is_not_reported_as_success(self):
        import importlib.util
        if importlib.util.find_spec('src') is None:
            self.skipTest('Run this case in the pinned Byparr image')
        from fastapi import HTTPException
        from fastapi.testclient import TestClient
        from ops.byparr.session_api import create_app
        from test_byparr_session import Page, Context, ORIGIN
        from booksys_core.diagnostics import event
        async def browser():
            yield SimpleNamespace(page=Page(), context=Context())
        events = []
        async def navigate(browser, request, timer):
            if request.url.endswith('/timeout'):
                raise TimeoutError('synthetic timeout')
            if request.url.endswith('/rate'):
                raise HTTPException(429, {'code': 'remote_rate_limited'})
            return await browser.page.goto(request.url)
        with patch('ops.byparr.session_api._navigate_and_solve', side_effect=navigate), \
             patch('ops.byparr.session_api.emit') as emitted:
            emitted.side_effect = lambda **fields: events.append(event(**{'reason': 'none', **fields})) or True
            with TestClient(create_app(browser)) as client:
                response = client.post('/v1', json={'url': ORIGIN+'/timeout'})
                self.assertEqual(response.status_code, 408, str(events))
                self.assertEqual(client.post('/v1', json={'url': ORIGIN+'/rate'}).status_code, 429)
                self.assertEqual(client.post('/v1', json={'url': ORIGIN+'/ok'}).status_code, 200)
        terminal = [row for row in events if row.get('outcome')]
        self.assertEqual(len(terminal), 3)
        self.assertEqual((terminal[0]['code'], terminal[0]['outcome'], terminal[0]['reason']),
                         ('failed', 'retry', 'browser_timeout'))

        self.assertEqual((terminal[1]['code'], terminal[1]['outcome'], terminal[1]['reason']),
                         ('failed', 'retry', 'remote_rate_limited'))
        self.assertEqual(terminal[1]['http_status'], 429)
        self.assertEqual((terminal[2]['code'], terminal[2]['outcome'], terminal[2]['reason']),
                         ('completed', 'done', 'none'))

if __name__ == '__main__':
    unittest.main()
