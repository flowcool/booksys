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

METHOD = 'comictagger-fa11a345-legacy-french-shapes-5'


def is_volume_title(value: str) -> bool:
    """A bare volume caption supplies no album title."""
    return re.fullmatch(r'(?:Tome|Volume|Vol|T)\.?\s*\d+|#\s*\d+', value.strip(), re.I) is not None


def parse_name(value: str) -> dict[str, Any]:
    name = PurePosixPath(value).name
    name = re.sub(r'\.(?:pdf|cbr|cbz)$', '', name, flags=re.I).strip()
    encoded = '_' in name or bool(re.search(r'@BD[_ ]Fr$', name, re.I))
    name = re.sub(r'@BD[_ ]Fr$', '', name, flags=re.I)
    name = re.sub(r'^zz(?=[a-zÀ-ÿ])', '', name, flags=re.I)
    dotted = not re.search(r'\s', name) and len(name.split('.')) >= 3
    if dotted:
        name = name.replace('.', ' ')
        name = re.split(r'\b(?:FRENCH|TRUEFRENCH|MULTi|RETAiL|COMiC|HYBRiD|iNTERNAL|eBook|SCAN|CBZ|CBR|PDF|DIGITAL|\d{4})\b', name, maxsplit=1, flags=re.I)[0].strip()
    name = name.replace('_', ' ')
    name, release_tags = re.subn(r'\[(?:DIGITAL\b[^]]*|BD FR)\]|\((?:TONER|DIGITAL-\d+)\)',
                                 ' ', name, flags=re.I)
    name = ' '.join(name.split())
    name = re.sub(r'\s*\((?:19|20)\d{2}\)\s*$', '', name)
    if (not name or len(name) > 500 or re.search(
            r'\b(?:tomes|volumes|best\s+of|pages?|pack|collection)\b|\d\s*[-–]\s*\d|\+', name, re.I)):
        return {}
    name = re.sub(r'\s*\[(?:PDF|CBR|CBZ|FRENCH|HD|SCAN)[^]]*\]\s*', ' ', name, flags=re.I).strip()
    integral = re.fullmatch(r'(.+?)\s+[-–]\s+Int[ée]grale', name, re.I)
    if integral:
        return {'title': integral[1].strip(), 'integral': True}
    repeated = re.fullmatch(r'(.+?)\s+[-–]\s+(\d{1,3})\.\s*(?:Tome|Volume)\s+(\d{1,3})', name, re.I)
    if repeated:
        if int(repeated[2]) != int(repeated[3]):
            return {}
        name = repeated[1] + ' - T' + repeated[2]
    if len({int(n) for n in re.findall(r'\b(?:T(?:ome)?|Vol(?:ume)?)\.?\s*(\d{1,3})\b', name, re.I)}) > 1:
        return {}
    match = None if dotted else re.fullmatch(r'(.+?)\s+#(\d{1,3})(?:\s+[-–]\s+(.+))?', name)
    explicit_issue = match is not None
    if not match:
        match = re.fullmatch(r'(.+?)\s+(?:[-–]\s+)?(?:T(?:ome)?|Vol(?:ume)?)\.?\s*(\d{1,3})(?:\s+[-–]\s+(.+)|:.*' + (r'|\s+.*' if dotted else '') + r')?', name, re.I)
    if not match:
        match = re.fullmatch(r'(.+?)\s+[-–]\s+(\d{1,3})(?:\s+[-–]\s+(.+))?', name)
    if not match and encoded:
        match = re.fullmatch(r'((?:.*\s)?[^\W\d_]+)\s+(\d{1,3})()', name)
    if not match:
        return {}
    if explicit_issue and re.search(r'\b(?:T(?:ome)?|Vol(?:ume)?)\.?\s*\d', match[1], re.I):
        return {}
    # The upstream parser handles issue normalization and series separators.
    parser = FileNameParser()
    parser.parse_filename(match[1] + ' #' + match[2] + '.cbz')
    if not parser.series or not parser.issue:
        return {}
    # ComicTagger drops parenthetical phrases. Keep qualified series labels,
    # as for explicit issue markers, rather than asserting a different series.
    series = (re.sub(r'\s*\((?:19|20)\d{2}\)\s*$', '', match[1]).strip()
              if explicit_issue or re.search(r'\([^)]*\)', match[1]) else parser.series)
    result: dict[str, Any] = {'series': series, 'number': parser.issue}
    if match[3] and not encoded and not dotted and not is_volume_title(match[3]):
        title = re.sub(r'\s+[-–]\s+Mythic$', '', match[3].strip(), flags=re.I)
        # Parentheses can also be part of the album title. Keep the original
        # assertion for validation until exact album credits qualify the suffix.
        suffix = re.fullmatch(r'(.+?)\s+\(([^()]+)\)', title) if release_tags else None
        if suffix:
            result.update(title=suffix[1], title_suffix_hint=suffix[2], raw_title=title)
        else:
            result['title'] = title
    return result
