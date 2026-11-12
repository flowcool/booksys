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
        self.work = WorkItems(self.store._database, idempotent=frozenset({'intake', 'copies'}), max_attempts={'intake': 3, 'copies': 3})
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














if __name__ == '__main__':
    unittest.main()
