"""Change tokens and the cache they validate (needs BOOKSYS_SCRATCH_DSN, a disposable PostgreSQL)."""
from __future__ import annotations

import os
import unittest
import uuid

DSN = os.environ.get('BOOKSYS_SCRATCH_DSN', '')


@unittest.skipUnless(DSN, 'BOOKSYS_SCRATCH_DSN is not set')
class VersionedTests(unittest.TestCase):
    def setUp(self) -> None:
        import psycopg
        from library.postgres import Database
        from library import versioned
        self.schema = 'vt_' + uuid.uuid4().hex[:12]
        self.psycopg = psycopg
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f'CREATE SCHEMA {self.schema}')
            admin.execute(f'CREATE TABLE {self.schema}.t1(k text PRIMARY KEY, v text)')
        self.database = Database(DSN, self.schema)
        versioned.install(self.database, ('t1',))
        versioned._CACHE.clear()
        versioned._AVAILABLE.clear()
        self.loads = 0

    def tearDown(self) -> None:
        with self.psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA {self.schema} CASCADE')

    def read(self) -> dict[str, str]:
        from library import versioned

        def load(db):  # type: ignore[no-untyped-def]
            self.loads += 1
            return dict(db.execute('SELECT k, v FROM t1').fetchall())
        value: dict[str, str] = versioned.cached(self.database, 'k', ('t1',), load)
        return value

    def write(self, sql: str, *args: str) -> None:
        with self.database.connection(write=True) as db:
            db.execute(sql, args)

    def test_reads_once_until_a_write_changes_the_table(self) -> None:
        self.assertEqual(self.read(), {})
        self.assertEqual(self.read(), {})
        self.assertEqual(self.loads, 1, 'an unchanged table must not be read twice')
        self.write("INSERT INTO t1 VALUES ('a','1')")
        self.assertEqual(self.read(), {'a': '1'})
        self.write("UPDATE t1 SET v='2' WHERE k='a'")
        self.assertEqual(self.read(), {'a': '2'})
        self.write("DELETE FROM t1")
        self.assertEqual(self.read(), {})
        self.write("INSERT INTO t1 VALUES ('b','3')")
        self.write("TRUNCATE t1")
        self.assertEqual(self.read(), {}, 'truncate must invalidate too')

    def test_the_caller_cannot_corrupt_the_cache(self) -> None:
        self.write("INSERT INTO t1 VALUES ('a','1')")
        first = self.read()
        first['evil'] = 'x'
        self.assertEqual(self.read(), {'a': '1'})
        second = self.read()
        second['evil'] = 'y'
        self.assertEqual(self.read(), {'a': '1'})

    def test_a_writer_with_a_lower_id_that_commits_last_is_seen(self) -> None:
        """Writer A takes the lower change id and commits after B. A reader that cached after B must reload after A."""
        a = self.psycopg.connect(DSN, autocommit=False)
        try:
            a.execute(f'SET search_path TO {self.schema}')
            a.execute("INSERT INTO t1 VALUES ('from-a','1')")             # takes change id n, not committed
            self.write("INSERT INTO t1 VALUES ('from-b','2')")             # takes id n+1 and commits
            self.assertEqual(self.read(), {'from-b': '2'})                # cached with the token of B only
            a.commit()                                                     # now id n becomes visible
            self.assertEqual(self.read(), {'from-a': '1', 'from-b': '2'}, 'the count must have invalidated the cache')
        finally:
            a.close()

    def test_trim_keeps_the_token_valid(self) -> None:
        from library import versioned
        for index in range(5):
            self.write("INSERT INTO t1 VALUES (%s,'v')", f'k{index}')
        before = self.read()
        loads = self.loads
        self.assertGreater(versioned.trim(self.database), 0)
        self.assertEqual(self.read(), before)
        self.assertEqual(self.read(), before)
        self.assertEqual(self.loads, loads + 1, 'one reload after the trim, then cached again')
        self.write("INSERT INTO t1 VALUES ('after','v')")
        self.assertIn('after', self.read())


if __name__ == '__main__':
    unittest.main()
