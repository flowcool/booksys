"""Versioned one-way migration commands on an isolated PostgreSQL target."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from library.pg_import import main,prepare_manifest,_write_json
from library.catalog import Catalog,LibraryError
from library.store import Change
from tests.import_fixture import source,target,imported


class StorageToolTests(unittest.TestCase):
    def invoke(self,*args):
        output=io.StringIO()
        with redirect_stdout(output):code=main(list(args))
        return code,json.loads(output.getvalue())

    def test_reviewed_code_rebind_refuses_old_authority_drift_and_preserves_business_state(self):
        from library.pg_import import import_snapshot
        with tempfile.TemporaryDirectory() as folder:
            bound,directory,database=imported(self,Path(folder))
            store=Catalog(directory).backend()
            store.commit(Change(upserts={"works":{"new":{"title":"Retained"}}}),expected_revision=7)
            before=store.export();selector=(directory/"storage-authority.yaml").read_bytes()
            with patch("library.pg_import.code_digest",return_value="changed-code"):
                with self.assertRaises(LibraryError):import_snapshot(bound,directory,database=database)
            self.assertEqual(store.export(),before)
            self.assertEqual((directory/"storage-authority.yaml").read_bytes(),selector)

    def test_frozen_import_rejects_a_different_approved_source_before_creating_database(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);frozen=source(root/"source");database=target(self)
            bound=prepare_manifest(frozen,candidate="fixture");path=root/"manifest.json";_write_json(path,bound)
            code,result=self.invoke("import","--manifest",str(path),"--state",str(root/"target"),"--dsn-file",os.environ["BOOKSYS_TEST_PG_DSN_FILE"],"--namespace",database.schema,"--expected-manifest-sha256","a"*64)
            self.assertEqual(code,2);self.assertFalse((root/"target").exists())

    def test_import_checkpoint_activate_current_write_and_reverse(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source(root/"source");database=target(self);dsn=os.environ["BOOKSYS_TEST_PG_DSN_FILE"]
            path=root/"manifest.json"
            code,result=self.invoke("manifest","--source",str(root/"source"),"--candidate","fixture","--output",str(path));self.assertEqual(code,0)
            common=("--manifest",str(path),"--state",str(root/"target"),"--dsn-file",dsn,"--namespace",database.schema)
            self.assertEqual(self.invoke("import",*common)[0],0)
            self.assertEqual(self.invoke("compare",*common,"--report",str(root/"semantic.json"))[0],0)
            self.assertEqual(self.invoke("select",*common,"--runtime-dsn-file",dsn)[0],0)
            catalog=Catalog(root/"target");catalog.backend().commit(Change(upserts={"works":{"new":{"title":"Current"}}}),expected_revision=7)
            code,result=self.invoke("inspect","--dsn-file",dsn,"--namespace",database.schema,"--report",str(root/"current.json"))
            self.assertEqual(code,0);self.assertEqual(result["revision"],8)
            self.assertEqual(self.invoke("import",*common)[0],2)
            for name in ("recover-yaml","restore"):
                with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as rejected:main([name])
                self.assertEqual(rejected.exception.code,2)
            self.assertEqual(Catalog(root/"target").get("works","new"),{"title":"Current"})
