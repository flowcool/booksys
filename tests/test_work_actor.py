"""WorkActor with a real Store on a disposable PostgreSQL (BOOKSYS_TEST_PG_DSN_FILE, mode 0600).

Proves the transaction shape of design.md section 9: result + end of claim + successor commit together or not at all.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

T0 = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)


@unittest.skipUnless(os.environ.get('BOOKSYS_TEST_PG_DSN_FILE'), 'BOOKSYS_TEST_PG_DSN_FILE is not set')
class WorkActorTests(unittest.TestCase):
    def setUp(self) -> None:
        import sys
        sys.path.insert(0, str(Path(__file__).parent))
        from postgres_fixture import create_store
        from library.primitives import empty
        from library.work_items import WorkItems
        from library.work_stages import work_stages
        self.dir = Path(tempfile.mkdtemp(prefix='work-actor-'))
        self.addCleanup(lambda: __import__('shutil').rmtree(self.dir, ignore_errors=True))
        (self.dir / 'state').mkdir()
        self.store = create_store(self, self.dir / 'state', empty())
        WorkItems.upgrade(self.store._database)
        self.work = WorkItems(self.store._database, max_attempts={'intake': 3, 'copies': 3})
        self.addCleanup(self.work.close)
        os.environ['BOOKSYS_WORK_STAGES'] = 'intake,copies'
        self.addCleanup(lambda: os.environ.pop('BOOKSYS_WORK_STAGES', None))
        self.assertEqual(work_stages(), frozenset({'intake', 'copies'}))

    def actor(self, execute, queue='intake', **hooks):  # type: ignore[no-untyped-def]
        from library.work_actor import WorkActor
        clock = hooks.pop('clock', lambda: T0 + timedelta(minutes=5))
        return WorkActor(self.store, self.work, queue, execute, clock=clock, **hooks)

    def state(self, stage: str, item: str) -> str:
        with self.store._database.connection() as db:
            return str(db.execute('SELECT state FROM work_items WHERE stage=%s AND item=%s', (stage, item)).fetchone()[0])

    def test_browser_deferred_keeps_deadline_without_spending_attempt(self):
        from booksys_core.scheduler import Deferred, Schedule
        from booksys_core.browser_outcomes import Outcome, BrowserOutcome
        from booksys_core.diagnostics import exception_details
        due = T0 + timedelta(minutes=20)
        schedule = Schedule({}, timestamp=T0.timestamp())
        schedule.record_outcome('source', Outcome('remote_rate_limited', 'remote',
                                retry_after_state='valid', retry_after_seconds=1200), timestamp=T0.timestamp())
        def execute(task, now):
            raise Deferred(schedule.record)
        self.work.add('intake', 'source', input_rev='r', method='m', now=T0)
        actor = self.actor(execute)
        self.assertEqual(actor.step(now=T0), 'defer')
        self.assertEqual(self.state('intake', 'source'), 'waiting')
        self.assertIsNone(self.work.claim('intake', 'next', now=due - timedelta(seconds=1)))
        item = self.work.claim('intake', 'next', now=due)
        self.assertEqual(item.attempts, 1)
        self.assertEqual(exception_details(Deferred(schedule.record))['exception_type'], 'Deferred')
        self.assertEqual(exception_details(BrowserOutcome(Outcome('browser_timeout', 'local')))['exception_type'], 'BrowserOutcome')

    def test_nested_failure_keeps_request_and_attempt_when_refs_change(self):
        from unittest.mock import patch
        from booksys_core.diagnostics import event, emit
        def execute(task, now):
            emit(service='collector', stage='collection', code='failed', reason='unknown_effect',
                 refs=[{'kind': 'source', 'id': 'synthetic-source'}])
            raise OSError('synthetic failure')
        self.work.add('intake', 'source', input_rev='r', method='m', now=T0)
        events = []
        with patch('booksys_core.diagnostics._default') as sink:
            sink.emit.side_effect = lambda value: events.append(value) or True
            self.assertEqual(self.actor(execute).step(now=T0), 'retry')
        failures = [event(**{k: v for k, v in value.items() if k not in
                    ('schema', 'event_id', 'at', 'level')}) for value in events if value['code'] in ('failed', 'waiting')]
        self.assertEqual(len(failures), 2)
        self.assertEqual([value['code'] for value in failures], ['failed', 'waiting'])
        self.assertEqual(failures[1]['task_state'], 'waiting')
        self.assertEqual(failures[1]['outcome'], 'retry')
        self.assertEqual(failures[1]['next_attempt'], (T0 + timedelta(minutes=10)).isoformat())
        self.assertEqual(failures[0]['refs'], [{'kind': 'source', 'id': 'synthetic-source'}])
        self.assertEqual(failures[0]['request_id'], failures[1]['request_id'])
        self.assertEqual([value['attempt'] for value in failures], [1, 1])














if __name__ == '__main__':
    unittest.main()
