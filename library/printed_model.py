"""Pure interpretation of retained printed evidence."""
from __future__ import annotations
import re
import unicodedata
from typing import Any
from library.bibliographic import isbn13, normalized
from library.primitives import now

def printed_isbns(printed: dict[str, Any]) -> tuple[set[str], bool]:
    codes: set[str] = set()
    invalid = False
    for page in printed.get('pages', []):
        for line in page.get('text', '').splitlines():
            labelled = list(re.finditer(r'\bISBN(?:[- ]?13)?\s*[:：]?\s*([0-9][0-9\s\-\"\']+)', line, re.I))
            values = [match.group(1) for match in labelled] if labelled else [line.strip()]
            for raw in values:
                if not labelled and not re.fullmatch(r'[0-9\s\-\"\'.]+', raw):
                    continue
                digits = re.sub(r'[^0-9]', '', raw)
                if not labelled and len(digits) != 13:
                    continue
                code = isbn13(digits)
                if code:
                    codes.add(code)
                elif labelled or len(digits) == 13:
                    invalid = True
    return codes, invalid


def words(value: str) -> str:
    text = ''.join(c for c in unicodedata.normalize('NFKD', value.casefold())
                   if not unicodedata.combining(c))
    return ' '.join(re.findall(r'[^\W_]+', text))


def identify_printed(printed: dict[str, Any], embedded: dict[str, Any],
                     candidates: list[dict[str, Any]]) -> dict[str, Any]:
    pending: dict[str, Any] = {'status': 'pending', 'reason': 'Printed edition evidence is incomplete',
                               'candidates': candidates}
    codes, invalid = printed_isbns(printed)
    if printed.get('status') != 'ok' or invalid or len(codes) != 1:
        return {**pending, 'reason': 'Printed ISBN is missing, invalid or contradictory in the bounded window'}
    code = next(iter(codes))
    matches = [c for c in candidates if c.get('isbn') == code and c.get('medium') == 'print']
    if not matches:
        return {**pending, 'reason': 'No exact printed edition candidate found', 'reason_code': 'no_candidate'}
    signatures = {(words(c.get('title', '')), words(c.get('author', '')), str(c.get('number', '')),
                   words(c.get('publisher', '')), words(c.get('language', '')), words(c.get('date', '')),
                   words(c.get('revision', ''))) for c in matches}
    if len(signatures) != 1 or any(len({c['edition_ref'] for c in matches if c['source'] == source}) > 1
                                    for source in {c['source'] for c in matches}):
        conflict = any(len({row[index] for row in signatures if row[index]}) > 1 for index in range(7))
        duplicate = any(len({c['edition_ref'] for c in matches if c['source'] == source}) > 1
                        for source in {c['source'] for c in matches})
        return {**pending, 'reason': 'Conflicting exact printed edition candidates' if conflict or duplicate else
                'Printed edition evidence is incomplete', 'reason_code': 'conflict' if conflict or duplicate else 'missing_evidence'}
    candidate = next((c for c in matches if c['source'] == 'bnf'), matches[0])
    required: tuple[str, ...] = ('title', 'author', 'language', 'publisher', 'date', 'work_ref', 'edition_ref', 'url')
    if not candidate.get('unnumbered'):
        required += ('number',)
    if candidate.get('medium') != 'print' or any(not candidate.get(k) for k in required):
        return pending
    front = printed['pages'][:4]
    text = ' ' + words('\n'.join(p['text'] for p in front)) + ' '
    authors = [candidate['author'], *candidate.get('author_forms', [])]
    # Cover/title pages anchor the work. The exact ISBN may be on a sampled
    # copyright page. A rear advertisement cannot supply the title/author anchor.
    volumes = set(re.findall(r'\b(?:volume|vol|tome) (\d+)\b', text))
    expected_volumes = {str(candidate['number'])} if candidate.get('number') else set()
    if volumes != expected_volumes:
        return {**pending, 'reason': 'Missing or conflicting opening-volume anchor', 'reason_code': 'missing_evidence'}
    concordance = {
        'title': [p['name'] for p in front if ' ' + words(candidate['title']) + ' ' in ' ' + words(p['text']) + ' '],
        'author': [p['name'] for p in front if any(' ' + words(a) + ' ' in ' ' + words(p['text']) + ' ' for a in authors)],
        'number': [p['name'] for p in front if re.search(r'\b(?:volume|vol|tome) ' + re.escape(str(candidate['number'])) + r'\b', words(p['text']))]}
    if candidate.get('unnumbered'):
        concordance.pop('number')
    if not all(concordance.values()):
        return {**pending, 'reason': 'Printed title, author or volume does not agree with exact edition'}
    # Unknown fields stay unknown. Present structured contradictions still block.
    for source_field, field in [('Title', 'title'), ('Writer', 'author'), ('Number', 'number'),
                                ('LanguageISO', 'language'), ('Publisher', 'publisher'),
                                ('EditionDate', 'date'), ('EditionRevision', 'revision')]:
        if embedded.get(source_field) and normalized(embedded[source_field]) != normalized(candidate.get(field, '')):
            return {**pending, 'reason': f'Embedded {field} contradicts printed edition'}
    embedded_code = embedded.get('ISBN', embedded.get('GTIN'))
    if embedded_code and isbn13(str(embedded_code)) != code:
        return {**pending, 'reason': 'Embedded ISBN contradicts printed edition'}
    if re.search(r'\b(?:revue|revisee|revised|augmentee|abridged)\b', text):
        return {**pending, 'reason': 'Printed revision requires manual review'}
    return {'status': 'identified', 'reason': 'Exact printed ISBN and consistent title, author and volume',
            'candidates': candidates, 'candidate': candidate,
            'evidence': {'isbn': code, 'url': candidate['url'], 'source': candidate['source'],
                         'representation': 'scan_of_print_edition', 'anchor': 'sampled_isbn_and_opening_title_author_volume', 'printed': printed,
                         'fields': concordance, 'isbn_scope': 'all eight sampled pages; not the complete archive', 'retrieved_at': now()}}
