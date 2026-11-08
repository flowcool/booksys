"""Bounded automatic attempts in the authoritative catalogue, with versioned evidence retries."""
from __future__ import annotations

from pathlib import Path
import time
from typing import Any, Callable

from library.catalog import Catalog, LibraryError, now, digest
from library.metadata import Providers, enrich
from library.printed import scan_printed
from library.publish import Publisher


METHOD = 'work-metadata-2026-11-08-v3'
WEEK = 7 * 86400


def evidence_version(asset: dict[str, Any]) -> str:
    return digest({key: asset.get(key) for key in ('embedded', 'printed', 'captured_hints', 'assertions', 'status')})


def ready_asset(asset: dict[str, Any], state: dict[str, Any], timestamp: float) -> bool:
    if (asset.get('resolution') or asset.get('disposition') in ('exclude', 'defer') or
            asset.get('status') != 'validated' or not any(state['origins'][origin].get('availability') == 'validated'
                                                         for origin in asset['origins'])):
        return False
    attempt = asset.get('automation', {})
    outcome = attempt.get('outcome')
    if str(attempt.get('reason', '')).startswith('Publication failed;'):
        return False
    changed = attempt.get('method') != METHOD or attempt.get('evidence') != evidence_version(asset)
    if changed:
        return True
    if outcome in ('pending', 'unavailable', 'published', 'identified'):
        return bool(attempt.get('next_attempt', float('inf')) <= timestamp)
    return bool(attempt.get('next_attempt', 0) <= timestamp)


def ready_assets(store: Catalog, *, clock: Callable[[], float] = time.time) -> int:
    state = store.read()
    return sum(ready_asset(asset, state, clock()) for asset in state['assets'].values())


def save_attempt(store: Catalog, asset: str, attempt: dict[str, Any]) -> None:
    current = store.read()
    current['assets'][asset]['automation'] = attempt
    store.save(current, expected=current['revision'])


def process_assets(store: Catalog, reception: Path, library: Path, providers: Providers,
                   *, limit: int = 1, clock: Callable[[], float] = time.time) -> dict[str, int]:
    initial = store.read()
    if limit <= 0:
        ready = ready_assets(store, clock=clock)
        return {'processed': 0, 'backlog': ready, 'ready_backlog': ready}
    completed = 0
    for key, asset in initial['assets'].items():
        if not ready_asset(asset, initial, clock()):
            continue
        attempt = dict(asset.get('automation', {}))
        if (attempt.get('method') != METHOD or attempt.get('evidence') != evidence_version(asset)
                or attempt.get('outcome') in ('pending', 'unavailable', 'published', 'identified')):
            attempt = {'attempts': 0, 'outcome': 'new'}
        if attempt.get('outcome') == 'running' and int(attempt.get('attempts', 0)) >= 3:
            attempt.update(outcome='unavailable', reason='Interrupted attempt limit reached', updated_at=now(),
                           method=METHOD, evidence=evidence_version(asset), next_attempt=clock() + WEEK)
            save_attempt(store, key, attempt)
            continue
        if completed >= limit or providers.budget <= 0:
            break
        completed += 1
        reused_identity = attempt.get('outcome') == 'identified'
        attempts = int(attempt.get('attempts', 0)) + (0 if reused_identity else 1)
        record = {'method': METHOD, 'evidence': evidence_version(asset), 'attempts': attempts, 'outcome': 'running', 'updated_at': now(),
                  'next_attempt': clock() + (300 if attempts <= 1 else 900)}
        save_attempt(store, key, record)
        event_start = len(providers.events)
        try:
            if not reused_identity:
                current = enrich(store, key, providers, [])
                value = current['assets'][key]
                work_identified = value.get('work_result', {}).get('status') == 'identified'
                if not work_identified and not value.get('identity') and asset.get('printed', {}).get('status') != 'ok':
                    try:
                        printed = scan_printed(store, key, reception)
                    except LibraryError as exc:
                        if any(fragment in str(exc).lower() for fragment in ('reader exceeded', 'reader failed', 'engine unavailable')):
                            raise OSError('Printed evidence engine unavailable') from exc
                        record.update(outcome='pending', reason='Printed source validation blocked; review required')
                        save_attempt(store, key, record)
                        continue
                    if printed['status'] != 'ok':
                        raise OSError('Printed evidence engine unavailable')
                    current = enrich(store, key, providers, [])
            else:
                current = store.read()
            value = current['assets'][key]
            events = providers.events[event_start:]
            deferred = any(event.get('reason') == 'Request budget exhausted' for event in events)
            temporary = any(event.get('status') == 'unavailable' and
                (event.get('http_status', 0) == 429 or event.get('http_status', 0) >= 500 or
                 ('reason' in event and event['reason'] != 'Request budget exhausted')) for event in events)
            identity = value.get('identity')
            work_identified = value.get('work_result', {}).get('status') == 'identified'
            record['reason'] = value.get('work_result', value.get('identity_result', {})).get('reason', 'Identity is unknown')
            if deferred and not identity and not work_identified:
                record.update(outcome='deferred', attempts=max(0, attempts - 1), next_attempt=0)
            elif temporary and not identity and not work_identified:
                record['outcome'] = 'retry' if attempts < 3 else 'unavailable'
                record['reason'] = 'Metadata source temporarily unavailable'
            else:
                record['outcome'] = 'pending'
                record['next_attempt'] = clock() + WEEK
                if work_identified:
                    record['outcome'] = 'identified'
                if identity and not value.get('contested'):
                    edition = current['editions'][identity['edition']]
                    if edition.get('selected') == identity['variant']:
                        if edition.get('publication', {}).get('asset') != key:
                            # Failed writes stop the cycle. Never resend them as transport retries.
                            try:
                                Publisher(store, reception, library, lab=True).publish(key)
                            except (LibraryError, OSError):
                                record.update(outcome='unavailable', reason='Publication failed; inspect journal before retry')
                                save_attempt(store, key, record)
                                raise
                        record.update(outcome='published', reason='Validated edition published')
        except OSError:
            if record.get('outcome') == 'unavailable':
                raise
            record.update(outcome='retry' if attempts < 3 else 'unavailable', reason='Printed evidence engine unavailable')
        if record.get('outcome') == 'unavailable':
            record['next_attempt'] = clock() + WEEK
        record['evidence'] = evidence_version(store.read()['assets'][key])
        save_attempt(store, key, record)
    current = store.read()
    backlog = sum(not asset.get('automation') or asset['automation'].get('outcome') in
                  ('running', 'retry', 'deferred') for asset in current['assets'].values()
                  if asset.get('disposition') not in ('exclude', 'defer'))
    return {'processed': completed, 'backlog': backlog, 'ready_backlog': ready_assets(store, clock=clock)}
