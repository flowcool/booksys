"""Classify listed periodical issues using bound file evidence."""
from __future__ import annotations

from typing import Any

from library.primitives import now
from library.work_identity import asset_assertions, normalized

PERIODICAL_TITLES = {
    'fillette': 'Fillette',
    'fluide glacial': 'Fluide Glacial',
    'nouveau tintin': 'Nouveau Tintin',
    'tintin hebdoptimiste': 'Tintin Hebdoptimiste',
}


def periodical_result(evidence: dict[str, Any]) -> dict[str, Any] | None:
    claims = [claim for claim in asset_assertions(evidence) if claim['field'] in ('series', 'number')]
    series = {normalized(claim['value'], 'series') for claim in claims if claim['field'] == 'series'}
    if not series & PERIODICAL_TITLES.keys():
        return None
    numbers = {normalized(claim['value'], 'number') for claim in claims if claim['field'] == 'number'}
    numbers.discard('')
    work = {}
    if len(series) == len(numbers) == 1:
        work = {'series': PERIODICAL_TITLES[next(iter(series))], 'number': next(iter(numbers))}
    return {
        'status': 'periodical' if work else 'review', 'work': work,
        'reason': 'Periodical issue named from file' if work else 'Periodical needs one file title and one issue number',
        'reason_code': 'periodical_file_evidence' if work else 'periodical_issue_uncertain',
        'provenance': claims, 'evidence': evidence, 'candidates': [],
        'concordant_candidates': [], 'field_conflicts': {}, 'updated_at': now(),
    }
