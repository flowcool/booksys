"""Literal frozen source fixtures for forward migration acceptance."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4
import psycopg
from psycopg import sql

from library.codec import encode
from library.primitives import TABLES
from tests.test_pg_import import source_fixture


def target(test):
    from library.postgres import Database
    database=Database(Path(os.environ['BOOKSYS_TEST_PG_DSN_FILE']).read_text().strip(),'test_'+uuid4().hex)
    def cleanup():
        with psycopg.connect(database.dsn,autocommit=True) as db:
            db.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(database.schema)))
    test.addCleanup(cleanup)
    return database


def source(root, state=None):
    result=source_fixture(root)
    if state is not None:
        with closing(sqlite3.connect(result/'catalogue.sqlite')) as db:
            db.execute('UPDATE meta SET global_revision=?,ledger=?',(state['revision'],encode(state['ledger'])))
            for kind in TABLES:
                db.execute('DELETE FROM '+kind)
                for identity,value in state[kind].items():
                    db.execute('INSERT INTO '+kind+' VALUES(?,?,?)',(identity,state['revision'],encode({'record':value})))
            db.commit()
    return result


def private_selector(directory,database):
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    secret=directory/'test.dsn';secret.write_text(database.dsn);secret.chmod(0o600)
    config=directory/'postgresql.json'
    config.write_text(json.dumps({'schema':database.schema,'dsn_file':str(secret)}));config.chmod(0o600)


def imported(test, root, state=None, *, select=True):
    from library.pg_import import prepare_manifest,import_snapshot,select as activate
    frozen=source(root/'source',state)
    database=target(test);manifest=prepare_manifest(frozen,candidate='isolated-fixture')
    directory=root/'target';import_snapshot(manifest,directory,database=database)
    private_selector(directory,database)
    if select:activate(manifest,directory,database=database)
    return manifest,directory,database
