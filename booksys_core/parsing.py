"""Bookys document and URL contracts. No network or command-line parsing."""
from __future__ import annotations
import ipaddress
import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit
from bs4 import BeautifulSoup
from booksys_core.errors import BrowserError, CollectionError
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
    plan = source_plan(html, url, hosters)
    downloads = {target: resolve_link(fetch(target), target, row) for target, row in plan.pop('targets').items()}
    return {**plan, 'downloads': downloads}


def source_plan(html: str, url: str, hosters: set[str] | None) -> dict[str, Any]:
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
        row = anchor.find_parent("tr")
        downloads[target] = {"hoster": hoster, "metadata": row.get_text(" ", strip=True) if row else ""}
    if not downloads:
        raise CollectionError(f"No selected hoster links found on {url}")
    heading = soup.select_one("h1")
    assert heading is not None
    result = {"title": heading.get_text(" ", strip=True), "targets": downloads,
              "complete_hosters": sorted(hosters) if hosters is not None else ["*"]}
    if soup.select_one('[itemprop="mainEntity"]'):
        result["page"] = extract_page(html, url)
    return result


def resolve_link(content: str, target: str, row: dict[str, Any]) -> dict[str, Any]:
    outgoing = document(content)
    links = sorted({hosted_url(urljoin(target, str(a['href']))) for a in outgoing.select('a.bys-link.bys-host[href]')})
    if not links:
        raise CollectionError('Selected hoster link is missing')
    return {**row, 'urls': links}
