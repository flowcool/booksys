"""Exercise /v1 in the exact Byparr image; no external network or live mounts."""

import importlib.util
import asyncio
import threading
import struct
import traceback
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
        from invisible_core.juggler.connection import ProcessOutput
        from invisible_core.juggler import connection
        from invisible_playwright.async_api import InvisiblePlaywright

        closed = []
        browser_pids = []
        outputs = []
        output_init = ProcessOutput.__init__
        processes = []
        spawn = connection._spawn_posix
        teardown = InvisiblePlaywright._teardown

        def retain_process(*args, **kwargs):
            result = spawn(*args, **kwargs)
            processes.append(result[2])
            return result

        async def report_before_cleanup(session):
            try:
                for process in processes:
                    code = process.poll()
                    print("Browser before cleanup:", {"pid": process.pid, "returncode": code,
                        "signal": -code if code is not None and code < 0 else None}, flush=True)
                # Mozilla FIREFOX_159_0_RELEASE minidump_format.h: stream 6,
                # MDRawExceptionStream. Read metadata, never process memory.
                for dump in list(Path('/tmp').rglob('*.dmp'))[:8]:
                    try:
                        size = dump.stat().st_size
                        with dump.open('rb') as stream:
                            header = stream.read(32)
                            if len(header) != 32:
                                continue
                            signature, _, count, directory = struct.unpack_from('<IIII', header)
                            if signature != 0x504d444d or count > 128 or directory + count * 12 > size:
                                continue
                            stream.seek(directory)
                            entries = stream.read(count * 12)
                            for offset in range(0, len(entries), 12):
                                kind, length, position = struct.unpack_from('<III', entries, offset)
                                if kind != 6 or length < 32 or position + 32 > size:
                                    continue
                                stream.seek(position)
                                record = stream.read(32)
                                print("Browser minidump exception:", {"file": dump.name, "size": size,
                                    "code": struct.unpack_from('<I', record, 8)[0],
                                    "address": hex(struct.unpack_from('<Q', record, 24)[0])}, flush=True)
                    except (OSError, ValueError, struct.error) as error:
                        print("Minidump metadata unavailable:", type(error).__name__, flush=True)
            finally:
                await teardown(session)

        def retain_output(output, stream):
            output_init(output, stream)
            outputs.append(output)

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
                try:
                    native = await anext(generator)
                except BaseException:
                    traceback.print_exc()
                    for output in outputs:
                        print("Browser startup output:", output.tail(), flush=True)
                    for pid in sorted(firefox_pids()):
                        status = Path('/proc', pid, 'status')
                        try:
                            print("Browser process:", [line for line in status.read_text().splitlines()
                                if line.startswith(('Name:', 'State:', 'VmRSS:', 'Threads:'))], flush=True)
                        except FileNotFoundError:
                            pass
                    for name in ('memory.events', 'pids.events'):
                        events = Path('/sys/fs/cgroup', name)
                        if events.is_file():
                            print(name, events.read_text(), flush=True)
                    raise
                browser_pids.append(firefox_pids())
                yield native
            finally:
                # A slow but successful teardown must not poison the service.
                try:
                    if not closed:
                        await asyncio.sleep(5.1)
                finally:
                    await generator.aclose()
                    closed.append(True)

        async def navigate(browser, request, timer):
            if request.url.endswith('/slow'):
                await asyncio.sleep(3)
            await browser.page.route(request.url, lambda route: route.fulfill(
                status=200, content_type='text/html', body='<h1>Local fixture</h1>'))
            return await browser.page.goto(request.url)

        with (patch.object(connection, '_spawn_posix', new=retain_process),
              patch.object(InvisiblePlaywright, '_teardown', new=report_before_cleanup),
              patch.object(ProcessOutput, 'KEEP', 2000),
              patch.object(ProcessOutput, '__init__', new=retain_output),
              patch.object(upstream, 'warm_up', new=AsyncMock()),
              patch.object(upstream, 'get_browser_geo', new=AsyncMock(
                  return_value=upstream.BrowserGeo('UTC', 'en-US'))),
              patch('invisible_playwright.async_api.prepare_session_geo',
                  return_value=invisible_core.SessionGeo('UTC', None)),
              patch('ops.byparr.session_api._navigate_and_solve', side_effect=navigate)):
            with TestClient(create_app(browser, memory_pressure=lambda: False)) as client:
                response = client.post('/v1', json={'url': ORIGIN + '/ok'})
                self.assertEqual(response.status_code, 200, response.json())
                failed = client.post('/v1', json={'url': ORIGIN + '/slow', 'maxTimeout': 1000})
                self.assertEqual((failed.status_code, client.get('/health').status_code), (408, 200))
                self.assertEqual(len(closed), 1)
                self.assertTrue(browser_pids[0])
                self.assertFalse(browser_pids[0] & firefox_pids())
                self.assertEqual(client.post('/v1', json={'url': ORIGIN + '/ok'}).status_code, 200)
            self.assertEqual(len(closed), 2)
            self.assertFalse(browser_pids[1] & firefox_pids())

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
        async def browser():
            yield SimpleNamespace(page=Page(), context=Context())

        with patch("src.endpoints.challenge_present", new=AsyncMock(return_value=False)):
            with TestClient(create_app(browser)) as client:
                first = client.post("/v1", json={"url": ORIGIN + "/first"})
                second = client.post("/v1", json={"url": ORIGIN + "/second"})
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.headers['X-Booksys-Request-ID'])
        self.assertEqual(second.json()["solution"]["response"], "<h1>Visit 2</h1>")

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
                self.assertEqual(visit(), "<h1>Visit 2</h1>")
                self.assertEqual(visit(), "<h1>Visit 1</h1>")
                now[0] = 1801.0
                self.assertEqual(visit(), "<h1>Visit 1</h1>")

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

        class RoutedPage(Page):
            async def content(self):
                return f"<h1>Visit {self.visits}; routes {len(self.routes)}</h1>"

        async def browser():
            yield SimpleNamespace(page=RoutedPage(), context=Context())

        with patch("src.endpoints.challenge_present", new=AsyncMock(return_value=False)):
            with TestClient(create_app(browser, clock=lambda: now[0])) as client:
                for index in range(1, 5):
                    response = client.post("/v1", json={"url": ORIGIN + "/ok", "blockMedia": True})
                    self.assertEqual(response.json()["solution"]["response"], f"<h1>Visit {index}; routes 1</h1>")
                    now[0] += 1000
                response = client.post("/v1", json={"url": ORIGIN + "/ok"})
                self.assertEqual(response.json()["solution"]["response"], "<h1>Visit 1; routes 1</h1>")

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
