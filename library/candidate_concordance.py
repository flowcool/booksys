"""Candidate words against asset labels. No provider calls or score ranking."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import re
from typing import Any
import unicodedata

from library.bdnex_protocol import bedetheque_album_id

METHOD = 'candidate-concordance-r3-v1'
STOP = frozenset('le la les de des du d l un une et a au aux the of en sur par pour vol volume tome t n no bd fr french cbr cbz pdf'.split())
JUNK = frozenset('hybrid comic ebook toner printer notag scan digital manga vf webrip fixed officiel paprika papri ka kashimoya beastieboy internal retail dp sp crossread'.split())
MARK = re.compile(r'\b(?:t|tome|vol|volume|int|integrale|hs|n|no)\s*(\d{1,4})\b|(?:^|\s)(\d{1,4})(?=\s|$)')


def normalize(value: Any) -> str:
    text = unicodedata.normalize('NFC', str(value or ''))
    def repair(match: re.Match[str]) -> str:
        chunk = match[0]
        for codec in ('latin-1', 'cp1252'):
            try:
                return chunk.encode(codec).decode('utf-8')
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
        return chunk
    text = re.sub(r'[^\x00-\x7f]+', repair, text)
    text = ''.join(c for c in unicodedata.normalize('NFKD', text.casefold())
                   if not unicodedata.combining(c)).replace('œ', 'oe')
    text = re.sub(r'@\w+', '', text)
    text = re.sub(r'\b([ivxlcdm]+|\d+)(?:eme|e|er|re)\b', r'\1', text)
    text = re.sub(r'\bzz(?=[a-z])', '', text)
    return ' '.join(re.findall(r'[a-z0-9]+', text))


def words(value: Any) -> set[str]:
    return {t[:-1] if len(t) > 3 and t.endswith('s') else t
            for t in normalize(value).split() if t not in STOP and not t.isdigit()
            and not re.fullmatch(r'(?:t|tome|vol|volume|n|no|hs|int)\d+', t)}


def number(value: Any) -> str | None:
    try:
        n = Decimal(str(value).replace(',', '.'))
        return format(n.normalize(), 'f') if n.is_finite() else None
    except InvalidOperation:
        return None


@dataclass
class Decision:
    candidate: dict[str, Any] | None = None
    mode: str = 'none'
    needs_cover: bool = False
    reason: str = 'No candidate agrees with asset labels'
    matched_words: list[str] = field(default_factory=list)
    editions: list[dict[str, Any]] = field(default_factory=list)

    def receipt(self) -> dict[str, Any]:
        return {'method': METHOD, 'mode': self.mode, 'needs_cover': self.needs_cover,
                'reason': self.reason, 'matched_words': self.matched_words,
                'album_id': self.candidate.get('edition_ref') if self.candidate else None}


def decide(evidence: dict[str, Any], candidates: list[dict[str, Any]]) -> Decision:
    """Return one work, its basis and whether cover confirmation is required.

    Only asset-scoped labels and bound embedded ComicInfo can establish R3.
    Parsed fields, OCR and discovery scores cannot veto or select a work.
    """
    labels = list(dict.fromkeys(a['value'] for a in evidence.get('provenance', {}).get('labels', [])
                               if a.get('scope') == 'asset' and isinstance(a.get('value'), str)))
    embedded = evidence.get('embedded', {}) if evidence.get('asset_bound') is True else {}
    if not labels and not embedded:
        return Decision(reason='No asset label or embedded ComicInfo')
    if evidence.get('truncated') or evidence.get('discovery_truncated'):
        return Decision(mode='incomplete', reason='Candidate set is incomplete')
    text = ' '.join(labels + [str(embedded.get(k) or '') for k in ('Series', 'Title')])
    tokens = words(text)
    nums = {str(int(n)) for n in re.findall(r'(?<!\d)\d{1,4}(?!\d)', normalize(text))}
    embedded_number = number(embedded.get('Number'))
    if embedded_number is not None:
        nums.add(embedded_number)
    years = {str(a['value']) for a in evidence.get('provenance', {}).get('year', [])
             if a.get('scope') == 'asset'}
    years.update(re.findall(r'\(((?:19|20)\d{2})\)', ' '.join(labels)))
    if embedded.get('Year'):
        years.add(str(embedded['Year']))
    nums.update(years)
    parts, titles = [], []
    for label in labels:
        nlabel = normalize(label)
        marks = list(MARK.finditer(nlabel))
        parts.append(words(nlabel[:marks[0].start()] if marks else nlabel))
        if marks:
            suffix = words(nlabel[marks[0].end():]) - JUNK
            if suffix:
                titles.append(suffix)
    web = bedetheque_album_id(embedded.get('Web'))
    matches: list[tuple[dict[str, Any], str, set[str]]] = []
    for row in candidates:
        if row.get('source') != 'bedetheque':
            continue
        sw, tw = words(row.get('series')), words(row.get('title'))
        combined = sw | tw
        n = number(row.get('number'))
        series_digits = set(re.findall(r'\b\d+\b', normalize(row.get('series'))))
        issues: set[str] = set()
        embedded_title_issue = re.match(r'\s*(?:tome|t|vol|volume|n|no)\s*(\d{1,4})\b',
                                        normalize(embedded.get('Title')))
        if embedded_title_issue:
            issues.add(str(int(embedded_title_issue[1])))
        for label in labels:
            # Scan resolution, dates and series qualifiers are not issue claims.
            bare = normalize(re.sub(r'\([^)]*\)|\[[^]]*\]', '', label))
            explicit_issues = re.findall(r'\b(?:t|tome|vol|volume|n|no|hs|int)\s*(\d{1,4})\b', bare)
            if explicit_issues:
                issues.update(str(int(v)) for v in explicit_issues)
            else:
                for mark in MARK.finditer(bare):
                    raw = mark[1] or mark[2]
                    if raw in series_digits or re.fullmatch(r'(?:19|20)\d{2}', raw):
                        continue
                    issues.add(str(int(raw)))
                    break
        if (n is not None and (n not in nums or issues - {n}
                              or embedded_number is not None and embedded_number != n)):
            continue
        if n is None and (issues or embedded_number is not None
                          or len(combined) < 2
                          or any(str(int(d)) not in nums for d in re.findall(r'\d+', normalize(row.get('title'))))):
            continue
        informative = bool(tw - sw) and not re.fullmatch(
            r'(?:(?:issue|numero|n|tome|volume|vol)\s*)?\d*', normalize(row.get('title')))
        missing_digits = {d for d in series_digits if str(int(d)) not in nums}
        dates = {d for d in series_digits if re.fullmatch(r'(?:19|20)\d{2}', d)}
        if dates and years and dates != years:
            continue
        if any(not re.fullmatch(r'(?:19|20)\d{2}', d) for d in missing_digits):
            continue
        dated = bool(missing_digits)
        # The file's own Web can explain a noisy title, but never a wrong number.
        if web and web == bedetheque_album_id(row.get('url')) and combined & tokens and not dated:
            matches.append((row, 'web', combined & tokens))
            continue
        if informative and any(not p & tw for p in titles if p - combined):
            if not any(p <= combined | JUNK for p in titles):
                continue
        series_ok = any(p <= combined for p in parts)
        mode = None
        if combined and combined <= tokens and series_ok:
            mode = 'cov' if informative and not dated else 'cov:cover'
        elif n is not None and sw and sw <= tokens and series_ok:
            for label in labels:
                bare = re.sub(r'\((?:19|20)\d{2}\)', '', label)
                digits = {str(int(d)) for d in re.findall(r'\b\d+\b', normalize(bare))} - {n}
                if not (words(label) - combined - JUNK) and not digits:
                    mode = 'series_number:cover'
                    break
        if (mode is None and n is not None and informative and len(tw) >= 2
                and tw <= tokens and any(not p for p in parts) and not dated):
            mode = 'title_number'
        if mode:
            matches.append((row, mode, combined & tokens))
    # A Web id is explicit file authority. Other candidates do not displace it.
    explicit = [m for m in matches if m[1] == 'web']
    if explicit:
        matches = explicit
    groups: list[list[tuple[dict[str, Any], str, set[str]]]] = []
    for match in sorted(matches, key=lambda m: (len(normalize(m[0].get('series'))), str(m[0].get('edition_ref')))):
        row = match[0]
        for group in groups:
            other = group[0][0]
            same_series = normalize(row.get('series')) == normalize(other.get('series'))
            same_title = normalize(row.get('title')) == normalize(other.get('title'))
            variant = (same_title and len(words(row.get('title'))) >= 2
                       and normalize(row.get('series')).startswith(normalize(other.get('series')) + ' '))
            if number(row.get('number')) == number(other.get('number')) and same_title and (same_series or variant):
                group.append(match)
                break
        else:
            groups.append([match])
    if len(groups) != 1:
        return Decision(mode='ambiguous' if groups else 'none',
                        reason='Several possible works' if groups else 'No candidate agrees with asset labels')
    chosen, mode, matched = groups[0][0]
    return Decision(chosen, mode, mode.endswith(':cover'), 'Asset evidence agrees with one work',
                    sorted(matched), [m[0] for m in groups[0]])
