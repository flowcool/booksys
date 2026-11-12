"""Compatible standalone collector CLI. Application code lives in booksys_core."""
from __future__ import annotations
import argparse
import json
import math
import os
from pathlib import Path
import sys
import yaml
from booksys_core.browser import Byparr as Byparr
from booksys_core.errors import BrowserError as BrowserError, CollectionError as CollectionError
from booksys_core.parsing import (DEFAULT_CATEGORY as DEFAULT_CATEGORY, hosted_url as hosted_url,
    site_url as site_url, document as document, catalogue as catalogue,
    extract_page as extract_page, resolve as resolve)
from booksys_core.files import atomic_write as atomic_write, export_links as export_links
from booksys_core.collection import collect as collect


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
        from library.catalog import lock
        from booksys_core.scheduler import ScheduledCalls
        canonical = Path(os.environ.get('BOOKSYS_COLLECTION_DIR', '/data/booksys/state'))
        if args.directory.resolve() != canonical.resolve():
            raise CollectionError('Standalone Bookys reads require the canonical collection directory')
        with lock(args.directory / 'pipeline.lock', blocking=False):
            browser = ScheduledCalls(args.directory, Byparr(args.byparr, args.delay))
            counts = collect(args.category or [DEFAULT_CATEGORY], browser,
                             args.directory, set(args.hoster) if args.hoster else None,
                             args.pages, args.max_items, args.refresh)
        print(json.dumps(counts))
        return 0
    except (CollectionError, OSError, ValueError, yaml.YAMLError) as exc:
        from booksys_core.diagnostics import emit
        emit(service='collector', stage='collection', code='failed', reason='unknown_browser_failure')
        print('Collection stopped; inspect filtered diagnostics', file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
