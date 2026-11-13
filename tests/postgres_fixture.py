"""Isolated PostgreSQL fixtures. Never select a runtime or shared database."""
import os
import json
from pathlib import Path
from uuid import uuid4
import psycopg
from psycopg import sql


def create_store(test, directory, state, *, authority='test'):
    from library.postgres import Database
    from library.store import Store
    dsn = Path(os.environ['BOOKSYS_TEST_PG_DSN_FILE']).read_text().strip()
    namespace = 'test_' + uuid4().hex
    database = Database(dsn,namespace)
    def cleanup():
        with psycopg.connect(dsn,autocommit=True) as db:
            db.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(namespace)))
    test.addCleanup(cleanup)
    store = Store.create(directory,database=database,authority=authority,state=state)
    secret = directory / 'postgresql-test.dsn'
    secret.write_text(dsn)
    secret.chmod(0o600)
    config = directory / 'postgresql.json'
    config.write_text(json.dumps({'schema':namespace,'dsn_file':str(secret)}))
    config.chmod(0o600)
    from library.authority import code_digest
    from library.primitives import atomic_yaml
    with database.connection() as db:
        source=db.execute('SELECT imported_source_digest,source_semantic FROM meta').fetchone()
    manifest=directory/'storage-authority.yaml'
    atomic_yaml(manifest,{'schema':2,'backend':'postgresql','authority_id':authority,
        'catalogue_path':'postgresql.json','source_digest':source[0],
        'source_semantic_sha256':source[1],'code_sha256':code_digest(),
        'runtime':{'python_minor':[3,14],'postgresql_min':[18,6]},'compatible_legacy_commit':None})
    manifest.chmod(0o400)
    return store


def create_activity(test, directory, **kwargs):
    from library.primitives import empty
    from library.migration import create_activity as initialize
    if not (directory/'postgresql.json').exists():
        create_store(test,directory,empty())
    initialize(directory,**kwargs)


def create_catalog(test, directory, state=None, *, authority='test'):
    """Declare an isolated PostgreSQL catalogue at the public domain seam."""
    from library.catalog import Catalog
    from library.primitives import empty
    directory=Path(directory)
    if (directory/'postgresql.json').exists():
        return Catalog(directory)
    return Catalog(directory,store=create_store(test,directory,empty() if state is None else state,authority=authority))


def require_postgresql(catalog):
    """A former conversion fixture now verifies the sole selected backend."""
    from library.postgres import Database
    selected=Database.configured(catalog.directory)
    assert selected==catalog.backend()._database, 'Fixture must select its isolated PostgreSQL authority'
