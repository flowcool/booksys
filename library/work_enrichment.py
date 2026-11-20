"""Reference discovery, exact asset binding and work-level metadata orchestration."""
from __future__ import annotations

from typing import Any

from library.asset_evidence import bind_evidence
from library.primitives import LibraryError, now
from library.french_metadata import BASE, discover, parse_album_editions
from library.french_source import ProviderPage, robots_allowed
from library.work_identity import asset_assertions, evaluate_work, classify_work, informative, normalized


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


def exact_bdnex_reference(evidence: dict[str, Any], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    assertions = asset_assertions(evidence)
    series = {normalized(a['value'], 'series') for a in assertions
              if a['field'] == 'series' and informative(a['value'])}
    numbers = {normalized(a['value'], 'number') for a in assertions if a['field'] == 'number'}
    if len(series) != 1 or len(numbers) != 1:
        return []
    bound = next(iter(series)), next(iter(numbers))
    matches = {row['url']: row for row in candidates
               if row.get('source') == 'bedetheque'
               and row.get('provenance', {}).get('method') == 'bdnex-sitemap-1'
               and numbered_work(row) == bound}
    return list(matches.values()) if len(matches) == 1 else []


def enrich_work(state: dict[str, Any], key: str, providers: Any,
                extra: list[dict[str, Any]] | None = None, *, discovery: bool = True,
                hydrate: bool = False) -> None:
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
    source = FrenchSource(providers)
    truncated = False
    # Local index rows carry identity only. Enrich the concordant exact pages
    # before returning, without another series search. An outage leaves fields
    # unknown and records the existing provider retry event.
    candidates.extend(extra or [])
    if hydrate:
        local_result = evaluate_work(printed_concordance(evidence, asset, candidates), candidates)
        early_reference = local_result['status'] == 'no_match'
        matching = []
        if early_reference:
            matching = exact_bdnex_reference(evidence, candidates)
        elif local_result['status'] == 'identified':
            matching = local_result['concordant_candidates'][:3]
        for row in matching:
            response = source.get(row['url'])
            attempts.append({'url': row['url'], 'status': response.status, 'reason': response.reason})
            if response.status != 'ok':
                continue
            try:
                page_candidates = [item.to_match_candidate() for item in
                                   parse_album_editions(response.body, row['url'], response.retrieved_at)]
            except LibraryError:
                if not early_reference:
                    raise
                attempts[-1]['status'] = 'unexpected_response'
                continue
            canonical = page_candidates[0]
            if not early_reference or (canonical['edition_ref'] == row['edition_ref']
                                       and numbered_work(canonical) == numbered_work(row)):
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
                candidates.extend(item.to_match_candidate() for item in editions)
            truncated = truncated or found.truncated
        except LibraryError as exc:
            attempts.append({'status': 'unexpected_response', 'reason': str(exc)})
            truncated = True
    candidates = list({(row.get('source'), row.get('edition_ref')): row for row in candidates}.values())
    evidence = printed_concordance(evidence, asset, candidates)
    evidence['discovery_truncated'] = truncated
    result = evaluate_work(evidence, candidates)
    result['attempts'] = attempts
    result['evidence'] = evidence
    result['updated_at'] = now()
    if not candidates and not evidence.get('identity_conflicts'):
        statuses = {a['status'] for a in attempts}
        result['source_status'] = ('unavailable' if statuses - {'ok', 'no_candidate'} else 'no_candidate')
    if any(event.get('reason') == 'Request budget exhausted'
           for event in getattr(providers, 'events', [])[event_start:]):
        result['source_status'] = 'budget_exhausted'
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
