"""Work items: one row per piece of work, state in real columns (state-column design).

No payload, no receipt, no guard, no generation. A worker takes one item with one statement
(FOR UPDATE SKIP LOCKED) and finishes it with a fencing check: the claim is (claimed_by, attempts).
A worker that lost its claim cannot finish the item. One event row per state change.
"""
from __future__ import annotations

import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from library.postgres import Database
from library.primitives import LibraryError

STATES = ('ready', 'claimed', 'waiting', 'blocked', 'done', 'failed', 'superseded')
NAME = re.compile(r'[a-z][a-z0-9_-]{0,31}')

DDL = """
CREATE TABLE IF NOT EXISTS work_items(
  stage       text COLLATE "C" NOT NULL,
  item        text COLLATE "C" NOT NULL,
  state       text COLLATE "C" NOT NULL DEFAULT 'ready'
              CHECK (state IN ('ready','claimed','waiting','blocked','done','failed','superseded')),  -- blocked: not claimable
  ready_at    timestamptz NOT NULL DEFAULT now(),   -- age for ordering: oldest first, survives a wait or a retry
  state_at    timestamptz NOT NULL DEFAULT now(),
  input_rev   text COLLATE "C" NOT NULL,
  method      text COLLATE "C" NOT NULL,
  source      text COLLATE "C" NOT NULL DEFAULT '',
  attempts    integer NOT NULL DEFAULT 0,
  last_error  text COLLATE "C" NOT NULL DEFAULT '',
  wait_reason text COLLATE "C" NOT NULL DEFAULT '',
  next_at     timestamptz NOT NULL DEFAULT now(),
  claimed_by  text COLLATE "C",
  claimed_at  timestamptz,
  completed_at timestamptz,
  PRIMARY KEY (stage, item));
CREATE INDEX IF NOT EXISTS work_ready ON work_items(stage, ready_at, item) WHERE state IN ('ready','waiting');
CREATE INDEX IF NOT EXISTS work_claim ON work_items(stage, claimed_at) WHERE state = 'claimed';
CREATE INDEX IF NOT EXISTS work_source ON work_items(source) WHERE source <> '';
CREATE TABLE IF NOT EXISTS work_events(
  id        bigserial PRIMARY KEY,
  stage     text COLLATE "C" NOT NULL,
  item      text COLLATE "C" NOT NULL,
  at        timestamptz NOT NULL DEFAULT now(),
  from_state text COLLATE "C",
  to_state  text COLLATE "C" NOT NULL,
  detail    text COLLATE "C" NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS work_events_item ON work_events(stage, item, id);
DO $grants$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'booksys_app') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON work_items, work_events TO booksys_app;
    GRANT USAGE ON SEQUENCE work_events_id_seq TO booksys_app;
  END IF;
END $grants$;
"""


@dataclass(frozen=True)
class Item:
    stage: str
    item: str
    input_rev: str
    method: str
    source: str
    attempts: int
    claimed_by: str
    ready_at: datetime


def _instant(value: datetime | None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise LibraryError('Work item time needs a time zone')
    return value


class WorkItems:
    def __init__(self, database: Database, *, max_attempts: dict[str, int] | None = None, lease_seconds: int = 300):
        self.db = database
        self.max_attempts = max_attempts or {}
        self.lease_seconds = lease_seconds
        self._local = threading.local()            # one long-lived connection per thread: opening one costs 15 ms

    @contextmanager
    def _tx(self) -> Iterator[Any]:
        """One transaction on the thread's connection. A broken connection is dropped; the next call reconnects."""
        import psycopg
        conn = getattr(self._local, 'conn', None)
        if conn is None or conn.closed:
            conn = psycopg.connect(self.db.dsn, autocommit=True, connect_timeout=10)
            conn.execute(f'SET search_path TO {self.db.schema}')
            conn.execute("SET lock_timeout='5s'")
            conn.execute("SET statement_timeout='30s'")
            conn.execute("SET idle_in_transaction_session_timeout='60s'")
            self._local.conn = conn
        try:
            with conn.transaction():
                yield conn
        except psycopg.OperationalError:
            conn.close()
            self._local.conn = None
            raise

    def close(self) -> None:
        conn = getattr(self._local, 'conn', None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    @staticmethod
    def upgrade(database: Database) -> None:
        """Additive and idempotent. Run with the migration role, never by the workers."""
        with database.connection(write=True) as db:
            db.execute(DDL)

    @staticmethod
    def _stage(stage: str) -> str:
        if not NAME.fullmatch(stage):
            raise LibraryError('Invalid stage name')
        return stage

    def add(self, stage: str, item: str, *, input_rev: str, method: str, source: str = '',
            next_at: datetime | None = None, now: datetime | None = None, replay: bool = False, blocked: str = '') -> str:
        """Create the work, or supersede it when its input changed. Returns created|same|superseded."""
        with self._tx() as db:
            return self.add_in(db, stage, item, input_rev=input_rev, method=method, source=source, next_at=next_at, now=now, replay=replay, blocked=blocked)

    def add_in(self, db: Any, stage: str, item: str, *, input_rev: str, method: str, source: str = '',
               next_at: datetime | None = None, now: datetime | None = None, replay: bool = False, blocked: str = '') -> str:
        """Same as add, inside the caller's transaction (Store.commit creates the successors with the result).

        blocked is the reason an obligation gives (a source scan): the item is held until a later call gives none.
        Historical effect_uncertain rows are retried by claim()."""
        stage, at = self._stage(stage), _instant(now)
        row = db.execute('SELECT input_rev, state, method FROM work_items WHERE stage=%s AND item=%s FOR UPDATE',
                         (stage, item)).fetchone()
        if row is None:
            db.execute('INSERT INTO work_items(stage,item,state,wait_reason,input_rev,method,source,ready_at,state_at,next_at) '
                       'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                       (stage, item, 'blocked' if blocked else 'ready', blocked, input_rev, method, source, at, at, next_at or at))
            db.execute("INSERT INTO work_events(stage,item,at,from_state,to_state,detail) VALUES (%s,%s,%s,NULL,%s,'created')",
                       (stage, item, at, 'blocked' if blocked else 'ready'))
            return 'created'
        if row[0] == input_rev:
            # Same input. A new step version re-runs unfinished work, and finished work only when the obligation says replay.
            if row[2] != method and row[1] != 'superseded' and (row[1] != 'done' or replay) and row[1] != 'claimed':
                db.execute("UPDATE work_items SET state='ready', method=%s, attempts=0, last_error='', wait_reason='', state_at=%s, "
                           "next_at=%s, completed_at=NULL WHERE stage=%s AND item=%s", (method, at, next_at or at, stage, item))
                db.execute("INSERT INTO work_events(stage,item,at,from_state,to_state,detail) VALUES (%s,%s,%s,%s,'ready','method changed')",
                           (stage, item, at, row[1]))
                return 'superseded'
            held = db.execute('SELECT wait_reason FROM work_items WHERE stage=%s AND item=%s', (stage, item)).fetchone()
            if blocked and row[1] in ('ready', 'waiting'):
                db.execute("UPDATE work_items SET state='blocked', wait_reason=%s, state_at=%s WHERE stage=%s AND item=%s", (blocked, at, stage, item))
                db.execute("INSERT INTO work_events(stage,item,at,from_state,to_state,detail) VALUES (%s,%s,%s,%s,'blocked',%s)", (stage, item, at, row[1], blocked))
            elif not blocked and row[1] == 'blocked' and held is not None and held[0] != 'effect_uncertain':
                db.execute("UPDATE work_items SET state='ready', wait_reason='', state_at=%s, next_at=%s WHERE stage=%s AND item=%s", (at, next_at or at, stage, item))
                db.execute("INSERT INTO work_events(stage,item,at,from_state,to_state,detail) VALUES (%s,%s,%s,'blocked','ready','obligation unblocked')", (stage, item, at))
            if next_at is not None and row[1] in ('ready', 'waiting'):
                # The obligation moves its due time forward (never back): the old _discover took the later due time.
                db.execute("UPDATE work_items SET next_at=%s WHERE stage=%s AND item=%s AND next_at < %s", (next_at, stage, item, next_at))
            return 'same'
        if row[1] == 'claimed':
            # The running step keeps its claim; the new input is recorded and applies after it ends.
            db.execute("UPDATE work_items SET input_rev=%s, method=%s WHERE stage=%s AND item=%s", (input_rev, method, stage, item))
            db.execute("INSERT INTO work_events(stage,item,at,from_state,to_state,detail) VALUES (%s,%s,%s,'claimed','claimed','input changed during the step')",
                       (stage, item, at))
            return 'superseded'
        db.execute("UPDATE work_items SET state=%s, input_rev=%s, method=%s, source=%s, attempts=0, last_error='', wait_reason=%s, "
                   "ready_at=%s, state_at=%s, next_at=%s, claimed_by=NULL, claimed_at=NULL, completed_at=NULL "
                   "WHERE stage=%s AND item=%s", ('blocked' if blocked else 'ready', input_rev, method, source, blocked, at, at, next_at or at, stage, item))
        db.execute("INSERT INTO work_events(stage,item,at,from_state,to_state,detail) VALUES (%s,%s,%s,%s,%s,'input changed')",
                   (stage, item, at, row[1], 'blocked' if blocked else 'ready'))
        return 'superseded'

    def claim(self, stage: str, worker: str, *, now: datetime | None = None) -> Item | None:
        """Take the oldest runnable item of a stage. One statement; two workers never take one item."""
        stage, at = self._stage(stage), _instant(now)
        with self._tx() as db:
            row = db.execute(
                "WITH pick AS (SELECT stage, item, state FROM work_items WHERE stage=%(s)s AND ("
                "  ((state IN ('ready','waiting') OR (state='blocked' AND wait_reason='effect_uncertain')) AND next_at <= %(n)s)"
                "  OR (state='claimed' AND claimed_at < %(n)s - make_interval(secs => %(l)s)))"
                " ORDER BY ready_at, item LIMIT 1 FOR UPDATE SKIP LOCKED), "
                "upd AS (UPDATE work_items w SET state='claimed', claimed_by=%(w)s, claimed_at=%(n)s, state_at=%(n)s, attempts=w.attempts+1 "
                "  FROM pick WHERE w.stage=pick.stage AND w.item=pick.item "
                "  RETURNING w.stage, w.item, w.input_rev, w.method, w.source, w.attempts, w.claimed_by, w.ready_at, pick.state AS before), "
                "ev AS (INSERT INTO work_events(stage,item,at,from_state,to_state,detail) "
                "  SELECT stage, item, %(n)s, before, 'claimed', %(w)s FROM upd) "
                "SELECT stage, item, input_rev, method, source, attempts, claimed_by, ready_at FROM upd",
                {'s': stage, 'n': at, 'l': self.lease_seconds, 'w': worker}).fetchone()
        return Item(*row) if row else None

    def _finish(self, item: Item, to_state: str, *, detail: str, now: datetime | None, error: str = '', reason: str = '',
                next_at: datetime | None = None, refund: bool = False) -> bool:
        with self._tx() as db:
            return self.finish_in(db, item, to_state, detail=detail, now=now, error=error, reason=reason, next_at=next_at, refund=refund)

    def finish_in(self, db: Any, item: Item, to_state: str, *, detail: str = '', now: datetime | None = None, error: str = '',
                  reason: str = '', next_at: datetime | None = None, refund: bool = False) -> bool:
        """Fenced end of a claim, inside the caller's transaction. False when the claim was lost."""
        at = _instant(now)
        row = db.execute(
            "UPDATE work_items SET "
            "state = CASE WHEN input_rev=%(rev)s THEN %(to)s ELSE 'ready' END, state_at=%(at)s, "
            "last_error = CASE WHEN input_rev=%(rev)s THEN %(err)s ELSE '' END, "
            "wait_reason = CASE WHEN input_rev=%(rev)s THEN %(why)s ELSE '' END, claimed_by=NULL, claimed_at=NULL, "
            "next_at = CASE WHEN input_rev=%(rev)s THEN coalesce(%(nx)s, next_at) ELSE %(at)s END, "
            "attempts = CASE WHEN input_rev=%(rev)s THEN attempts-%(refund)s ELSE 0 END, "
            "completed_at = CASE WHEN input_rev=%(rev)s AND %(to)s='done' THEN %(at)s ELSE completed_at END "
            "WHERE stage=%(st)s AND item=%(it)s AND state='claimed' AND claimed_by=%(w)s AND attempts=%(n)s "
            "RETURNING state, input_rev=%(rev)s",
            {'rev': item.input_rev, 'to': to_state, 'at': at, 'err': error, 'why': reason, 'nx': next_at, 'refund': int(refund),
             'st': item.stage, 'it': item.item, 'w': item.claimed_by, 'n': item.attempts}).fetchone()
        if row is None:
            return False            # the claim was lost: the caller must discard its result
        db.execute("INSERT INTO work_events(stage,item,at,from_state,to_state,detail) VALUES (%s,%s,%s,'claimed',%s,%s)",
                   (item.stage, item.item, at, row[0], (detail or to_state if row[1] else 'input changed during the step')[:200]))
        return True

    def done(self, item: Item, *, detail: str = '', now: datetime | None = None) -> bool:
        return self._finish(item, 'done', detail=detail or 'done', now=now)

    def wait(self, item: Item, reason: str, until: datetime, *, now: datetime | None = None) -> bool:
        """A prerequisite is missing. No attempt is consumed; the oldest-first order is kept."""
        return self._finish(item, 'waiting', detail=reason, now=now, reason=reason, next_at=until, refund=True)

    def retry(self, item: Item, error: str, delay: timedelta, *, now: datetime | None = None) -> bool:
        """The step failed. Retry later, or fail for good at the stage limit."""
        with self._tx() as db:
            return self.retry_in(db, item, error, delay, now=now)

    def retry_in(self, db: Any, item: Item, error: str, delay: timedelta, *, now: datetime | None = None) -> bool:
        at = _instant(now)
        limit = self.max_attempts.get(item.stage)
        if limit is not None and item.attempts >= limit:
            return self.finish_in(db, item, 'failed', detail='attempts exhausted: ' + error, now=at, error=error)
        return self.finish_in(db, item, 'waiting', detail='retry: ' + error, now=at, error=error, reason='retry', next_at=at + delay)

    def fail(self, item: Item, error: str, *, now: datetime | None = None) -> bool:
        return self._finish(item, 'failed', detail=error, now=now, error=error)

    def claim_item(self, stage: str, item: str, worker: str, *, now: datetime | None = None) -> Item | None:
        """Take one named item that is ready (an operator command), not the oldest one."""
        stage, at = self._stage(stage), _instant(now)
        with self._tx() as db:
            row = db.execute(
                "WITH upd AS (UPDATE work_items w SET state='claimed', claimed_by=%(w)s, claimed_at=%(n)s, state_at=%(n)s, attempts=w.attempts+1 "
                "  WHERE w.stage=%(s)s AND w.item=%(i)s AND w.state IN ('ready','waiting') "
                "  RETURNING w.stage, w.item, w.input_rev, w.method, w.source, w.attempts, w.claimed_by, w.ready_at), "
                "ev AS (INSERT INTO work_events(stage,item,at,from_state,to_state,detail) SELECT stage, item, %(n)s, 'ready', 'claimed', %(w)s FROM upd) "
                "SELECT * FROM upd", {'s': stage, 'i': item, 'w': worker, 'n': at}).fetchone()
        return Item(*row) if row else None

    def release(self, stage: str, item: str, *, now: datetime | None = None) -> bool:
        """Clear a block (operator or recovery proof). The item keeps its age."""
        at = _instant(now)
        with self._tx() as db:
            row = db.execute("UPDATE work_items SET state='ready', wait_reason='', state_at=%s, next_at=%s "
                             "WHERE stage=%s AND item=%s AND state='blocked' RETURNING 1", (at, at, self._stage(stage), item)).fetchone()
            if row is None:
                return False
            db.execute("INSERT INTO work_events(stage,item,at,from_state,to_state,detail) VALUES (%s,%s,%s,'blocked','ready','released')", (stage, item, at))
            return True

    def revive(self, stage: str, *, now: datetime | None = None, older_than: timedelta = timedelta(hours=1)) -> int:
        """Give failed items one more round after a fixed cause (hourly retry of exhausted work)."""
        at = _instant(now)
        with self._tx() as db:
            rows = db.execute("UPDATE work_items SET state='ready', attempts=0, state_at=%s, next_at=%s "
                              "WHERE stage=%s AND state='failed' AND state_at < %s RETURNING item", (at, at, self._stage(stage), at - older_than)).fetchall()
            for (key,) in rows:
                db.execute("INSERT INTO work_events(stage,item,at,from_state,to_state,detail) VALUES (%s,%s,%s,'failed','ready','revive')", (stage, key, at))
            return len(rows)

    def counts(self, stage: str | None = None) -> dict[str, dict[str, int]]:
        """State counts per stage in one aggregate over the index."""
        with self._tx() as db:
            rows = db.execute("SELECT stage, state, count(*) FROM work_items WHERE (%s::text IS NULL OR stage=%s) GROUP BY 1,2", (stage, stage)).fetchall()
        result: dict[str, dict[str, int]] = {}
        for name, state, n in rows:
            result.setdefault(name, {})[state] = int(n)
        return result

    def events(self, stage: str, item: str, *, limit: int = 50) -> list[tuple[Any, ...]]:
        with self._tx() as db:
            return list(db.execute("SELECT at, from_state, to_state, detail FROM work_events WHERE stage=%s AND item=%s ORDER BY id DESC LIMIT %s",
                                   (stage, item, limit)).fetchall())
