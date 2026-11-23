"""Reference discovery, exact asset binding and work-level metadata orchestration."""
from __future__ import annotations

from typing import Any

from library.asset_evidence import bind_evidence
from library.cover_confirmation import CoverSource, finalize
from library.primitives import LibraryError, now
from library.french_metadata import BASE, discover, parse_album_editions
from library.french_source import ProviderPage, robots_allowed
from library.names import METHOD as NAME_METHOD
from library.work_identity import (
    NUMBERED_SERIES_ALIASES, WORK_FIELDS, asset_assertions, evaluate_work, classify_work,
    informative, known_work_value, normalized, present,
)

COMPETING_NUMBERED_SERIES = {'garry': frozenset({
    'garry sergent imperia 1re serie grand format', 'garry soleil', 'garry pacifique imperia', 'garry special',
})}


def printed_concordance(evidence: dict[str, Any], asset: dict[str, Any],
                        candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Opening-page title AND creator corroboration; no rear advertisements.

    OCR locates complete reference phrases. It cannot supply edition fields or
    silently resolve a structured identity contradiction.
    """
    from copy import deepcopy
    from library.printed_model import words
    printed = (asset.get('printed') or {})
    if printed.get('status') != 'ok' or printed.get('asset') != asset['sha256']:
        return evidence
    text = ' ' + words('\n'.join(p.get('text', '') for p in printed.get('pages', [])[:4])) + ' '
    result = deepcopy(evidence)
    for candidate in candidates:
        title, author = words(candidate.get('title') or ''), words(candidate.get('author') or '')
        if len(title.split()) < 2 or not author or ' '+title+' ' not in text or ' '+author+' ' not in text:
            continue
        for field in ('title', 'series', 'number', 'integral'):
            if field == 'series' and candidate.get('field_provenance', {}).get('series', {}).get('file_series_alias'):
                continue  # The existing file series assertion was verified against this reference.
            if candidate.get(field) not in (None, ''):
                result['provenance'].setdefault(field, []).append({'value': candidate[field],
                    'scope': 'asset', 'source': 'printed:'+asset['sha256'],
                    'method': 'opening4-reference-title-and-creator-1',
                    'pages': [{k: p[k] for k in ('name', 'sha256', 'page') if k in p}
                              for p in printed.get('pages', [])[:4]]})
    return result


class FrenchSource:
    """Use the existing sequential cache and shared network budget."""
    def __init__(self, providers: Any):
        self.providers = providers

    def get(self, url: str) -> ProviderPage:
        policy = self.providers.get(BASE + '/robots.txt')
        if policy['status'] != 'ok' or 'user-agent:' not in policy.get('body', '').lower():
            return ProviderPage('unavailable', url, reason='Robots policy unavailable')
        if not robots_allowed(policy['body'], url):
            return ProviderPage('robots_disallowed', url, reason='Robots policy disallows reference')
        response = self.providers.get(url)
        return ProviderPage(response['status'], url, response.get('body', ''),
                            response.get('retrieved_at', ''), response.get('reason', ''))


def numbered_work(row: dict[str, Any]) -> tuple[str, str]:
    return normalized(row.get('series', ''), 'series'), normalized(row.get('number', ''), 'number')


def exact_bdnex_reference(evidence: dict[str, Any], candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str | None]:
    assertions = asset_assertions(evidence)
    series = {normalized(a['value'], 'series') for a in assertions
              if a['field'] == 'series' and informative(a['value'])}
    numbers = {normalized(a['value'], 'number') for a in assertions if a['field'] == 'number'}
    if len(series) != 1 or len(numbers) != 1:
        return [], None
    file_series, number = next(iter(series)), next(iter(numbers))
    allowed = {file_series, NUMBERED_SERIES_ALIASES.get(file_series, file_series)}
    numbered = {(row['url'], row['edition_ref']): row for row in candidates
                if row.get('source') == 'bedetheque'
                and row.get('provenance', {}).get('method') == 'bdnex-sitemap-1'
                and numbered_work(row)[1] == number}
    matches = [row for row in numbered.values() if numbered_work(row)[0] in allowed]
    if len(matches) != 1:
        return [], None
    row = matches[0]
    if numbered_work(row)[0] == file_series:
        return [row], None
    competing = COMPETING_NUMBERED_SERIES.get(file_series, frozenset())
    if any(numbered_work(other)[0] in competing for other in numbered.values()):
        return [], None
    return [row], file_series


def qualify_filename_titles(evidence: dict[str, Any], reference: dict[str, Any]) -> None:
    title = reference.get('title') or ''
    if not informative(title):
        return
    creators = {normalized(name) for field in ('writers', 'artists') for name in reference.get(field, [])}
    for entry in evidence['provenance'].get('title', []):
        if (entry.get('method') == NAME_METHOD and normalized(entry['value']) in creators
                and normalized(entry['value'], 'title') != normalized(title, 'title')):
            entry.update(scope='discovery', creator_reference=reference['url'])


def bind_cover(rows: list[dict[str, Any]], selected: dict[str, Any]) -> None:
    """Only the checked canonical album owns its page cover, never edition cards."""
    canonical = rows[0]
    if (canonical.get('edition_ref') == selected.get('edition_ref')
            and canonical.get('provenance', {}).get('canonical_album_ref') == selected.get('edition_ref')
            and canonical.get('url') == selected.get('url') and canonical.get('cover_url')):
        canonical['cover_binding'] = {'album_id': selected['edition_ref'], 'album_url': selected['url'],
                                      'cover_url': canonical['cover_url']}
    canonical['candidate_lineage'] = selected.get('candidate_lineage', {
        key: selected.get(key) for key in ('source', 'edition_ref', 'url', 'provenance')})


def enrich_work(state: dict[str, Any], key: str, providers: Any,
                extra: list[dict[str, Any]] | None = None, *, discovery: bool = True,
                hydrate: bool = False, exact_page_attempts: list[dict[str, str]] | None = None,
                cover_source: CoverSource | None = None) -> None:
    asset = state['assets'][key]
    evidence = bind_evidence(state, key)
    queries: list[str] = []
    # Structured fields are preferred. Unstructured labels only locate references.
    for field in ('series', 'title'):
        values = evidence.get('provenance', {}).get(field, [])
        queries.extend(str(value['value']) for value in values)
    # Labels without qualified structured identity cannot establish concordance.
    # Avoid spending public requests until there is a usable anchor.
    queries = list(dict.fromkeys(queries))[:2]
    if not queries and (asset.get('printed') or {}).get('status') == 'ok':
        queries = list(dict.fromkeys(evidence['labels'] + evidence['discovery_queries']))[:2]
    # The complete reference list is identical within a cycle for the same query.
    cache = getattr(providers, '_french_discovery', None)
    if cache is None:
        cache = {}
        providers._french_discovery = cache
    candidates: list[dict[str, Any]] = []
    event_start = len(getattr(providers, 'events', []))
    attempts = []
    if exact_page_attempts is None:
        exact_page_attempts = []
    source = FrenchSource(providers)
    truncated = False
    # Local index rows carry identity only. Enrich the concordant exact pages
    # before returning, without another series search. An outage leaves fields
    # unknown and records the existing provider retry event.
    candidates.extend(extra or [])
    if hydrate:
        local_result = evaluate_work(printed_concordance(evidence, asset, candidates), candidates)
        early_reference = local_result['status'] == 'no_match'
        matching: list[dict[str, Any]] = []
        file_series_alias = None
        if early_reference:
            matching, file_series_alias = exact_bdnex_reference(evidence, candidates)
        elif local_result['status'] == 'identified':
            matching = local_result['concordant_candidates'][:3]
        for row in matching:
            response = source.get(row['url'])
            attempts.append({'url': row['url'], 'status': response.status, 'reason': response.reason})
            page_attempt = {'edition_ref': row['edition_ref'], 'status': response.status}
            exact_page_attempts.append(page_attempt)
            if response.status != 'ok':
                continue
            try:
                page_candidates = [item.to_match_candidate() for item in
                                   parse_album_editions(response.body, row['url'], response.retrieved_at)]
            except LibraryError:
                if not early_reference:
                    raise
                attempts[-1]['status'] = 'unexpected_response'
                page_attempt['status'] = 'unexpected_response'
                continue
            page_attempt['status'] = 'parsed'
            canonical = page_candidates[0]
            bind_cover(page_candidates, row)
            if early_reference:
                if (canonical['edition_ref'] != row['edition_ref']
                        or numbered_work(canonical) != numbered_work(row)
                        or (file_series_alias and canonical['provenance'].get('canonical_album_ref') != row['edition_ref'])):
                    page_attempt['status'] = 'reference_mismatch'
                    continue
                if file_series_alias:
                    for candidate in page_candidates:
                        candidate['field_provenance']['series'] = {
                            **candidate['field_provenance']['series'], 'file_series_alias': file_series_alias,
                        }
                else:
                    qualify_filename_titles(evidence, canonical)
            candidates.extend(page_candidates)
    for query in queries if discovery else []:
        try:
            if query not in cache:
                cache[query] = discover(source, query)
            found = cache[query]
            attempts.append({'query': query, 'status': found.status, 'reason': found.reason,
                             'truncated': found.truncated, 'retrieved_at': now()})
            rows = [row.to_match_candidate() for row in found.candidates]
            preliminary = evaluate_work(printed_concordance(evidence, asset, rows), rows)
            matching = preliminary.get('concordant_candidates', [])
            # Series listings omit creators. OCR may select complete title
            # phrases for bounded page retrieval; final title+creator proof below
            # is still mandatory before identification.
            if not matching and (asset.get('printed') or {}).get('asset') == key:
                from library.printed_model import words
                text = ' '+words('\n'.join(p.get('text', '') for p in asset['printed'].get('pages', [])[:4]))+' '
                matching = [row for row in rows if len(words(row.get('title') or '').split()) >= 2
                            and ' '+words(row['title'])+' ' in text]
            if len(matching) > 3:
                truncated = True
                continue
            for row in matching:
                response = source.get(row['url'])
                if response.status != 'ok':
                    truncated = True
                    attempts.append({'url': row['url'], 'status': response.status, 'reason': response.reason})
                    continue
                editions = parse_album_editions(response.body, row['url'], response.retrieved_at)
                page_rows = [item.to_match_candidate() for item in editions]
                bind_cover(page_rows, row)
                candidates.extend(page_rows)
            truncated = truncated or found.truncated
        except LibraryError as exc:
            attempts.append({'status': 'unexpected_response', 'reason': str(exc)})
            truncated = True
    candidates = list({(row.get('source'), row.get('edition_ref')): row for row in candidates}.values())
    evidence = printed_concordance(evidence, asset, candidates)
    evidence['discovery_truncated'] = truncated
    result = evaluate_work(evidence, candidates)
    result['attempts'] = attempts
    result['exact_page_attempts'] = exact_page_attempts
    result['evidence'] = evidence
    result['updated_at'] = now()
    if not candidates and not evidence.get('identity_conflicts'):
        statuses = {a['status'] for a in attempts}
        result['source_status'] = ('unavailable' if statuses - {'ok', 'no_candidate'} else 'no_candidate')
    if any(event.get('reason') == 'Request budget exhausted'
           for event in getattr(providers, 'events', [])[event_start:]):
        result['source_status'] = 'budget_exhausted'
    save_work_result(asset, result, providers, cover_source)


def save_work_result(asset: dict[str, Any], result: dict[str, Any], providers: Any = None,
                     cover_source: CoverSource | None = None) -> None:
    finalize(asset, result, providers, cover_source)
    previous = asset.get('work_result', {})
    prior_receipt = previous.get('evidence', {}).get('cover_confirmation')
    if (result['status'] != 'identified' and prior_receipt
            and prior_receipt.get('result') == 'review'
            and 'cover_confirmation' not in result.get('evidence', {})):
        result.update(status='review', reason=previous['reason'],
                      reason_code=previous.get('reason_code', 'cover_confirmation_unavailable'), work={})
        result.setdefault('evidence', {})['cover_confirmation'] = prior_receipt
        result['candidates'] = list({(row.get('source'), row.get('edition_ref')): row
            for row in [*previous.get('candidates', []), *result.get('candidates', [])]}.values())
    if result['status'] == 'identified':
        assertions = []
        for row in result['concordant_candidates']:
            for field in ('family', 'genres', 'adult', 'recommended_age'):
                if row.get(field) is not None:
                    assertions.append({'field': field, 'value': row[field], 'source': row['source'],
                        'url': row['url'], 'retrieved_at': row.get('retrieved_at') or row.get('provenance', {}).get('retrieved_at')})
        classification = classify_work(result['work'], assertions)
        result['classification'] = classification
        result['work'].update({k: classification[k] for k in ('family', 'genres', 'adult', 'recommended_age')})
        result['field_conflicts'].update(classification['field_conflicts'])
    asset['work_result'] = result


def apply_bdnex_decision(state: dict[str, Any], key: str, providers: Any,
                        response: dict[str, Any], *, cover_source: CoverSource | None = None) -> bool:
    decision = response.get('decision', {})
    decision_status = decision.get('status')
    if decision_status not in ('selected', 'proposed', 'review') or decision.get('reason') == 'insufficient':
        return False
    asset = state['assets'][key]
    evidence = bind_evidence(state, key)
    candidates = response['candidates']
    result: dict[str, Any] = {
        'status': 'review', 'reason': 'BDneX found no decisive reference', 'work': {},
        'field_conflicts': {}, 'provenance': [], 'candidates': candidates, 'concordant_candidates': [],
        'evidence': evidence, 'attempts': [], 'exact_page_attempts': [], 'updated_at': now(),
        'bdnex_decision': decision,
    }
    if decision_status in ('selected', 'proposed'):
        selected = next(row for row in candidates if row['edition_ref'] == decision['edition_ref'])
        previous = asset.get('work_result', {})
        retained = previous.get('concordant_candidates', []) if previous.get('bdnex_decision') == decision else []
        row = retained[0] if retained else selected
        page = FrenchSource(providers).get(selected['url'])
        status = page.status
        if status == 'ok':
            try:
                editions = [edition.to_match_candidate() for edition in
                            parse_album_editions(page.body, selected['url'], page.retrieved_at)]
                canonical = editions[0]
            except LibraryError:
                status = 'unexpected_response'
            else:
                if canonical['provenance'].get('canonical_album_ref') != selected['edition_ref']:
                    status = 'reference_mismatch'
                else:
                    status = 'parsed'
                    bind_cover(editions, selected)
                    row = dict(selected)
                    row.pop('cover_binding', None)
                    if 'cover_binding' in canonical:
                        row['cover_binding'] = canonical['cover_binding']
                    row['candidate_lineage'] = canonical['candidate_lineage']
                    # Keep source identity contradictions visible to the finalizer.
                    row['canonical_identity'] = {field: canonical.get(field) for field in WORK_FIELDS}
                    for field in WORK_FIELDS:
                        value = canonical.get(field)
                        if present(value) and all(
                            normalized(edition.get(field), field) == normalized(value, field)
                            for edition in editions
                        ):
                            if not present(selected.get(field)) or normalized(selected[field], field) == normalized(value, field):
                                row[field] = value
                                field_source = canonical.get('field_provenance', {}).get(field)
                                if field_source:
                                    row.setdefault('field_provenance', {})[field] = field_source
        result['attempts'] = [{'url': selected['url'], 'status': status, 'reason': page.reason}]
        result['exact_page_attempts'] = [{'edition_ref': selected['edition_ref'], 'status': status}]
        if status != 'parsed':
            # A retained pre-guard page is not current authority after a failed fetch.
            row.pop('cover_binding', None)
        result.update(
            status='identified', reason=f'BDneX {decision_status} one reference', concordant_candidates=[row],
            work={field: row[field] for field in WORK_FIELDS if known_work_value(field, row.get(field))},
            provenance=[{'source': selected['source'], 'edition_ref': selected['edition_ref'],
                         'url': selected['url'], 'score': selected['score'],
                         'decision': decision['reason'], **selected.get('provenance', {})}],
        )
    save_work_result(asset, result, providers, cover_source)
    return True
