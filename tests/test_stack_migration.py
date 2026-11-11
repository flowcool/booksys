"""Versioned migration preserves sources and refuses drift or blind replay."""
from pathlib import Path
import tempfile
import unittest

from library.stack_migration import preview, copy_verified, verify_current
from library.primitives import LibraryError


class StackMigrationTests(unittest.TestCase):
    def test_preview_does_not_write_and_copy_retains_exact_originals(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, target, evidence = root/'source', root/'target', root/'proof'
            source.mkdir()
            (source/'queue.db').write_bytes(b'queue and accounts')
            (source/'decisions.yaml').write_bytes(b'immutable decision')
            plan = preview([{'source': str(source), 'target': str(target), 'logical': str(target)}])
            self.assertFalse(target.exists())
            self.assertFalse(evidence.exists())
            copy_verified(plan, evidence)
            self.assertEqual((source/'queue.db').read_bytes(), (target/'queue.db').read_bytes())
            self.assertEqual((source/'decisions.yaml').read_bytes(), b'immutable decision')
            self.assertTrue((evidence/'relocation.json').is_file())
            verify_current(plan)
            with self.assertRaises((LibraryError, FileExistsError)):
                copy_verified(plan, evidence)

    def test_drift_stops_before_target_creation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, target = root/'source', root/'target'
            source.mkdir()
            (source/'book.cbz').write_bytes(b'old')
            plan = preview([{'source': str(source), 'target': str(target), 'logical': str(target)}])
            (source/'book.cbz').write_bytes(b'new')
            with self.assertRaises(LibraryError):
                copy_verified(plan, root/'evidence')
            self.assertFalse(target.exists())

    def test_new_effects_forbid_old_snapshot_verdict_without_overwriting_them(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, target = root/'source', root/'target'
            source.mkdir()
            (source/'queue.db').write_bytes(b'old queue')
            plan = preview([{'source': str(source), 'target': str(target), 'logical': str(target)}])
            copy_verified(plan, root/'evidence')
            (target/'queue.db').write_bytes(b'new download and decision')
            with self.assertRaises(LibraryError):
                verify_current(plan)
            self.assertEqual((target/'queue.db').read_bytes(), b'new download and decision')
