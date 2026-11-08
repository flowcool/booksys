"""Watch new category entries and submit the available AllDebrid primary host set to local pyLoad."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

import yaml

from booksys import BrowserError, Byparr, CollectionError, DEFAULT_CATEGORY, atomic_write, catalogue, collect, export_links, extract_page, resolve
from fiches import CoverError, ImageFetcher, write_fiche


def save(path: Path, data: dict[str, Any]) -> None:
    atomic_write(path, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))


class Pyload:
    downloads: Path | None = None
    fetch_image: ImageFetcher | None = None

    def __init__(self, endpoint: str, key: str):
        parts = urlsplit(endpoint)
        if (parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "localhost", "::1", "pyload"}
                or parts.username or parts.password):
            raise CollectionError("pyLoad must use a local endpoint")
        self.endpoint, self.key = endpoint.rstrip("/"), key

    def call(self, method: str, *, write: bool = False, **parameters: Any) -> Any:
        url = f"{self.endpoint}/api/{method}"
        body = json.dumps(parameters).encode() if write else None
        if not write and parameters:
            url += "?" + urlencode(parameters)
        headers = {"X-API-Key": self.key}
        if write:
            headers["Content-Type"] = "application/json"
        request = Request(url, body, headers)
        with urlopen(request, timeout=30) as response:
            result = json.load(response)
        if isinstance(result, dict) and "error" in result:
            raise CollectionError(f"pyLoad rejected {method}")
        return result

    def host_availability(self) -> dict[str, Any]:
        from mirrors import public_hosts
        return public_hosts()

    def submit(self, source: str, record: dict[str, Any], journal: dict[str, Any], path: Path) -> int:
        """Create in collector, verify all links, then move to the download queue.

        Record intent before POST. Reconcile uncertain writes by exact package name.
        Never resend an unconfirmed create operation.
        """
        submissions = journal.setdefault("submissions", {})
        entry = submissions.get(source)
        if entry and entry["status"] == "queued":
            return int(entry["package_id"])
        marker = "Booksys-" + hashlib.sha256(source.encode()).hexdigest()[:16]
        name = f"{marker} {record['title']}"
        from mirrors import host_sets, select_primary
        all_links = sorted({url for urls in host_sets(record).values() for url in urls})
        if entry:
            links = entry['links']
            host = entry.get('primary_host')
        else:
            host, links = select_primary(record, self.host_availability())
        if not links:
            raise CollectionError("Refusing to submit an empty item")
        packages = self.call("get_queue") + self.call("get_collector")
        matches = [package for package in packages if package["name"] == name]
        if len(matches) > 1:
            raise CollectionError(f"Several pyLoad packages match {marker}; intervention required")
        if matches:
            package_id = int(matches[0]["pid"])
        elif entry:
            raise CollectionError(f"Unconfirmed pyLoad write for {marker}; no automatic resend")
        else:
            submissions[source] = {"status": "creating", "name": name, "links": links, "all_links": all_links,
                                   "primary_host": host, "primary_selected_at": datetime.now(timezone.utc).isoformat()}
            save(path, journal)
            result = self.call("add_package", write=True, name=name, links=links, dest=0)
            if type(result) is not int:
                raise CollectionError("pyLoad returned an invalid package ID")
            package_id = result
        package = self.call("get_package_data", package_id=package_id)
        from collections import Counter
        actual = Counter(file["url"] for file in package["links"])
        if actual != Counter(links):
            raise CollectionError(f"pyLoad link readback differs for {marker}; queue unchanged")
        if self.downloads is not None:
            try:
                write_fiche(source, record, package, self.downloads, self.fetch_image)
            except CoverError as exc:
                if isinstance(exc, BrowserError):
                    raise
                logging.warning("Text fiche saved; cover pending for %s: %s", source, exc)
        submissions[source] = {**submissions.get(source, {}), "status": "moving", "name": name, "links": links,
                               "all_links": all_links, "primary_host": host, "primary_links": links, "package_id": package_id}
        save(path, journal)
        if package["dest"] != 1:
            self.call("move_package", write=True, destination=1, package_id=package_id)
        confirmed = self.call("get_package_data", package_id=package_id)
        if confirmed["dest"] != 1:
            raise CollectionError(f"pyLoad did not confirm queue destination for {marker}")
        submissions[source]["status"] = "queued"
        save(path, journal)
        return package_id


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
         "items": items})


def run_fiche_once(directory: Path, browser: Byparr, downloader: Pyload, root: Path,
                   minimum_free: int = 20 * 1024**3) -> bool:
    """Attempt one eligible fiche between monitor cycles. Never change the queue."""
    with (directory / "pipeline.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if shutil.disk_usage(root).free < minimum_free:
            raise CollectionError("Free disk reserve reached during fiche preservation")
        state = yaml.safe_load((directory / "state.yaml").read_text())
        journal = yaml.safe_load((directory / "monitor.yaml").read_text())
        path = directory / "fiches.yaml"
        progress = yaml.safe_load(path.read_text()) if path.exists() else {"version": 1, "items": {}}
        if progress.get("version") != 1:
            raise CollectionError("Unsupported fiche progress")
        from mirrors import logical_snapshot
        packages = {p["pid"]: p for p in logical_snapshot(downloader, directory / "mirror-routing.yaml")}
        attempted = False
        for source, entry in journal["submissions"].items():
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
                    content = browser(source)
                    record["page"] = extract_page(content, source)
                    fetching_page = False
                    save(directory / "state.yaml", state)
                folder = write_fiche(source, record, package, root, browser.cover,
                                     known_cover=prior.get("cover_relative"))
                progress["items"][source] = {"status": "complete", "folder": folder,
                                              "fiche_relative": f"{folder}/fiche.md",
                                              "cover_relative": next((str(p.relative_to(root)) for p in (root / folder).glob("couverture.*")), None),
                                              "signature": signature,
                                              "saved_at": datetime.now(timezone.utc).isoformat()}
                logging.info("Fiche saved: %s", folder)
            except (OSError, ValueError, CollectionError) as exc:
                browser_failed = fetching_page or isinstance(exc, BrowserError)
                progress["items"][source] = {**prior, "status": "retry", "error": str(exc),
                                              "retry_after": time.time() + (3600 if browser_failed else 900)}
                if isinstance(exc, CoverError) and package is not None:
                    progress["items"][source]["fiche_relative"] = f"{package['folder']}/fiche.md"
                logging.warning("Fiche retry scheduled for %s: %s", source, exc)
                if browser_failed:
                    attempted = False  # Stop browser work this cycle after a solver failure.
            save(path, progress)
            break
        export_catalogue(directory, state["items"], journal, packages, progress)
        return attempted


def run_once(directory: Path, fetch: Callable[[str], str], downloader: Pyload,
             category: str = DEFAULT_CATEGORY, minimum_free: int = 20 * 1024**3) -> dict[str, Any]:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "pipeline.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CollectionError("Another pipeline is running") from exc
        path = directory / "monitor.yaml"
        journal = yaml.safe_load(path.read_text()) if path.exists() else {"version": 1, "categories": {}, "submissions": {}}
        if journal.get("version") != 1:
            raise CollectionError("Unsupported monitor state")
        if category not in journal["categories"]:
            items, _ = catalogue(fetch(category), category)
            identifiers = [int(re.search(r"/(\d+)-", urlsplit(url).path).group(1)) for url in items]  # type: ignore[union-attr]
            if identifiers != sorted(set(identifiers), reverse=True):
                raise CollectionError("Initial catalogue is not ordered by descending ID")
            journal["categories"][category] = {"after_id": max(identifiers),
                "initialized_at": datetime.now(timezone.utc).isoformat()}
            save(path, journal)
            return {"initialized": True, "after_id": max(identifiers), "queued": 0}
        if shutil.disk_usage(directory).free < minimum_free:
            raise CollectionError("Less than the configured free disk reserve; submissions stopped")
        status = downloader.call("status_server")
        if status["pause"] or not status["download"]:
            raise CollectionError("pyLoad downloads are paused or disabled")
        checkpoint = journal["categories"][category]
        counts = collect([category], fetch, directory, pages=0, after_id=checkpoint["after_id"])
        state_path = directory / "state.yaml"
        records = yaml.safe_load(state_path.read_text())["items"] if state_path.exists() else {}
        queued = 0
        for source, record in records.items():
            match = re.search(r"/(\d+)-", urlsplit(source).path)
            if match is None or int(match.group(1)) <= checkpoint["after_id"]:
                continue
            prior = journal["submissions"].get(source, {}).get("status")
            if downloader.downloads is not None and "page" not in record:
                record["page"] = extract_page(fetch(source), source)
                save(state_path, {"version": 1, "items": records})
            downloader.submit(source, record, journal, path)
            queued += prior != "queued"
        checkpoint["after_id"] = counts["newest_id"]
        checkpoint["last_success"] = datetime.now(timezone.utc).isoformat()
        save(path, journal)
        return {**counts, "queued": queued}


def run_backfill(directory: Path, fetch: Callable[[str], str], downloader: Pyload,
                 minimum_free: int = 20 * 1024**3, *, max_items: int | None = None) -> dict[str, int] | None:
    """Run a durable backfill batch. Release the collector lock between batches."""
    path = directory / "backfill.yaml"
    if not path.exists():
        return None
    with (directory / "pipeline.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CollectionError("Another pipeline is running") from exc
        job = yaml.safe_load(path.read_text())
        if (job.get("version") != 1 or type(job.get("pages_requested")) is not int
                or not 1 <= job["pages_requested"] <= 50):
            raise CollectionError("Invalid bounded backfill request")
        if job["status"] == "complete":
            return None
        monitor_path = directory / "monitor.yaml"
        journal = yaml.safe_load(monitor_path.read_text())
        state_path = directory / "state.yaml"
        state = yaml.safe_load(state_path.read_text()) if state_path.exists() else {"version": 1, "items": {}}
        if state.get("version") != 1 or journal.get("version") != 1:
            raise CollectionError("Unsupported collection or monitor state")
        records = state["items"]
        while len(job["pages"]) < job["pages_requested"]:
            url = job["next_page"]
            if not url or url in job["pages"]:
                raise CollectionError("Backfill pagination ended or repeated before requested page count")
            items, next_url = catalogue(fetch(url), url)
            job["pages"][url] = list(items)
            job["items"].update(items)
            job["next_page"] = next_url
            job["status"] = "collecting"
            save(path, job)
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
            if "*" not in records.get(source, {}).get("complete_hosters", []):
                records[source] = resolve(fetch(source), source, None, fetch)
                save(state_path, state)
                export_links(records, directory)
            if downloader.downloads is not None and "page" not in records[source]:
                records[source]["page"] = extract_page(fetch(source), source)
                save(state_path, state)
            package_id = downloader.submit(source, records[source], journal, monitor_path)
            job["submitted"] = sum(journal["submissions"].get(u, {}).get("status") == "queued" for u in job["items"])
            save(path, job)
            logging.info("Backfill queued: %d/%d items; package %d", job["submitted"], len(job["items"]), package_id)
            processed += 1
        # A prior process may have confirmed the submission but not saved batch progress.
        job["submitted"] = sum(journal["submissions"].get(u, {}).get("status") == "queued" for u in job["items"])
        job["status"] = "complete"
        job["completed_at"] = datetime.now(timezone.utc).isoformat()
        save(path, job)
        return {"pages": len(job["pages"]), "items": len(job["items"]), "queued": job["submitted"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("data"))
    parser.add_argument("--category", default=DEFAULT_CATEGORY)
    parser.add_argument("--byparr", default="http://127.0.0.1:8191/v1")
    parser.add_argument("--pyload", default="http://127.0.0.1:8000")
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--interval", type=int, default=900)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--downloads", type=Path, help="Enable offline fiches in the shared download root")
    args = parser.parse_args()
    if args.interval < 60:
        parser.error("--interval must be at least 60 seconds")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.umask(0o077)
    credentials = json.loads(args.credentials.read_text())
    downloader = Pyload(args.pyload, credentials["api_key"])
    browser = Byparr(args.byparr)
    downloader.downloads, downloader.fetch_image = args.downloads, browser.cover
    while True:
        cycle_ok = False
        try:
            backfill = run_backfill(args.directory, browser, downloader,
                                    max_items=None if args.once else 8)
            if backfill is not None and backfill.get("pending"):
                logging.info("Backfill progress; collector lock released for 60 seconds: %s", json.dumps(backfill))
                time.sleep(60)
                continue
            if backfill is not None:
                logging.info("Backfill complete: %s", json.dumps(backfill))
            result = run_once(args.directory, browser, downloader, args.category)
            logging.info("Cycle complete: %s", json.dumps(result))
            cycle_ok = True
        except Exception as exc:
            logging.error("Cycle stopped: %s: %s", type(exc).__name__, exc)
            if args.once:
                return 1
        if args.once:
            return 0
        deadline = time.monotonic() + args.interval
        # Fill the existing idle period; do not hold up novelty checks for the
        # entire metadata batch. A single in-flight page may exceed the deadline.
        while cycle_ok and args.downloads and time.monotonic() < deadline:
            try:
                if not run_fiche_once(args.directory, browser, downloader, args.downloads):
                    break
            except (OSError, ValueError, CollectionError) as exc:
                logging.error("Fiche processing stopped: %s", exc)
                break
        time.sleep(max(0, deadline - time.monotonic()))


if __name__ == "__main__":
    sys.exit(main())
