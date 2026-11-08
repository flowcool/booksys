"""Collect Bookys hoster links through a local Byparr instance."""

from __future__ import annotations

import argparse
import fcntl
import json
import ipaddress
import math
import re
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

import yaml
from bs4 import BeautifulSoup

DEFAULT_CATEGORY = "https://www7.bookys-ebooks.com/bandes-dessinees/new"


def hosted_url(url: str) -> str:
    """Accept public HTTP URLs without credentials. Never fetch them here."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if (parts.scheme not in {"http", "https"} or not host or parts.username or parts.password
            or host in {"localhost", "host.docker.internal"}
            or host.endswith((".localhost", ".local", ".internal")) or "." not in host):
        raise CollectionError(f"Invalid outgoing download URL: {url}")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return url
    if not address.is_global:
        raise CollectionError(f"Non-public download address: {url}")
    return url


class CollectionError(Exception):
    """A fetch or document failed validation. No automatic retry."""


class BrowserError(CollectionError):
    """Stop browser work for this cycle after a solver or page-shape failure."""


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
        if (address.scheme != "http" or address.hostname not in {"localhost", "127.0.0.1", "::1", "byparr"}
                or address.username or address.password):
            raise CollectionError("Byparr must use a local HTTP endpoint")
        self.endpoint, self.delay, self.timeout = endpoint, delay, timeout
        self.last_request = 0.0
        self.session: dict[str, Any] = {}

    def __call__(self, url: str) -> str:
        time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
        from activity import report
        report('byparr', 'working', 'Bookys request; Cloudflare solving unknown', item=url)
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
            self.session = {k: solution.get(k) for k in ["url", "cookies", "userAgent"]}
            report('byparr', 'sleeping', 'Request complete', item=url, progressed=True)
            return html
        except (OSError, ValueError, CollectionError) as exc:
            report('byparr', 'blocked', 'Browser failure', reason='Browser request or Cloudflare validation failed')
            raise BrowserError(str(exc)) from exc
        finally:
            self.last_request = time.monotonic()

    def cover(self, url: str, source: str) -> tuple[bytes, str]:
        """Fetch only same-site Bookys covers using the solved browser identity."""
        def validate(target: str) -> None:
            site_url(target, source)
            if (not re.fullmatch(r"www\d*\.bookys-ebooks\.com", urlsplit(target).hostname or "")
                    or not urlsplit(target).path.startswith("/storage/items/")):
                raise CollectionError("Cover must use Bookys item storage")
        validate(url)
        if urlsplit(str(self.session.get("url", ""))).netloc != urlsplit(source).netloc:
            self(source)

        class Redirect(HTTPRedirectHandler):
            def redirect_request(self, req: Any, fp: Any, code: Any, msg: Any, headers: Any, newurl: str) -> Any:
                validate(newurl)
                return super().redirect_request(req, fp, code, msg, headers, newurl)

        host = urlsplit(source).hostname or ""
        cookies = [c for c in (self.session.get("cookies") or [])
                   if host == c["domain"].lstrip(".") or host.endswith("." + c["domain"].lstrip("."))]
        if not self.session.get("userAgent"):
            raise CollectionError("Browser returned no cover identity")
        headers = {"User-Agent": self.session["userAgent"], "Referer": source,
                   "Cookie": "; ".join(c["name"] + "=" + c["value"] for c in cookies)}
        try:
            with build_opener(Redirect()).open(Request(url, headers=headers), timeout=30) as response:
                data = response.read(8 * 1024**2 + 1)
                mime = response.headers.get_content_type()
        except HTTPError as exc:
            if exc.code == 403:
                self.session = {}  # Solve again on a later retry, not an endless stale-cookie loop.
            raise
        signatures = {"image/jpeg": (".jpg", data.startswith(b"\xff\xd8\xff")),
                      "image/png": (".png", data.startswith(b"\x89PNG\r\n\x1a\n")),
                      "image/gif": (".gif", data[:6] in {b"GIF87a", b"GIF89a"}),
                      "image/webp": (".webp", data[:4] == b"RIFF" and data[8:12] == b"WEBP")}
        extension, valid = signatures.get(mime, ("", False))
        if not valid or len(data) > 8 * 1024**2:
            raise CollectionError("Invalid or oversized cover image")
        return data, extension


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


def extract_page(content: str, source: str) -> dict[str, Any]:
    soup = document(content)
    entity = soup.select_one('[itemprop="mainEntity"]')
    abstract = soup.select_one('[itemprop="abstract"], [itemprop="description"]')
    if entity is None or abstract is None:
        raise BrowserError("Source description structure missing")
    details = {}
    for item in entity.select("li"):
        label = item.select_one("b")
        if label and label.get_text(strip=True) != "Vues":
            details[label.get_text(strip=True)] = item.get_text(" ", strip=True).split(":", 1)[-1].strip()
    for name in ["author", "publisher", "datePublished", "inLanguage"]:
        node = entity.select_one(f'[itemprop="{name}"]')
        if node:
            details[name] = str(node.get("content") or node.get_text(" ", strip=True))
    image = soup.select_one("#item-cover")
    cover = urljoin(source, str(image.get("data-src") or image.get("src"))) if image else None
    return {"description": abstract.get_text("\n", strip=True), "details": details,
            "cover_url": cover, "captured_at": datetime.now(timezone.utc).isoformat()}



def resolve(html: str, url: str, hosters: set[str] | None, fetch: Callable[[str], str]) -> dict[str, Any]:
    soup = document(html)
    downloads = {}
    for anchor in soup.select("a[href]"):
        hoster = anchor.get_text(" ", strip=True).lower()
        target = urljoin(url, str(anchor["href"]))
        if (hosters is not None and hoster not in hosters) or not re.search(r"/dl/\d+$", urlsplit(target).path):
            continue
        site_url(target, url)
        if target in downloads:
            continue
        outgoing = document(fetch(target))
        links = sorted({hosted_url(urljoin(target, str(a["href"])))
                        for a in outgoing.select("a.bys-link.bys-host[href]")})
        if not links:
            raise CollectionError(f"Missing {hoster} URL on {target}")
        row = anchor.find_parent("tr")
        downloads[target] = {"hoster": hoster, "metadata": row.get_text(" ", strip=True) if row else "",
                             "urls": links}
    if not downloads:
        raise CollectionError(f"No selected hoster links found on {url}")
    heading = soup.select_one("h1")
    assert heading is not None
    result = {"title": heading.get_text(" ", strip=True), "downloads": downloads,
              "complete_hosters": sorted(hosters) if hosters is not None else ["*"]}
    if soup.select_one('[itemprop="mainEntity"]'):
        result["page"] = extract_page(html, url)
    return result


def atomic_write(path: Path, content: str) -> None:
    temporary = path.with_name(path.name + ".part")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def export_links(records: dict[str, Any], directory: Path, hosters: set[str] | None = None) -> None:
    available = {entry["hoster"] for item in records.values() for entry in item["downloads"].values()}
    all_links = set()
    for hoster in sorted(hosters if hosters is not None else available):
        links = sorted({link for item in records.values()
                        for entry in item["downloads"].values() if entry["hoster"] == hoster
                        for link in entry["urls"]})
        all_links.update(links)
        slug = re.sub(r"[^a-z0-9-]+", "-", hoster.lower()).strip("-") or "unnamed"
        atomic_write(directory / f"{slug}.txt", "".join(link + "\n" for link in links))
    atomic_write(directory / "all-links.txt", "".join(link + "\n" for link in sorted(all_links)))


def collect(categories: list[str], fetch: Callable[[str], str], directory: Path,
            hosters: set[str] | None = None, pages: int = 1, max_items: int = 0,
            refresh: bool = False, after_id: int | None = None) -> dict[str, int]:
    """Persist each complete item. Export accumulated links, including on failure."""
    if pages < 0 or max_items < 0 or hosters == set():
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
        if after_id is not None:
            counts["newest_id"] = after_id
        visited: set[str] = set()
        try:
            for category in categories:
                url: str | None = category
                page_count = 0
                seen_ids: set[int] = set()
                last_id: int | None = None
                while url and (pages == 0 or page_count < pages):
                    if url in visited:
                        raise CollectionError(f"Repeated catalogue page: {url}")
                    visited.add(url)
                    items, next_url = catalogue(fetch(url), url)
                    counts["pages"] += 1
                    page_count += 1
                    for item_url in items:
                        if after_id is not None:
                            match = re.search(r"/(\d+)-", urlsplit(item_url).path)
                            assert match is not None
                            identifier = int(match.group(1))
                            if identifier in seen_ids:
                                continue
                            if last_id is not None and identifier >= last_id:
                                raise CollectionError("Catalogue order changed; watermark was not advanced")
                            seen_ids.add(identifier)
                            last_id = identifier
                            counts["newest_id"] = max(counts["newest_id"], identifier)
                            if identifier <= after_id:
                                break
                        previous = records.get(item_url, {})
                        wanted = hosters if hosters is not None else {"*"}
                        complete = set(previous.get("complete_hosters", []))
                        if not refresh and ("*" in complete or wanted <= complete):
                            counts["skipped"] += 1
                            continue
                        if max_items and counts["collected"] >= max_items:
                            return counts
                        record = resolve(fetch(item_url), item_url, hosters, fetch)
                        record["downloads"] = {**previous.get("downloads", {}), **record["downloads"]}
                        record["complete_hosters"] = sorted(wanted | complete)
                        records[item_url] = record
                        atomic_write(path, yaml.safe_dump(state, allow_unicode=True, sort_keys=False))
                        counts["collected"] += 1
                    if after_id is not None and last_id is not None and last_id <= after_id:
                        break
                    if after_id is not None and next_url is None:
                        raise CollectionError("Pagination ended before reaching the previous watermark")
                    if max_items and counts["collected"] >= max_items:
                        return counts
                    url = next_url
            return counts
        finally:
            export_links(records, directory, hosters)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--category", action="append", help="Repeat for multiple categories")
    parser.add_argument("--byparr", default="http://127.0.0.1:8191/v1")
    parser.add_argument("--directory", type=Path, default=Path("data"))
    parser.add_argument("--hoster", action="append", help="Optional exact hoster label; default: all")
    parser.add_argument("--pages", type=int, default=1, help="Pages per category; 0 follows all pages")
    parser.add_argument("--max-items", type=int, default=0, help="New/refreshed items per run; 0 has no limit")
    parser.add_argument("--delay", type=float, default=2, help="Seconds between requests")
    parser.add_argument("--refresh", action="store_true", help="Revisit completed items")
    args = parser.parse_args()
    if not math.isfinite(args.delay) or args.delay < 0:
        parser.error("--delay must be nonnegative")
    try:
        counts = collect(args.category or [DEFAULT_CATEGORY], Byparr(args.byparr, args.delay),
                         args.directory, set(args.hoster) if args.hoster else None,
                         args.pages, args.max_items, args.refresh)
        print(json.dumps(counts))
        return 0
    except (CollectionError, OSError, ValueError, yaml.YAMLError) as exc:
        print(f"Collection stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
