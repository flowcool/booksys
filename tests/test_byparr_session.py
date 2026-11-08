"""Exercise /v1 in the exact Byparr image; no external network or live mounts."""

import importlib.util
import asyncio
import threading
import unittest
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
        return SimpleNamespace(request=SimpleNamespace(headers={"user-agent": "fixture"}), headers={})

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
    def test_successive_requests_reuse_the_browser_page(self):
        async def browser():
            yield SimpleNamespace(page=Page(), context=Context())

        with patch("src.endpoints.challenge_present", new=AsyncMock(return_value=False)):
            with TestClient(create_app(browser)) as client:
                first = client.post("/v1", json={"url": ORIGIN + "/first"})
                second = client.post("/v1", json={"url": ORIGIN + "/second"})
        self.assertEqual(first.status_code, 200)
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
                self.assertEqual(client.post("/v1", json={"url": ORIGIN + "/bad-html"}).status_code, 500)
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
