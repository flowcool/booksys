"""Classify chapters from WEBTOONSVF receipt provenance."""
from __future__ import annotations

from pathlib import Path
import re
from typing import Any
import unicodedata

from library.primitives import now

WEBTOONSVF = 1288796629


def series_key(title: str) -> str:
    text = unicodedata.normalize('NFKD', title.casefold())
    text = ''.join(char for char in text if not unicodedata.combining(char))
    return ' '.join(re.findall(r'[^\W_]+', text))


def chapter_number(filename: str) -> str:
    label = unicodedata.normalize('NFKC', Path(filename).stem).replace('_', ' ')
    marked = re.findall(r'\bchapitre[ .-]+([0-9]+)(?![0-9]|[.,-][0-9]|\w)', label, re.IGNORECASE)
    if marked:
        numbers = {number.lstrip('0') or '0' for number in marked}
        return next(iter(numbers)) if len(numbers) == 1 else ''
    if re.search(r'\bchapitre\b', label, re.IGNORECASE):
        return ''
    match = re.search(r'(?<!\w)(?<![0-9][.,-])([0-9]+)\s*$', label)
    return (match[1].lstrip('0') or '0') if match else ''


def webtoon_origins(state: dict[str, Any], asset_id: str) -> dict[str, dict[str, Any]]:
    origins = {oid: state['origins'][oid].get('telegram', {}) for oid in state['assets'][asset_id]['origins']}
    return origins if all(doc.get('channel') == WEBTOONSVF for doc in origins.values()) else {}


def webtoon_result(state: dict[str, Any], asset_id: str) -> dict[str, Any] | None:
    origins = webtoon_origins(state, asset_id)
    if not origins:
        return None
    titles: list[str] = []
    chapters: list[str] = []
    provenance: list[dict[str, Any]] = []
    for oid, telegram in sorted(origins.items()):
        title = ' '.join(unicodedata.normalize('NFC', telegram.get('index_title', '')).split())
        chapter = chapter_number(telegram['original_name'])
        titles.append(title)
        chapters.append(chapter)
        for field, value, source in (('series', title, 'telegram:index_title'),
                                     ('number', chapter, 'telegram:original_name')):
            if value:
                provenance.append({'scope': 'asset', 'asset': asset_id, 'origin': oid,
                                   'source': source, 'field': field, 'value': value})
    keys = {series_key(title) for title in titles}
    work: dict[str, str] = {}
    if '' in keys:
        code, reason = 'missing_index_title', 'WEBTOONSVF origin has no WEBTOONS_LISTS index title'
    elif len(keys) != 1:
        code, reason = 'conflicting_series', 'WEBTOONSVF origins disagree on the index title'
    elif '' in chapters:
        code, reason = 'missing_chapter', 'WEBTOONSVF origin needs one chapter number in the filename'
    elif len(set(chapters)) != 1:
        code, reason = 'conflicting_chapter', 'WEBTOONSVF origins disagree on the chapter number'
    else:
        code, reason = 'file_evidence', 'Webtoon chapter grouped by Telegram index and filename'
        work = {'series': titles[0], 'number': chapters[0]}
    return {'status': 'webtoon' if work else 'review', 'collection': 'webtoon', 'work': work,
            'reason': reason, 'reason_code': 'webtoon_' + code, 'provenance': provenance,
            'candidates': [], 'concordant_candidates': [], 'field_conflicts': {}, 'updated_at': now()}
