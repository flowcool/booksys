"""Selected PostgreSQL authority and retained business outcomes."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from library.catalog import Catalog, LibraryError
from library.store import Change
from tests.import_fixture import imported
from tests import test_pg_import


class AuthorityTests(unittest.TestCase):
    target = test_pg_import.ImportTests.target
    test_cutover_and_recovery_resume_after_exchange_before_manifest = test_pg_import.ImportTests.test_bound_legacy_selector_is_replaced_and_interrupted_selection_resumes
    test_an_unselected_database_is_never_silently_ignored = test_pg_import.ImportTests.test_interrupted_import_stays_unselected_and_resumes_same_bound_target

    def test_guard_blocks_old_reader_and_writer_and_recovery_keeps_new_values(self):
        from library.sqlite_runtime import connection
        from library.migration import recover_yaml
        with tempfile.TemporaryDirectory() as folder:
            manifest,directory,database=imported(self,Path(folder))
            catalog=Catalog(directory)
            catalog.backend().commit(Change(upserts={"works":{"new":{"title":"After import"}}}),expected_revision=7)
            for operation in (lambda: connection(directory/"catalogue.sqlite",write=True),lambda: recover_yaml(directory)):
                with self.assertRaises(LibraryError):operation()
            self.assertEqual(Catalog(directory).get("works","new"),{"title":"After import"})

    def test_new_publication_and_admission_results_reverse_export_losslessly(self):
        from test_library_publish import setup_library
        from library.publish import Publisher, Capacity
        from library.admission import AdmissionPublisher
        from library.operational_state import CopyOperations
        from library.migration import recover_yaml
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            catalog,reception,original,asset=setup_library(root,self)
            publication=Publisher(catalog,reception,root/"library",isolated=True,capacity=Capacity(reserve=0)).publish(asset)
            admission=AdmissionPublisher(catalog,reception,root/"library",isolated=True,capacity=Capacity(reserve=0)).publish_asset(asset)
            self.assertEqual(publication["phase"],"committed")
            self.assertEqual(admission["phase"],"committed")
            before=catalog.backend().export()
            with self.assertRaises(LibraryError):recover_yaml(catalog.directory)
            self.assertEqual(Catalog(catalog.directory).read(),before)
            self.assertEqual(CopyOperations(catalog.backend()._database,"publication").get(publication["id"]),publication)
            self.assertTrue(original.is_file())

    def test_manifest_binds_exact_code_source_and_read_only_selection(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest,directory,database=imported(self,Path(folder))
            with patch("library.authority.code_digest",return_value="c"*64):
                with self.assertRaisesRegex(LibraryError,"code differs"):Catalog(directory)
            with database.connection(write=True) as db:db.execute("UPDATE meta SET imported_source_digest=%s",("d"*64,))
            with self.assertRaisesRegex(LibraryError,"source differs"):Catalog(directory)
            (directory/"storage-authority.yaml").chmod(0o600)
            with self.assertRaisesRegex(LibraryError,"read-only"):Catalog(directory)
