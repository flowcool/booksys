#!/usr/bin/env python3
"""Reproduce T1 parser qualification without mutating the runtime catalogue.

Input: a JSON list of cases with id, query, optional number/title, expected_work
(operator/fixture labels only), optional reference_url. No hosters or queue APIs.
Retained cache permits a network-free replay. --live enables <=50 cumulative
polite requests within the isolated output cache. This tool never writes CBZs.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from library.french_metadata import discover, normalized_work_label, parse_album_page, parse_album_editions
from library.french_source import QualificationSource
from library.catalog import LibraryError


def qualify(cases: list[dict[str, Any]], source: QualificationSource) -> dict[str, Any]:
    results = []
    fields: Counter[str] = Counter()
    for case in cases:
        candidates = []
        if case.get('reference_url'):
            page = source.get(case['reference_url'])
            status, reason, truncated = page.status, page.reason, False
            if status == 'ok':
                candidate = parse_album_page(page.body, page.url, page.retrieved_at)
                if not candidate.title and not candidate.series:
                    status, reason = 'unexpected_response', 'Reference page has no identity fields'
                else:
                    candidates = parse_album_editions(page.body, page.url, page.retrieved_at)
        else:
            response = discover(source, str(case['query']))
            candidates = response.candidates
            status, reason, truncated = response.status, response.reason, response.truncated
        if status == 'ok' and not case.get('reference_url'):
            selected_rows = [candidate for candidate in candidates
                if (not case.get('number') or str(case['number']) == str(candidate.number))
                and (not case.get('title') or normalized_work_label(str(case['title'])) == normalized_work_label(candidate.title or ''))]
            if case.get('enrich'):
                expanded = []
                for row in selected_rows:
                    page = source.get(row.url)
                    if page.status != 'ok':
                        status, reason, truncated = page.status, page.reason, True
                        break
                    expanded.extend(parse_album_editions(page.body, page.url, page.retrieved_at))
                if not truncated:
                    candidates = expanded
        plausible = []
        for candidate in candidates:
            if case.get('number') and str(case['number']) != str(candidate.number):
                continue
            if case.get('title') and normalized_work_label(str(case['title'])) != normalized_work_label(candidate.title or ''):
                continue
            plausible.append(candidate)
        # Same album editions can share a work key; edition identity is never
        # inferred from choosing one title/series/number. No first-record selection.
        groups = {(normalized_work_label(candidate.series or ''), candidate.number,
                   normalized_work_label(candidate.title or '')) for candidate in plausible}
        match = status == 'ok' and not truncated and len(groups) == 1 and bool(plausible)
        expected = case.get('expected_work')
        supported = list(plausible[0].fields) if match else []
        for name in supported:
            values = [candidate.fields.get(name) for candidate in plausible]
            if values and all(value == values[0] for value in values) and values[0] not in (None, '', []):
                fields[name] += 1
        correct: bool | None = None
        if match and expected:
            actual = next(iter(groups))
            correct = actual == (normalized_work_label(expected.get('series', '')), expected.get('number'),
                                 normalized_work_label(expected.get('title', '')))
        results.append({'id': case['id'], 'status': status, 'reason': reason, 'truncated': truncated,
            'work_match': match, 'correct_against_label': correct, 'label_source': case.get('label_source'),
            'input_provenance': case.get('provenance'), 'plausible_works': len(groups),
            'visible_reference_records': len(plausible),
            'candidates': [asdict(candidate) for candidate in plausible], 'exact_edition_confirmed': False})
        if status in ('unexpected_response', 'unavailable', 'budget_exhausted'):
            # Preserve dependent work. A future replay can use repaired retained
            # input, but do not keep probing after an unexpected live result.
            break
    return {'schema': 1, 'cases_requested': len(cases), 'cases_measured': len(results),
        'counts': {'work_matches': sum(row['work_match'] for row in results),
                   'correct_labelled': sum(row['correct_against_label'] is True for row in results),
                   'wrong_labelled': sum(row['correct_against_label'] is False for row in results),
                   'unlabelled_matches': sum(row['work_match'] and row['correct_against_label'] is None for row in results),
                   'abstentions': sum(not row['work_match'] for row in results), 'exact_editions': 0},
        'field_coverage_among_matches': dict(fields), 'cumulative_requests': source.request_count(),
        'limits': ['Work concordance is not exact-edition confirmation.',
                   'Unlabelled candidates are not verified real-corpus truth.',
                   'One candidate is not automatically correct without input concordance.',
                   'No downloaded archives, images or runtime catalogue were mutated.'],
        'results': results}


def evidence_inventory(path: Path) -> dict[str, Any]:
    """Read only the frozen input pack; never reopen reception or runtime state."""
    payload = path.read_bytes()
    inputs = json.loads(payload)
    counts: Counter[str] = Counter()
    rows = []
    for key, asset in inputs['assets'].items():
        embedded = asset.get('embedded') or {}
        origins = asset.get('origins') or []
        hints = asset.get('captured_hints') or {}
        member = [origin['member'] for origin in origins if origin.get('parent_origin') and origin.get('member')]
        names = member or [Path(origin['path']).name for origin in origins
                           if origin.get('path') and not origin.get('parent_origin')]
        labels = [hint['title'] for hint in hints.values() if isinstance(hint, dict) and hint.get('title')]
        flags = {'embedded_title': bool(embedded.get('Title')), 'embedded_series': bool(embedded.get('Series')),
                 'embedded_number': bool(embedded.get('Number')), 'embedded_isbn': bool(embedded.get('ISBN') or embedded.get('GTIN')),
                 'embedded_provider_url': bool(embedded.get('Web')), 'member_name': bool(member),
                 'name_hint': bool(names), 'source_label_hint': bool(labels),
                 'printed_ok': (asset.get('printed') or {}).get('status') == 'ok'}
        counts.update(field for field, available in flags.items() if available)
        rows.append({'asset': key, **flags, 'names': names, 'captured_labels': labels})
    return {'input_sha256': hashlib.sha256(payload).hexdigest(), 'assets': len(rows),
            'evidence_availability': dict(counts), 'identity_matches': 0, 'exact_editions': 0,
            'limits': ['Availability inventory only; names and source labels do not establish identity.',
                       'Actual bundle member names are retained separately; parent captions are not promoted.',
                       'Frozen JSON only; no source file or runtime catalogue reopened.'], 'rows': rows}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--input-pack', type=Path)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--budget', type=int, default=50)
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text())
    if not isinstance(cases, list) or any(not isinstance(case, dict) or not case.get('id') for case in cases):
        raise LibraryError('Expected an explicit list of qualification cases')
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    source = QualificationSource(args.output / 'cache', live=args.live, max_requests=args.budget)
    report = qualify(cases, source)
    if args.input_pack:
        report['frozen_evidence_inventory'] = evidence_inventory(args.input_pack)
    report['input_sha256'] = hashlib.sha256(args.cases.read_bytes()).hexdigest()
    (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({key: report[key] for key in ('counts', 'cases_measured', 'cumulative_requests')}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
