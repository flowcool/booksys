"""Exercise /v1 in the exact Byparr image; no external network or live mounts."""

import importlib.util
import asyncio
import os
import runpy
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

AVAILABLE = importlib.util.find_spec("src") is not None
if AVAILABLE:
    from fastapi.testclient import TestClient
    from ops.byparr.session_api import create_app

ORIGIN = "https://www7.bookys-ebooks.com"


class Page:
    def __init__(self):
        self.url = "about:blank"
        self.visits = 0
        self.main_frame = object()
        self.routes = []
        self.listeners = {}

    def on(self, name, callback):
        self.listeners.setdefault(name, []).append(callback)

    def remove_listener(self, name, callback):
        self.listeners[name].remove(callback)

    def is_closed(self):
        return False

    def response(self, status=200, headers=None):
        request = SimpleNamespace(headers={'user-agent': 'fixture'}, url=self.url,
            frame=self.main_frame, is_navigation_request=lambda: True)
        response = SimpleNamespace(request=request, headers=headers or {}, status=status, url=self.url)
        for callback in self.listeners.get('request', []):
            callback(request)
        for callback in self.listeners.get('response', []):
            callback(response)
        return response

    async def unroute_all(self, **kwargs):
        self.routes.clear()

    async def route(self, pattern, handler):
        self.routes.append(handler)

    async def goto(self, url, **kwargs):
        self.url = url
        self.visits += 1
        if url.endswith("/timeout"):
            raise TimeoutError("fixture timeout")
        if url.endswith("/slow"):
            await asyncio.sleep(3)
        return self.response()

    async def wait_for_load_state(self, *args, **kwargs):
        pass

    async def content(self):
        if self.url.endswith("/bad-html"):
            return "<title>Just a moment...</title>"
        return f"<h1>Visit {self.visits}</h1>"


class Context:
    async def cookies(self, urls):
        return []


@unittest.skipUnless(AVAILABLE, "Run in the pinned Byparr image with /app on PYTHONPATH")
class SessionApiTests(unittest.TestCase):
    def test_environment_budget_sets_default_maximum_and_total_timeout(self):
        adapter = sys.modules[create_app.__module__].__file__
        observed, navigation = [], []
        wait_for = asyncio.wait_for

        async def timed(awaitable, timeout):
            observed.append(timeout)
            return await wait_for(awaitable, timeout)

        class TimedPage(Page):
            async def goto(self, url, **kwargs):
                navigation.append(kwargs['timeout'])
                return await super().goto(url, **kwargs)

        async def browser():
            yield SimpleNamespace(page=TimedPage(), context=Context())

        with patch.dict(os.environ, {'BOOKSYS_BYPARR_BUDGET': '180'}):
            factory = runpy.run_path(adapter)['create_app']
        with patch('src.endpoints.challenge_present', new=AsyncMock(return_value=False)), \
                patch('asyncio.wait_for', side_effect=timed):
            with TestClient(factory(browser)) as client:
                for fields in ({}, {'maxTimeout': 180000}, {'maxTimeout': 5000}):
                    self.assertEqual(client.post('/v1', json={'url': ORIGIN + '/ok', **fields}).status_code, 200)
                self.assertEqual(client.post('/v1', json={'url': ORIGIN + '/ok', 'maxTimeout': 181000}).status_code, 422)
        self.assertEqual([timeout for timeout in observed if timeout != 20], [180, 180, 5, 180])
        for actual, expected in zip(navigation, (180000, 180000, 5000), strict=True):
            self.assertGreater(actual, expected - 1000)
            self.assertLessEqual(actual, expected)
        for invalid in ('0', '-1', 'invalid', '1.5'):
            with patch.dict(os.environ, {'BOOKSYS_BYPARR_BUDGET': invalid}):
                with self.assertRaisesRegex(ValueError, 'BOOKSYS_BYPARR_BUDGET must be a positive integer'):
                    runpy.run_path(adapter)

    def test_listener_transport_failure_still_closes_browser_and_allows_reuse(self):
        closed = []

        class BrokenListenerPage(Page):
            def remove_listener(self, name, callback):
                raise RuntimeError('fixture transport closed before page state updated')

        async def browser():
            try:
                page = BrokenListenerPage() if not closed else Page()
                yield SimpleNamespace(page=page, context=Context())
            finally:
                closed.append(True)

        with patch('src.endpoints.challenge_present', new=AsyncMock(return_value=False)):
            with TestClient(create_app(browser)) as client:
                self.assertEqual(client.post('/v1', json={'url': ORIGIN + '/timeout'}).status_code, 502)
                self.assertEqual(len(closed), 1)
                self.assertEqual(client.get('/health').status_code, 200)
                self.assertEqual(client.post('/v1', json={'url': ORIGIN + '/ok'}).status_code, 200)
            self.assertEqual(len(closed), 2)

    def test_pinned_browser_slow_cleanup_recovers_after_request_timeout(self):
        import invisible_core
        import src.utils as upstream

        closed = []
        browser_pids = []

        def firefox_pids():
            result = set()
            for process in Path('/proc').iterdir():
                if process.name.isdecimal():
                    try:
                        if (process / 'comm').read_text().strip() == 'firefox':
                            result.add(process.name)
                    except FileNotFoundError:
                        pass
            return result

        async def browser():
            generator = upstream.get_browser()
            try:
                native = await anext(generator)
                browser_pids.append(firefox_pids())
                yield native
            finally:
                # A slow but successful teardown must not poison the service.
                try:
                    if len(closed) == 1:
                        await asyncio.sleep(5.1)
                finally:
                    await generator.aclose()
                    closed.append(True)

        async def navigate(browser, request, timer):
            if request.url.endswith('/slow'):
                await asyncio.sleep(11)
            await browser.page.route(request.url, lambda route: route.fulfill(
                status=200, content_type='text/html', body='<h1>Local fixture</h1>'))
            return await browser.page.goto(request.url)

        with (patch.object(upstream, 'warm_up', new=AsyncMock()),
              patch.object(upstream, 'get_browser_geo', new=AsyncMock(
                  return_value=upstream.BrowserGeo('UTC', 'en-US'))),
              patch('invisible_playwright.async_api.prepare_session_geo',
                  return_value=invisible_core.SessionGeo('UTC', None)),
              patch('ops.byparr.session_api._navigate_and_solve', side_effect=navigate)):
            with TestClient(create_app(browser, memory_pressure=lambda: False)) as client:
                self.assertEqual(client.post('/v1', json={'url': ORIGIN + '/ok'}).status_code, 200)
                self.assertEqual(len(closed), 1)
                self.assertTrue(browser_pids[0])
                self.assertFalse(browser_pids[0] & firefox_pids())
                failed = client.post('/v1', json={'url': ORIGIN + '/slow', 'maxTimeout': 10000})
                self.assertEqual((failed.status_code, client.get('/health').status_code), (408, 200))
                self.assertEqual(len(closed), 2)
                self.assertEqual(len(browser_pids), 2)
                self.assertTrue(browser_pids[1])
                self.assertFalse(browser_pids[1] & firefox_pids())
                self.assertEqual(client.post('/v1', json={'url': ORIGIN + '/ok'}).status_code, 200)
                self.assertEqual(len(closed), 3)
            self.assertEqual(len(browser_pids), 3)
            for pids in browser_pids:
                self.assertTrue(pids)
                self.assertFalse(pids & firefox_pids())

    def test_final_navigation_status_wins_over_initial_status_and_html_heading(self):
        class RatePage(Page):
            async def goto(self, url, **kwargs):
                self.url = url
                initial = self.response(429 if url.endswith('/solved') else 200)
                self.response(200 if url.endswith('/solved') else 429, {'retry-after': '1200'})
                return initial
        async def browser():
            yield SimpleNamespace(page=RatePage(), context=Context())
        with patch('src.endpoints.challenge_present', new=AsyncMock(return_value=False)):
            with TestClient(create_app(browser)) as client:
                self.assertEqual(client.post('/v1', json={'url': ORIGIN + '/solved'}).status_code, 200)
                response = client.post('/v1', json={'url': ORIGIN + '/limited'}, headers={'X-Booksys-Request-ID': 'fixture-1'})
                self.assertEqual(response.status_code, 429)
                detail = response.json()['detail']
                self.assertEqual(detail['code'], 'remote_rate_limited')
                self.assertEqual(detail['origin'], 'remote')
                self.assertEqual(detail['retry_after_seconds'], 1200)
                self.assertEqual(detail['request_id'], 'fixture-1')
                self.assertEqual(response.headers['X-Booksys-Request-ID'], 'fixture-1')

    def test_successive_requests_reuse_the_browser_page(self):
        pages, closed = [], []

        async def browser():
            page = Page()
            pages.append(page)
            try:
                yield SimpleNamespace(page=page, context=Context())
            finally:
                closed.append(page)

        with patch("src.endpoints.challenge_present", new=AsyncMock(return_value=False)):
            with TestClient(create_app(browser)) as client:
                first = client.post("/v1", json={"url": ORIGIN + "/first"})
                self.assertEqual(closed, pages)
                second = client.post("/v1", json={"url": ORIGIN + "/second"})
                self.assertEqual(closed, pages)
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.headers['X-Booksys-Request-ID'])
        self.assertEqual(first.json()["solution"]["response"], "<h1>Visit 1</h1>")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["solution"]["response"], "<h1>Visit 1</h1>")
        self.assertEqual(len(pages), 2)
        self.assertIsNot(pages[0], pages[1])

    def test_timeout_is_not_retried_and_next_request_gets_a_clean_session(self):
        async def browser():
            yield SimpleNamespace(page=Page(), context=Context())

        with patch("src.endpoints.challenge_present", new=AsyncMock(return_value=False)):
            with TestClient(create_app(browser)) as client:
                failed = client.post("/v1", json={"url": ORIGIN + "/timeout"})
                resumed = client.post("/v1", json={"url": ORIGIN + "/second"})
        self.assertEqual(failed.status_code, 408)
        self.assertEqual(resumed.json()["solution"]["response"], "<h1>Visit 1</h1>")

    def test_invalid_html_resets_session_and_off_site_requests_are_rejected(self):
        async def browser():
            yield SimpleNamespace(page=Page(), context=Context())

        with patch("src.endpoints.challenge_present", new=AsyncMock(return_value=False)):
            with TestClient(create_app(browser)) as client:
                invalid = client.post("/v1", json={"url": ORIGIN + "/bad-html"})
                external = client.post("/v1", json={"url": "https://hoster.example/file"})
                resumed = client.post("/v1", json={"url": ORIGIN + "/ok"})
        self.assertEqual(invalid.status_code, 502)
        self.assertEqual(external.status_code, 422)
        self.assertEqual(resumed.json()["solution"]["response"], "<h1>Visit 1</h1>")

    def test_session_renews_after_request_limit_and_idle_expiry(self):
        now = [0.0]

        async def browser():
            yield SimpleNamespace(page=Page(), context=Context())

        with patch("src.endpoints.challenge_present", new=AsyncMock(return_value=False)):
            with TestClient(create_app(browser, max_requests=2, clock=lambda: now[0])) as client:
                def visit():
                    return client.post("/v1", json={"url": ORIGIN + "/ok"}).json()["solution"]["response"]
                self.assertEqual(visit(), "<h1>Visit 1</h1>")
                self.assertEqual(visit(), "<h1>Visit 1</h1>")
                self.assertEqual(visit(), "<h1>Visit 1</h1>")
                self.assertEqual(client.get("/health").json()["generation"], 3)
                now[0] = 1801.0
                self.assertEqual(visit(), "<h1>Visit 1</h1>")
                self.assertEqual(client.get("/health").json()["generation"], 4)

    def test_busy_page_rejects_a_second_caller_without_navigating(self):
        entered = threading.Event()
        release = threading.Event()

        class SlowPage(Page):
            async def goto(self, url, **kwargs):
                entered.set()
                while not release.is_set():
                    await asyncio.sleep(0.01)
                return await super().goto(url, **kwargs)

        async def browser():
            yield SimpleNamespace(page=SlowPage(), context=Context())

        with patch("src.endpoints.challenge_present", new=AsyncMock(return_value=False)):
            with TestClient(create_app(browser)) as client:
                responses = []
                thread = threading.Thread(target=lambda: responses.append(
                    client.post("/v1", json={"url": ORIGIN + "/first"})))
                thread.start()
                try:
                    self.assertTrue(entered.wait(3))
                    second = []
                    contender = threading.Thread(target=lambda: second.append(
                        client.post("/v1", json={"url": ORIGIN + "/second"})))
                    contender.start()
                    contender.join(0.5)
                finally:
                    release.set()
                    thread.join(3)
                    contender.join(3)
                self.assertEqual(second[0].status_code, 429)
                self.assertEqual(responses[0].json()["solution"]["response"], "<h1>Visit 1</h1>")

    def test_total_budget_cancels_request_and_shutdown_releases_browser(self):
        closed = []

        async def browser():
            try:
                yield SimpleNamespace(page=Page(), context=Context())
            finally:
                closed.append(True)

        with patch("src.endpoints.challenge_present", new=AsyncMock(return_value=False)):
            with TestClient(create_app(browser)) as client:
                failed = client.post("/v1", json={"url": ORIGIN + "/slow", "maxTimeout": 1000})
                self.assertEqual(failed.status_code, 408)
                self.assertEqual(len(closed), 1)
                self.assertEqual(client.post("/v1", json={"url": ORIGIN + "/ok"}).status_code, 200)
            self.assertEqual(len(closed), 2)

    def test_failed_cleanup_requires_restart_without_opening_more_browsers(self):
        async def browser():
            try:
                yield SimpleNamespace(page=Page(), context=Context())
            finally:
                raise RuntimeError("fixture cleanup failed")

        with patch("src.endpoints.challenge_present", new=AsyncMock(return_value=False)):
            with TestClient(create_app(browser), raise_server_exceptions=False) as client:
                self.assertEqual(client.post("/v1", json={"url": ORIGIN + "/bad-html"}).status_code, 503)
                self.assertEqual(client.get("/health").status_code, 503)
                self.assertEqual(client.post("/v1", json={"url": ORIGIN + "/ok"}).status_code, 503)

    def test_expired_age_and_repeated_media_routes_do_not_accumulate(self):
        now = [0.0]
        pages, closed = [], []

        class RoutedPage(Page):
            async def content(self):
                return f"<h1>Visit {self.visits}; routes {len(self.routes)}</h1>"

        async def browser():
            page = RoutedPage()
            pages.append(page)
            try:
                yield SimpleNamespace(page=page, context=Context())
            finally:
                closed.append(page)

        with patch("src.endpoints.challenge_present", new=AsyncMock(return_value=False)):
            with TestClient(create_app(browser, clock=lambda: now[0])) as client:
                for index in range(1, 5):
                    response = client.post("/v1", json={"url": ORIGIN + "/ok", "blockMedia": True})
                    self.assertEqual(response.json()["solution"]["response"], "<h1>Visit 1; routes 1</h1>")
                    self.assertEqual(len(pages), index)
                    self.assertEqual(closed, pages)
                    self.assertTrue(all(not callbacks for callbacks in pages[-1].listeners.values()))
                    now[0] += 1000
                response = client.post("/v1", json={"url": ORIGIN + "/ok"})
                self.assertEqual(response.json()["solution"]["response"], "<h1>Visit 1; routes 1</h1>")
                self.assertEqual(len(pages), 5)
                self.assertEqual(closed, pages)
                self.assertEqual(len({id(page) for page in pages}), 5)
                self.assertTrue(all(not callbacks for page in pages for callbacks in page.listeners.values()))

    def test_memory_pressure_releases_browser_before_the_next_request(self):
        async def browser():
            yield SimpleNamespace(page=Page(), context=Context())

        pressure = [False]
        with patch("src.endpoints.challenge_present", new=AsyncMock(return_value=False)):
            with TestClient(create_app(browser, memory_pressure=lambda: pressure[0])) as client:
                client.post("/v1", json={"url": ORIGIN + "/ok"})
                pressure[0] = True
                client.post("/v1", json={"url": ORIGIN + "/ok"})
                pressure[0] = False
                response = client.post("/v1", json={"url": ORIGIN + "/ok"})
                self.assertEqual(response.json()["solution"]["response"], "<h1>Visit 1</h1>")

    def test_solver_failure_is_not_retried_and_discards_clearance(self):
        closed = []

        async def browser():
            try:
                yield SimpleNamespace(page=Page(), context=Context())
            finally:
                closed.append(True)

        with patch("src.endpoints.challenge_present", new=AsyncMock(side_effect=[True, False])):
            with patch("src.endpoints.solve_challenge", new=AsyncMock(side_effect=TimeoutError("solver failed"))) as solver:
                with TestClient(create_app(browser)) as client:
                    failed = client.post("/v1", json={"url": ORIGIN + "/ok"})
                    self.assertEqual(failed.status_code, 408)
                    self.assertEqual(len(closed), 1)
                    self.assertEqual(solver.await_count, 1)
                    resumed = client.post("/v1", json={"url": ORIGIN + "/ok"})
                    self.assertEqual(resumed.json()["solution"]["response"], "<h1>Visit 1</h1>")
