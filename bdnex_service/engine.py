"""Reuse the pinned native matcher with an immutable SQLite sitemap snapshot."""
from __future__ import annotations

from contextvars import ContextVar
import hashlib
from pathlib import Path
import re
import sqlite3
from typing import Any
import xml.etree.ElementTree as ET

from library.bdnex_protocol import validate_candidates

_hints: ContextVar[dict[str, str]] = ContextVar('embedded_hints', default={})
MIN_SCORE = 70
MIN_AGREEING_SCORE = 60
URLSET = '{http://www.sitemaps.org/schemas/sitemap/0.9}urlset'


def validate_xml(content: bytes) -> bytes:
    if len(content) > 8_000_000 or b'<!DOCTYPE' in content.upper() or ET.fromstring(content).tag != URLSET:
        raise ValueError('Expected a bounded sitemap urlset')
    return content


class SitemapCache:
    """Only upstream source_cache, never a Booksys catalogue or archive mount."""
    def __init__(self, path: Path, *, writable: bool = False):
        self.connection = sqlite3.connect(path if writable else path.resolve().as_uri() + '?mode=ro', uri=not writable)
        if writable:
            self.connection.execute('CREATE TABLE source_cache (source TEXT, kind TEXT, url TEXT, content BLOB, '
                                    'PRIMARY KEY(source,kind,url))')

    def cache_get(self, source: str, kind: str, url: str) -> bytes | None:
        row = self.connection.execute('SELECT content FROM source_cache WHERE source=? AND kind=? AND url=?',
                                      (source, kind, url)).fetchone()
        return bytes(row[0]) if row else None

    def cache_put(self, source: str, kind: str, url: str, content: bytes) -> None:
        self.connection.execute('INSERT OR REPLACE INTO source_cache VALUES (?,?,?,?)', (source, kind, url, content))
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()


class Engine:
    def __init__(self, path: Path):
        from importlib.metadata import version
        if version('bdnex') != '0.10.0':
            raise RuntimeError('BDneX 0.10.0 is required')
        from bdnex.importer.bedetheque import Bedetheque  # type: ignore[import-not-found]
        from bdnex.importer.bedetheque.sitemap import query_tokens, sitemap_urls  # type: ignore[import-not-found]
        from bdnex.library import scan  # type: ignore[import-not-found]

        class VolumeFirstBedetheque(Bedetheque):  # type: ignore[misc]
            def candidates(self, name: str, limit: int = 5) -> list[tuple[Any, float]]:
                # Private upstream methods are accepted for pinned commit 9de29a73.
                index = self._load_index()
                words, volumes = query_tokens(name)
                if words and volumes:
                    pool = self._pool(words)
                    if pool:
                        ranked = sorted(((self._score(words, index[pos]), pos) for pos in pool),
                                        key=lambda sp: sp[0], reverse=True)
                        wanted = [sp for sp in ranked if index[sp[1]].volume in volumes]
                        others = [sp for sp in ranked if index[sp[1]].volume not in volumes]
                        picked = wanted[:2] + others[:limit - 2]
                        for item in ranked:
                            if len(picked) >= limit:
                                break
                            if item not in picked:
                                picked.append(item)
                        return [(index[pos], score) for score, pos in picked[:limit]]
                return super().candidates(name, limit=limit)  # type: ignore[no-any-return]

        # The pinned matcher imports this hook when it needs ComicInfo. Return
        # only supplied hints. Never open its logical path on this host. Context
        # isolation also prevents hints leaking between requests.
        scan.read_embedded = lambda path: dict(_hints.get())
        cache = SitemapCache(path)
        try:
            revision = hashlib.sha256()
            terminal = False
            for url in sitemap_urls():
                content = cache.cache_get('bedetheque', 'sitemap', url)
                if content is None:
                    raise ValueError('Incomplete sitemap snapshot')
                validate_xml(content)
                revision.update(url.encode())
                revision.update(content)
                if not list(ET.fromstring(content)):
                    terminal = True
                    break
            if not terminal:
                raise ValueError('Missing terminal sitemap')
            self.source = VolumeFirstBedetheque(cache)
            self.count = len(self.source.catalogue())
            self.index = revision.hexdigest()
        finally:
            cache.close()
        # Catalogue is warm. known_candidate_from_url builds its id map from
        # this in-memory catalogue, so matching needs neither DB nor network.

    def match(self, request: dict[str, Any]) -> dict[str, Any]:
        from bdnex.importer.matching import find_matches, query_from_filename, volumes_for  # type: ignore[import-not-found]
        from bdnex.importer.bedetheque.sitemap import query_tokens
        rows: dict[str, dict[str, Any]] = {}
        for item in request['inputs']:
            token = _hints.set(item['embedded'])
            try:
                matches = find_matches(self.source, item['path'], limit=5)
            finally:
                _hints.reset(token)
            for match in matches:
                if match.score < MIN_SCORE:
                    series = re.split(r'\b\d{1,3}\b', query_from_filename(item['path']), maxsplit=1)[0]
                    words = set(query_tokens(series)[0])
                    if (match.score < MIN_AGREEING_SCORE
                            or match.candidate.volume not in volumes_for(item['path'])
                            or not words or not words <= set(match.candidate.name.split())):
                        continue
                info = self.source.partial_info(match.candidate)
                row = {'source': 'bedetheque', 'edition_ref': info.source_id,
                       'url': info.url, 'title': info.title,
                       'series': info.series.name if info.series else None,
                       'number': str(info.volume) if info.volume is not None else None, 'score': match.score}
                # URL slugs do not prove language, integral status or family.
                if info.source_id not in rows or rows[info.source_id]['score'] < match.score:
                    rows[info.source_id] = row
        candidates = sorted(rows.values(), key=lambda row: row['score'], reverse=True)
        return {'index': self.index, 'candidates': validate_candidates(candidates)}
