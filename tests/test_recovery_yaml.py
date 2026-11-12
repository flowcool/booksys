"""Lossless native YAML values in retained source payloads; no YAML runtime."""
from datetime import date
from pathlib import Path
import tempfile
import unittest
from library.catalog import Catalog,empty,LibraryError
from library.pg_import import compare
from library.store import Change
from tests.import_fixture import imported


class RecoveryYAMLTests(unittest.TestCase):
    def test_streamed_comparison_preserves_native_types_and_rejects_changed_entity(self):
        shared=["same",None]
        extension={"date":date(2026,11,9),"bytes":b"\x00\xff","set":{"a","b"},"null":None,"reserved":{"$booksys":"data"},"one":shared,"two":shared}
        with tempfile.TemporaryDirectory() as folder:
            state=empty();state["works"]["w"]={"title":"Fixture","extension":extension}
            bound,directory,database=imported(self,Path(folder),state)
            catalog=Catalog(directory)
            self.assertEqual(catalog.get("works","w")["extension"],extension)
            self.assertEqual(compare(bound,directory,database=database)["status"],"equal")
            catalog.backend().commit(Change(upserts={"works":{"w":{"title":"Current","extension":extension}}}),expected_revision=0)
            self.assertEqual(compare(bound,directory,database=database)["status"],"different")
            self.assertEqual(Catalog(directory).get("works","w")["title"],"Current")
