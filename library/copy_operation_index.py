"""Indexed copy-journal ownership. Install explicitly with the migration role."""
from __future__ import annotations

import argparse
from pathlib import Path

from library.postgres import Database, DeadlineConnection
from library.primitives import LibraryError


def schema(db: DeadlineConnection) -> None:
    # Version the immutable function: replacing it would invalidate existing index keys.
    existing = db.execute("SELECT to_regprocedure('copy_operation_asset_v1(text)')").fetchone()
    if existing is None or existing[0] is None:
        db.execute("""CREATE FUNCTION copy_operation_asset_v1(payload text) RETURNS text
            LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE
            SECURITY INVOKER SET search_path=pg_catalog AS $body$
            DECLARE record json := payload::json; asset text := record->>'asset';
            BEGIN
                IF json_typeof(record->'asset') IS DISTINCT FROM 'string'
                   OR asset IS NULL OR asset !~ '^[0-9a-f]{64}$' THEN
                    RETURN NULL;
                END IF;
                IF record->>'kind' = 'admission' AND
                   (record->>'target' IS NULL OR record->>'target' !~
                    ('--' || asset || E'--[0-9a-f]{12}\\n?$')) THEN
                    RETURN NULL;
                END IF;
                RETURN asset;
            END;
            $body$""")
    # NULL keys retain malformed scopes. Readers must still reject them globally.
    db.execute('CREATE INDEX IF NOT EXISTS copy_operations_asset_v1 ON copy_operations '
               '(namespace, copy_operation_asset_v1(payload), id)')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dsn-file', type=Path, required=True)
    parser.add_argument('--schema', default='booksys')
    parser.add_argument('--seconds', type=int, default=300)
    args = parser.parse_args()
    if not Database._private(args.dsn_file) or args.seconds <= 0:
        raise LibraryError('Use an owned private credential file and a positive deadline')
    database = Database(args.dsn_file.read_text().strip(), args.schema)
    with database.connection(write=True, seconds=args.seconds) as db:
        schema(db)
    print('Copy-journal ownership index installed; journal payloads unchanged')


if __name__ == '__main__':
    main()
