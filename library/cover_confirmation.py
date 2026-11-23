"""Worker-side cover evidence. Native imports belong exclusively to the child."""
from __future__ import annotations

import base64
import hashlib
import math
import re
from pathlib import Path
import tempfile
import time
from collections.abc import Callable
from typing import Any

from library import decode
from library.asset_evidence import file_years
from library.bdnex_protocol import bedetheque_album_id
from library.cover_sift import METHOD, THRESHOLD, FUNCTION_VERSION, PREPROCESSING
from library.primitives import LibraryError, digest
from library.work_identity import (
    NUMBERED_SERIES_ALIASES, asset_assertions, creator_names, informative, normalized, present,
)

CoverSource = Callable[[], Path]


def identity_conflict(evidence: dict[str, Any], row: dict[str, Any], *, indexed: bool) -> bool:
    """Check structured contradictions; absent album titles remain unconfirmed.

    An index prefix is retrieval evidence only. It grants no exemption and cannot
    absorb a different numbered series or a contradictory date/title/number.
    """
    assertions = asset_assertions(evidence)
    fields = {a['field'] for a in assertions}
    bound: dict[str, Any] = {}
    for field in fields:
        values = {normalized(a['value'], field) for a in assertions if a['field'] == field}
        if len(values) > 1:
            return True
        bound[field] = next(iter(values))
    series = normalized(row.get('series') or '', 'series')
    title = normalized(row.get('title') or '', 'title')
    for field, value in bound.items():
        candidate = row.get(field)
        if not present(candidate):
            continue
        other = normalized(candidate, field)
        if field == 'title':
            if not informative(value) or not informative(candidate) or title == series:
                continue
        elif field == 'series' and value != other:
            prefix = indexed and other.startswith(value + ' ')
            alias = (NUMBERED_SERIES_ALIASES.get(value) == other
                     and row.get('field_provenance', {}).get('series', {}).get('file_series_alias') == value)
            dated = re.fullmatch(re.escape(value) + r' (?:19|20)\d{2}', other)
            if ((prefix or alias or dated) and present(bound.get('number'))
                    and bound['number'] == normalized(row.get('number'), 'number')):
                continue
        if value != other:
            return True
    years = file_years(evidence)
    suffix = series[len(bound.get('series', '')):] if series.startswith(bound.get('series', '')) else ''
    dates = set(re.findall(r'\b(?:18|19|20)\d{2}\b', suffix))
    return bool(dates and years and dates != years)


def work_slug(row: dict[str, Any]) -> str | None:
    """Resolve a retained edition card through its parser-bound album URL.

    Edition-record ids and canonical album ids use different namespaces. Never
    reinterpret the former as the latter or fetch a page to repair absent proof.
    """
    url = row.get('url')
    if bedetheque_album_id(url) is not None and bedetheque_album_id(url) == row.get('edition_ref'):
        return str(url)
    provenance = row.get('provenance', {})
    if (row.get('edition_record') is True and provenance.get('edition_record_id') == row.get('edition_ref')
            and url == 'https://www.bedetheque.com/album/index/i/' + str(row.get('edition_ref'))
            and bedetheque_album_id(provenance.get('url'))):
        return str(provenance['url'])
    return None


def slug_identity(url: str) -> dict[str, str]:
    stem = url.split('/BD-', 1)[1].rsplit('-', 1)[0]
    match = re.fullmatch(r'(.+)-Tome-([0-9]+)(?:-(.*))?', stem)
    if match:
        return {'series': normalized(match[1], 'series'), 'number': normalized(match[2], 'number'),
                'title': normalized(match[3] or '', 'title')}
    # Unstructured slugs can prove equality, but cannot locate a difference in
    # series, number or title. Keep a differing unstructured slug unresolved.
    return {'stem': normalized(stem)}


def web_comparison(web: str, row: dict[str, Any]) -> dict[str, Any]:
    candidate_url = work_slug(row)
    same_album = candidate_url is not None and bedetheque_album_id(web) == bedetheque_album_id(candidate_url)
    # A retained Web label can predate an index rename of this exact album.
    # Resolve that id to the candidate's current slug before comparing works.
    left = slug_identity(candidate_url if same_album and candidate_url else web)
    right = slug_identity(candidate_url) if candidate_url else None
    same = left == right
    conflict = (right is not None and 'stem' not in left and 'stem' not in right
                and any(left[field] and right[field] and left[field] != right[field]
                        for field in ('series', 'number', 'title')))
    return {'web_album_id': bedetheque_album_id(web), 'candidate_album_id': row.get('edition_ref'),
            'web_url': web, 'candidate_url': row.get('url'), 'resolved_album_url': candidate_url,
            'web_work': left, 'candidate_work': right,
            'result': 'same_work' if same else 'different_work' if conflict else 'unresolved',
            'basis': 'same_album_reference' if same_album else
                     'normalized_index_slug' if candidate_url else 'missing_album_slug'}


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
        'reason': 'reference_not_eligible', 'basis': None, 'member': None, 'input_sha256': None,
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
    indexed = all(row.get('provenance', {}).get('method') == 'bdnex-sitemap-1'
                  or (row.get('candidate_lineage', {}).get('provenance') or {}).get('method') == 'bdnex-sitemap-1'
                  for row in rows)
    if any(identity_conflict(evidence, row, indexed=indexed) for row in rows):
        review('identity_conflict')
        return
    if evidence.get('discovery_truncated') or evidence.get('identity_conflicts'):
        review('incomplete_evidence')
        return
    references = {(row.get('source'), row.get('edition_ref'), row.get('url')) for row in rows}
    row = rows[0]
    canonical = row.get('canonical_identity')
    if canonical and identity_conflict(evidence, canonical, indexed=indexed):
        review('identity_conflict')
        return
    receipt.update(album_id=row.get('edition_ref'), album_url=row.get('url'))
    web = [a for a in evidence.get('provenance', {}).get('web', [])
           if a.get('scope') == 'asset' and a.get('source') == 'embedded:' + asset['sha256']
           and bedetheque_album_id(a.get('value'))]
    comparisons = [web_comparison(a['value'], candidate) for a in web for candidate in rows]
    if comparisons:
        receipt['web_comparisons'] = comparisons
    if any(item['result'] == 'different_work' for item in comparisons):
        review('web_conflict')
        return
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
    # Page failure blocks every exemption, including retained title/Web evidence.
    page_statuses = [a['status'] for a in result.get('exact_page_attempts', [])
                     if a.get('edition_ref') == row.get('edition_ref')]
    if any(status != 'parsed' for status in page_statuses):
        review('page_' + page_statuses[-1])
        return
    require_cover = result.get('bdnex_decision', {}).get('reason') == 'dated_unique'
    if web:
        if any(item['result'] == 'unresolved' for item in comparisons):
            review('web_unresolved')
            return
        if not page_statuses and row.get('provenance', {}).get('canonical_album_ref') != row.get('edition_ref'):
            review('page_unavailable')
            return
        if not require_cover:
            receipt.update(result='exempt', reason='asset_web', basis='web', assertions=web)
            return
    if not require_cover and (title_agrees or credit_basis):
        receipt.update(result='exempt', reason='asset_title' if title_agrees else 'asset_creator',
                       basis='title' if title_agrees else 'creator',
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
        else:
            receipt['basis'] = 'cover'
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
