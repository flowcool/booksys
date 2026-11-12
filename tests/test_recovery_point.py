"""Historical input checks and current PostgreSQL forward recovery."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
import yaml

from library.catalog import LibraryError
from library.pg_import import prepare_manifest,read_manifest,import_snapshot,compare
from tests.import_fixture import source,target
from tests import test_pg_import


class RecoveryTests(unittest.TestCase):
    target=test_pg_import.ImportTests.target
    test_corrupt_database_restore_replays_retained_suffix_without_selecting_writers=test_pg_import.ImportTests.test_pending_indexed_tail_and_immutable_suffix_project_once_after_selection
    test_restore_uses_new_directory_and_keeps_corrupt_source_and_point=test_pg_import.ImportTests.test_interrupted_import_stays_unselected_and_resumes_same_bound_target
    test_activity_backup_preserves_producer_session_and_event_ids=test_pg_import.ImportTests.test_activity_keeps_exact_ids_sessions_times_gap_and_payload

    def test_legacy_checkpoint_remains_valid_without_ignoring_new_stage_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            frozen=source(Path(folder)/"source")
            bound=prepare_manifest(frozen,candidate="fixture")
            self.assertEqual(read_manifest(bound)["source"]["revision"],7)
            (frozen/"files"/"unknown-stage-progress.yaml").write_text("phase: creating\n")
            with self.assertRaises(LibraryError):prepare_manifest(frozen,candidate="fixture")
            with self.assertRaises(LibraryError):read_manifest(bound)

    def test_post_write_checkpoint_preserves_journals_and_detects_corruption(self):
        from library.operational_state import CopyOperations
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);frozen=source(root/"source")
            journal=frozen/"files"/"library-operations"/"operation.yaml";journal.parent.mkdir()
            operation={"id":"operation","phase":"verified","target":"retained","absence":None}
            journal.write_text(yaml.safe_dump(operation))
            bound=prepare_manifest(frozen,candidate="fixture");database=target(self)
            import_snapshot(bound,root/"target",database=database)
            self.assertEqual(CopyOperations(database,"publication").get("operation"),operation)
            self.assertEqual(compare(bound,root/"target",database=database)["status"],"equal")
            journal.write_text("altered: true\n")
            with self.assertRaises(LibraryError):read_manifest(bound)
            self.assertEqual(CopyOperations(database,"publication").get("operation"),operation)

    def test_checkpoint_refuses_missing_or_altered_immutable_evidence(self):
        for corruption in ("missing","altered"):
            with self.subTest(corruption=corruption),tempfile.TemporaryDirectory() as folder:
                frozen=source(Path(folder)/"source");proof=frozen/"pieces"/"proof.yaml";proof.parent.mkdir();proof.write_text("retained: true\n")
                import hashlib
                media={"pieces":{"root":str(proof.parent),"files":{"proof.yaml":hashlib.sha256(proof.read_bytes()).hexdigest()}}}
                bound=prepare_manifest(frozen,candidate="fixture",media=media)
                if corruption=="missing":proof.rename(proof.with_name("preserved-proof.yaml"))
                else:proof.write_text("altered: true\n")
                with self.assertRaises(LibraryError):read_manifest(bound)
