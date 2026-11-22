"""Worker-side cover evidence. Native imports belong exclusively to the child."""
from __future__ import annotations

import base64
import hashlib
import math
from pathlib import Path
import tempfile
import time
from collections.abc import Callable
from typing import Any

from library import decode
from library.cover_sift import METHOD, THRESHOLD, FUNCTION_VERSION, PREPROCESSING
from library.primitives import LibraryError, digest
from library.work_identity import asset_assertions, creator_names, informative, normalized, present, evaluate_work

CoverSource = Callable[[], Path]


def finalize(asset: dict[str, Any], result: dict[str, Any], providers: Any,
             source: CoverSource | None = None) -> None:
    """Finalize every automatic Bedetheque proposal before classification.

    A visual pass can confirm only the one reference selected by textual rules.
    Receipt reuse is limited to this evaluation's exact evidence and inputs.
    """
    if result['status'] != 'identified':
        return
    rows = result.get('concordant_candidates', [])
    if not any(row.get('source') == 'bedetheque' for row in rows):
        return
    evidence = result.setdefault('evidence', {})
    receipt: dict[str, Any] = {'version': 1, 'asset_sha256': asset['sha256'],
        'method': METHOD, 'function_version': FUNCTION_VERSION, 'preprocessing': PREPROCESSING,
        'opencv': None, 'numpy': None, 'threshold': THRESHOLD, 'score': None, 'result': 'review',
        'reason': 'reference_not_eligible', 'member': None, 'input_sha256': None,
        'reference_sha256': None, 'normalized_input_sha256': None,
        'normalized_reference_sha256': None}

    def review(reason: str) -> None:
        receipt.update(result='review', reason=reason)
        result.update(status='review', reason='Cover confirmation: ' + reason,
                      reason_code='cover_' + reason, work={})

    evidence['cover_confirmation'] = receipt
    if evidence.get('asset') != asset['sha256'] or evidence.get('asset_bound') is not True:
        review('asset_binding_missing')
        return
    # Preserve the pure evaluator's conflicts, dated-series and title veto rules.
    checked = evaluate_work(evidence, rows)
    if checked['status'] != 'identified':
        review('identity_conflict')
        return
    if evidence.get('discovery_truncated') or evidence.get('identity_conflicts'):
        review('incomplete_evidence')
        return
    references = {(row.get('source'), row.get('edition_ref'), row.get('url')) for row in rows}
    row = rows[0]
    canonical = row.get('canonical_identity')
    if canonical and evaluate_work(evidence, [canonical])['status'] != 'identified':
        review('identity_conflict')
        return
    receipt.update(album_id=row.get('edition_ref'), album_url=row.get('url'))
    assertions = asset_assertions(evidence)
    # All title assertions must agree. Repeated series labels are not album titles.
    titles = [a for a in assertions if a['field'] == 'title' and informative(a['value'])
              and str(a.get('source', '')).startswith(('origin:', 'embedded:' + asset['sha256'],
                                                        'printed:' + asset['sha256']))]
    title = row.get('title')
    series_labels = {normalized(a['value'], 'title') for a in assertions if a['field'] == 'series'}
    series_labels.add(normalized(row.get('series', ''), 'title'))
    title_agrees = (informative(title) and normalized(title, 'title') not in series_labels
                    and bool(titles) and all(normalized(a['value'], 'title') == normalized(title, 'title') for a in titles))
    credits = creator_names(row)
    credit_basis = []
    for role in ('writer', 'penciller'):
        entries = [a for a in evidence.get('provenance', {}).get(role, [])
                   if a.get('scope') == 'asset' and str(a.get('source', '')).startswith('embedded:' + asset['sha256'])]
        names = {normalized(a['value']) for a in entries if present(a.get('value'))}
        for profile in (credits, creator_names(canonical or {})):
            if names and profile[role] and names != profile[role]:
                review('creator_conflict')
                return
        if names and names == credits[role]:
            credit_basis.extend(entries)
    if title_agrees or credit_basis:
        receipt.update(result='exempt', reason='asset_title' if title_agrees else 'asset_creator',
                       assertions=titles if title_agrees else credit_basis)
        return
    if len(references) != 1:
        review('reference_not_unique')
        return
    prior = asset.get('work_result', {}).get('evidence', {}).get('cover_confirmation', {})
    if (prior.get('result') == 'review' and prior.get('album_id')
            and (prior['album_id'], prior.get('album_url')) != (row.get('edition_ref'), row.get('url'))):
        review('reference_changed_after_failure')
        return
    # Neither a strong score nor a captured heading is an independent anchor.
    bound = {a['field'] for a in assertions if present(a['value'])
             and (a['field'] != 'series' or informative(a['value']))}
    if not {'series', 'number'} <= bound:
        review('missing_series_number')
        return
    binding = row.get('cover_binding', {})
    if (not binding or binding.get('album_id') != row.get('edition_ref')
            or binding.get('album_url') != row.get('url')):
        review('canonical_cover_unavailable')
        return
    receipt.update(reference_url=binding.get('cover_url'), reference_retrieved_at=None,
                   order_policy=asset.get('reader_policy'))
    try:
        if source is None:
            review('archive_unavailable')
            return
        manifest = asset.get('page_manifest', [])
        if not manifest:
            review('manifest_unavailable')
            return
        receipt['member'] = manifest[0]
        response = providers.get_binary(binding['cover_url'])
        receipt['reference_retrieved_at'] = response.get('retrieved_at')
        if response['status'] != 'ok':
            review('reference_unavailable')
            return
        reference = response['body']
        receipt['reference_sha256'] = hashlib.sha256(reference).hexdigest()
        archive_path = source()  # Existing resolver rechecks source/parent hashes even on reuse.
        # Failures and passes within a fallback chain reuse only identical inputs.
        reuse_key = digest({'asset': asset['sha256'], 'evidence': {k: v for k, v in evidence.items()
            if k != 'cover_confirmation'}, 'member': manifest[0], 'policy': asset.get('reader_policy'),
            'binding': binding, 'reference': receipt['reference_sha256'], 'method': METHOD, 'threshold': THRESHOLD})
        cache = getattr(providers, '_cover_confirmations', None)
        if cache is None:
            cache = providers._cover_confirmations = {}
        if reuse_key in cache and cache[reuse_key]['expires'] > time.time():
            receipt.update(cache[reuse_key]['receipt'])
        else:
            with tempfile.TemporaryDirectory(dir=decode.WORK_ROOT) as directory:
                extracted = decode.run('first_page', archive_path, {'asset_sha256': asset['sha256'],
                    'reader_policy': asset.get('reader_policy'), 'member': manifest[0]}, destination=Path(directory))
                if extracted.get('status') != 'extracted' or extracted.get('input_sha256') != asset['sha256']:
                    review('extraction_unavailable')
                    return
                original = (Path(directory) / 'first-page.img').read_bytes()
                if hashlib.sha256(original).hexdigest() != manifest[0]['sha256']:
                    review('member_hash_drift')
                    return
                compared = compare(original, reference)
            # A native failure can recover without restarting the metadata worker.
            cache[reuse_key] = {'receipt': compared,
                'expires': time.time() + (7 * 86400 if compared['result'] == 'pass' else 300)}
            receipt.update(compared)
        if receipt['result'] != 'pass':
            review(receipt['reason'])
    except (LibraryError, OSError, ValueError, KeyError, TypeError):
        review('confirmation_unavailable')


def compare(original: bytes, reference: bytes) -> dict[str, Any]:
    receipt: dict[str, Any] = {'version': 1, 'method': METHOD, 'threshold': THRESHOLD,
        'function_version': FUNCTION_VERSION, 'preprocessing': PREPROCESSING, 'opencv': None, 'numpy': None,
        'input_sha256': hashlib.sha256(original).hexdigest(),
        'reference_sha256': hashlib.sha256(reference).hexdigest(),
        'normalized_input_sha256': None, 'normalized_reference_sha256': None,
        'score': None, 'result': 'review', 'reason': 'comparison_unavailable'}
    try:
        with tempfile.TemporaryDirectory(dir=decode.WORK_ROOT) as directory:
            source = Path(directory) / 'first.img'
            source.write_bytes(original)
            result = decode.run('cover_compare', source,
                                {'reference': base64.b64encode(reference).decode('ascii')})
        if result.get('status') != 'compared':
            receipt['reason'] = result.get('cover_reason', 'comparison_unavailable')
            return receipt
        score = result['score']
        if (isinstance(score, bool) or not isinstance(score, (float, int))
                or not math.isfinite(score) or not 0 <= score <= 100
                or result.get('input_sha256') != receipt['input_sha256']
                or result.get('reference_sha256') != receipt['reference_sha256']):
            raise LibraryError('Invalid comparison receipt')
        receipt.update({key: result[key] for key in ('score', 'opencv', 'numpy',
            'normalized_input_sha256', 'normalized_reference_sha256',
            'wall_seconds', 'cpu_seconds', 'peak_rss_kib')})
        receipt.update(result='pass' if score >= THRESHOLD else 'review',
                       reason='cover_agreement' if score >= THRESHOLD else 'below_threshold')
    except (LibraryError, OSError, ValueError, KeyError):
        receipt['reason'] = 'comparison_unavailable'
    return receipt
