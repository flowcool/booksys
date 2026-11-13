"""Booksys entrypoint for the pinned Byparr image."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from html.parser import HTMLParser
import os
from pathlib import Path
import time
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from src.endpoints import _navigate_and_solve
from src.content import build_response_content
from src.models import LinkRequest, LinkResponse, Solution
from src.utils import TimeoutTimer, get_browser
import uuid

from booksys_core.browser_outcomes import retry_after
from booksys_core.diagnostics import opaque, emit, install_logging

try:
    BYPARR_BUDGET = int(os.environ.get('BOOKSYS_BYPARR_BUDGET', '120'))
    if BYPARR_BUDGET <= 0:
        raise ValueError
except ValueError:
    raise ValueError('BOOKSYS_BYPARR_BUDGET must be a positive integer') from None

ORIGIN = "https://www7.bookys-ebooks.com"
# Native teardown closes Playwright and reaps the process tree (up to 5s).
# Leave room for both steps, within the runtime's 30s shutdown grace period.
CLEANUP_TIMEOUT = 20


class PageShape(HTMLParser):
    """Check the same basic page shape required by the Booksys client."""

    def __init__(self) -> None:
        super().__init__()
        self.heading = False
        self.in_title = False
        self.title = ""
        self.challenge = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.heading |= tag == "h1"
        self.in_title |= tag == "title"
        self.challenge |= dict(attrs).get("id") == "challenge-running"

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title += data.lower()


def same_origin(url: str, origin: str = ORIGIN) -> bool:
    parts = urlsplit(url)
    return (f"{parts.scheme}://{parts.netloc}" == origin
            and not parts.username and not parts.password)


def memory_pressure() -> bool:
    """Recycle at 80% of the cgroup limit; other bounds cover non-cgroup runs."""
    try:
        limit = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        return limit != "max" and int(Path("/sys/fs/cgroup/memory.current").read_text()) >= int(limit) * 0.8
    except (OSError, ValueError):
        return False


def create_app(browser_factory: Callable[..., Any] = get_browser, *, max_requests: int = 256,
               clock: Callable[[], float] = time.monotonic,
               memory_pressure: Callable[[], bool] = memory_pressure) -> FastAPI:
    install_logging('byparr')
    generator: Any = None
    browser: Any = None
    count = 0
    opened = last_used = 0.0
    lock = asyncio.Lock()
    broken = False
    generation = 0
    active_request_id: str | None = None

    def detail(code: str, origin: str, request_id: str, **fields: Any) -> dict[str, Any]:
        return {'schema': 1, 'code': code, 'origin': origin, 'request_id': request_id,
                'retry_after_state': 'absent', **fields}

    async def close() -> None:
        nonlocal generator, browser, broken
        browser = None
        if generator is not None:
            try:
                await asyncio.wait_for(generator.aclose(), timeout=CLEANUP_TIMEOUT)
                generator = None
            except BaseException:
                broken = True
                emit(service='byparr', stage='byparr', code='blocked', reason='cleanup_failed')
                raise HTTPException(503, detail('cleanup_failed', 'local', active_request_id or 'shutdown')) from None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await close()

    app = FastAPI(lifespan=lifespan)

    @app.middleware('http')
    async def correlation(http: Request, call_next: Any) -> Any:
        supplied = http.headers.get('X-Booksys-Request-ID')
        http.state.request_id = supplied if opaque(supplied) else str(uuid.uuid4())
        response = await call_next(http)
        response.headers['X-Booksys-Request-ID'] = http.state.request_id
        return response

    async def perform(request: LinkRequest, request_id: str) -> LinkResponse:
        nonlocal generator, browser, count, opened, last_used, generation
        if (not same_origin(request.url) or request.cmd != "request.get"
                or request.return_only_cookies or not 1 <= request.max_timeout <= BYPARR_BUDGET):
            raise HTTPException(422, f"Expected a Bookys HTML request with a 1-{BYPARR_BUDGET} second budget")
        page: Any = None
        request_ids: set[int] = set()
        responses: list[Any] = []
        remote_fields: dict[str, Any] = {}
        overflow = False

        def navigation(outgoing: Any) -> None:
            nonlocal overflow
            if outgoing.is_navigation_request() and outgoing.frame == page.main_frame:
                if len(request_ids) >= 64:
                    overflow = True
                else:
                    request_ids.add(id(outgoing))

        def response_seen(response: Any) -> None:
            nonlocal overflow
            if id(response.request) in request_ids:
                if len(responses) >= 64:
                    overflow = True
                else:
                    responses.append(response)

        def detach() -> None:
            nonlocal page
            if page is not None:
                detached = page
                page = None
                if not detached.is_closed():
                    detached.remove_listener('request', navigation)
                    detached.remove_listener('response', response_seen)

        try:
            if browser is not None and (count >= max_requests or clock() - opened >= 3600
                                        or clock() - last_used >= 1800):
                await close()
            if browser is None:
                generator = browser_factory()
                browser = await anext(generator)
                count = 0
                opened = clock()
                generation += 1
                emit(service='byparr', stage='byparr', code='started', request_id=request_id)
            # Upstream installs media routes per request. Replace them rather than
            # retaining handlers, and keep the top-level origin guard first.
            await browser.page.unroute_all(behavior="wait")

            async def guard(route: Any) -> None:
                outgoing = route.request
                if (outgoing.is_navigation_request() and outgoing.frame == browser.page.main_frame
                        and not same_origin(outgoing.url)):
                    await route.abort()
                elif request.block_media and outgoing.resource_type in {"image", "media", "font"}:
                    await route.abort()
                else:
                    await route.continue_()

            await browser.page.route("**/*", guard)
            page = browser.page
            page.on('request', navigation)
            page.on('response', response_seen)
            started = int(time.time() * 1000)
            timer = TimeoutTimer(duration=request.max_timeout)
            initial = await _navigate_and_solve(browser, request.model_copy(update={'block_media': False}), timer)
            final = next((response for response in reversed(responses) if response.url == page.url), None)
            if overflow or final is None or not same_origin(page.url):
                raise HTTPException(502, detail('unknown_browser_failure', 'unknown', request_id))
            status = final.status
            if status == 429:
                state, delay = retry_after(final.headers.get('retry-after'), received_at=time.time())
                remote_fields = {'retry_after_state': state}
                if delay is not None:
                    remote_fields['retry_after_seconds'] = delay
                raise HTTPException(429, detail('remote_rate_limited', 'remote', request_id, **remote_fields))
            if status != 200:
                raise HTTPException(502, detail('remote_http_failure', 'remote', request_id))
            content_type, content = await build_response_content(page, request, initial)
            cookies = await browser.context.cookies([request.url, page.url])
            result = LinkResponse(message='Success', start_timestamp=started,
                solution=Solution(url=page.url, status=status, cookies=cookies, headers={},
                                  user_agent=final.request.headers.get('user-agent', ''),
                                  response=content, content_type=content_type))
            shape = PageShape()
            shape.feed(result.solution.response)
            if (not same_origin(result.solution.url) or not shape.heading or shape.challenge
                    or "just a moment" in shape.title or "attention required" in shape.title):
                raise HTTPException(502, detail('invalid_content', 'remote', request_id))
            count += 1
            last_used = clock()
            detach()
            await close()
            return result
        except BaseException:
            # Native Playwright cannot update subscriptions after its transport
            # closes. Detach while it is alive, preserving the original error.
            try:
                detach()
            finally:
                try:
                    await close()
                except HTTPException:
                    raise HTTPException(503, detail('cleanup_failed', 'local', request_id, **remote_fields)) from None
            raise
        finally:
            detach()

    @app.post("/v1")
    async def fetch(request: LinkRequest, http: Request) -> LinkResponse:
        nonlocal active_request_id
        if 'max_timeout' not in request.model_fields_set:
            request = request.model_copy(update={'max_timeout': BYPARR_BUDGET})
        request_id = http.state.request_id
        if broken:
            raise HTTPException(503, detail('cleanup_failed', 'local', request_id))
        if lock.locked():
            raise HTTPException(429, detail('browser_busy', 'local', request_id,
                **({'active_request_id': active_request_id} if active_request_id else {})))
        async with lock:
            active_request_id = request_id
            started = clock()
            try:
                # Include browser startup in the caller's budget. Upstream also
                # passes this limit to navigation and Cloudflare solving.
                return await asyncio.wait_for(perform(request, request_id), timeout=min(BYPARR_BUDGET, max(1, request.max_timeout)))
            except TimeoutError as exc:
                raise HTTPException(408, detail('browser_timeout', 'local', request_id)) from exc
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(502, detail('unknown_browser_failure', 'unknown', request_id)) from None
            finally:
                active_request_id = None
                emit(service='byparr', stage='byparr', code='completed', request_id=request_id,
                     duration_ms=max(0, int((clock() - started) * 1000)))

    @app.get("/health")
    async def health() -> dict[str, Any]:
        if broken:
            raise HTTPException(503, "Browser cleanup failed; restart service")
        return {"status": "ok", "generation": generation, "requests": count}

    return app


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(create_app(), host="0.0.0.0", port=8191, access_log=False, log_config=None)
