"""Bounded private candidate protocol, shared by the worker and matching container."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

PIN = '9de29a73859a527a0c8ab4b0173d2f1e0e1e6f33'
MIN_CANDIDATE_SCORE = 60
STRONG_SCORE = 90
IDENTITY_KEYS = frozenset({'title', 'series', 'number', 'integral'})
MAX_BODY = 16384
MAX_RESPONSE = 65536


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def validate_request(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get('schema') != 1:
        raise ValueError('Unsupported request')
    if not isinstance(value.get('asset'), str) or not re.fullmatch('[a-f0-9]{64}', value['asset']):
        raise ValueError('Invalid asset')
    inputs = value.get('inputs')
    if not isinstance(inputs, list) or not 1 <= len(inputs) <= 3:
        raise ValueError('Invalid inputs')
    for item in inputs:
        if not isinstance(item, dict):
            raise ValueError('Invalid input')
        path = item.get('path')
        if (not isinstance(path, str) or not 1 <= len(path) <= 1024 or path.startswith('/')
                or '\\' in path or any(ord(c) < 32 for c in path)
                or any(part in ('', '.', '..') for part in path.split('/'))):
            raise ValueError('Invalid logical path')
        hints = item.get('embedded')
        if not isinstance(hints, dict) or hints.keys() - {'Web', 'Series', 'Number', 'Volume'}:
            raise ValueError('Invalid hints')
        if any(not isinstance(v, str) or len(v) > 2048 for v in hints.values()):
            raise ValueError('Invalid hint value')
    identity = value.get('identity', {})
    if not isinstance(identity, dict) or identity.keys() - IDENTITY_KEYS:
        raise ValueError('Invalid identity hints')
    for hints in identity.values():
        if not isinstance(hints, list) or not 1 <= len(hints) <= 2 or any(
            not isinstance(hint, str) or not 1 <= len(hint) <= 512 for hint in hints
        ) or len(set(hints)) != len(hints):
            raise ValueError('Invalid identity hint values')
    return value


def validate_candidates(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list) or len(rows) > 15:
        raise ValueError('Invalid candidates')
    for row in rows:
        if not isinstance(row, dict) or row.get('source') != 'bedetheque':
            raise ValueError('Invalid candidate source')
        ref, url = row.get('edition_ref'), row.get('url')
        if (not isinstance(ref, str) or not ref.isdigit() or not isinstance(url, str)
                or not re.fullmatch(r'https://www\.bedetheque\.com/BD-[^/\s?#]+-' + re.escape(ref) + r'\.html', url)):
            raise ValueError('Invalid candidate reference')
        score = row.get('score')
        if isinstance(score, bool) or not isinstance(score, (float, int)) or not MIN_CANDIDATE_SCORE <= score <= 100:
            raise ValueError('Invalid candidate score')
        if row.keys() - {'source', 'edition_ref', 'url', 'score', 'title', 'series', 'number'}:
            raise ValueError('Unsupported candidate field')
        for key in ('title', 'series', 'number'):
            if row.get(key) is not None and (not isinstance(row[key], str) or len(row[key]) > 2048):
                raise ValueError('Invalid candidate field')
    return rows



def validate_decision(decision: Any, rows: list[dict[str, Any]]) -> None:
    if isinstance(decision, dict):
        reason = decision.get('reason')
        if (decision.keys() == {'status', 'reason'} and decision['status'] == 'review' and rows
                and reason in ('ambiguous', 'insufficient', 'conflicting_file_evidence')):
            return
        if (decision.keys() == {'status', 'reason', 'edition_ref'} and decision['status'] == 'selected'
                and reason in ('strong', 'numbered')):
            chosen = [row for row in rows if row['edition_ref'] == decision['edition_ref']]
            if len(chosen) == 1 and (reason != 'strong' or chosen[0]['score'] >= STRONG_SCORE):
                return
    raise ValueError('Invalid BDneX decision')


def valid_match_counts(counts: Any, candidate_count: int) -> bool:
    return (isinstance(counts, dict) and counts.keys() == {'native', 'filtered'}
            and all(type(count) is int for count in counts.values())
            and 0 <= counts['filtered'] <= counts['native'] <= 15
            and candidate_count <= counts['native'] - counts['filtered'])
