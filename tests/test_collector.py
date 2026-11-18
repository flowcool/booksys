"""Exercise public collection boundaries with captured HTML and a local API."""

import json
import os
import runpy
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from postgres_fixture import create_store
from library.primitives import empty
from library.operational_state import CollectorState

from booksys import Byparr, CollectionError, DEFAULT_CATEGORY, catalogue, collect

FIXTURES = Path(__file__).parent / "fixtures"
FIRST = "https://www7.bookys-ebooks.com/bandes-dessinees/358548-star-wars-histoires-de-lhyperespace-tome-06-la-decrypteuse-2026"
SECOND = "https://www7.bookys-ebooks.com/bandes-dessinees/358545-les-amis-de-jacobs-tome-23"
PAGES = {
    DEFAULT_CATEGORY: "catalogue-1.html",
    DEFAULT_CATEGORY + "?page=2": "catalogue-2.html",
    FIRST: "item-358548.html", SECOND: "item-358545.html",
    FIRST + "/dl/2435949": "download-2435949.html",
    FIRST + "/dl/2435951": "download-2435951.html",
    SECOND + "/dl/2435940": "download-2435940.html",
    SECOND + "/dl/2435942": "download-2435942.html",
}


def captured(url):
    return (FIXTURES / PAGES[url]).read_text()


class CollectorTests(unittest.TestCase):
    def test_large_configured_browser_budget_runs_bounded_passes_and_resumes(self):
        from booksys_core.collection import run_once
        for warm_session in (False, True):
            with self.subTest(warm_session=warm_session), \
                    patch.dict(os.environ, BOOKSYS_BYPARR_BUDGET='300'), \
                    tempfile.TemporaryDirectory() as directory:
                browser = Byparr('http://byparr/v1', timeout=int(os.environ['BOOKSYS_BYPARR_BUDGET']))
                if warm_session:
                    browser.session = {'cookies': [{'name': 'session', 'value': 'synthetic'}],
                                       'userAgent': 'synthetic'}
                elapsed, submitted, requests = [0.0], [], []
                journal = {'categories': {}, 'submissions': {}}
                store = Mock()
                store.monitor.return_value = journal
                store.schedule.return_value = {}
                store.captured.return_value = {'items': {
                    source: {'title': 'Synthetic', 'complete_hosters': ['*'], 'downloads': {}}
                    for source in (FIRST, SECOND)}}
                store.category.side_effect = lambda source, value: journal['categories'].__setitem__(source, value)
                downloader = Mock()
                def status(*args):
                    elapsed[0] += 40
                    return {'pause': False, 'download': True}
                def submit(source, *args):
                    elapsed[0] += 30
                    self.assertNotIn(source, submitted)
                    submitted.append(source)
                    journal['submissions'][source] = {'status': 'queued', 'package_id': len(submitted)}
                def transport(request, *, timeout):
                    elapsed[0] += timeout
                    if request.full_url != browser.endpoint:
                        raise TimeoutError('Synthetic cached session failure')
                    requests.append(json.loads(request.data))
                    response = Mock()
                    response.read.return_value = json.dumps({'status': 'ok', 'solution': {
                        'status': 200, 'url': DEFAULT_CATEGORY,
                        'response': '<h1>Catalogue</h1><div class="bys-items-container">'
                                    f'<a class="bys-item" href="{FIRST}">First</a>'
                                    f'<a class="bys-item" href="{SECOND}">Second</a></div>'}}).encode()
                    response.__enter__ = Mock(return_value=response)
                    response.__exit__ = Mock(return_value=False)
                    return response
                downloader.call.side_effect = status
                downloader.submit.side_effect = submit
                with patch('booksys_core.collection.CollectorState', return_value=store), \
                        patch('booksys_core.scheduler.CollectorState', return_value=store), \
                        patch('booksys_core.collection.CRAWL_PAGES', 1), \
                        patch('booksys_core.browser.urlopen', side_effect=transport), \
                        patch('booksys_core.browser.time.sleep', side_effect=lambda _: elapsed.__setitem__(0, elapsed[0] + 2)):
                    for _ in range(4):
                        elapsed[0] = 0
                        result = run_once(Path(directory), browser, downloader, monotonic=lambda: elapsed[0])
                        self.assertLessEqual(elapsed[0], 420)
                        self.assertEqual(browser.timeout, 300, 'The configured budget must survive the pass')
                        if not result.get('continued'):
                            break
                    else:
                        self.fail('Bounded passes did not finish')
                self.assertEqual(result['pages'], 0, 'Resume must use the retained catalogue page')
                self.assertEqual(submitted, [FIRST, SECOND])
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0]['maxTimeout'], 300000)
                self.assertNotIn('continuation', journal['categories'][DEFAULT_CATEGORY])

    def test_pass_budget_after_native_write_reconciles_without_a_second_submission(self):
        from booksys_core.collection import run_once
        from booksys_core.downloader import Pyload
        journal = {'categories': {}, 'submissions': {}}
        records = {FIRST: {'title': 'Synthetic', 'complete_hosters': ['*'],
                          'downloads': {'target': {'hoster': 'synthetic', 'urls': ['https://files.example/a']}}}}
        store = Mock()
        store.monitor.return_value = journal
        store.captured.return_value = {'items': records}
        store.category.side_effect = lambda source, value: journal['categories'].__setitem__(source, value)
        client = object.__new__(Pyload)
        client.admission = None
        elapsed, packages, calls = [0], [], []
        def native(method, *, write=False, **params):
            elapsed[0] += 30  # The configured native transport timeout.
            calls.append(method)
            if method == 'status_server':
                return {'pause': False, 'download': True}
            if method in ('get_queue', 'get_collector'):
                return [p.copy() for p in packages if p['dest'] == (1 if method == 'get_queue' else 0)]
            if method == 'add_package':
                packages.append({'pid': 7, 'name': params['name'], 'dest': 0,
                                 'links': [{'url': url} for url in params['links']]})
                return 7
            if method == 'get_package_data':
                return packages[0].copy()
            if method == 'move_package':
                packages[0]['dest'] = 1
                return None
            self.fail(method)
        with tempfile.TemporaryDirectory() as directory, \
                patch('booksys_core.collection.CollectorState', return_value=store), \
                patch('booksys_core.downloader.CollectorState', return_value=store), \
                patch('booksys_core.collection.ScheduledCalls'), \
                patch('booksys_core.collection.catalogue', return_value=([FIRST], None)), \
                patch('booksys_core.collection.CRAWL_PAGES', 1), \
                patch.object(client, '_call', side_effect=native), \
                patch.object(client, 'host_availability', return_value={'hosts': {'synthetic': {'status': True, 'domains': ['files.example']}}}):
            first = run_once(Path(directory), Mock(), client, max_seconds=210, monotonic=lambda: elapsed[0])
            self.assertTrue(first['continued'])
            self.assertEqual(journal['submissions'][FIRST]['status'], 'moving')
            self.assertLess(elapsed[0], 300)
            elapsed[0] = 0
            second = run_once(Path(directory), Mock(), client, max_seconds=210, monotonic=lambda: elapsed[0])
            self.assertNotIn('continued', second)
            self.assertLess(elapsed[0], 300)
        self.assertEqual(journal['submissions'][FIRST]['status'], 'queued')
        self.assertEqual((calls.count('add_package'), calls.count('move_package')), (1, 1))

    def test_native_api_error_response_is_a_certain_refusal(self):
        import io
        from booksys_core.downloader import Pyload
        from booksys_core.errors import NativeRefusal
        client = object.__new__(Pyload)
        client.endpoint, client.key, client.admission = 'http://localhost', 'synthetic', None
        response = Mock()
        response.__enter__ = Mock(return_value=io.BytesIO(b'{"error":"refused"}'))
        response.__exit__ = Mock(return_value=False)
        with patch('booksys_core.downloader.urlopen', return_value=response), \
                patch('booksys_core.diagnostics.emit') as emitted:
            with self.assertRaises(NativeRefusal):
                client.call('add_package', write=True)
        failure = emitted.call_args.kwargs
        self.assertEqual(failure['reason'], 'submission_refused')
        self.assertEqual(failure['exception_type'], 'NativeRefusal')

    def test_submit_failures_distinguish_refusal_from_uncertain_effect_and_keep_operation(self):
        from booksys_core.collection import run_once
        from booksys_core.errors import NativeRefusal
        from booksys_core.diagnostics import context, event
        journal = {'categories': {}, 'submissions': {
            FIRST: {'status': 'creating', 'operation_id': 'refused-operation'},
            SECOND: {'status': 'creating', 'operation_id': 'known-operation'}}}
        records = {source: {'downloads': {}} for source in (FIRST, SECOND)}
        store = Mock(); store.monitor.return_value = journal; store.captured.return_value = {'items': records}
        downloader = Mock(); downloader.call.return_value = {'pause': False, 'download': True}
        downloader.submit.side_effect = [NativeRefusal('pyLoad rejected add_package'), OSError('token=secret')]
        events = []
        with tempfile.TemporaryDirectory() as directory, \
                patch('booksys_core.collection.CollectorState', return_value=store), \
                patch('booksys_core.collection.ScheduledCalls'), \
                patch('booksys_core.collection.catalogue', return_value=([FIRST, SECOND], None)), \
                patch('booksys_core.collection.CRAWL_PAGES', 1), \
                patch('booksys_core.diagnostics.emit', side_effect=lambda **fields: events.append(event(**fields))), \
                context(request_id='owning-request', attempt=2):
            run_once(Path(directory), Mock(), downloader)
        self.assertEqual([value['reason'] for value in events], ['submission_refused', 'unknown_effect'])
        self.assertEqual([value['exception_type'] for value in events], ['NativeRefusal', 'OSError'])
        self.assertEqual([value['request_id'] for value in events], ['owning-request'] * 2)
        self.assertEqual([value['attempt'] for value in events], [2, 2])
        self.assertEqual(events[1]['operation_id'], 'known-operation')
        self.assertIn({'kind': 'operation', 'id': 'known-operation'}, events[1]['refs'])
        self.assertNotIn('secret', json.dumps(events))

    def test_bounded_pass_resumes_saved_items_and_partial_links_without_resubmit(self):
        from booksys_core.collection import run_once
        journal = {'version': 1, 'categories': {}, 'submissions': {}}
        records = {}
        store = Mock()
        store.monitor.side_effect = lambda: journal
        store.captured.side_effect = lambda: {'items': records}
        store.category.side_effect = lambda source, value: journal['categories'].__setitem__(source, value.copy())
        store.capture.side_effect = lambda source, value: records.__setitem__(source, value.copy())
        submitted = []
        downloader = Mock()
        downloader.call.return_value = {'pause': False, 'download': True}
        def submit(source, record, journal, path):
            self.assertNotIn(source, submitted)
            self.assertTrue(all('urls' in row for row in record['downloads'].values()))
            submitted.append(source)
            journal['submissions'][source] = {'status': 'queued', 'package_id': len(submitted)}
        downloader.submit.side_effect = submit
        elapsed = [0.0]
        pages = []
        def fetch(url):
            elapsed[0] += 100  # Each browser read is slow. One pass cannot fit two reads.
            if '?page=' in url:
                pages.append(url)
                # A later fresh catalogue omits SECOND. Resume must use the saved page.
                return (f'<h1>Catalogue</h1><div class="bys-items-container"><a class="bys-item" href="{FIRST}">First</a>'
                        f'<a class="bys-item" href="{SECOND}">Second</a></div>') if len(pages) == 1 else '<h1>Catalogue</h1>'
            if url in (FIRST, SECOND):
                return '<h1>Book</h1><a href="/book/dl/1">First host</a><a href="/book/dl/2">Second host</a>'
            return '<h1>Links</h1><a class="bys-link bys-host" href="https://files.example/a">File</a>'
        with tempfile.TemporaryDirectory() as directory, \
                patch('booksys_core.collection.CollectorState', return_value=store), \
                patch('booksys_core.collection.ScheduledCalls', side_effect=lambda directory, fetch, **kw: fetch), \
                patch('booksys_core.collection.CRAWL_PAGES', 1):
            for _ in range(12):
                elapsed[0] = 0
                result = run_once(Path(directory), fetch, downloader, max_seconds=210, monotonic=lambda: elapsed[0])
                self.assertLess(elapsed[0], 300)
                if not result.get('continued'):
                    break
            else:
                self.fail('Bounded passes did not finish')
        self.assertEqual(submitted, [FIRST, SECOND])
        self.assertEqual(len(pages), 1)
        self.assertNotIn('continuation', journal['categories'][DEFAULT_CATEGORY])

    def test_run_once_continues_after_package_folder_name_too_long(self):
        from booksys_core.collection import run_once
        journal = {'categories': {}, 'submissions': {}}
        records = {source: {'downloads': {}} for source in (FIRST, SECOND)}
        store = Mock(); store.monitor.return_value = journal; store.captured.return_value = {'items': records}
        downloader = Mock(); downloader.call.return_value = {'pause': False, 'download': True}
        downloader.submit.side_effect = [CollectionError('pyLoad package folder name too long'), 2]
        with tempfile.TemporaryDirectory() as temporary, \
                patch('booksys_core.collection.CollectorState', return_value=store), \
                patch('booksys_core.collection.ScheduledCalls'), \
                patch('booksys_core.collection.catalogue', return_value=([FIRST, SECOND], None)), \
                patch('booksys_core.collection.CRAWL_PAGES', 1):
            counts = run_once(Path(temporary), Mock(), downloader)
        self.assertEqual(counts, {'pages': 1, 'queued': 1, 'skipped': 1})
        self.assertEqual([call.args[0] for call in downloader.submit.call_args_list], [FIRST, SECOND])

    def test_run_once_continues_after_local_oserror(self):
        from booksys_core.collection import run_once
        journal = {'categories': {}, 'submissions': {}}
        records = {source: {'downloads': {}} for source in (FIRST, SECOND)}
        store = Mock(); store.monitor.return_value = journal; store.captured.return_value = {'items': records}
        downloader = Mock(); downloader.call.return_value = {'pause': False, 'download': True}
        downloader.submit.side_effect = [OSError(5, "boom"), 2]
        with tempfile.TemporaryDirectory() as temporary, \
                patch('booksys_core.collection.CollectorState', return_value=store), \
                patch('booksys_core.collection.ScheduledCalls'), \
                patch('booksys_core.collection.catalogue', return_value=([FIRST, SECOND], None)), \
                patch('booksys_core.collection.CRAWL_PAGES', 1):
            counts = run_once(Path(temporary), Mock(), downloader)
        self.assertEqual(counts, {'pages': 1, 'queued': 1, 'skipped': 1})
        self.assertEqual([call.args[0] for call in downloader.submit.call_args_list], [FIRST, SECOND])

    def test_run_once_continues_after_one_unconfirmed_submission(self):
        from unittest.mock import Mock, patch
        from booksys_core.collection import run_once
        journal = {'categories': {}, 'submissions': {FIRST: {'status': 'creating'}}}
        records = {source: {'downloads': {}} for source in (FIRST, SECOND)}
        store = Mock(); store.monitor.return_value = journal; store.captured.return_value = {'items': records}
        downloader = Mock(); downloader.call.return_value = {'pause': False, 'download': True}
        downloader.submit.side_effect = [CollectionError('Unconfirmed write'), 2]
        with tempfile.TemporaryDirectory() as temporary, \
                patch('booksys_core.collection.CollectorState', return_value=store), \
                patch('booksys_core.collection.ScheduledCalls'), \
                patch('booksys_core.collection.catalogue', return_value=([FIRST, SECOND], None)), \
                patch('booksys_core.collection.CRAWL_PAGES', 1):
            counts = run_once(Path(temporary), Mock(), downloader)
        self.assertEqual(counts, {'pages': 1, 'queued': 1, 'skipped': 1})
        self.assertEqual([call.args[0] for call in downloader.submit.call_args_list], [FIRST, SECOND])
        self.assertEqual(journal['submissions'][FIRST], {'status': 'creating'})

    def test_real_catalogues_have_40_distinct_items_and_next_page(self):
        first, next_url = catalogue(captured(DEFAULT_CATEGORY), DEFAULT_CATEGORY)
        second, _ = catalogue(captured(next_url), next_url)
        self.assertEqual((len(first), len(second), len(first.keys() | second.keys())), (20, 20, 40))
        self.assertEqual(next_url, DEFAULT_CATEGORY + "?page=2")

    def test_collect_exports_real_links_and_skips_completed_items_on_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            create_store(self, directory, empty())
            counts = collect([DEFAULT_CATEGORY], captured, directory,
                             {"1fichier", "nitroflare"}, max_items=2)
            self.assertEqual(counts["collected"], 2)
            self.assertEqual(set((directory / "nitroflare.txt").read_text().splitlines()), {
                "https://nitroflare.com/view/E2C0E9A951C27E1",
                "https://nitroflare.com/view/5C559684FE7B60E"})
            self.assertEqual(len((directory / "1fichier.txt").read_text().splitlines()), 2)
            calls = []

            def restart(url):
                calls.append(url)
                if url == DEFAULT_CATEGORY:
                    return f'<h1>Catalogue</h1><div class="bys-items-container"><a class="bys-item" href="{FIRST}">First</a><a class="bys-item" href="{SECOND}">Second</a></div>'
                self.fail("Restart requested an already completed item")

            counts = collect([DEFAULT_CATEGORY], restart, directory, {"1fichier", "nitroflare"})
            self.assertEqual(counts, {"pages": 1, "collected": 0, "skipped": 2})
            self.assertEqual(calls, [DEFAULT_CATEGORY])

    def test_collect_follows_pagination_and_filters_hosters(self):
        def fetch(url):
            if url in {DEFAULT_CATEGORY, DEFAULT_CATEGORY + "?page=2"}:
                item = FIRST if url == DEFAULT_CATEGORY else SECOND
                next_link = '<a href="?page=2">Next</a>' if item == FIRST else ""
                return f'<h1>Catalogue</h1><div class="bys-items-container"><a class="bys-item" href="{item}">Item</a></div>{next_link}'
            self.assertNotIn(url, {FIRST + "/dl/2435951", SECOND + "/dl/2435942"})
            return captured(url)

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            create_store(self, directory, empty())
            counts = collect([DEFAULT_CATEGORY], fetch, directory, {"1fichier"}, pages=0)
            self.assertEqual(counts["pages"], 2)
            self.assertEqual(counts["collected"], 2)
            self.assertFalse((directory / "nitroflare.txt").exists())

    def test_challenge_stops_but_keeps_prior_item_and_exports(self):
        def fetch(url):
            if url == SECOND:
                return "<title>Just a moment...</title>"
            return captured(url)

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            create_store(self, directory, empty())
            with self.assertRaises(CollectionError):
                collect([DEFAULT_CATEGORY], fetch, directory, {"nitroflare"})
            self.assertEqual((directory / "nitroflare.txt").read_text(),
                             "https://nitroflare.com/view/E2C0E9A951C27E1\n")
            self.assertIn(FIRST, CollectorState(directory).captured()["items"])
            self.assertNotIn(SECOND, CollectorState(directory).captured()["items"])

    def test_off_site_link_is_rejected_before_fetch(self):
        calls = []

        def fetch(url):
            calls.append(url)
            return '<h1>Catalogue</h1><div class="bys-items-container"><a class="bys-item" href="https://evil.example/123-book">Book</a></div>'

        with tempfile.TemporaryDirectory() as temporary:
            create_store(self, Path(temporary), empty())
            with self.assertRaises(CollectionError):
                collect([DEFAULT_CATEGORY], fetch, Path(temporary), {"1fichier"})
        self.assertEqual(calls, [DEFAULT_CATEGORY])

    def test_all_hosters_and_every_host_link_are_kept(self):
        page = '<h1>Book</h1><table><tr><td><a href="/book/dl/99">NewHost</a></td></tr></table>'
        outgoing = ('<h1>Download</h1><a class="bys-link bys-host" href="https://files.example/a">Part 1</a>'
                    '<a class="bys-link bys-host" href="https://files.example/b">Part 2</a>'
                    '<a href="https://advert.example/">Advertisement</a>')
        def fetch(url):
            if url == DEFAULT_CATEGORY:
                return f'<h1>Catalogue</h1><div class="bys-items-container"><a class="bys-item" href="{FIRST}">Book</a></div>'
            return page if url == FIRST else outgoing
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            create_store(self, directory, empty())
            collect([DEFAULT_CATEGORY], fetch, directory)
            self.assertEqual((directory / "all-links.txt").read_text(),
                             "https://files.example/a\nhttps://files.example/b\n")
            self.assertTrue((directory / "newhost.txt").exists())




class ByparrTests(unittest.TestCase):
    def test_owned_local_busy_is_distinct_from_remote_or_unknown_refusal(self):
        from booksys_core.browser_outcomes import BrowserOutcome
        outcomes = [('browser_busy', 'local'), ('remote_rate_limited', 'remote'), ('unknown_browser_failure', 'unknown')]
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                code, origin = outcomes.pop(0)
                body = json.dumps({'detail': {'schema': 1, 'code': code, 'origin': origin,
                    'request_id': self.headers.get('X-Booksys-Request-ID'),
                    'retry_after_state': 'valid', 'retry_after_seconds': 1200}}).encode()
                self.send_response(429)
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *_args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = Byparr(f'http://127.0.0.1:{server.server_port}/v1', delay=0)
            for expected in ('browser_busy', 'remote_rate_limited', 'unknown_browser_failure'):
                with self.assertRaises(BrowserOutcome) as result:
                    client(DEFAULT_CATEGORY)
                self.assertEqual(result.exception.outcome.code, expected)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_real_api_contract_and_error_handling(self):
        responses = [
            {"status": "ok", "solution": {"status": 200, "url": DEFAULT_CATEGORY,
                                           "response": captured(DEFAULT_CATEGORY)}},
            {"status": "error", "message": "Challenge timeout"},
            {"status": "ok", "solution": {"status": 200, "url": DEFAULT_CATEGORY,
                                           "response": "<title>Just a moment...</title>"}},
        ]
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                body = json.dumps(responses.pop(0)).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.dict(os.environ, {'BOOKSYS_BYPARR_BUDGET': '180'}):
                browser = runpy.run_path(str(Path(__file__).parents[1] / 'booksys_core/browser.py'))['Byparr']
            client = browser(f"http://127.0.0.1:{server.server_port}/v1", delay=0)
            transport = client._fetch_byparr.__globals__
            opener = Mock(wraps=transport['urlopen'])
            with patch.dict(transport, {'urlopen': opener}):
                self.assertIn("bys-items-container", client(DEFAULT_CATEGORY))
            self.assertEqual(opener.call_args.kwargs['timeout'], 200)
            for _ in range(2):
                with self.assertRaises(CollectionError):
                    client(DEFAULT_CATEGORY)
            self.assertEqual(received[0], {"cmd": "request.get", "url": DEFAULT_CATEGORY,
                                            "maxTimeout": 180000, "blockMedia": False})
            for invalid in ('0', '-1', 'invalid', '1.5'):
                with patch.dict(os.environ, {'BOOKSYS_BYPARR_BUDGET': invalid}):
                    with self.assertRaisesRegex(ValueError, 'BOOKSYS_BYPARR_BUDGET must be a positive integer'):
                        runpy.run_path(str(Path(__file__).parents[1] / 'booksys_core/browser.py'))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
