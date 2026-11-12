"""Public operational-state contracts on the qualified isolated PostgreSQL."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch, Mock
from uuid import uuid4

import psycopg

from library.primitives import empty


@unittest.skipUnless(os.environ.get('BOOKSYS_TEST_PG_DSN_FILE'), 'isolated PostgreSQL required')
class PostgreSQLStateTests(unittest.TestCase):
    def setUp(self) -> None:
        from library.postgres import Database
        from library.store import Store
        self.temp = tempfile.TemporaryDirectory(prefix='booksys-pg-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dsn = Path(os.environ['BOOKSYS_TEST_PG_DSN_FILE']).read_text().strip()
        self.schema = 'test_' + uuid4().hex
        self.database = Database(self.dsn, self.schema)
        self.store = Store.create(self.root, database=self.database, authority='test', state=empty())
        from import_fixture import private_selector
        private_selector(self.root,self.database)
        self.addCleanup(self.drop_schema)
        self.now = datetime(2026, 11, 10, tzinfo=timezone.utc)
        from library.catalog import Catalog
        self.catalog = Catalog(self.root, store=self.store)

    def drop_schema(self) -> None:
        from psycopg import sql
        with psycopg.connect(self.dsn, autocommit=True) as db:
            db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))







    def test_guard_order_does_not_bind_later_absent_key_while_earlier_key_waits(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        from library.postgres import guards
        from threading import Event
        import time
        waiting=Event();worker_pid=[]
        def ordered_writer() -> None:
            with self.database.connection(write=True) as db:
                worker_pid.append(db.info.backend_pid);waiting.set()
                guards(db,{'a-existing','z-absent'})
        with self.database.connection(write=True) as seed:
            guards(seed,{'a-existing'})
        with ThreadPoolExecutor(max_workers=1) as executor:
            with self.database.connection(write=True) as first:
                guards(first,{'a-existing'})
                pending=executor.submit(ordered_writer)
                self.assertTrue(waiting.wait(3))
                deadline=time.monotonic()+3
                while True:
                    with self.database.connection() as observer:
                        blocked=observer.execute('SELECT cardinality(pg_blocking_pids(%s))',(worker_pid[0],)).fetchone()[0]
                    if blocked:break
                    self.assertLess(time.monotonic(),deadline)
                    time.sleep(.01)
                with self.database.connection(write=True,seconds=1) as later:
                    guards(later,{'z-absent'})
                self.assertFalse(pending.done())
            pending.result(timeout=3)











    def asset_fixture(self) -> str:
        import hashlib
        from library.store import Change
        self.reception = self.root/'reception'
        self.reception.mkdir()
        raw = b'isolated printed-reader boundary fixture'
        (self.reception/'album.cbz').write_bytes(raw)
        aid = hashlib.sha256(raw).hexdigest()
        self.store.commit(Change(upserts={
            'assets': {aid: {'sha256':aid,'size':len(raw),'format':'cbz','origins':['origin-a'],
                             'status':'validated','embedded':{}}},
            'origins': {'origin-a': {'source':'b'*64,'pid':1,'fid':1,'path':'album.cbz',
                                    'sha256':aid,'availability':'validated'}}}),expected_revision=0)
        return aid






    def test_new_acquisition_member_invalidates_prepared_result(self) -> None:
        from library.store import Change
        from library.primitives import LibraryError
        aid = self.asset_fixture()
        preview = self.catalog.result_set('identify',{'assets':[aid]})
        self.store.commit(Change(upserts={'origins':{'new-origin':{'source':'b'*64,'pid':2,'fid':2,
            'path':'other.cbz','sha256':None,'availability':'waiting'}}}),expected_revision=self.store.revision)
        preview['assets'][aid]['new_result'] = 'stale'
        with self.assertRaisesRegex(LibraryError,'Result conflict'):
            self.catalog.save(preview,expected=preview['revision'])

    def test_connection_handshake_uses_the_same_operator_deadline(self) -> None:
        import socket,time
        from library.postgres import Database
        from library.locking import bounded_acquisition
        from library.primitives import LibraryError
        listener = socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        listener.bind(str(self.root/'.s.PGSQL.15432'))
        listener.listen(1)
        self.addCleanup(listener.close)
        database = Database(f'host={self.root} port=15432 user=deadline-probe dbname=deadline-probe',self.schema)
        started = time.monotonic()
        with bounded_acquisition(started+.5),self.assertRaisesRegex(LibraryError,'storage_busy'):
            with database.connection():
                self.fail('unanswered handshake succeeded')
        self.assertLess(time.monotonic()-started,.8)














if __name__ == '__main__':
    unittest.main()
