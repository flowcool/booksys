"""Legacy SQLite checkpoint schedule remains evidence, never a restore authority."""
from pathlib import Path
import tempfile
import unittest
from library.catalog import Catalog,LibraryError
from library.store import Change
from library.pg_import import prepare_manifest,import_snapshot,read_manifest
from tests.import_fixture import source,target,private_selector


class RecoveryScheduleTests(unittest.TestCase):
    def test_commit_time_and_cycle_thresholds_keep_previous_verified_point(self):
        from library.recovery_schedule import RecoverySchedule
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);frozen=source(root/"source")
            proof=frozen/"files"/"automatic-recovery.yaml";proof.write_text("schema: 1\nrequested: true\nlast_verified: retained-point\nrevision: 7\n")
            before=proof.read_bytes();bound=prepare_manifest(frozen,candidate="fixture");database=target(self)
            import_snapshot(bound,root/"target",database=database);private_selector(root/"target",database)
            from library.pg_import import select
            select(bound,root/"target",database=database)
            catalog=Catalog(root/"target");catalog.backend().commit(Change(upserts={"works":{"new":{"title":"Current"}}}),expected_revision=7)
            with self.assertRaises(LibraryError):import_snapshot(bound,root/"target",database=database)
            self.assertEqual(proof.read_bytes(),before)
            self.assertEqual(read_manifest(bound)["source"]["revision"],7)
            self.assertEqual(Catalog(root/"target").get("works","new"),{"title":"Current"})
