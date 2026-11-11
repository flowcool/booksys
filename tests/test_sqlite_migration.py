"""One-way import interruption; old SQLite transport is retired."""
from tests import test_pg_import
import unittest
from pathlib import Path
import tempfile
import sqlite3
from contextlib import closing
from tests.import_fixture import source
from library.pg_import import prepare_manifest
from library.primitives import LibraryError


class MigrationTests(unittest.TestCase):
    target = test_pg_import.ImportTests.target
    # Preserve the original two migration obligations through their public seam.
    test_interrupted_import_is_not_a_runtime_authority_and_resumes_only_same_source = test_pg_import.ImportTests.test_interrupted_import_stays_unselected_and_resumes_same_bound_target
    def test_nonprefix_ledger_is_rejected_before_import(self):
        with tempfile.TemporaryDirectory() as folder:
            frozen=source(Path(folder)/'source')
            with closing(sqlite3.connect(frozen/'catalogue.sqlite')) as db:
                db.execute('UPDATE meta SET ledger=?',('["missing"]',));db.commit()
            with self.assertRaises(LibraryError):prepare_manifest(frozen,candidate='fixture')
