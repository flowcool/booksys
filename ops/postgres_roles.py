"""Provision the concrete Booksys application and closed launcher roles.

Run with the migration credential after schema import. Password files are private
inputs. This command does not select authority or run business operations.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

from psycopg import sql

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from library.launcher_access import grant
from library.postgres import Database
from library.primitives import LibraryError


def private(path: Path) -> str:
    if (not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents))
            or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077):
        raise LibraryError('Use an owned private absolute credential file')
    value = path.read_text().strip()
    if not value:
        raise LibraryError('Empty credential file')
    return value


def provision(database: Database, *, application: str, launcher: str, definer: str,
              app_password: str, launcher_password: str) -> None:
    if len({application, launcher, definer}) != 3:
        raise LibraryError('Database roles must differ')
    with database.connection(write=True) as db:
        for role, password in ((application, app_password), (launcher, launcher_password), (definer, None)):
            observed = db.execute('SELECT rolcanlogin,rolsuper,rolcreaterole,rolcreatedb,rolbypassrls FROM pg_catalog.pg_roles WHERE rolname=%s', (role,)).fetchone()
            if observed is None:
                db.execute(sql.SQL('CREATE ROLE {} ').format(sql.Identifier(role)) + (
                    sql.SQL('LOGIN PASSWORD {}').format(sql.Literal(password)) if password is not None else sql.SQL('NOLOGIN')))
            elif observed != (password is not None, False, False, False, False):
                raise LibraryError('Existing database role has unexpected privileges')
        if db.execute('SELECT 1 FROM pg_catalog.pg_auth_members m JOIN pg_catalog.pg_roles r ON r.oid=m.member WHERE r.rolname=%s', (application,)).fetchone():
            raise LibraryError('Application role membership forbidden')
        namespace, app = sql.Identifier(database.schema), sql.Identifier(application)
        db.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(namespace, app))
        tables = db.execute("SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname=%s ORDER BY tablename", (database.schema,)).fetchall()
        for (table,) in tables:
            privileges = sql.SQL('SELECT') if table in ('migration_import', 'migration_phases') else sql.SQL('SELECT,INSERT,UPDATE,DELETE')
            db.execute(sql.SQL('GRANT {} ON {}.{} TO {}').format(privileges, namespace, sql.Identifier(table), app))
        db.execute(sql.SQL('GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}').format(namespace, app))
        db.execute(sql.SQL('GRANT EXECUTE ON FUNCTION {}.lock_conflict_guards(text[]) TO {}').format(namespace, app))
        db.execute(sql.SQL('GRANT EXECUTE ON FUNCTION pg_catalog.pg_control_system() TO {}').format(app))
    grant(database, launcher=launcher, definer=definer)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dsn-file', type=Path, required=True)
    parser.add_argument('--schema', required=True)
    parser.add_argument('--application-role', default='booksys_app')
    parser.add_argument('--launcher-role', default='booksys_launcher')
    parser.add_argument('--definer-role', default='booksys_launcher_functions')
    parser.add_argument('--app-password-file', type=Path, required=True)
    parser.add_argument('--launcher-password-file', type=Path, required=True)
    args = parser.parse_args()
    provision(Database(private(args.dsn_file), args.schema), application=args.application_role,
        launcher=args.launcher_role, definer=args.definer_role,
        app_password=private(args.app_password_file), launcher_password=private(args.launcher_password_file))
    print('Provisioned concrete application and closed launcher privileges; no authority selection')


if __name__ == '__main__':
    main()
