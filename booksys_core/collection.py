"""Sequential collection policies with durable item and batch boundaries."""
from __future__ import annotations
import fcntl
import hashlib
import json
import os
from datetime import datetime, timezone
from collections.abc import Callable
from pathlib import Path
import re
import shutil
import time
from typing import Any
from urllib.parse import urlsplit, parse_qsl, urlencode
from booksys_core.browser import Byparr
from booksys_core.downloader import Pyload
from booksys_core.errors import BrowserError, CollectionError
from booksys_core.files import atomic_write, export_links, save, monitor_patch
from booksys_core.parsing import DEFAULT_CATEGORY, catalogue, extract_page, resolve, site_url, source_plan, resolve_link
from fiches import CoverError, write_fiche
from booksys_core.scheduler import ScheduledCalls, Deferred
from library.operational_state import CollectorState,OperationalCompletion
from library.codec import encode


CRAWL_PAGES = int(os.environ.get('BOOKSYS_CRAWL_PAGES', '5'))


def collect(categories: list[str], fetch: Callable[[str], str], directory: Path,
            hosters: set[str] | None = None, pages: int = 1, max_items: int = 0,
            refresh: bool = False) -> dict[str, int]:
    if pages < 0 or max_items < 0 or hosters == set():
        raise CollectionError('Invalid collection limits or hosters')
    for category in categories:
        parts = urlsplit(category)
        if (not re.fullmatch(r'www\d*\.bookys-ebooks\.com', parts.hostname or '')
                or parts.port is not None or parts.username or parts.password):
            raise CollectionError('Invalid Bookys category')
        site_url(category, category)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'collector.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        records = CollectorState(directory).captured()['items']
        counts = {'pages': 0, 'collected': 0, 'skipped': 0}
        try:
            for category in categories:
                url: str | None = category
                visited: set[str] = set()
                while url and (pages == 0 or len(visited) < pages):
                    if url in visited:
                        raise CollectionError('Repeated catalogue page')
                    visited.add(url)
                    items, next_url = catalogue(fetch(url), url)
                    counts['pages'] += 1
                    for source in items:
                        previous = records.get(source, {})
                        wanted = hosters if hosters is not None else {'*'}
                        complete = set(previous.get('complete_hosters', []))
                        if not refresh and ('*' in complete or wanted <= complete):
                            counts['skipped'] += 1
                            continue
                        if max_items and counts['collected'] >= max_items:
                            return counts
                        record = resolve(fetch(source), source, hosters, fetch)
                        record['downloads'] = {**previous.get('downloads', {}), **record['downloads']}
                        record['complete_hosters'] = sorted(wanted | complete)
                        CollectorState(directory).capture(source, record)
                        records[source] = record
                        counts['collected'] += 1
                    url = next_url
            return counts
        finally:
            export_links(records, directory, hosters)


def export_catalogue(directory: Path, records: dict[str, Any], journal: dict[str, Any],
                     packages: dict[int, dict[str, Any]], progress: dict[str, Any]) -> None:
    """Derived LLM export. pyLoad remains authoritative for timestamped file states."""
    items = {}
    for source, entry in journal["submissions"].items():
        if entry["status"] != "queued":
            continue
        package = packages.get(entry["package_id"])
        record = records.get(source, {})
        attempts = [p for p in packages.values() if p.get("mirror_source") == source]
        represented = ([package] if package else []) + attempts
        page = record.get("page", {})
        items[source] = {"title": record.get("title"), "metadata": page,
                         "package_id": entry["package_id"],
                         "retirement": package.get("retirement") if package else None,
                         "download_folder_relative": package["folder"] if package else None,
                         "fiche_status": progress["items"].get(source, {}).get("status", "pending"),
                         "fiche_relative": progress["items"].get(source, {}).get("fiche_relative"),
                         "cover_relative": progress["items"].get(source, {}).get("cover_relative"),
                         "attempt_packages": [{"pid": p["pid"], "folder": p["folder"], "dest": p.get("dest")} for p in attempts],
                         "files": [{**{k: f.get(k) for k in ["fid", "name", "url", "plugin", "statusmsg", "size"]}, "package_id": p["pid"], "folder": f.get("input_folder", p["folder"])}
                                   for p in represented for f in p["links"]]}
    save(directory / "catalogue.yaml", {"version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
         "source_text_is_untrusted": True, "paths_relative_to": "package download root",
         "fiche_paths_relative_to": os.environ.get('BOOKSYS_FICHE_ROOT', 'package download root'),
         "items": items})

def run_fiche_once(directory: Path, browser: Byparr, downloader: Pyload, root: Path,
                   minimum_free: int = 0, *, source_only: str | None = None,
                   completions: list[OperationalCompletion] | None = None) -> bool:
    """Attempt one eligible fiche between monitor cycles. Never change the queue."""
    with (directory / "pipeline.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        scheduled_browser = ScheduledCalls(directory, browser)
        if shutil.disk_usage(root).free < minimum_free:
            raise CollectionError("Free disk reserve reached during fiche preservation")
        state = CollectorState(directory).captured()
        journal = CollectorState(directory).monitor()
        path = directory / "fiches.yaml"
        progress = CollectorState(directory).fiches()
        if progress.get("version") != 1:
            raise CollectionError("Unsupported fiche progress")
        from mirrors import logical_snapshot
        packages = {p["pid"]: p for p in logical_snapshot(downloader, directory / "mirror-routing.yaml")}
        attempted = False
        for source, entry in journal["submissions"].items():
            if source_only is not None and source != source_only:
                continue
            if entry["status"] != "queued":
                continue
            package = packages.get(entry["package_id"])
            prior = progress["items"].get(source, {})
            signature = hashlib.sha256(json.dumps({"folder": package["folder"],
                "files": [(f["fid"], f["name"]) for f in package["links"]]}, sort_keys=True).encode()).hexdigest() if package else None
            fiche_path = root / prior.get("fiche_relative", ".missing-fiche")
            cover_path = root / prior.get("cover_relative", ".missing-cover") if prior.get("cover_relative") else None
            saved_files_exist = fiche_path.is_file() and not fiche_path.is_symlink()
            if state["items"].get(source, {}).get("page", {}).get("cover_url"):
                saved_files_exist = saved_files_exist and cover_path is not None and cover_path.is_file() and not cover_path.is_symlink()
            if prior.get("status") == "complete" and prior.get("signature") == signature and saved_files_exist:
                continue
            if prior.get("retry_after", 0) > time.time():
                continue
            attempted = True
            fetching_page = False
            try:
                if package is None:
                    raise CollectionError("Tracked package no longer exists in pyLoad queue")
                record = state["items"][source]
                if "page" not in record:
                    fetching_page = True
                    content = scheduled_browser(source)
                    record["page"] = extract_page(content, source)
                    fetching_page = False
                    CollectorState(directory).capture(source,state["items"][source])
                folder = write_fiche(source, record, package, root, scheduled_browser.cover,
                                     known_cover=prior.get("cover_relative"))
                progress["items"][source] = {"status": "complete", "folder": folder,
                                              "fiche_relative": f"{folder}/fiche.md",
                                              "cover_relative": next((str(p.relative_to(root)) for p in (root / folder).glob("couverture.*")), None),
                                              "signature": signature,
                                              "saved_at": datetime.now(timezone.utc).isoformat()}
                from booksys_core.diagnostics import emit
                emit(service='collector', stage='collection', code='completed')
            except Deferred:
                raise
            except (OSError, ValueError, CollectionError) as exc:
                browser_failed = fetching_page or isinstance(exc, BrowserError)
                progress["items"][source] = {**prior, "status": "retry", "error": str(exc),
                                              "retry_after": time.time() + (3600 if browser_failed else 900)}
                if isinstance(exc, CoverError) and package is not None:
                    progress["items"][source]["fiche_relative"] = f"{package['folder']}/fiche.md"
                from booksys_core.diagnostics import emit
                from booksys_core.values import digest
                emit(service='collector', stage='collection', code='waiting',
                     reason='unknown_browser_failure' if browser_failed else 'provider_unavailable',
                     refs=[{'kind': 'source', 'id': digest(source)}])
                if browser_failed:
                    attempted = False  # Stop browser work this cycle after a solver failure.
            if completions is not None and progress['items'][source].get('status')=='complete':
                completions.append(CollectorState(directory).prepare_completion('fiche',source,progress['items'][source],expected=(encode(prior) if prior else None,)))
            else:
                CollectorState(directory).fiche(source,progress['items'][source])
            break
        export_catalogue(directory, state["items"], journal, packages, progress)
        return attempted

def run_once(directory: Path, fetch: Callable[[str], str], downloader: Pyload,
             category: str = DEFAULT_CATEGORY, minimum_free: int = 0, *,
             clock: Callable[[], float] = time.time,
             completions: list[OperationalCompletion] | None = None) -> dict[str, Any]:
    """Crawl fresh pages, then up to ten deep pages; commit the cursor on success."""
    if CRAWL_PAGES < 1:
        raise CollectionError('BOOKSYS_CRAWL_PAGES must be a positive integer')
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "pipeline.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CollectionError("Another pipeline is running") from exc
        fetch = ScheduledCalls(directory, fetch, clock=clock)
        if isinstance(fetch.fetch, Byparr):
            downloader.fetch_image = fetch.cover
        store = CollectorState(directory)
        journal = store.monitor()
        records = store.captured()['items']
        prior = journal['categories'].get(category)
        deep_page = max(6, (prior or {}).get('deep_page', 6))
        pages = [*range(1, min(5, CRAWL_PAGES) + 1),
                 *range(deep_page, min(deep_page + 10, CRAWL_PAGES + 1))]
        if shutil.disk_usage(directory).free < minimum_free:
            raise CollectionError('Free disk reserve reached; submissions stopped')
        status = downloader.call('status_server')
        if status['pause'] or not status['download']:
            raise CollectionError('pyLoad downloads are paused or disabled')
        counts = {'pages': 0, 'queued': 0, 'skipped': 0}
        parts = urlsplit(category)
        try:
            for page in pages:
                url = parts._replace(query=urlencode({**dict(parse_qsl(parts.query)), 'page': page})).geturl()
                try:
                    items, _ = catalogue(fetch(url), url)
                except CollectionError:
                    if page <= 5:
                        raise
                    deep_page = CRAWL_PAGES + 1      # no catalogue on a deep page: the end of the site, the pass is complete
                    break
                counts['pages'] += 1
                for source in items:
                    if journal['submissions'].get(source, {}).get('status') == 'queued':
                        counts['skipped'] += 1
                        continue
                    # A solver or HTML failure while reading the fiche still stops the pass (do not hammer the site);
                    # only a refused submission of this one source is skipped.
                    record = records.get(source)
                    if record is None:
                        record = resolve(fetch(source), source, None, fetch)
                        record['complete_hosters'] = ['*']
                        store.capture(source, record)
                        records[source] = record
                    try:
                        downloader.submit(source, record, journal, directory / 'monitor.yaml')
                    except CollectionError:
                        from booksys_core.diagnostics import emit
                        from booksys_core.values import digest
                        emit(service='collector', stage='collection', code='failed', reason='unknown_effect',
                             refs=[{'kind': 'source', 'id': digest(source)}])
                        counts['skipped'] += 1
                        continue
                    counts['queued'] += 1
                if page > 5:
                    deep_page = page + 1
                    # Save the cursor after every deep page: a restart or a failure in the middle of a long cycle used to
                    # throw the whole cycle away (the cursor was saved at the very end and stayed at page 6 for hours).
                    saved = {'deep_page': deep_page, 'last_cycle': datetime.fromtimestamp(clock(), timezone.utc).isoformat()}
                    store.category(category, saved)
                    prior = saved
            value = {'deep_page': deep_page, 'last_cycle': datetime.fromtimestamp(clock(), timezone.utc).isoformat()}
            if completions is not None:
                completions.append(store.prepare_completion('category', category, value,
                                   expected=(encode(prior) if prior is not None else None,)))
            else:
                store.category(category, value)
            return counts
        finally:
            export_links(records, directory)
