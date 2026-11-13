"""Private PostgreSQL connection lifecycle and total acquisition deadlines.

This module implements transport only. Domain callers use Store and StageTasks.
No connection, SQL callback or retry wrapper is part of their public interface.
"""
from __future__ import annotations

import atexit
from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import select
import stat
import sys
import threading
import time
from typing import Any, Iterator

import psycopg
from psycopg import pq, sql

from library.locking import acquisition_deadline
from library.primitives import LibraryError


class CommitUnknown(LibraryError):
    """Resolve the same logical commit key before any dependent action."""


class DeadlineConnection(psycopg.Connection[Any]):
    deadline: float
    lock_cap_ms: int | None = None
    _applied: tuple[int, int] | None = None   # (statement, lock) timeout in ms last sent to the server

    def wait(self, gen: Any, interval: float = 0.1, timeout: float | None = None) -> Any:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise LibraryError('storage_busy')
        return super().wait(gen, interval=interval, timeout=remaining)

    def execute(self, query: Any, params: Any = None, *, prepare: bool | None = None,
                binary: bool = False) -> psycopg.Cursor[Any]:
        remaining = int((self.deadline - time.monotonic()) * 1000)
        if remaining < 1:
            raise LibraryError('storage_busy')
        # Each statement receives the remaining TOTAL budget, not a fresh wait. The server side timeout is sent again
        # only when 0.5 s of the budget went by since the last one: a statement can overrun the deadline by at most
        # 0.5 s on the server, and wait() above still stops the client at the exact deadline. Most transactions last
        # milliseconds, so this removes about half of all statements (one set_config before each query).
        lock_ms = min(remaining, self.lock_cap_ms) if self.lock_cap_ms is not None else remaining
        applied = self._applied
        if (applied is None or abs(applied[0] - remaining) > 500 or remaining > applied[0]
                or abs(applied[1] - lock_ms) > 500 or lock_ms > applied[1]):
            super().execute("SELECT set_config('lock_timeout', %s, false), "
                            "set_config('statement_timeout', %s, false)", (f'{lock_ms}ms', f'{remaining}ms'))
            self._applied = (remaining, lock_ms)
        return super().execute(query, params, prepare=prepare, binary=binary)


@dataclass(frozen=True)
class Database:
    dsn: str = field(repr=False)
    schema: str = 'booksys'

    def __post_init__(self) -> None:
        if not re.fullmatch('[a-z][a-z0-9_]{0,62}', self.schema):
            raise LibraryError('Invalid PostgreSQL namespace')
        from psycopg.conninfo import conninfo_to_dict
        try:
            values = conninfo_to_dict(self.dsn)
        except psycopg.Error:
            raise LibraryError('Invalid PostgreSQL connection configuration') from None
        host = str(values.get('host', ''))
        address = str(values.get('hostaddr', ''))
        if address or host and not host.startswith('/'):
            import ipaddress
            try:
                ipaddress.ip_address(address or host)
            except ValueError:
                raise LibraryError('PostgreSQL requires a socket or numeric address') from None

    @classmethod
    def configured(cls, directory: Path) -> Database:
        path = directory / 'postgresql.json'
        if not cls._private(path):
            raise LibraryError('Private PostgreSQL configuration required')
        value = json.loads(path.read_text())
        if set(value) != {'schema', 'dsn_file'}:
            raise LibraryError('Invalid PostgreSQL configuration')
        secret = Path(value['dsn_file'])
        if not cls._private(secret):
            raise LibraryError('Private PostgreSQL credential required')
        return cls(secret.read_text().strip(), value['schema'])

    @staticmethod
    def _private(path: Path) -> bool:
        try:
            info = path.lstat()
            return (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                    and not info.st_mode & 0o077
                    and all(not parent.is_symlink() for parent in path.parents))
        except OSError:
            return False

    @contextmanager
    # Connection, lock, query and commit waits share one remaining total database deadline.
    def connection(self, *, write: bool = False, seconds: float = 10, lock_cap_ms: int | None = None) -> Iterator[DeadlineConnection]:
        """One transaction. The backend connection is kept for the next call of this thread when the transaction ended
        cleanly: opening one costs a server process, a password check and two statements (15 ms; about 250 a second
        were opened on the lab). Any error closes it. A kept connection that died while idle is replaced once."""
        stop = min(acquisition_deadline.get() or float('inf'), time.monotonic() + seconds)
        key = (self.dsn, self.schema, os.getpid(), threading.get_ident())
        connection = _POOL.pop(key, None)
        reused = connection is not None
        if connection is not None and (connection.closed or connection.info.transaction_status != pq.TransactionStatus.IDLE):
            connection.close()
            connection, reused = None, False
        keep = False
        try:
            while True:
                if connection is None:
                    connection = self._open(stop)
                connection.deadline = stop
                connection.lock_cap_ms = lock_cap_ms
                connection._applied = None
                try:
                    connection.execute('BEGIN' if write else 'BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
                except (psycopg.OperationalError, psycopg.InterfaceError):
                    if not reused:
                        raise
                    connection.close()                  # it died while idle: nothing ran, open a fresh one
                    connection, reused = None, False
                    continue
                break
            yield connection
            if not connection.closed and connection.info.transaction_status != pq.TransactionStatus.IDLE:
                try:
                    connection.execute('COMMIT')
                except psycopg.errors.DiskFull:
                    raise LibraryError('PostgreSQL storage exhausted') from None
                except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
                    raise CommitUnknown('commit_unresolved') from exc
            keep = not connection.closed and connection.info.transaction_status == pq.TransactionStatus.IDLE
        except (psycopg.errors.LockNotAvailable, psycopg.errors.QueryCanceled,
                psycopg.errors.DeadlockDetected) as exc:
            raise LibraryError('storage_busy') from exc
        except psycopg.errors.DiskFull:
            raise LibraryError('PostgreSQL storage exhausted') from None
        except psycopg.OperationalError:
            raise LibraryError('storage_busy') from None
        except psycopg.IntegrityError:
            raise LibraryError('Catalogue relational constraint failed') from None
        finally:
            if connection is not None:
                if keep:
                    _POOL[key] = connection
                    # Threads and schemas that are gone leave their idle connection here: keep at most 8 per process
                    # (a test run that creates many stores otherwise exhausts max_connections), oldest first out.
                    while len(_POOL) > _POOL_LIMIT:
                        old = _POOL.pop(next(iter(_POOL)))
                        try:
                            old.close()
                        except Exception:
                            pass
                else:
                    connection.close()

    def _open(self, stop: float) -> DeadlineConnection:
        from psycopg.conninfo import make_conninfo
        # The role name in pg_stat_activity tells which process runs which query (profiling without extensions).
        name = os.environ.get('BOOKSYS_QUEUE') or Path(sys.argv[0] or 'booksys').stem
        pg = pq.PGconn.connect_start(make_conninfo(self.dsn, application_name=re.sub(r'[^a-z0-9_.-]', '-', name.lower())[:48]).encode())
        try:
            while True:
                status = pg.connect_poll()
                if status == pq.PollingStatus.OK:
                    break
                if status == pq.PollingStatus.FAILED:
                    raise LibraryError('PostgreSQL connection failed')
                remaining = stop - time.monotonic()
                if remaining <= 0:
                    raise LibraryError('storage_busy')
                select.select([pg.socket] if status == pq.PollingStatus.READING else [],
                              [pg.socket] if status == pq.PollingStatus.WRITING else [], [], remaining)
        except BaseException:
            pg.finish()
            raise
        connection = DeadlineConnection(pg)
        connection.deadline = stop
        connection.autocommit = True
        try:
            connection.execute(sql.SQL('SET search_path TO {}, pg_catalog').format(sql.Identifier(self.schema)))
        except BaseException:
            connection.close()
            raise
        return connection


# One idle connection per (dsn, schema, process, thread): see Database.connection.
_POOL: dict[tuple[str, str, int, int], DeadlineConnection] = {}
# Retain at most eight idle database connections per process and reconnect a stale idle session only once.
_POOL_LIMIT = 8


def _close_pool() -> None:
    for kept in list(_POOL.values()):
        try:
            kept.close()
        except Exception:
            pass
    _POOL.clear()


atexit.register(_close_pool)


def guard_schema(db: DeadlineConnection, namespace: str) -> None:
    """Keep exact sequential lock order without a client round trip for every key."""
    target=sql.Identifier(namespace)
    db.execute(sql.SQL("""
        CREATE FUNCTION {}.lock_conflict_guards(requested text[]) RETURNS void
        LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $body$
        DECLARE guard_key text;
        BEGIN
            FOR guard_key IN SELECT item FROM unnest(requested) AS g(item) ORDER BY item COLLATE "C" LOOP
                INSERT INTO {}.conflict_guards(key) VALUES(guard_key) ON CONFLICT DO NOTHING;
                PERFORM key FROM {}.conflict_guards WHERE key=guard_key FOR UPDATE;
            END LOOP;
        END;
        $body$
    """).format(target,target,target))
    db.execute(sql.SQL('REVOKE ALL ON FUNCTION {}.lock_conflict_guards(text[]) FROM PUBLIC').format(target))


def guards(db: DeadlineConnection, keys: set[str]) -> None:
    """Every member insertion/removal locks the same sorted absent-key rows."""
    if keys:db.execute('SELECT lock_conflict_guards(%s::text[])',(sorted(keys),))
