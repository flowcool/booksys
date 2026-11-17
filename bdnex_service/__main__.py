"""Serve a warm snapshot; refresh into a separate file and publish atomically."""
from __future__ import annotations

import argparse
import fcntl
from http.server import HTTPServer
import json
import os
import sqlite3
from pathlib import Path
import tempfile
import threading
import time
from typing import Any

from bdnex_service.engine import Engine, SitemapCache, validate_xml
from bdnex_service.server import handler


def refresh(path: Path) -> dict[str, Any]:
    from bdnex.importer.bedetheque import Bedetheque, http_fetch  # type: ignore[import-not-found]

    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / 'refresh.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with tempfile.TemporaryDirectory(prefix='index-', dir=path.parent) as folder:
            staging = Path(folder) / 'index.sqlite'
            cache = SitemapCache(staging, writable=True)
            try:
                source = Bedetheque(cache, sitemap_fetch=lambda url: validate_xml(http_fetch(url)),
                                    sitemap_delay=(6.2, 6.2))
                source.prepare(recache=True)
            finally:
                cache.close()
            candidate = Engine(staging)
            if path.exists() and candidate.count < Engine(path).count * 0.95:
                raise ValueError('Index lost more than 5 percent of albums; previous snapshot retained')
            with staging.open('rb') as stream:
                os.fsync(stream.fileno())
            os.replace(staging, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return {'index': candidate.index, 'albums': candidate.count}


class SnapshotMatcher:
    """Retain the last warm engine until a replacement loads successfully."""
    def __init__(self, path: Path):
        self.path = path
        self.engine = Engine(path)
        self.stamp = path.stat().st_mtime_ns
        self.next_check = 0.0

    def __call__(self, request: dict[str, Any]) -> dict[str, Any]:
        if time.monotonic() >= self.next_check:
            self.next_check = time.monotonic() + 60
            try:
                changed = self.path.stat().st_mtime_ns
                if changed != self.stamp:
                    updated = Engine(self.path)
                    self.engine, self.stamp = updated, changed
            except (OSError, ValueError, sqlite3.Error):
                print(json.dumps({'event': 'index_reload_failed', 'retained': self.engine.index}), flush=True)
        return self.engine.match(request)


def serve(path: Path, token: str, port: int, interval: int) -> None:
    match = SnapshotMatcher(path)

    def daily() -> None:
        while True:
            time.sleep(interval)
            try:
                print(json.dumps({'event': 'index_refreshed', **refresh(path)}), flush=True)
            except Exception as exc:
                print(json.dumps({'event': 'index_refresh_failed', 'error': type(exc).__name__}), flush=True)

    threading.Thread(target=daily, daemon=True).start()
    print(json.dumps({'event': 'ready', 'index': match.engine.index, 'albums': match.engine.count}), flush=True)
    HTTPServer(('0.0.0.0', port), handler(token, match)).serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('serve', 'refresh'))
    parser.add_argument('--index', type=Path, default=Path('/data/index.sqlite'))
    parser.add_argument('--port', type=int, default=8766)
    args = parser.parse_args()
    if args.command == 'refresh':
        print(json.dumps(refresh(args.index)))
    else:
        token = Path(os.environ['BOOKSYS_BDNEX_TOKEN_FILE']).read_text().strip()
        serve(args.index, token, args.port, 86400)


if __name__ == '__main__':
    main()
