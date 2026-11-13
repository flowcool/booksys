"""Gate G2 of the state-column design: kill test, two workers, lost claim, supersede.

Needs a disposable PostgreSQL: set BOOKSYS_SCRATCH_DSN (a numeric host or a socket). Each test uses its own schema.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone

DSN = os.environ.get('BOOKSYS_SCRATCH_DSN', '')
T0 = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)


@unittest.skipUnless(DSN, 'BOOKSYS_SCRATCH_DSN is not set')
class WorkItemsTests(unittest.TestCase):
    def setUp(self) -> None:
        import psycopg
        from library.postgres import Database
        from library.work_items import WorkItems
        self.schema = 'wi_' + uuid.uuid4().hex[:12]
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f'CREATE SCHEMA {self.schema}')
        self.database = Database(DSN, self.schema)
        WorkItems.upgrade(self.database)
        self.make = lambda **kw: WorkItems(self.database, **kw)

    def tearDown(self) -> None:
        import psycopg
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA {self.schema} CASCADE')

    def state(self, stage: str, item: str) -> str:
        with self.database.connection() as db:
            return str(db.execute('SELECT state FROM work_items WHERE stage=%s AND item=%s', (stage, item)).fetchone()[0])

    def test_oldest_first_and_wait_keeps_age(self) -> None:
        work = self.make()
        for index, name in enumerate(('c', 'a', 'b')):
            work.add('routing', name, input_rev='r1', method='m1', now=T0 + timedelta(seconds=index))
        first = work.claim('routing', 'w1', now=T0 + timedelta(minutes=1))
        assert first is not None and first.item == 'c'
        self.assertTrue(work.wait(first, 'network_budget_wait', T0 + timedelta(minutes=10), now=T0 + timedelta(minutes=1)))
        second = work.claim('routing', 'w1', now=T0 + timedelta(minutes=2))
        assert second is not None and second.item == 'a'                     # c waits, a goes next
        work.done(second, now=T0 + timedelta(minutes=2))
        third = work.claim('routing', 'w1', now=T0 + timedelta(minutes=3))
        assert third is not None and third.item == 'b'
        work.done(third, now=T0 + timedelta(minutes=3))
        self.assertIsNone(work.claim('routing', 'w1', now=T0 + timedelta(minutes=4)))   # only c, still waiting
        back = work.claim('routing', 'w1', now=T0 + timedelta(minutes=11))
        assert back is not None and back.item == 'c'                         # its age survived the wait
        self.assertEqual(back.attempts, 1, 'a wait must not consume an attempt')

    def test_two_workers_never_take_one_item(self) -> None:
        work = self.make()
        for index in range(240):
            work.add('intake', f'i{index:03}', input_rev='r', method='m', now=T0)
        taken: list[str] = []
        lock = threading.Lock()

        def run(name: str) -> None:
            while True:
                item = work.claim('intake', name, now=T0 + timedelta(seconds=1))
                if item is None:
                    return
                with lock:
                    taken.append(item.item)
                assert work.done(item, now=T0 + timedelta(seconds=2))

        threads = [threading.Thread(target=run, args=(f'w{n}',)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(taken), 240)
        self.assertEqual(len(set(taken)), 240, 'an item was taken twice')
        self.assertEqual(work.counts('intake')['intake'], {'done': 240})

    def test_lost_claim_cannot_finish(self) -> None:
        work = self.make(lease_seconds=60)
        work.add('intake', 'x', input_rev='r', method='m', now=T0)
        a = work.claim('intake', 'A', now=T0)
        assert a is not None
        b = work.claim('intake', 'B', now=T0 + timedelta(seconds=30))
        self.assertIsNone(b, 'the lease is still valid')
        b = work.claim('intake', 'B', now=T0 + timedelta(seconds=120))
        assert b is not None and b.attempts == 2
        self.assertFalse(work.done(a, now=T0 + timedelta(seconds=130)), 'stale worker finished the item')
        self.assertTrue(work.done(b, now=T0 + timedelta(seconds=140)))
        self.assertEqual(self.state('intake', 'x'), 'done')

    def test_kill_at_each_point_ends_in_the_same_state(self) -> None:
        """The worker dies after the claim (before any effect). The next run finishes the item once."""
        env = dict(os.environ, BOOKSYS_SCRATCH_SCHEMA=self.schema)
        work = self.make(lease_seconds=60)
        work.add('intake', 'k', input_rev='r', method='m', now=datetime.now(timezone.utc) - timedelta(hours=1))
        child = ("import os; from library.postgres import Database; from library.work_items import WorkItems; "
                 "w=WorkItems(Database(os.environ['BOOKSYS_SCRATCH_DSN'], os.environ['BOOKSYS_SCRATCH_SCHEMA']), lease_seconds=60); "
                 "i=w.claim('intake','dying'); assert i is not None; os._exit(17)")
        result = subprocess.run([sys.executable, '-c', child], env=env, cwd=os.getcwd(), capture_output=True)
        self.assertEqual(result.returncode, 17, result.stderr.decode()[-300:])
        self.assertEqual(self.state('intake', 'k'), 'claimed')
        later = datetime.now(timezone.utc) + timedelta(seconds=120)
        item = work.claim('intake', 'next', now=later)
        assert item is not None and item.attempts == 2
        self.assertTrue(work.done(item, now=later))
        self.assertEqual(self.state('intake', 'k'), 'done')
        done_events = [e for e in work.events('intake', 'k') if e[2] == 'done']
        self.assertEqual(len(done_events), 1)

    def test_changed_input_supersedes(self) -> None:
        work = self.make()
        self.assertEqual(work.add('copies', 'a', input_rev='r1', method='m', now=T0), 'created')
        self.assertEqual(work.add('copies', 'a', input_rev='r1', method='m', now=T0), 'same')
        item = work.claim('copies', 'w', now=T0)
        assert item is not None
        self.assertEqual(work.add('copies', 'a', input_rev='r2', method='m', now=T0), 'superseded')   # during the step
        self.assertTrue(work.done(item, now=T0))                       # result of the old input
        self.assertEqual(self.state('copies', 'a'), 'ready', 'the new input must run again')
        again = work.claim('copies', 'w', now=T0)
        assert again is not None and again.input_rev == 'r2' and again.attempts == 1

    def test_result_item_and_successor_commit_together(self) -> None:
        """Store.commit keeps its atomic shape: the domain result, the end of the claim and the successor share one transaction."""
        work = self.make()
        with self.database.connection(write=True) as db:
            db.execute('CREATE TABLE domain(x text PRIMARY KEY)')
        work.add('intake', 'o1', input_rev='r', method='m', now=T0)
        item = work.claim('intake', 'w', now=T0)
        assert item is not None

        class Boom(Exception):
            pass
        with self.assertRaises(Boom):
            with work._tx() as db:                      # the caller's transaction fails after all three writes
                db.execute("INSERT INTO domain VALUES ('result-1')")
                self.assertTrue(work.finish_in(db, item, 'done', now=T0))
                work.add_in(db, 'copies', 'a1', input_rev='r', method='m', now=T0)
                raise Boom()
        with self.database.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM domain').fetchone()[0], 0)
        self.assertEqual(self.state('intake', 'o1'), 'claimed', 'nothing may be half applied')
        self.assertEqual(work.counts('copies'), {})
        with work._tx() as db:                          # the same three writes, now committed together
            db.execute("INSERT INTO domain VALUES ('result-1')")
            self.assertTrue(work.finish_in(db, item, 'done', now=T0))
            self.assertEqual(work.add_in(db, 'copies', 'a1', input_rev='r', method='m', now=T0), 'created')
        self.assertEqual(self.state('intake', 'o1'), 'done')
        self.assertEqual(self.state('copies', 'a1'), 'ready')

    def test_obligation_block_holds_and_releases(self) -> None:
        work = self.make()
        self.assertEqual(work.add('routing', 's:a', input_rev='r', method='m', blocked='remote_review_block', now=T0), 'created')
        self.assertEqual(self.state('routing', 's:a'), 'blocked')
        self.assertIsNone(work.claim('routing', 'w', now=T0 + timedelta(hours=1)), 'a held item is not claimable')
        self.assertEqual(work.add('routing', 's:a', input_rev='r', method='m', now=T0 + timedelta(hours=2)), 'same')
        self.assertEqual(self.state('routing', 's:a'), 'ready', 'the next scan gave no reason: the hold ends')
        work.add('routing', 's:a', input_rev='r2', method='m', blocked='remote_review_block', now=T0 + timedelta(hours=5))
        self.assertEqual(self.state('routing', 's:a'), 'blocked', 'a new input keeps the obligation block')

    def test_retry_limit_then_revive(self) -> None:
        work = self.make(max_attempts={'metadata': 1, 'collection': 1})
        for stage in ('metadata', 'collection'):
            work.add(stage, 'm', input_rev='r', method='m', now=T0)
            at = T0
            for count, minutes in enumerate((10, 20, 40, 80, 160, 320, 360, 360), 1):
                item = work.claim(stage, 'w', now=at)
                assert item is not None and item.attempts == (1 if stage == 'collection' else count)
                self.assertTrue(work.retry(item, 'TimeoutError: provider timeout', timedelta(minutes=5), now=at))
                due = at + timedelta(minutes=10 if stage == 'collection' else minutes)
                self.assertEqual(work.revive(stage, now=due - timedelta(seconds=1)), 0)
                at = due + timedelta(seconds=1)
                self.assertEqual(work.revive(stage, now=at), 1)

    def test_identical_failure_has_no_new_event(self) -> None:
        work = self.make(max_attempts={'metadata': 1})
        work.add('metadata', 'm', input_rev='r', method='m', now=T0)
        for count, (error, expected) in enumerate((('ValueError: a', 1), ('ValueError: a', 1), ('ValueError: b', 2), ('TypeError: b', 3))):
            at = T0 + timedelta(days=count)
            item = work.claim('metadata', 'w', now=at)
            assert item is not None and item.attempts == count + 1
            work.retry(item, error, timedelta(minutes=5), now=at)
            self.assertEqual(sum(event[2] == 'failed' for event in work.events('metadata', 'm')), expected)
            work.revive('metadata', now=at + timedelta(hours=7))


if __name__ == '__main__':
    unittest.main()
