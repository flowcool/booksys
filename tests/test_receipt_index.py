"""Queue-control history must not make each catalogue check scan all receipts."""
from pathlib import Path
import tempfile
import unittest

from library.primitives import empty
from postgres_fixture import create_store


class ReceiptIndexTests(unittest.TestCase):
    def test_catalogue_revision_uses_selective_index_and_tracks_receipt_transitions(self):
        with tempfile.TemporaryDirectory() as folder:
            store = create_store(self, Path(folder), empty())
            # Fixture loading is separate from the ordinary ten-second checks.
            with store._database.connection(write=True, seconds=60) as db:
                db.execute("INSERT INTO commit_receipts(key,digest,state,catalogue,receipt) "
                           "SELECT 'control-'||n,md5(n::text),'committed',false,repeat(md5(n::text),48) "
                           "FROM generate_series(1,75000) n")
                db.execute('ANALYZE commit_receipts')
            with store._database.connection() as db:
                plan = db.execute("EXPLAIN (FORMAT JSON) SELECT count(*) FROM commit_receipts "
                                  "WHERE catalogue AND state='committed'").fetchone()[0]
                self.assertEqual(store.check(db)[0], 0)
            self.assertIn('commit_receipts_catalogue_committed', str(plan))
            with store._database.connection(write=True) as db:
                db.execute("UPDATE commit_receipts SET catalogue=true,state='registered' WHERE key='control-1'")
                self.assertEqual(store.check(db)[0], 0)
                db.execute("UPDATE commit_receipts SET state='committed' WHERE key='control-1'")
                self.assertEqual(store.check(db)[0], 1)
                db.execute("UPDATE commit_receipts SET state='aborted' WHERE key='control-1'")
                self.assertEqual(store.check(db)[0], 0)


if __name__ == '__main__':
    unittest.main()
