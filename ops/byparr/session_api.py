"""Booksys entrypoint for the pinned Byparr image."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from html.parser import HTMLParser
from pathlib import Path
import time
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException
from src.endpoints import read_item
from src.models import LinkRequest, LinkResponse
from src.utils import get_browser, logger


ORIGIN = "https://www7.bookys-ebooks.com"


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
    generator: Any = None
    browser: Any = None
    count = 0
    opened = last_used = 0.0
    lock = asyncio.Lock()
    broken = False
    generation = 0

    async def close() -> None:
        nonlocal generator, browser, broken
        browser = None
        if generator is not None:
            try:
                await asyncio.wait_for(generator.aclose(), timeout=5)
                generator = None
            except BaseException:
                broken = True
                logger.error("Browser cleanup failed; restart the Byparr service")
                raise

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await close()

    app = FastAPI(lifespan=lifespan)

    async def perform(request: LinkRequest) -> LinkResponse:
        nonlocal generator, browser, count, opened, last_used, generation
        if (not same_origin(request.url) or request.cmd != "request.get"
                or request.return_only_cookies or not 1 <= request.max_timeout <= 60):
            raise HTTPException(422, "Expected a Bookys HTML request with a 1-60 second budget")
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
                logger.info("Bookys session opened: generation=%d", generation)
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
            result = await read_item(request.model_copy(update={"block_media": False}), browser)
            shape = PageShape()
            shape.feed(result.solution.response)
            if (not same_origin(result.solution.url) or not shape.heading or shape.challenge
                    or "just a moment" in shape.title or "attention required" in shape.title):
                raise HTTPException(502, "Unexpected Bookys HTML; session discarded")
            count += 1
            last_used = clock()
            if memory_pressure():
                logger.info("Bookys session recycled: memory pressure")
                await close()
            return result
        except BaseException:
            await close()
            raise

    @app.post("/v1")
    async def fetch(request: LinkRequest) -> LinkResponse:
        if broken:
            raise HTTPException(503, "Browser cleanup failed; restart service")
        if lock.locked():
            raise HTTPException(429, "Bookys browser busy; use sequential requests")
        async with lock:
            started = clock()
            try:
                # Include browser startup in the caller's budget. Upstream also
                # passes this limit to navigation and Cloudflare solving.
                return await asyncio.wait_for(perform(request), timeout=min(60, max(1, request.max_timeout)))
            except TimeoutError as exc:
                raise HTTPException(408, "Bookys request timed out; session discarded") from exc
            finally:
                logger.info("Bookys request: generation=%d count=%d seconds=%.3f",
                            generation, count, clock() - started)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        if broken:
            raise HTTPException(503, "Browser cleanup failed; restart service")
        return {"status": "ok", "generation": generation, "requests": count}

    return app


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(create_app(), host="0.0.0.0", port=8191)
