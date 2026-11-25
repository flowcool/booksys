"""Candidate words against asset labels. No provider calls or score ranking."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from functools import cache
import re
from typing import Any
import unicodedata

from library.bdnex_protocol import FAMILY_METHOD, FAMILY_SCHEMA, FamilyLookup, bedetheque_album_id, family_matches
from library.asset_evidence import file_years
from library.names import parse_name
from library.work_identity import normalized

METHOD = 'candidate-concordance-r3-v1'
STOP = frozenset('le la les de des du d l un une et a au aux the of en sur par pour vol volume tome t n no bd fr french cbr cbz pdf'.split())
JUNK = frozenset('hybrid comic ebook toner printer notag scan digital manga vf webrip fixed officiel paprika papri ka kashimoya beastieboy internal retail dp sp crossread'.split())
MARK = re.compile(r'\b(?:t|tome|vol|volume|int|integrale|hs|n|no)\s*(\d{1,4})\b|(?:^|\s)(\d{1,4})(?=\s|$)')
EXPLICIT_ISSUE = re.compile(r'(?<!\w)(?:t(?:ome)?|vol(?:ume)?|n[°o]?|hs|int(?:[ée]grale)?|#)'
                            r'\.?\s*(\d{1,4})(?!\w|[.,–-]\s*\d)', re.I)
CATALOGUE_PREFIX_MODE = 'catalogue_prefix:cover'


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


def asset_labels(evidence: dict[str, Any]) -> list[str]:
    return list(dict.fromkeys(a['value'] for a in evidence.get('provenance', {}).get('labels', [])
                              if a.get('scope') == 'asset' and isinstance(a.get('value'), str)))


def label_issue(label: str) -> tuple[str, str, set[str]] | None:
    # Keep the series qualifier and separate it from the issue marker.
    label = re.sub(r'^zz(?=[a-zÀ-ÿ])', '', label.strip(), flags=re.I).replace('_', ' ')
    bare = re.sub(r'\[[^]]*\]|\((?!\s*s[ée]rie\s+\d+\s*\))[^)]*\)', ' ', label, flags=re.I)
    marks = list(EXPLICIT_ISSUE.finditer(bare))
    if len(marks) > 1:
        return None
    raw_series: Any
    raw_issue: Any
    title: Any
    if marks:
        mark = marks[0]
        raw_series, raw_issue, title = bare[:mark.start()], mark[1], bare[mark.end():]
    else:
        parsed = parse_name(label)
        raw_series, raw_issue, title = parsed.get('series'), parsed.get('number'), parsed.get('title')
    series = normalized(raw_series, 'series') if raw_series else ''
    issue = number(raw_issue)
    if series and issue is not None:
        return series, issue, words(title) - JUNK
    return None


def catalogue_family_for(evidence: dict[str, Any], row: dict[str, Any], lookup: FamilyLookup, *,
                         prefix: bool = False) -> dict[str, Any] | None:
    """Bind the full-index family lookup to a proposed album and its file label."""
    if evidence.get('asset_bound') is not True or row.get('source') != 'bedetheque':
        return None
    ref = bedetheque_album_id(row.get('url'))
    issue = number(row.get('number'))
    if ref is None or ref != row.get('edition_ref') or issue is None:
        return None
    series = str(row.get('series') or '')
    labels = asset_labels(evidence)
    claims = [claim for label in labels if (claim := label_issue(label)) is not None]
    embedded = evidence.get('embedded', {})
    claims_numbers = {n for _, n, _ in claims}
    claims_numbers.update(str(int(n)) for label in labels for n in EXPLICIT_ISSUE.findall(
        re.sub(r'\([^)]*\)|\[[^]]*\]', '', label.replace('_', ' '))))
    claims_numbers.update(str(int(n)) for n in EXPLICIT_ISSUE.findall(str(embedded.get('Title') or '')))
    if embedded_issue := number(embedded.get('Number')):
        claims_numbers.add(embedded_issue)
    if claims_numbers != {issue}:
        return None
    if prefix:
        years = file_years(evidence)
        if embedded.get('Year'):
            years.add(str(embedded['Year']))
        dates = set(re.findall(r'\b(?:18|19|20)\d{2}\b', normalize(series)))
        if years and dates and dates != years:
            return None
        sw, tw = words(series), words(row.get('title'))
        titles = [title for _, _, title in claims if title]
        if (tw - sw and any(not title & tw for title in titles if title - (sw | tw))
                and not any(title <= sw | tw for title in titles)):
            return None
    for query in dict.fromkeys(query for query, _, _ in claims):
        if not family_matches(query, series) or prefix and query == normalized(series, 'series'):
            continue
        response = lookup(query, issue)
        if response is None:
            continue
        albums = response['albums']
        if not any(album['album_id'] == ref and album['number'] == issue for album in albums):
            continue
        families = sorted({str(normalized(album['series'], 'series')) for album in albums})
        # The issue number is common to the response. Absent titles cannot prove edition equivalence.
        works = {title if (title := normalized(album['title'])) else ('untitled', album['album_id'])
                 for album in albums}
        if (prefix and len(families) == 1) or (not prefix and len(works) == 1):
            return {'method': FAMILY_METHOD, 'version': FAMILY_SCHEMA, 'index': response['index'],
                    'family_size': len(works), 'matched_families': families, 'series': query, 'number': issue}
    return None


@dataclass
class Decision:
    candidate: dict[str, Any] | None = None
    mode: str = 'none'
    needs_cover: bool = False
    reason: str = 'No candidate agrees with asset labels'
    matched_words: list[str] = field(default_factory=list)
    editions: list[dict[str, Any]] = field(default_factory=list)
    catalogue_family: dict[str, Any] | None = None

    def receipt(self) -> dict[str, Any]:
        result = {'method': METHOD, 'mode': self.mode, 'needs_cover': self.needs_cover,
                'reason': self.reason, 'matched_words': self.matched_words,
                'album_id': self.candidate.get('edition_ref') if self.candidate else None}
        if self.catalogue_family is not None:
            result['catalogue_family'] = self.catalogue_family
        return result


def decide(evidence: dict[str, Any], candidates: list[dict[str, Any]], *,
           family_lookup: FamilyLookup | None = None) -> Decision:
    """Return one work, its basis and whether cover confirmation is required.

    Only asset-scoped labels and bound embedded ComicInfo can establish R3.
    Parsed fields, OCR and discovery scores cannot veto or select a work.
    """
    labels = asset_labels(evidence)
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
    family_proofs: dict[str, dict[str, Any]] = {}
    if not matches and family_lookup is not None:
        lookup = cache(family_lookup)
        for row in candidates:
            proof = catalogue_family_for(evidence, row, lookup, prefix=True)
            if proof is not None:
                family_proofs[row['edition_ref']] = proof
                matches.append((row, CATALOGUE_PREFIX_MODE, words(row.get('series')) & tokens))
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
                    sorted(matched), [m[0] for m in groups[0]], family_proofs.get(chosen.get('edition_ref', '')))
