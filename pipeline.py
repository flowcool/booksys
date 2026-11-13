"""Compatible automatic pipeline CLI. Policies do not depend on this entry point."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys
import time
from booksys_core.browser import Byparr as Byparr
from booksys_core.errors import BrowserError as BrowserError, CollectionError as CollectionError
from booksys_core.parsing import DEFAULT_CATEGORY
from booksys_core.files import save as save
from booksys_core.downloader import Pyload as Pyload
from booksys_core.collection import (export_catalogue as export_catalogue, run_once as run_once,
    run_fiche_once as run_fiche_once)
from booksys_core.scheduler import Deferred, seconds
from booksys_core.diagnostics import emit, install_logging


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
    canonical = Path(os.environ.get("BOOKSYS_COLLECTION_DIR", "/data/booksys/state"))
    if args.directory.resolve() != canonical.resolve():
        parser.error("Bookys reads require the canonical collection directory")
    install_logging("collector")
    os.umask(0o077)
    credentials = json.loads(args.credentials.read_text())
    downloader = Pyload(args.pyload, credentials["api_key"])
    browser = Byparr(args.byparr)
    downloader.downloads, downloader.fetch_image = args.downloads, browser.cover
    while True:
        cycle_ok = False
        retry_wait = args.interval
        try:
            result = run_once(args.directory, browser, downloader, args.category)
            emit(service="collector", stage="collection", code="completed")
            cycle_ok = True
        except Deferred as exc:
            retry_wait = max(1, seconds(exc.record['not_before']) - time.time())
            if exc.record['review_block']:
                retry_wait = args.interval
            emit(service='collector', stage='collection', code='waiting', reason=exc.record['wait_reason'])
            if args.once:
                return 1
        except Exception as exc:
            emit(service='collector', stage='collection', code='failed', reason='unknown_effect')
            if args.once:
                return 1
        if args.once:
            return 0
        deadline = time.monotonic() + retry_wait
        # Fill the existing idle period; do not hold up novelty checks for the
        # entire metadata batch. A single in-flight page may exceed the deadline.
        while cycle_ok and args.downloads and time.monotonic() < deadline:
            try:
                if not run_fiche_once(args.directory, browser, downloader, args.downloads):
                    break
            except Deferred as exc:
                deadline = min(deadline, time.monotonic() + max(1, seconds(exc.record['not_before']) - time.time()))
                break
            except (OSError, ValueError, CollectionError):
                emit(service='collector', stage='collection', code='failed', reason='unknown_effect')
                break
        time.sleep(max(0, deadline - time.monotonic()))


if __name__ == "__main__":
    sys.exit(main())
