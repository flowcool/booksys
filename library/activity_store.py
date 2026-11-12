"""Best-effort PostgreSQL Activity, separate from business state and locks."""
from __future__ import annotations

from contextlib import contextmanager
import json
import math
import re
from pathlib import Path
import psycopg
from typing import Any, Iterator
import uuid

from booksys_core.diagnostics import CODES, REASONS, opaque, safe_text
from library.primitives import LibraryError
from library.postgres import Database, DeadlineConnection

STAGES = ('collection', 'byparr', 'downloads', 'local', 'identity', 'retirement')
SCOPES = ('collector', 'observer', 'controller')
STATES = ('working', 'waiting', 'sleeping', 'stopped', 'blocked', 'unknown')
WINDOW = 86400
MAX_EVENTS = 8192


def valid_time(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def schema(db: DeadlineConnection) -> None:
    db.execute('CREATE TABLE activity_meta(singleton INTEGER PRIMARY KEY CHECK(singleton=1), '
               'schema INTEGER NOT NULL,authority_id TEXT NOT NULL,history_started_at DOUBLE PRECISION NOT NULL, '
               'gap_before DOUBLE PRECISION)')
    db.execute('CREATE TABLE activity_producers(stage TEXT NOT NULL,producer_scope TEXT NOT NULL,session TEXT NOT NULL,'
               'observed_at DOUBLE PRECISION NOT NULL,progress_at DOUBLE PRECISION,state TEXT NOT NULL,operation_code TEXT NOT NULL,'
               'reason_code TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(stage,producer_scope))')
    db.execute('CREATE TABLE activity_events(id TEXT PRIMARY KEY,at DOUBLE PRECISION NOT NULL,stage TEXT NOT NULL,'
               'session TEXT NOT NULL,code TEXT NOT NULL,payload TEXT NOT NULL)')
    db.execute('CREATE INDEX events_at ON activity_events(at,id)')
    db.execute('CREATE INDEX events_stage_at ON activity_events(stage,at,id)')


class ActivityStore:
    def __init__(self, directory: Path, *, authority: str, database: Database | None = None):
        self.directory = directory.absolute()
        self._database = database or Database.configured(directory)
        self.authority = authority
        self.dropped = 0

    @classmethod
    def create(cls, directory: Path, *, authority: str, database: Database,
               timestamp: float, gap_before: float | None = None) -> ActivityStore:
        """Explicit isolated/import initialization; never a runtime fallback."""
        with database.connection(write=True) as db:
            schema(db)
            db.execute('INSERT INTO activity_meta VALUES (1,1,%s,%s,%s)',
                       (authority,timestamp,gap_before))
        return cls(directory,authority=authority,database=database)

    @contextmanager
    def _open(self, *, write: bool = False) -> Iterator[DeadlineConnection]:
        with self._database.connection(write=write, seconds=.2 if write else .5, lock_cap_ms=1 if write else None) as db:
            row = db.execute('SELECT schema,authority_id FROM activity_meta WHERE singleton=1').fetchone()
            if row != (1, self.authority):
                raise LibraryError('Activity authority or schema mismatch')
            yield db

    def degradation(self) -> dict[str, int]:
        return {'dropped_count': self.dropped}

    def report(self, stage: str, scope: str, session: str, *, state: str, operation: str,
               reason: str, timestamp: float, progressed: bool = False, title: str = '',
               item_ref: str = '', next_attempt: float | None = None,
               process_start: str = '', process_id: int = 0, session_started: float = 0, operation_label: str = '') -> bool:
        if stage not in STAGES or scope not in SCOPES or state not in STATES or operation not in CODES or reason not in REASONS:
            raise ValueError('Invalid controlled Activity field')
        if (not valid_time(timestamp) or not valid_time(session_started)
                or next_attempt is not None and not valid_time(next_attempt)
                or type(process_id) is not int or process_id < 0
                or not isinstance(process_start, str) or not re.fullmatch(r'[0-9]{0,32}', process_start)
                or type(progressed) is not bool):
            raise ValueError('Invalid Activity time or process identity')
        if not opaque(session) or (item_ref and not opaque(item_ref)):
            raise ValueError('Invalid Activity reference')
        current: dict[str, Any] = {'stage': stage, 'producer_scope': scope, 'session': session,
            'observed_at': timestamp, 'state': state, 'operation': operation, 'operation_label':safe_text(operation_label), 'reason': reason,
            'title': safe_text(title), 'item_ref': item_ref, 'next_attempt': next_attempt,
            'process_start': process_start, 'pid': process_id, 'session_started': session_started}
        try:
            with self._open(write=True) as db:
                row = db.execute('SELECT payload FROM activity_producers WHERE stage=%s AND producer_scope=%s FOR UPDATE NOWAIT', (stage, scope)).fetchone()
                prior = json.loads(row[0]) if row else {}
                changed = not prior or any(current[key] != prior.get(key) for key in ('state', 'operation', 'operation_label', 'reason', 'item_ref'))
                current['since'] = timestamp if changed else prior.get('since', timestamp)
                current['progress_at'] = timestamp if progressed else prior.get('progress_at')
                current['dropped_count'] = prior.get('dropped_count', 0) + self.dropped
                payload = json.dumps(current, separators=(',', ':'), allow_nan=False)
                if len(payload.encode()) > 8192:
                    raise ValueError('Activity record exceeds bound')
                db.execute('INSERT INTO activity_producers VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(stage,producer_scope) DO UPDATE SET '
                           'session=excluded.session,observed_at=excluded.observed_at,progress_at=excluded.progress_at,'
                           'state=excluded.state,operation_code=excluded.operation_code,reason_code=excluded.reason_code,payload=excluded.payload',
                           (stage, scope, session, timestamp, current['progress_at'], state, operation, reason, payload))
                db.execute('DELETE FROM activity_events WHERE id IN (SELECT id FROM activity_events WHERE at<%s ORDER BY at,id LIMIT 256)',(timestamp-WINDOW,))
                if changed:
                    db.execute('INSERT INTO activity_events VALUES (%s,%s,%s,%s,%s,%s)',
                               (str(uuid.uuid4()), timestamp, stage, session, operation, payload))
                db.execute('COMMIT')
            self.dropped = 0
            return True
        except (psycopg.Error, LibraryError, OSError, ValueError, TypeError):
            self.dropped += 1
            return False

    def heartbeat(self, stage: str, scope: str, session: str, *, timestamp: float) -> bool:
        if stage not in STAGES or scope not in SCOPES or not opaque(session) or not valid_time(timestamp):
            raise ValueError('Invalid Activity producer')
        try:
            with self._open(write=True) as db:
                row = db.execute('SELECT payload FROM activity_producers WHERE stage=%s AND producer_scope=%s AND session=%s FOR UPDATE NOWAIT',
                                 (stage, scope, session)).fetchone()
                if row:
                    current = json.loads(row[0])
                    current['observed_at'] = timestamp
                    current['dropped_count'] = current.get('dropped_count', 0) + self.dropped
                    db.execute('UPDATE activity_producers SET observed_at=%s,payload=%s WHERE stage=%s AND producer_scope=%s AND session=%s',
                               (timestamp, json.dumps(current, allow_nan=False), stage, scope, session))
                db.execute('COMMIT')
            if row:
                self.dropped = 0
            return bool(row)
        except (psycopg.Error, LibraryError, OSError, ValueError, TypeError):
            self.dropped += 1
            return False

    def producers(self) -> dict[str, Any]:
        with self._open() as db:
            return {stage + ':' + scope: json.loads(payload) for stage, scope, payload in
                    db.execute('SELECT stage,producer_scope,payload FROM activity_producers ORDER BY stage,producer_scope')}

    def events(self, *, after: tuple[float, str] | None, limit: int = 50,
               stage: str | None = None) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 200 or stage is not None and stage not in STAGES:
            raise ValueError('Invalid Activity query')
        if after is not None and (not isinstance(after, (tuple, list)) or len(after) != 2
                                  or not valid_time(after[0]) or not isinstance(after[1], str)
                                  or len(after[1]) > 64):
            raise ValueError('Invalid Activity cursor')
        start, ident = after or (0, '')
        with self._open() as db:
            conditions = '(at,id)>(%s,%s)' + (' AND stage=%s' if stage else '')
            rows = db.execute('SELECT id,at,stage,payload FROM activity_events WHERE ' + conditions + ' ORDER BY at,id LIMIT %s',
                              (start, ident, *((stage,) if stage else ()), limit + 1)).fetchall()
            items = [{**json.loads(payload), 'id': eid, 'at': at, 'stage': name} for eid, at, name, payload in rows[:limit]]
            row = db.execute('SELECT gap_before FROM activity_meta').fetchone()
            assert row is not None
            gap = row[0]
            return {'items': items, 'cursor': (items[-1]['at'], items[-1]['id']) if len(rows) > limit else None,
                    'gap_before': gap}

    def prune(self, *, timestamp: float) -> bool:
        """At most256 deletes per pass; no business lock or heartbeat rewrite."""
        if not valid_time(timestamp):
            raise ValueError('Invalid Activity prune time')
        try:
            with self._open(write=True) as db:
                expired = [row[0] for row in db.execute('SELECT id FROM activity_events WHERE at<%s ORDER BY at,id LIMIT 256',
                                                       (timestamp - WINDOW,))]
                db.execute('DELETE FROM activity_events WHERE id=ANY(%s)', (expired,))
                remaining = 256 - len(expired)
                for stage in STAGES:
                    if not remaining:
                        break
                    count = db.execute('SELECT greatest(0,count(*)-%s) FROM activity_events WHERE stage=%s', (MAX_EVENTS, stage)).fetchone()
                    assert count is not None
                    excess = count[0]
                    if excess:
                        rows = db.execute('SELECT id,at FROM activity_events WHERE stage=%s ORDER BY at,id LIMIT %s',
                                          (stage, min(excess, remaining))).fetchall()
                        db.execute('DELETE FROM activity_events WHERE id=ANY(%s)', ([row[0] for row in rows],))
                        db.execute('UPDATE activity_meta SET gap_before=greatest(coalesce(gap_before,0),%s)', (rows[-1][1],))
                        remaining -= len(rows)
                db.execute('COMMIT')
            return True
        except (psycopg.Error, LibraryError, OSError):
            self.dropped += 1
            return False
