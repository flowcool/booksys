"""Explicit isolated import/export tooling. This module alone creates stores."""
from __future__ import annotations

from collections.abc import Callable
import json
import ctypes
import os
from pathlib import Path
from typing import Any

from library.primitives import TABLES, LibraryError, digest, validate, atomic_yaml, sync_dir
from library.codec import encode, state_digest


def create_activity(directory: Path, *, authority: str, timestamp: float,
                    legacy: dict[str, Any] | None = None, history_started_at: float | None = None) -> None:
    from booksys_core.diagnostics import CODES, REASONS, safe_text, opaque
    from library.activity_store import STAGES, WINDOW, MAX_EVENTS, schema as activity_schema
    # This is an explicit importer, never a runtime initialization fallback.
    from library.postgres import Database
    with Database.configured(directory).connection(write=True) as db:
        activity_schema(db)
        db.execute('INSERT INTO activity_meta VALUES (1,1,%s,%s,NULL)', (authority, timestamp if history_started_at is None else history_started_at))
        for identity, saved in (legacy or {}).items():
            stage, _, scope = identity.partition(':')
            scope = scope or ('collector' if stage in ('collection', 'byparr') else 'controller')
            if scope not in ('collector', 'observer', 'controller'):
                raise LibraryError('Invalid legacy producer scope')
            if stage not in STAGES or not isinstance(saved, dict) or not isinstance(saved.get('events', {}), dict):
                raise LibraryError('Invalid legacy Activity data')
            retained = sorted(((eid, value) for eid, value in saved.get('events', {}).items()
                               if type(value.get('at')) in (int, float) and timestamp - WINDOW <= value['at'] <= timestamp), key=lambda item: (item[1]['at'], item[0]))
            if len(retained) > MAX_EVENTS:
                db.execute('UPDATE activity_meta SET gap_before=greatest(coalesce(gap_before,0),%s)', (retained[-MAX_EVENTS - 1][1]['at'],))
            for eid, value in retained[-MAX_EVENTS:]:
                import re
                if not isinstance(eid, str) or not re.fullmatch(r'(?:[0-9a-f]{64}|[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})', eid):
                    raise LibraryError('Invalid retained Activity identity')
                reason = value.get('reason') if value.get('reason') in REASONS else 'unknown'
                code = value.get('operation') if value.get('operation') in CODES else 'progress'
                payload = {'title': safe_text(str(value.get('title', ''))), 'reason': reason,
                           'operation': code, 'state': value.get('state') if value.get('state') in
                           ('working', 'waiting', 'sleeping', 'stopped', 'blocked', 'unknown') else 'unknown'}
                db.execute('INSERT INTO activity_events VALUES (%s,%s,%s,%s,%s,%s)',
                           (eid, value['at'], stage, 'legacy', code, json.dumps(payload, allow_nan=False)))
            current = saved.get('current', {})
            if current:
                stamp = current.get('observed_at')
                if type(stamp) not in (int, float) or not 0 <= stamp <= timestamp:
                    raise LibraryError('Invalid retained producer timestamp')
                session = current.get('session', 'legacy')
                if not opaque(session):
                    raise LibraryError('Invalid retained producer session')
                state = current.get('state') if current.get('state') in ('working', 'waiting', 'sleeping', 'stopped', 'blocked', 'unknown') else 'unknown'
                reason = current.get('reason') if current.get('reason') in REASONS else 'unknown'
                code = current.get('operation') if current.get('operation') in CODES else 'progress'
                item = current.get('item', '')
                payload = {'stage': stage, 'producer_scope': scope, 'session': session,
                    'observed_at': stamp, 'progress_at': current.get('last_progress_at'),
                    'state': state, 'operation': code, 'reason': reason, 'title': safe_text(str(current.get('title', ''))),
                    'item_ref': item if not item or opaque(item) else '', 'next_attempt': current.get('next_attempt'),
                    'since': current.get('since', stamp), 'session_started': current.get('session_started', 0),
                    'pid': current.get('pid', 0), 'process_start': str(current.get('process_start', ''))[:128]}
                db.execute('INSERT INTO activity_producers VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)', (stage, scope, session, stamp,
                    payload['progress_at'], state, code, reason, json.dumps(payload, allow_nan=False)))
        db.execute('COMMIT')
    atomic_yaml(directory / 'activity-authority.yaml', {'schema': 1, 'authority_id': authority})
    os.chmod(directory / 'activity-authority.yaml', 0o400)
    sync_dir(directory)



def _retired(*args: Any, **kwargs: Any) -> Any:
    raise LibraryError('Legacy SQLite conversion and restore are retired; use the qualified PostgreSQL importer and forward recovery')


activate_catalogue = _retired
recover_yaml = _retired
import_catalogue = _retired
compact_catalogue = _retired
manifest = _retired
