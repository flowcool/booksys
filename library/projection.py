"""Rebuildable lightweight operator rows, updated with catalogue changes."""
from __future__ import annotations
from pathlib import Path
from typing import Any
from library.presentation import bounded, operator_reason
from booksys_core.diagnostics import safe_text
import hashlib
import json


def digest(value: str) -> str:
    # Preserve the catalogue's canonical source reference rule.
    from library.primitives import digest as canonical
    return canonical(value)


def operator_row(oid: str, origin: dict[str, Any], asset: dict[str, Any],
                 edition: dict[str, Any], work: dict[str, Any],
                 captured: dict[str, Any]) -> dict[str, Any]:
    identity = asset.get('identity', {})
    work_result = asset.get('work_result', {})
    from library.work_identity import accepted_work, proposal_conflicts
    conflicts = proposal_conflicts(asset, work)
    work = accepted_work(asset, work, edition) or work
    overrides = asset.get('overrides', {})
    publication = edition.get('publication', {})
    published = bool(publication and publication.get('asset') == origin.get('sha256'))
    hints = list(asset.get('captured_hints', {}).values()) or [captured.get(origin['source'], {})]
    title = overrides.get('title') or work.get('title') or next((hint.get('title') for hint in hints
        if isinstance(hint, dict) and hint.get('title')), Path(origin['path']).name)
    if origin.get('parent_origin') and not identity and not work:
        title = origin.get('member', title)
    automation = asset.get('automation', {})
    result = asset.get('work_result', asset.get('identity_result', {}))
    reason = origin.get('reason') if origin.get('availability') != 'validated' else (
        automation.get('reason') or result.get('reason', 'Metadata not attempted'))
    if conflicts:
        reason = 'Work proposal conflicts with confirmed identity: ' + ', '.join(conflicts)
    if origin.get('transfer_error'):
        reason = 'Downloader reports an error; inspect pyLoad'
    candidates = [candidate for candidate in [result.get('candidate'), *result.get('candidates', [])]
                  if isinstance(candidate, dict)][:10]
    from library.printed_model import printed_isbns
    codes = sorted(printed_isbns(asset.get('printed', {}))[0])
    isbn = ', '.join(codes) or bounded(edition.get('isbn') or asset.get('embedded', {}).get('ISBN'))
    provisional = asset.get('provisional', {}).get('status') == 'published'
    admission = asset.get('admission', {})
    classified = admission.get('status') == 'classified'
    holding = admission.get('status') == 'holding'
    row = {'id': oid, 'source_id': digest(origin['source']), 'title': bounded(title), 'author': bounded(overrides.get('author') or work.get('author')),
           'isbn': isbn, 'series': bounded(overrides.get('series') or work.get('series') or asset.get('embedded', {}).get('Series')),
           'transfer': 'finished' if origin.get('transfer_status') == 0 else 'not finished (code ' + bounded(origin.get('transfer_status', 'unknown')) + ')',
           'intake': bounded(origin['availability']),
           'retirement': bounded(origin.get('retirement', 'live')),
           'ocr': bounded(asset.get('printed', {}).get('status') or
                          (asset.get('printed_attempts') or [{}])[-1].get('status', 'not attempted')),
           'identity': 'contested' if asset.get('contested') else 'identified' if identity or work else 'pending' if asset else 'not attempted',
           'edition': 'confirmed' if identity else 'unknown',
           'publication': 'classified' if classified else 'À identifier' if holding else 'published' if published else 'available_edition_unknown' if provisional else 'pending', 'reason': operator_reason(reason),
           'observed_at': bounded(origin.get('observed_at')), 'attempt_at': bounded(automation.get('updated_at')),
           'fiche': '/fiche/' + oid}
    if origin.get('retirement'):
        row['transfer'] = 'Retiré de pyLoad · dernier état : ' + row['transfer']
    row['kind'] = 'bundle' if origin.get('bundle') else 'bundle_member' if origin.get('parent_origin') else 'file'
    row['parent'] = origin.get('parent_origin', '')
    if origin.get('bundle'):
        row.update(title='Lot : ' + bounded(title), publication='not applicable', identity='not applicable', ocr='not applicable')
    elif origin.get('parent_origin'):
        row['title'] = 'Membre : ' + row['title']
    row['status'] = 'rejected' if row['intake'] == 'rejected' else 'classified' if classified else 'À identifier' if holding else 'published' if published else 'available_edition_unknown' if provisional else 'pending'
    for key, value in row.items():
        if isinstance(value, str):
            row[key] = safe_text(value) if key != 'fiche' else value
    return row
