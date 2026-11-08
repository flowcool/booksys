"""Bounded French name adapter over ComicTagger's pinned legacy parser.

Only explicit single-album shapes qualified by tests become identity assertions.
Other labels remain discovery hints. Page names and parent bundle labels are not
passed here as album evidence. No surname, edition or family inference occurs.
"""
from __future__ import annotations

from pathlib import PurePosixPath
import re
from typing import Any

from library._vendor.comictagger_filenameparser import FileNameParser

METHOD = 'comictagger-fa11a345-legacy-french-shapes-1'


def parse_name(value: str) -> dict[str, Any]:
    name = PurePosixPath(value).name
    name = re.sub(r'\.(?:pdf|cbr|cbz)$', '', name, flags=re.I).strip()
    name = re.sub(r'@BD[_ ]Fr$', '', name, flags=re.I)
    name = re.sub(r'^Z[zZ](?=[A-ZÀ-Ý])', '', name)
    encoded = '_' in name
    name = name.replace('_', ' ')
    name = ' '.join(name.split())
    name = re.sub(r'\s*\((?:19|20)\d{2}\)\s*$', '', name)
    if (not name or len(name) > 500 or re.search(
            r'\b(?:tomes|volumes|best\s+of|pages?|pack|collection)\b|\d\s*[-–]\s*\d|\+', name, re.I)):
        return {}
    name = re.sub(r'\s*\[(?:PDF|CBR|CBZ|FRENCH|HD|SCAN)[^]]*\]\s*', ' ', name, flags=re.I).strip()
    integral = re.fullmatch(r'(.+?)\s+[-–]\s+Int[ée]grale', name, re.I)
    if integral:
        return {'title': integral[1].strip(), 'integral': True}
    match = re.fullmatch(r'(.+?)\s+(?:[-–]\s+)?(?:T(?:ome)?|Vol(?:ume)?)\.?\s*(\d{1,3})(?:\s+[-–]\s+(.+))?', name, re.I)
    if not match:
        match = re.fullmatch(r'(.+?)\s+[-–]\s+(\d{1,3})(?:\s+[-–]\s+(.+))?', name)
    if not match:
        return {}
    # The upstream parser handles issue normalization and series separators.
    parser = FileNameParser()
    parser.parse_filename(match[1] + ' #' + match[2] + '.cbz')
    if not parser.series or not parser.issue:
        return {}
    result: dict[str, Any] = {'series': parser.series, 'number': parser.issue}
    if match[3] and not encoded:
        result['title'] = match[3].strip()
    return result
