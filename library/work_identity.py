"""Pure work concordance and sourced classification, independent of exact editions."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import re
from typing import Any
import unicodedata

IDENTITY_FIELDS = ('title', 'series', 'number', 'integral')
WORK_FIELDS = (*IDENTITY_FIELDS, 'author', 'contributors', 'summary', 'language',
               'publisher', 'date', 'isbn', 'genres', 'adult', 'recommended_age', 'family')
NUMBERED_SERIES_ALIASES = {
    'big bull': 'big bull imperia',
    'cassidy': 'hopalong cassidy puis cassidy imperia',
    'garry': 'garry imperia 2e serie',
    'hulk l incroyable ed heritage': 'incroyable hulk editions heritage',
}


def normalized(value: Any, field: str = '') -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, list):
        return tuple(sorted((normalized(item, field) for item in value), key=str))
    if isinstance(value, dict):
        return tuple(sorted((key, normalized(item, key)) for key, item in value.items()))
    text = str(value).strip()
    if field == 'number':
        try:
            number = Decimal(text.replace(',', '.'))
            if number.is_finite():
                return format(number.normalize(), 'f')
        except InvalidOperation:
            pass
    text = ''.join(c for c in unicodedata.normalize('NFKD', text.casefold()) if not unicodedata.combining(c))
    text = text.replace('œ', 'oe').replace('’', "'")
    if field in ('title', 'series'):
        text = re.sub(r"\s*\((?:le|la|les|l'|un|une|des)\)\s*$", '', text)
        text = re.sub(r"^(?:(?:le|la|les|un|une|des)[ .]+|[ld]'(?=\w)|[ld][ _.-]+(?=[aeiouhy]))", '', text)
    return ' '.join(re.findall(r'\w+', text))


def present(value: Any) -> bool:
    return value is not None and value != '' and value != [] and value != {}


def accepted_work(asset: dict[str, Any]) -> dict[str, Any] | None:
    if asset.get('contested'):
        return None
    result = asset.get('work_result', {})
    if result.get('status') == 'identified':
        return {**result['work'], 'field_conflicts': result.get('field_conflicts', {})}
    return None


def informative(value: Any) -> bool:
    if not present(value):
        return False
    text = normalized(value, 'title')
    generic = {'album', 'integrale', 'tome', 'volume', 'vol', 'bd', 'comic', 'comics',
               'manga', 'numero', 'issue', 'untitled', 'n'}
    return any(not token.isdigit() and token not in generic for token in text.split())


def usable_reference_title(candidate: dict[str, Any]) -> bool:
    title = candidate.get('title')
    if informative(title):
        return True
    # An issue label can accompany a series and number match without naming the work.
    label = re.fullmatch(r'(?:tome|volume|vol\.?)\s+(\d+(?:[.,]\d+)?)', str(title).strip(), re.I)
    return label is not None and normalized(label[1], 'number') == normalized(candidate.get('number'), 'number')


def known_work_value(field: str, value: Any) -> bool:
    return informative(value) if field == 'title' else present(value)


def asset_assertions(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    assertions = [deepcopy(a) for a in evidence.get('assertions', [])
                  if isinstance(a, dict) and a.get('scope') == 'asset' and a.get('field') in IDENTITY_FIELDS
                  and present(a.get('value'))]
    for field, entries in evidence.get('provenance', {}).items():
        if field in IDENTITY_FIELDS and isinstance(entries, list):
            assertions.extend({**deepcopy(a), 'field': field} for a in entries
                              if isinstance(a, dict) and a.get('scope') == 'asset' and present(a.get('value')))
    for field in IDENTITY_FIELDS:
        provenance = evidence.get('field_provenance', {}).get(field, {})
        if present(evidence.get(field)) and (evidence.get('asset_bound') is True or provenance.get('scope') == 'asset'):
            assertions.append({**deepcopy(provenance), 'field': field, 'value': evidence[field], 'scope': 'asset'})
    return [a for a in assertions if a['field'] != 'title' or informative(a['value'])]


def creator_names(candidate: dict[str, Any]) -> dict[str, set[str]]:
    """Compare explicit primary roles; missing credits are not disagreements."""
    names: dict[str, set[str]] = {'writer': set(), 'penciller': set()}
    contributors = candidate.get('contributors')
    if isinstance(contributors, dict):
        for role in names:
            values = contributors.get(role, [])
            values = values if isinstance(values, list) else [values]
            names[role].update(normalized(value) for value in values if present(value))
    elif isinstance(contributors, list):
        for person in contributors:
            if not isinstance(person, dict) or not present(person.get('name')):
                continue
            role = normalized(person.get('role', ''))
            for target, aliases in (('writer', ('scenario', 'writer', 'author')),
                                    ('penciller', ('dessin', 'penciller'))):
                if any(alias in role.split() for alias in aliases):
                    names[target].add(normalized(person['name']))
    if not names['writer'] and present(candidate.get('author')):
        names['writer'].add(normalized(candidate['author']))
    return names


def evaluate_work(evidence: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Require exact informative file/reference concordance; never pick a top score.

    Direct evidence requires asset_bound=True or per-field provenance scope=asset.
    Parent bundle/capture labels alone cannot identify a member. Candidate values
    are source assertions, not operator decisions or exact-edition confirmations.
    """
    result: dict[str, Any] = {'status': 'review', 'reason': 'No informative asset-bound evidence',
        'work': {}, 'field_conflicts': {}, 'provenance': [],
        'candidates': deepcopy(candidates)}
    if evidence.get('identity_conflicts'):
        result.update(reason='Contradictory asset identity evidence',
                      field_conflicts=deepcopy(evidence['identity_conflicts']))
        return result
    assertions = asset_assertions(evidence)
    bound: dict[str, Any] = {}
    for field in IDENTITY_FIELDS:
        values = [a for a in assertions if a['field'] == field]
        if len({normalized(a['value'], field) for a in values}) > 1:
            result['field_conflicts'][field] = values
        elif values:
            bound[field] = values[0]['value']
    if result['field_conflicts']:
        result['reason'] = 'Contradictory asset identity evidence'
        return result
    title_anchor = present(bound.get('title')) and informative(bound['title'])
    series_anchor = informative(bound.get('series', '')) and present(bound.get('number'))
    if not title_anchor and not series_anchor:
        return result
    comparable = {field: value for field, value in bound.items() if field != 'title' or title_anchor}
    file_series = normalized(bound.get('series', ''), 'series')
    file_number = normalized(bound.get('number', ''), 'number')
    rejected_fields: set[str] = set()
    matched = []
    for candidate in candidates:
        candidate_series = normalized(candidate.get('series', ''), 'series')
        number_matches = file_number == normalized(candidate.get('number', ''), 'number')
        series_matches = file_series == candidate_series or (
            series_anchor and number_matches and NUMBERED_SERIES_ALIASES.get(file_series) == candidate_series
            and candidate.get('field_provenance', {}).get('series', {}).get('file_series_alias') == file_series)
        conflicts = {field for field, value in comparable.items()
                     if field != 'series' and present(candidate.get(field))
                     and normalized(value, field) != normalized(candidate[field], field)}
        if 'series' in comparable and present(candidate.get('series')) and not series_matches:
            conflicts.add('series')
        if not usable_reference_title(candidate):
            conflicts.add('title')
        if conflicts:
            rejected_fields.update(conflicts)
            continue
        if ((title_anchor and normalized(bound['title'], 'title') == normalized(candidate.get('title', ''), 'title'))
                or (series_anchor and series_matches and number_matches)):
            matched.append(candidate)
    year_suffix = r'\s+(?:(?:19|20)[0-9]{2}|\((?:19|20)[0-9]{2}\))\s*$'
    if not matched and series_anchor and not re.search(year_suffix, str(bound['series'])):
        for candidate in candidates:
            series = str(candidate.get('series', ''))
            year = re.search(year_suffix, series)
            if not year or any(value != year.group().strip().strip('()')
                               for label in evidence.get('labels', [])
                               for value in re.findall(r'\(((?:19|20)[0-9]{2})\)', label)):
                continue
            if (usable_reference_title(candidate)
                    and normalized(bound['number'], 'number') == normalized(candidate.get('number', ''), 'number')
                    and normalized(bound['series'], 'series') == normalized(series[:year.start()], 'series')
                    and not any(present(candidate.get(field))
                                and normalized(value, field) != normalized(candidate[field], field)
                                for field, value in bound.items()
                                if field != 'series' and (field != 'title' or title_anchor))):
                matched.append(candidate)
    result['concordant_candidates'] = deepcopy(matched)
    if not matched:
        result.update(status='no_match', reason='No reference agrees with asset evidence')
        if rejected_fields:
            result['rejected_fields'] = sorted(rejected_fields)
        return result
    for field in IDENTITY_FIELDS:
        values = [c[field] for c in matched if known_work_value(field, c.get(field))]
        if len({normalized(value, field) for value in values}) > 1:
            result['field_conflicts'][field] = [
                {'value': c.get(field), 'url': c.get('url'), 'source': c.get('source')}
                for c in matched if known_work_value(field, c.get(field))]
    if result['field_conflicts']:
        result['reason'] = 'Concordant references describe different works'
        return result
    # A reliable shared title is sufficient until positive creator evidence
    # contradicts it. Provider IDs alone neither prove nor disprove sameness.
    profiles = [(candidate, creator_names(candidate)) for candidate in matched]
    for role, field in (('writer', 'author'), ('penciller', 'contributors')):
        known = [(candidate, names[role]) for candidate, names in profiles if names[role]]
        if len(known) > 1 and not set.intersection(*(names for _, names in known)):
            result['field_conflicts'][field] = [
                {key: deepcopy(candidate.get(key)) for key in
                 ('author', 'contributors', 'source', 'url', 'work_ref', 'edition_ref')}
                for candidate, _ in known]
    if result['field_conflicts']:
        result['reason'] = 'Concordant references have conflicting work creators'
        return result
    work: dict[str, Any] = {}
    for field in WORK_FIELDS:
        values = [c[field] for c in matched if known_work_value(field, c.get(field))]
        distinct = {normalized(value, field) for value in values}
        if field == 'genres' and len(values) == len(matched) and all(isinstance(value, list) for value in values):
            common = set(normalized(value) for value in values[0])
            for genres in values[1:]:
                common.intersection_update(normalized(value) for value in genres)
            if common:
                work[field] = sorted({value for value in values[0] if normalized(value) in common})
        if len(distinct) > 1:
            result['field_conflicts'][field] = [{'value': deepcopy(c.get(field)), 'url': c.get('url'),
                'source': c.get('source')} for c in matched if present(c.get(field))]
        elif values and len(values) == len(matched):
            work[field] = deepcopy(sorted(values, key=str)[0])
    result.update(status='identified', reason='Asset evidence agrees with one work', work=work,
        provenance=[{key: deepcopy(c[key]) for key in ('source', 'upstream', 'work_ref', 'edition_ref', 'url',
                    'retrieved_at', 'field_provenance') if key in c} for c in matched])
    if evidence.get('truncated') or evidence.get('discovery_truncated'):
        result.update(status='review', reason='Discovery was truncated; candidate set is incomplete', work={})
    return result


def classify_work(work: dict[str, Any], assertions: list[dict[str, Any]]) -> dict[str, Any]:
    """Return only explicit sourced categories; adult content never implies an age.

    Each assertion carries field, value, source and url. Work is context only.
    Unknown values remain None. Contradictions retain all assertions for review.
    """
    result: dict[str, Any] = {'family': None, 'genres': [], 'adult': None,
                              'recommended_age': None, 'field_conflicts': {}, 'provenance': []}
    qualified = [a for a in assertions if a.get('source') and a.get('url')]
    for field in ('family', 'adult', 'recommended_age'):
        entries = [a for a in qualified if a.get('field') == field and present(a.get('value'))]
        if field == 'family':
            entries = [a for a in entries if a['value'] in ('BD', 'Manga', 'Comics')]
        elif field == 'adult':
            entries = [a for a in entries if isinstance(a['value'], bool)]
        else:
            entries = [a for a in entries if type(a['value']) is int and 0 <= a['value'] <= 99]
        if len({normalized(a['value']) for a in entries}) > 1:
            result['field_conflicts'][field] = deepcopy(entries)
        elif entries:
            result[field] = entries[0]['value']
        result['provenance'].extend(deepcopy(entries))
    genre_entries = [a for a in qualified if a.get('field') == 'genres' and isinstance(a.get('value'), list)]
    genres = {str(value).strip() for a in genre_entries for value in a['value'] if str(value).strip()}
    result['genres'] = sorted(genres)
    result['provenance'].extend(deepcopy(genre_entries))
    adult_genres = {'adulte', 'adultes', 'adult', 'erotique', 'erotisme', 'erotic', 'erotica'}
    if any(normalized(genre) in adult_genres for genre in genres):
        if result['adult'] is False:
            result['field_conflicts']['adult'] = deepcopy(genre_entries + [a for a in qualified if a.get('field') == 'adult'])
            result['adult'] = None
        elif 'adult' not in result['field_conflicts']:
            result['adult'] = True
    return result
