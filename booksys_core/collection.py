"""Sequential collection policies with durable item and batch boundaries."""
from __future__ import annotations
import fcntl
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from collections.abc import Callable
from pathlib import Path
import re
import shutil
import time
from typing import Any
from urllib.parse import urlsplit
import yaml
from booksys_core.browser import Byparr
from booksys_core.downloader import Pyload
from booksys_core.errors import BrowserError, CollectionError
from booksys_core.files import atomic_write, export_links, save, monitor_patch
from booksys_core.parsing import DEFAULT_CATEGORY, catalogue, extract_page, resolve, site_url, source_plan, resolve_link
from fiches import CoverError, write_fiche
from booksys_core.scheduler import ScheduledCalls, Deferred
from library.operational_state import CollectorState,OperationalCompletion
from library.codec import encode


def resolve_source(source: str, fetch: Callable[[str], str], directory: Path,
                   hosters: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(fetch, ScheduledCalls):
        return resolve(fetch(source), source, hosters, fetch)
    monitor = CollectorState(directory).monitor()
    checkpoint = monitor.get('source_checkpoint')
    selected = sorted(hosters) if hosters is not None else None
    if checkpoint is not None and (checkpoint.get('source') != source or checkpoint.get('hosters') != selected):
        raise CollectionError('Saved source checkpoint differs from requested work')
    if checkpoint is None:
        checkpoint = {'source': source, 'hosters': selected, 'plan': source_plan(fetch(source), source, hosters), 'downloads': {}}
        CollectorState(directory).patch_monitor( source_checkpoint=checkpoint)
    for target, row in checkpoint['plan']['targets'].items():
        if target not in checkpoint['downloads']:
            checkpoint['downloads'][target] = resolve_link(fetch(target), target, row)
            CollectorState(directory).patch_monitor( source_checkpoint=checkpoint)
    return {**{k: v for k, v in checkpoint['plan'].items() if k != 'targets'}, 'downloads': checkpoint['downloads']}

def collect(categories: list[str], fetch: Callable[[str], str], directory: Path,
            hosters: set[str] | None = None, pages: int = 1, max_items: int = 0,
            refresh: bool = False, after_id: int | None = None, max_pages_per_call: int | None = None) -> dict[str, int]:
    if pages < 0 or max_items < 0 or hosters == set():
        raise CollectionError('Invalid collection limits or hosters')
    for category in categories:
        parts = urlsplit(category)
        if (not re.fullmatch(r'www\d*\.bookys-ebooks\.com', parts.hostname or '')
                or parts.port is not None or parts.username or parts.password):
            raise CollectionError('Invalid Bookys category')
        site_url(category, category)
    directory.mkdir(parents=True, exist_ok=True)
    durable = isinstance(fetch, ScheduledCalls)
    with (directory / 'collector.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = directory / 'state.yaml'
        state = CollectorState(directory).captured()
        if not isinstance(state, dict) or state.get('version') != 1 or not isinstance(state.get('items'), dict):
            raise CollectionError('Unsupported or invalid state file')
        records = state['items']
        monitor_path = directory / 'monitor.yaml'
        monitor = CollectorState(directory).monitor() if durable else {}
        scope = {'categories': categories, 'hosters': sorted(hosters) if hosters is not None else None,
                 'pages': pages, 'refresh': refresh, 'after_id': after_id}
        cp = monitor.get('collection_checkpoint')
        if cp is not None and cp.get('scope') != scope:
            raise CollectionError('Collection checkpoint scope differs')
        if cp is None:
            cp = {'schema': 1, 'scope': scope, 'category_index': 0, 'url': None, 'visited': [],
                  'page_count': 0, 'seen_ids': [], 'last_id': None, 'items': None, 'item_index': 0,
                  'next_url': None, 'complete': False, 'counts': {'pages': 0, 'collected': 0, 'skipped': 0}}
            if after_id is not None:
                cp['counts']['newest_id'] = after_id
        counts = cp['counts']
        collected_start = counts['collected']
        pages_start = counts['pages']
        def checkpoint() -> None:
            if durable:
                CollectorState(directory).patch_monitor( collection_checkpoint=cp)
        checkpoint()
        try:
            while cp['category_index'] < len(categories) and not cp['complete']:
                if cp['url'] is None:
                    cp['url'] = categories[cp['category_index']]
                finished = False
                while cp['url'] and (pages == 0 or cp['page_count'] < pages):
                    url = cp['url']
                    if cp['items'] is None:
                        if max_pages_per_call is not None and counts['pages'] - pages_start >= max_pages_per_call:
                            return dict(counts)
                        if url in cp['visited']:
                            raise CollectionError('Repeated catalogue page')
                        items, next_url = catalogue(fetch(url), url)
                        cp.update(items=list(items), next_url=next_url, item_index=0,
                                  page_count=cp['page_count'] + 1)
                        cp['visited'].append(url)
                        counts['pages'] += 1
                        checkpoint()
                    while cp['item_index'] < len(cp['items']):
                        item_url = cp['items'][cp['item_index']]
                        identifier = None
                        if after_id is not None:
                            match = re.search(r'/(\d+)-', urlsplit(item_url).path)
                            if match is None:
                                raise CollectionError('Invalid item identifier')
                            identifier = int(match.group(1))
                            if identifier in cp['seen_ids']:
                                cp['item_index'] += 1
                                checkpoint()
                                continue
                            if cp['last_id'] is not None and identifier >= cp['last_id']:
                                raise CollectionError('Catalogue order changed; watermark was not advanced')
                            if identifier <= after_id:
                                finished = True
                                break
                        captured = False
                        previous = records.get(item_url, {})
                        wanted = hosters if hosters is not None else {'*'}
                        complete = set(previous.get('complete_hosters', []))
                        if not refresh and ('*' in complete or wanted <= complete):
                            counts['skipped'] += 1
                        else:
                            if max_items and counts['collected'] - collected_start >= max_items:
                                return dict(counts)
                            record = resolve_source(item_url, fetch, directory, hosters)
                            record['downloads'] = {**previous.get('downloads', {}), **record['downloads']}
                            record['complete_hosters'] = sorted(wanted | complete)
                            records[item_url] = record
                            captured = True
                            counts['collected'] += 1
                        if identifier is not None:
                            cp['seen_ids'].append(identifier)
                            cp['last_id'] = identifier
                            counts['newest_id'] = max(counts['newest_id'], identifier)
                        cp['item_index'] += 1
                        if captured:
                            CollectorState(directory).capture(item_url,records[item_url],
                                **({'collection_checkpoint':cp,'source_checkpoint':None} if durable else {}))
                        else:
                            checkpoint()
                    if finished:
                        break
                    if after_id is not None and cp['next_url'] is None:
                        raise CollectionError('Pagination ended before reaching the previous watermark')
                    cp.update(url=cp['next_url'], items=None, item_index=0)
                    checkpoint()
                cp.update(category_index=cp['category_index'] + 1, url=None, page_count=0,
                          seen_ids=[], last_id=None, items=None, item_index=0)
                checkpoint()
            cp['complete'] = True
            checkpoint()
            return dict(counts)
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
             clock: Callable[[], float] = time.time, step_limit: int | None = None, completions: list[OperationalCompletion] | None = None) -> dict[str, Any]:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "pipeline.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CollectionError("Another pipeline is running") from exc
        fetch = ScheduledCalls(directory, fetch, clock=clock)
        if isinstance(fetch.fetch, Byparr):
            downloader.fetch_image = fetch.cover
        path = directory / "monitor.yaml"
        journal = CollectorState(directory).monitor()
        if journal.get("version") != 1:
            raise CollectionError("Unsupported monitor state")
        if category not in journal["categories"]:
            items, _ = catalogue(fetch(category), category)
            identifiers = [int(re.search(r"/(\d+)-", urlsplit(url).path).group(1)) for url in items]  # type: ignore[union-attr]
            if identifiers != sorted(set(identifiers), reverse=True):
                raise CollectionError("Initial catalogue is not ordered by descending ID")
            journal["categories"][category] = {"after_id": max(identifiers),
                "initialized_at": datetime.now(timezone.utc).isoformat()}
            CollectorState(directory).category(category,journal['categories'][category])
            return {"initialized": True, "after_id": max(identifiers), "queued": 0}
        if shutil.disk_usage(directory).free < minimum_free:
            raise CollectionError("Less than the configured free disk reserve; submissions stopped")
        status = downloader.call("status_server")
        if status["pause"] or not status["download"]:
            raise CollectionError("pyLoad downloads are paused or disabled")
        checkpoint = journal["categories"][category]
        original_category=encode(checkpoint)
        counts = collect([category], fetch, directory, pages=0, after_id=checkpoint["after_id"],
                         max_items=step_limit or 0, max_pages_per_call=1 if step_limit else None)
        state_path = directory / "state.yaml"
        records = CollectorState(directory).captured()["items"]
        queued = 0
        bounded_complete = True
        for source, record in records.items():
            match = re.search(r"/(\d+)-", urlsplit(source).path)
            if match is None or int(match.group(1)) <= checkpoint["after_id"]:
                continue
            from activity import report
            report('collection', 'working', 'New source', item=source, title=record.get('title', ''))
            prior = journal["submissions"].get(source, {}).get("status")
            if step_limit is not None:
                if prior == "queued":
                    continue
                if queued >= step_limit:
                    bounded_complete = False
                    break
            if downloader.downloads is not None and "page" not in record:
                record["page"] = extract_page(fetch(source), source)
                CollectorState(directory).capture(source,records[source])
            downloader.submit(source, record, journal, path)
            queued += prior != "queued"
        if step_limit is not None:
            retained = CollectorState(directory).monitor()
            if not retained.get('collection_checkpoint', {}).get('complete') or not bounded_complete:
                CollectorState(directory).category(category,journal['categories'][category])
                return {**counts, 'queued': queued, 'continued': True}
        checkpoint["after_id"] = counts["newest_id"]
        checkpoint["last_success"] = datetime.now(timezone.utc).isoformat()
        if completions is not None:
            completions.append(CollectorState(directory).prepare_completion('category',category,journal['categories'][category],expected=(original_category,)))
        else:
            CollectorState(directory).category(category,journal['categories'][category])
            CollectorState(directory).patch_monitor(collection_checkpoint=None)
        return {**counts, "queued": queued}

def run_backfill(directory: Path, fetch: Callable[[str], str], downloader: Pyload,
                 minimum_free: int = 0, *, max_items: int | None = None, max_pages_per_call: int | None = None,
                 completions: list[OperationalCompletion] | None = None) -> dict[str, int] | None:
    """Run a durable backfill batch. Release the collector lock between batches."""
    path = directory / "backfill.yaml"
    if CollectorState(directory).backfill() is None:
        return None
    with (directory / "pipeline.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CollectionError("Another pipeline is running") from exc
        fetch = ScheduledCalls(directory, fetch)
        if isinstance(fetch.fetch, Byparr):
            downloader.fetch_image = fetch.cover
        job = CollectorState(directory).backfill()
        if job is None:
            return None
        if (job.get("version") != 1 or type(job.get("pages_requested")) is not int
                or not 1 <= job["pages_requested"] <= 50):
            raise CollectionError("Invalid bounded backfill request")
        if job["status"] == "complete":
            return None
        monitor_path = directory / "monitor.yaml"
        journal = CollectorState(directory).monitor()
        state_path = directory / "state.yaml"
        state = CollectorState(directory).captured()
        if state.get("version") != 1 or journal.get("version") != 1:
            raise CollectionError("Unsupported collection or monitor state")
        records = state["items"]
        pages_start = len(job["pages"])
        while len(job["pages"]) < job["pages_requested"]:
            if max_pages_per_call is not None and len(job["pages"]) - pages_start >= max_pages_per_call:
                return {"pages": len(job["pages"]), "items": len(job["items"]), "queued": job["submitted"], "pending": 1}
            url = job["next_page"]
            if not url or url in job["pages"]:
                raise CollectionError("Backfill pagination ended or repeated before requested page count")
            items, next_url = catalogue(fetch(url), url)
            job["pages"][url] = list(items)
            job["items"].update(items)
            job["next_page"] = next_url
            job["status"] = "collecting"
            CollectorState(directory).backfill_page(job,url,items)
            logging.info("Backfill catalogue: %d/%d pages, %d unique items",
                         len(job["pages"]), job["pages_requested"], len(job["items"]))
        processed = 0
        for source in job["items"]:
            if journal["submissions"].get(source, {}).get("status") == "queued":
                continue
            if max_items is not None and processed >= max(1, max_items):
                return {"pages": len(job["pages"]), "items": len(job["items"]),
                        "queued": job["submitted"], "pending": 1}
            if shutil.disk_usage(directory).free < minimum_free:
                raise CollectionError("Free disk reserve reached during backfill")
            status = downloader.call("status_server")
            if status["pause"] or not status["download"]:
                raise CollectionError("pyLoad downloads are paused or disabled")
            from activity import report
            report('collection', 'working', 'Backfill source', item=source, title=job['items'].get(source, ''))
            if "*" not in records.get(source, {}).get("complete_hosters", []):
                records[source] = resolve_source(source, fetch, directory)
                CollectorState(directory).capture(source,records[source],source_checkpoint=None)
                export_links(records, directory)
            if downloader.downloads is not None and "page" not in records[source]:
                records[source]["page"] = extract_page(fetch(source), source)
                CollectorState(directory).capture(source,records[source])
            from activity import report
            report('collection', 'working', 'Backfill submission', item=source, title=records[source].get('title', ''))
            package_id = downloader.submit(source, records[source], journal, monitor_path)
            report('collection', 'working', 'Backfill submitted', item=source, title=records[source].get('title', ''), progressed=True)
            job["submitted"] = sum(journal["submissions"].get(u, {}).get("status") == "queued" for u in job["items"])
            CollectorState(directory).save_backfill(job)
            logging.info("Backfill queued: %d/%d items; package %d", job["submitted"], len(job["items"]), package_id)
            processed += 1
        # A prior process may have confirmed the submission but not saved batch progress.
        job["submitted"] = sum(journal["submissions"].get(u, {}).get("status") == "queued" for u in job["items"])
        job["status"] = "complete"
        job["completed_at"] = datetime.now(timezone.utc).isoformat()
        if completions is not None:
            completions.append(CollectorState(directory).prepare_completion('backfill','current',{k:v for k,v in job.items() if k not in ('pages','items')}))
        else:
            CollectorState(directory).save_backfill(job)
        return {"pages": len(job["pages"]), "items": len(job["items"]), "queued": job["submitted"]}
