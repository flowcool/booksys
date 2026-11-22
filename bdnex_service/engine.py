"""Reuse the pinned native matcher with an immutable SQLite sitemap snapshot."""
from __future__ import annotations

from collections import defaultdict
from contextvars import ContextVar
import hashlib
from pathlib import Path
import re
import sqlite3
from typing import Any
import xml.etree.ElementTree as ET

from library.bdnex_protocol import STRONG_SCORE, bedetheque_album_id, validate_candidates
from library.work_identity import normalized

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
            catalogue = self.source.catalogue()
            self.count = len(catalogue)
            self.numbered: dict[str, list[tuple[str, str]]] = defaultdict(list)
            self.albums = {candidate.source_id: candidate for candidate in catalogue}
            for candidate in catalogue:
                series, separator, _ = candidate.url.split('/BD-', 1)[-1].partition('-Tome-')
                if separator and candidate.volume is not None:
                    self.numbered[str(candidate.volume)].append((series, candidate.source_id))
            self.index = revision.hexdigest()
        finally:
            cache.close()
        # Catalogue is warm. known_candidate_from_url builds its id map from
        # this in-memory catalogue, so matching needs neither DB nor network.

    def match(self, request: dict[str, Any]) -> dict[str, Any]:
        from bdnex.importer.matching import find_matches, query_from_filename, volumes_for  # type: ignore[import-not-found]
        from bdnex.importer.bedetheque.sitemap import query_tokens
        rows: dict[str, dict[str, Any]] = {}
        native = filtered = 0
        web_refs: set[str] = set()

        def add(candidate: Any, score: float) -> None:
            info = self.source.partial_info(candidate)
            row = {'source': 'bedetheque', 'edition_ref': info.source_id,
                   'url': info.url, 'title': info.title,
                   'series': info.series.name if info.series else None,
                   'number': str(info.volume) if info.volume is not None else None, 'score': score}
            if info.source_id not in rows or rows[info.source_id]['score'] < score:
                rows[info.source_id] = row

        for item in request['inputs']:
            hints = dict(item['embedded'])
            ref = bedetheque_album_id(hints.get('Web'))
            if ref is not None and ref in self.albums:
                web_refs.add(ref)
                add(self.albums[ref], 100)
            # Upstream repairs loose URLs. Only canonical Web hints are allowed here.
            if not ref:
                hints.pop('Web', None)
            token = _hints.set(hints)
            try:
                matches = find_matches(self.source, item['path'], limit=5)
            finally:
                _hints.reset(token)
            native += len(matches)
            for match in matches:
                if match.score < MIN_SCORE:
                    series = re.split(r'\b\d{1,3}\b', query_from_filename(item['path']), maxsplit=1)[0]
                    words = set(query_tokens(series)[0])
                    if (match.score < MIN_AGREEING_SCORE
                            or match.candidate.volume not in volumes_for(item['path'])
                            or not words or not words <= set(match.candidate.name.split())):
                        filtered += 1
                        continue
                # URL slugs do not prove language, integral status or family.
                add(match.candidate, match.score)
        identity = request.get('identity', {})
        if len(identity.get('series', [])) == len(identity.get('number', [])) == 1:
            series = identity['series'][0]
            number = identity['number'][0]
            for slug, ref in self.numbered.get(number, ()):
                label = normalized(slug, 'series')
                if label == series or label.startswith(series + ' '):
                    add(self.albums[ref], MIN_AGREEING_SCORE)
        candidates = sorted(rows.values(), key=lambda row: row['score'], reverse=True)[:15]
        result = {'index': self.index, 'candidates': validate_candidates(candidates),
                  'match_counts': {'native': native, 'filtered': filtered}}
        if 'identity' in request and candidates:
            if len(web_refs) == 1:
                # This is a proposal. The worker checks every file conflict and
                # hydrates this exact album before any Web exemption.
                result['decision'] = {'status': 'selected', 'reason': 'strong', 'edition_ref': next(iter(web_refs))}
            elif web_refs:
                result['decision'] = {'status': 'review', 'reason': 'conflicting_file_evidence'}
            else:
                result['decision'] = self.decide(candidates, identity)
        return result

    def decide(self, candidates: list[dict[str, Any]], identity: dict[str, list[str]]) -> dict[str, str]:
        if any(len(values) > 1 for values in identity.values()):
            return {'status': 'review', 'reason': 'conflicting_file_evidence'}
        number = next(iter(identity.get('number', [])), None)
        series = next(iter(identity.get('series', [])), None)
        title = next(iter(identity.get('title', [])), None)
        year = next(iter(identity.get('year', [])), None)

        def agrees(row: dict[str, Any]) -> bool:
            row_series = normalized(row.get('series') or '', 'series')
            row_title = normalized(row.get('title') or '', 'title')
            if row.get('number') and (identity.get('integral') == ['true']
                                      or (number and normalized(row['number'], 'number') != number)):
                return False
            if series and row_series.startswith(series + ' '):
                dates = re.findall(r'\b(?:18|19|20)\d{2}\b', row_series[len(series):])
                if dates and ((year and year not in dates) or (
                        not year and (not title or row_title != title))):
                    return False
            if title and row_title and row_title != row_series and row_title != title:
                return False
            return not series or not row_series or row_series == series or row_series.startswith(series + ' ') or (
                bool(title) and row_title == title
            )

        compatible = [row for row in candidates if agrees(row)]
        strong = [row for row in compatible if row['score'] >= STRONG_SCORE]

        refs: set[str] = set()
        if series and number:
            for slug, ref in self.numbered.get(number, ()):
                label = normalized(slug, 'series')
                if label == series or label.startswith(series + ' '):
                    refs.add(ref)
        distinct_series = {normalized(slug, 'series') for slug, ref in self.numbered.get(number or '', ())
                           if ref in refs}
        if len(distinct_series) > 1:
            return {'status': 'review', 'reason': 'ambiguous'}
        if len(strong) > 1 or (len(refs) > 1 and not (
            len(strong) == 1 and title and normalized(strong[0].get('title') or '', 'title') == title
        )):
            return {'status': 'review', 'reason': 'ambiguous'}
        if len(strong) == 1:
            return {'status': 'selected', 'reason': 'strong', 'edition_ref': strong[0]['edition_ref']}
        if series and number and len(refs) == 1:
            for row in compatible:
                label = normalized(row.get('series') or '', 'series')
                if (row['edition_ref'] not in refs or not row.get('number')
                        or not (label == series or label.startswith(series + ' '))):
                    continue
                dated_variant = re.search(r'\b(?:18|19|20)\d{2}\b', label[len(series):])
                if dated_variant and not year and normalized(row.get('title') or '', 'title') not in identity.get('title', []):
                    break
                return {'status': 'selected', 'reason': 'numbered', 'edition_ref': row['edition_ref']}
        return {'status': 'review', 'reason': 'insufficient'}
