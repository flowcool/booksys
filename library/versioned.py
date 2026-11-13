"""Change tokens per table, kept by the database, and a process cache validated by them.

Several readers loaded a whole table at every call (the captured downloads, the retirement history, the copy operations,
the collection state): about 40 call sites and tens of thousands of rows a second for data that changes a few times a minute.

A statement trigger writes one row into table_changes for each write statement on a watched table. A reader reads a token
(count and maximum id of those rows, per table) in the SAME transaction as its data (repeatable read), so the token and the
data always match. If the token is the one of its cached copy, the copy is returned; otherwise the table is read again.

Why count and maximum: row ids are not in commit order. A writer can take id 10, a second writer id 11 and commit first.
The maximum alone would then not move when the first one commits; the count does. trim() keeps the last row of each table,
so the pair never repeats.
"""
from __future__ import annotations

import copy
import threading
from collections.abc import Callable
from typing import Any

from psycopg import sql

from library.postgres import Database, DeadlineConnection

TABLES = ('captured_sources', 'captured_downloads', 'captured_download_links', 'collection_categories', 'collection_submissions',
          'collection_checkpoints',
          'fiche_progress', 'retirement_acquisitions', 'retired_packages', 'retired_links', 'retirement_cursor',
          'routing_packages', 'copy_operations')

_CACHE: dict[tuple[str, str, str], tuple[tuple[tuple[int, int], ...], Any]] = {}
_AVAILABLE: dict[tuple[str, str], bool] = {}
_LOCK = threading.Lock()


def install(database: Database, tables: tuple[str, ...] = TABLES) -> None:
    """Additive and repeatable. Run with the migration role."""
    schema = sql.Identifier(database.schema)
    with database.connection(write=True) as db:
        db.execute(sql.SQL('CREATE TABLE IF NOT EXISTS {}.table_changes(id bigserial PRIMARY KEY, name text COLLATE "C" NOT NULL)').format(schema))
        db.execute(sql.SQL('CREATE INDEX IF NOT EXISTS table_changes_name ON {}.table_changes(name, id)').format(schema))
        db.execute(sql.SQL(
            "CREATE OR REPLACE FUNCTION {s}.note_change() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $f$ "
            "BEGIN INSERT INTO {s}.table_changes(name) VALUES (TG_ARGV[0]); RETURN NULL; END $f$").format(s=schema))
        for table in tables:
            name = sql.Identifier(table)
            db.execute(sql.SQL(
                'CREATE OR REPLACE TRIGGER {t} AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON {s}.{n} '
                'FOR EACH STATEMENT EXECUTE FUNCTION {s}.note_change({lit})').format(
                    t=sql.Identifier('changes_' + table), s=schema, n=name, lit=sql.Literal(table)))
        db.execute(sql.SQL(
            "DO $g$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'booksys_app') THEN "
            "GRANT SELECT, DELETE ON {s}.table_changes TO booksys_app; END IF; END $g$").format(s=schema))


def token(db: DeadlineConnection, tables: tuple[str, ...]) -> tuple[tuple[int, int], ...]:
    found = {name: (int(n), int(top)) for name, n, top in db.execute(
        'SELECT name, count(*), max(id) FROM table_changes WHERE name = ANY(%s) GROUP BY name', (list(tables),)).fetchall()}
    return tuple(found.get(table, (0, 0)) for table in tables)


def cached(database: Database, key: str, tables: tuple[str, ...], loader: Callable[[DeadlineConnection], Any]) -> Any:
    """loader(db) returns the value; it is reused while no watched table was written. Callers get their own copy."""
    scope = (database.dsn, database.schema)
    with database.connection() as db:
        if scope not in _AVAILABLE:
            row = db.execute("SELECT to_regclass('table_changes')").fetchone()
            _AVAILABLE[scope] = bool(row and row[0] is not None)
        if not _AVAILABLE[scope]:
            return loader(db)           # the triggers are not installed: read as before
        current = token(db, tables)
        with _LOCK:
            hit = _CACHE.get(scope + (key,))
        if hit is not None and hit[0] == current:
            return copy.deepcopy(hit[1])
        value = loader(db)
    with _LOCK:
        _CACHE[scope + (key,)] = (current, copy.deepcopy(value))
    return value


def trim(database: Database) -> int:
    """Keep the last change row of each table: the token stays valid and the table stays small."""
    with database.connection(write=True) as db:
        return db.execute(
            'DELETE FROM table_changes c USING (SELECT name, max(id) AS top FROM table_changes GROUP BY name) k '
            'WHERE c.name = k.name AND c.id < k.top').rowcount
