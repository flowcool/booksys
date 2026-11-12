"""Bounded native-journal views. Source prose and native IDs stay out of task keys."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from booksys_core.values import seconds
from booksys_core.errors import CollectionError
from library.primitives import LibraryError, digest
from library.stage_model import instant, METHODS


def project(queue: str, value: dict[str, Any], *, auxiliary: dict[str, Any] | None = None) -> dict[str, Any]:
    items: dict[str, Any] = {}
    bindings: dict[str,Any]={}
    auxiliary = auxiliary or {}
    if value.get('version') != 1 or not isinstance(value.get('submissions'), dict) or not isinstance(value.get('categories'), dict):
        raise LibraryError('Invalid native monitor')
    deadline = value.get('schedule', {}).get('not_before')
    try:
        deadline_at = seconds(deadline) if deadline else 0
    except (CollectionError, ValueError) as exc:
        raise LibraryError('Invalid native schedule time') from exc
    due = instant(datetime.fromtimestamp(deadline_at, timezone.utc))
    blocked = 'remote_review_block' if value.get('schedule', {}).get('review_block') else ''
    if queue == 'collection':
        for source, checkpoint in value['categories'].items():
            key = 'category-' + digest(source)
            category_due = max(deadline_at,
                               datetime.fromisoformat(checkpoint['last_cycle']).timestamp() + 300 if checkpoint.get('last_cycle') else 0)
            revision = digest(checkpoint)
            bindings[key]={'kind':'category','identity':source,'original':digest(checkpoint)}
            items[key] = {'input_revision': revision, 'method': METHODS['collection'], 'due_at': instant(datetime.fromtimestamp(category_due, timezone.utc)), 'blocked': blocked}
    for source, entry in value['submissions'].items():
        if entry.get('status') != 'queued':
            continue
        fiche = auxiliary.get('items', {}).get(source, {}) if queue == 'collection' else {}
        if fiche.get('status') == 'complete':
            continue
        key = ('fiche-' if queue == 'collection' else 'source-') + digest(source)
        fiche_due = max(deadline_at, fiche.get('retry_after', 0))
        bindings[key]={'kind':'fiche' if queue=='collection' else 'submission','identity':source}
        items[key] = {'input_revision': digest(entry), 'method': METHODS[queue],
                      'due_at': instant(datetime.fromtimestamp(fiche_due, timezone.utc)) if queue == 'collection' else None, 'blocked': blocked if queue == 'collection' else ''}
    return {'items': items, '_native': {'bindings':bindings,'queue': queue, 'inputs': {key: row['input_revision'] for key, row in items.items()},
        'guards': {key: digest({'due': row.get('due_at'), 'blocked': row.get('blocked', '')}) for key, row in items.items()}}}
