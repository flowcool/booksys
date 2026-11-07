"""Collect Bookys hoster links through a local Byparr instance."""

from __future__ import annotations

import argparse
import fcntl
import json
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit
from urllib.request import Request, urlopen

import yaml
from bs4 import BeautifulSoup

HOSTERS = {"1fichier": {"1fichier.com", "www.1fichier.com"},
           "nitroflare": {"nitroflare.com", "www.nitroflare.com"}}
DEFAULT_CATEGORY = "https://www7.bookys-ebooks.com/bandes-dessinees/new"


class CollectionError(Exception):
    """A fetch or document failed validation. No automatic retry."""


def site_url(url: str, origin: str) -> str:
    candidate, base = urlsplit(url), urlsplit(origin)
    if (candidate.scheme != "https" or candidate.netloc != base.netloc
            or candidate.username or candidate.password):
        raise CollectionError(f"Refusing an off-site URL: {url}")
    return url


def document(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text().lower() if soup.title else ""
    if (not soup.select_one("h1") or "just a moment" in title
            or "attention required" in title or soup.select_one("#challenge-running")):
        raise CollectionError("Challenge or unexpected HTML; collection stopped")
    return soup


class Byparr:
    def __init__(self, endpoint: str, delay: float = 2, timeout: int = 60):
        address = urlsplit(endpoint)
        if (address.scheme != "http" or address.hostname not in {"localhost", "127.0.0.1", "::1"}
                or address.username or address.password):
            raise CollectionError("Byparr must use a local HTTP endpoint")
        self.endpoint, self.delay, self.timeout = endpoint, delay, timeout
        self.last_request = 0.0

    def __call__(self, url: str) -> str:
        time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
        payload = {"cmd": "request.get", "url": url,
                   "maxTimeout": self.timeout * 1000, "blockMedia": False}
        request = Request(self.endpoint, json.dumps(payload).encode(),
                          {"Content-Type": "application/json"})
        print(f"GET {url}", file=sys.stderr, flush=True)
        try:
            with urlopen(request, timeout=self.timeout + 20) as response:
                data = json.load(response)
            solution = data.get("solution", {})
            if data.get("status") != "ok" or solution.get("status") != 200:
                raise CollectionError("Byparr failed; collection stopped")
            site_url(solution.get("url", ""), url)
            html = solution.get("response")
            if not isinstance(html, str):
                raise CollectionError("Byparr returned no HTML")
            document(html)
            return html
        finally:
            self.last_request = time.monotonic()


def catalogue(html: str, url: str) -> tuple[dict[str, str], str | None]:
    soup = document(html)
    container = soup.select_one(".bys-items-container")
    if container is None:
        raise CollectionError("Catalogue structure changed: missing items container")
    items = {}
    for anchor in container.select("a.bys-item[href]"):
        target = site_url(urljoin(url, str(anchor["href"])), url)
        if re.search(r"/\d+-[^/]+$", urlsplit(target).path):
            items[target] = anchor.get_text(" ", strip=True)
    if not items:
        raise CollectionError("Catalogue contains no recognized items")
    current = int(parse_qs(urlsplit(url).query).get("page", ["1"])[0])
    next_url = None
    for anchor in soup.select("a[href]"):
        candidate = urljoin(url, str(anchor["href"]))
        parts = urlsplit(candidate)
        if (parts.path == urlsplit(url).path
                and parse_qs(parts.query).get("page") == [str(current + 1)]):
            next_url = site_url(candidate, url)
            break
    return items, next_url


def resolve(html: str, url: str, hosters: set[str], fetch: Callable[[str], str]) -> dict[str, Any]:
    soup = document(html)
    downloads = {}
    for anchor in soup.select("a[href]"):
        hoster = anchor.get_text(" ", strip=True).lower()
        target = urljoin(url, str(anchor["href"]))
        if hoster not in hosters or not re.search(r"/dl/\d+$", urlsplit(target).path):
            continue
        site_url(target, url)
        if target in downloads:
            continue
        outgoing = document(fetch(target))
        links = sorted({urljoin(target, str(a["href"])) for a in outgoing.select("a[href]")
                        if urlsplit(urljoin(target, str(a["href"]))).hostname in HOSTERS[hoster]
                        and urlsplit(urljoin(target, str(a["href"]))).scheme == "https"})
        if not links:
            raise CollectionError(f"Missing {hoster} URL on {target}")
        row = anchor.find_parent("tr")
        downloads[target] = {"hoster": hoster, "metadata": row.get_text(" ", strip=True) if row else "",
                             "urls": links}
    if not downloads:
        raise CollectionError(f"No selected hoster links found on {url}")
    heading = soup.select_one("h1")
    assert heading is not None
    return {"title": heading.get_text(" ", strip=True), "downloads": downloads,
            "complete_hosters": sorted(hosters)}


def atomic_write(path: Path, content: str) -> None:
    temporary = path.with_name(path.name + ".part")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def collect(categories: list[str], fetch: Callable[[str], str], directory: Path,
            hosters: set[str], pages: int = 1, max_items: int = 0,
            refresh: bool = False) -> dict[str, int]:
    """Persist each complete item. Export accumulated links, including on failure."""
    if pages < 0 or max_items < 0 or not hosters or not hosters <= HOSTERS.keys():
        raise CollectionError("Invalid collection limits or hosters")
    for category in categories:
        parts = urlsplit(category)
        if (not re.fullmatch(r"www\d*\.bookys-ebooks\.com", parts.hostname or "")
                or parts.port is not None or parts.username or parts.password):
            raise CollectionError("Category must use a Bookys hostname without credentials or port")
        site_url(category, category)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "collector.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CollectionError("Another collector is using this state directory") from exc
        path = directory / "state.yaml"
        state = yaml.safe_load(path.read_text()) if path.exists() else {"version": 1, "items": {}}
        if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("items"), dict):
            raise CollectionError("Unsupported or invalid state file")
        records = state["items"]
        counts = {"pages": 0, "collected": 0, "skipped": 0}
        visited: set[str] = set()
        try:
            for category in categories:
                url: str | None = category
                page_count = 0
                while url and (pages == 0 or page_count < pages):
                    if url in visited:
                        raise CollectionError(f"Repeated catalogue page: {url}")
                    visited.add(url)
                    items, next_url = catalogue(fetch(url), url)
                    counts["pages"] += 1
                    page_count += 1
                    for item_url in items:
                        previous = records.get(item_url, {})
                        if not refresh and hosters <= set(previous.get("complete_hosters", [])):
                            counts["skipped"] += 1
                            continue
                        if max_items and counts["collected"] >= max_items:
                            return counts
                        record = resolve(fetch(item_url), item_url, hosters, fetch)
                        record["downloads"] = {**previous.get("downloads", {}), **record["downloads"]}
                        record["complete_hosters"] = sorted(hosters | set(previous.get("complete_hosters", [])))
                        records[item_url] = record
                        atomic_write(path, yaml.safe_dump(state, allow_unicode=True, sort_keys=False))
                        counts["collected"] += 1
                    if max_items and counts["collected"] >= max_items:
                        return counts
                    url = next_url
            return counts
        finally:
            for hoster in sorted(hosters):
                links = sorted({link for item in records.values()
                                for entry in item["downloads"].values() if entry["hoster"] == hoster
                                for link in entry["urls"]})
                atomic_write(directory / f"{hoster}.txt", "".join(link + "\n" for link in links))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--category", action="append", help="Repeat for multiple categories")
    parser.add_argument("--byparr", default="http://127.0.0.1:8191/v1")
    parser.add_argument("--directory", type=Path, default=Path("data"))
    parser.add_argument("--hoster", action="append", choices=sorted(HOSTERS))
    parser.add_argument("--pages", type=int, default=1, help="Pages per category; 0 follows all pages")
    parser.add_argument("--max-items", type=int, default=0, help="New/refreshed items per run; 0 has no limit")
    parser.add_argument("--delay", type=float, default=2, help="Seconds between requests")
    parser.add_argument("--refresh", action="store_true", help="Revisit completed items")
    args = parser.parse_args()
    if args.delay < 0:
        parser.error("--delay must be nonnegative")
    try:
        counts = collect(args.category or [DEFAULT_CATEGORY], Byparr(args.byparr, args.delay),
                         args.directory, set(args.hoster or HOSTERS), args.pages, args.max_items, args.refresh)
        print(json.dumps(counts))
        return 0
    except (CollectionError, OSError, ValueError, yaml.YAMLError) as exc:
        print(f"Collection stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
