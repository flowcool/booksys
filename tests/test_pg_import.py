"""Frozen-source import and current-state recovery at the migration seam."""
import os
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import yaml
from uuid import uuid4
from contextlib import closing

from library.primitives import LibraryError
from library.postgres import Database


def source_fixture(root):
    """A source, not a reverse export from the candidate implementation."""
    (root/'files').mkdir(parents=True)
    (root/'collector').mkdir()
    with closing(sqlite3.connect(root/'catalogue.sqlite')) as db:
        db.execute('CREATE TABLE meta(singleton,authority_id,schema_version,global_revision,imported_source_digest,ledger,case_scope_revision,ready,source_semantic,required_runtime,created_at)')
        db.execute("INSERT INTO meta VALUES(1,'source-test',1,7,'original','[]',0,1,'old','python3.14','before')")
        for kind in ('works','editions','variants','assets','artifacts','origins','cases','operations'):
            db.execute(f'CREATE TABLE {kind}(id TEXT PRIMARY KEY,revision INTEGER,payload TEXT)')
        db.execute('INSERT INTO works VALUES(?,?,?)',('work-kept',3,'{"record":{"title":"Retained"}}'))
        db.commit()
    return root


class ImportTests(unittest.TestCase):

    def target(self):
        import psycopg
        from psycopg import sql
        database=Database(Path(os.environ['BOOKSYS_TEST_PG_DSN_FILE']).read_text().strip(),'test_'+uuid4().hex)
        def cleanup():
            with psycopg.connect(database.dsn,autocommit=True) as db:
                db.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(database.schema)))
        self.addCleanup(cleanup)
        return database












if __name__=='__main__':
    unittest.main()
