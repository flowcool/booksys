"""Reference discovery, exact asset binding and work-level metadata orchestration."""
from __future__ import annotations

from typing import Any

from library.asset_evidence import bind_evidence
from library.catalog import LibraryError, now
from library.french_metadata import BASE, discover, parse_album_editions
from library.french_source import ProviderPage, robots_allowed
from library.work_identity import evaluate_work, classify_work


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


def enrich_work(state: dict[str, Any], key: str, providers: Any,
                extra: list[dict[str, Any]] | None = None) -> None:
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
    candidates: list[dict[str, Any]] = []
    attempts = []
    source = FrenchSource(providers)
    truncated = False
    for query in queries:
        try:
            found = discover(source, query)
            attempts.append({'query': query, 'status': found.status, 'reason': found.reason,
                             'truncated': found.truncated, 'retrieved_at': now()})
            rows = [row.to_match_candidate() for row in found.candidates]
            preliminary = evaluate_work(evidence, rows)
            matching = preliminary.get('concordant_candidates', [])
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
    candidates.extend(extra or [])
    candidates = list({(row.get('source'), row.get('edition_ref')): row for row in candidates}.values())
    evidence['discovery_truncated'] = truncated
    result = evaluate_work(evidence, candidates)
    result['attempts'] = attempts
    result['evidence'] = evidence
    result['updated_at'] = now()
    if not candidates and not evidence.get('identity_conflicts'):
        statuses = {a['status'] for a in attempts}
        result['source_status'] = ('unavailable' if statuses - {'ok', 'no_candidate'} else 'no_candidate')
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
    # Explicit work decisions are replayable and take precedence over enrichment.
    if asset.get('work_decision'):
        result = {**result, **asset['work_decision']}
    asset['work_result'] = result
