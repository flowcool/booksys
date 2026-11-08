"""Bounded automatic attempts in the authoritative catalogue, without identity retries."""
from __future__ import annotations

from pathlib import Path
import time
from typing import Any, Callable

from library.catalog import Catalog, LibraryError, now
from library.metadata import Providers, enrich
from library.printed import scan_printed
from library.publish import Publisher


def save_attempt(store: Catalog, asset: str, attempt: dict[str, Any]) -> None:
    current = store.read()
    current['assets'][asset]['automation'] = attempt
    store.save(current, expected=current['revision'])


def process_assets(store: Catalog, reception: Path, library: Path, providers: Providers,
                   *, limit: int = 8, clock: Callable[[], float] = time.time) -> dict[str, int]:
    initial = store.read()
    completed = 0
    for key, asset in initial['assets'].items():
        if asset.get('resolution'):
            continue  # Operator application owns its retries and source evidence.
        attempt = dict(asset.get('automation', {}))
        # Delivered evidence is reused. An uncertain identity is not a transport error.
        if not attempt and asset.get('identity_result'):
            attempt = {'attempts': 0, 'outcome': 'identified' if asset.get('identity') else 'pending',
                       'reason': asset['identity_result'].get('reason', 'Prior metadata attempt'),
                       'updated_at': now(), 'reused': True}
            save_attempt(store, key, attempt)
        if attempt.get('outcome') == 'running' and int(attempt.get('attempts', 0)) >= 3:
            attempt.update(outcome='unavailable', reason='Interrupted attempt limit reached', updated_at=now())
            save_attempt(store, key, attempt)
        if attempt.get('outcome') in ('pending', 'unavailable', 'published', 'identified'):
            if attempt.get('outcome') != 'identified':
                continue
        elif attempt.get('next_attempt', 0) > clock():
            continue
        if asset.get('disposition') in ('exclude', 'defer'):
            continue
        if not any(initial['origins'][origin].get('availability') == 'validated' for origin in asset['origins']):
            continue
        if completed >= limit or providers.budget <= 0:
            break
        completed += 1
        reused_identity = attempt.get('outcome') == 'identified'
        attempts = int(attempt.get('attempts', 0)) + (0 if reused_identity else 1)
        record = {'attempts': attempts, 'outcome': 'running', 'updated_at': now(),
                  'next_attempt': clock() + (300 if attempts <= 1 else 900)}
        save_attempt(store, key, record)
        event_start = len(providers.events)
        try:
            if not reused_identity:
                if asset.get('printed', {}).get('status') != 'ok':
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
            record['reason'] = value.get('identity_result', {}).get('reason', 'Identity is unknown')
            if deferred and not identity:
                record.update(outcome='deferred', attempts=max(0, attempts - 1), next_attempt=0)
            elif temporary and not identity:
                record['outcome'] = 'retry' if attempts < 3 else 'unavailable'
                record['reason'] = 'Metadata source temporarily unavailable'
            else:
                record['outcome'] = 'pending'
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
        save_attempt(store, key, record)
    current = store.read()
    backlog = sum(not asset.get('automation') or asset['automation'].get('outcome') in
                  ('running', 'retry', 'deferred') for asset in current['assets'].values()
                  if asset.get('disposition') not in ('exclude', 'defer'))
    return {'processed': completed, 'backlog': backlog}
