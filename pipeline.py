"""Watch new category entries and submit every collected link to local pyLoad."""

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

from booksys import Byparr, CollectionError, DEFAULT_CATEGORY, atomic_write, catalogue, collect


def save(path: Path, data: dict[str, Any]) -> None:
    atomic_write(path, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))


class Pyload:
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
        links = sorted({link for download in record["downloads"].values() for link in download["urls"]})
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
            submissions[source] = {"status": "creating", "name": name, "links": links}
            save(path, journal)
            result = self.call("add_package", write=True, name=name, links=links, dest=0)
            if type(result) is not int:
                raise CollectionError("pyLoad returned an invalid package ID")
            package_id = result
        package = self.call("get_package_data", package_id=package_id)
        actual = {file["url"] for file in package["links"]}
        if actual != set(links):
            raise CollectionError(f"pyLoad link readback differs for {marker}; queue unchanged")
        submissions[source] = {"status": "moving", "name": name, "links": links, "package_id": package_id}
        save(path, journal)
        if package["dest"] != 1:
            self.call("move_package", write=True, destination=1, package_id=package_id)
        confirmed = self.call("get_package_data", package_id=package_id)
        if confirmed["dest"] != 1:
            raise CollectionError(f"pyLoad did not confirm queue destination for {marker}")
        submissions[source]["status"] = "queued"
        save(path, journal)
        return package_id


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
            downloader.submit(source, record, journal, path)
            queued += prior != "queued"
        checkpoint["after_id"] = counts["newest_id"]
        checkpoint["last_success"] = datetime.now(timezone.utc).isoformat()
        save(path, journal)
        return {**counts, "queued": queued}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("data"))
    parser.add_argument("--category", default=DEFAULT_CATEGORY)
    parser.add_argument("--byparr", default="http://127.0.0.1:8191/v1")
    parser.add_argument("--pyload", default="http://127.0.0.1:8001")
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--interval", type=int, default=900)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.interval < 60:
        parser.error("--interval must be at least 60 seconds")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.umask(0o077)
    credentials = json.loads(args.credentials.read_text())
    downloader = Pyload(args.pyload, credentials["api_key"])
    while True:
        try:
            result = run_once(args.directory, Byparr(args.byparr), downloader, args.category)
            logging.info("Cycle complete: %s", json.dumps(result))
        except Exception as exc:
            logging.error("Cycle stopped: %s: %s", type(exc).__name__, exc)
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
