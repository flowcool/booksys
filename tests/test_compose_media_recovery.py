"""Frozen local database binds retained external media without copying it."""
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from library.pg_import import prepare_manifest,read_manifest
from library.primitives import LibraryError
from tests.import_fixture import source


class MediaRecoveryTests(unittest.TestCase):
    def test_snapshot_keeps_sqlite_local_and_binds_retained_external_attachments(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);frozen=source(root/"local"/"source");media=root/"nas"/"attachments";media.mkdir(parents=True)
            raw=b"immutable-cover";identity=hashlib.sha256(raw).hexdigest();(media/identity).write_bytes(raw)
            with closing(sqlite3.connect(frozen/"catalogue.sqlite")) as db:
                db.execute("CREATE TABLE attachments(digest TEXT,encoding TEXT,original_length INTEGER,relative_path TEXT)")
                db.execute("INSERT INTO attachments VALUES(?,?,?,?)",(identity,"utf8-legacy-cover",len(raw),"payloads/"+identity));db.commit()
            for invalid in ({"attachments":{"root":str(media),"files":{}}},
                            {"attachments":{"root":"relative-root","files":{identity:identity}}}):
                with self.assertRaises(LibraryError):prepare_manifest(frozen,candidate="fixture",media=invalid)
            bound=prepare_manifest(frozen,candidate="fixture",media={"attachments":{"root":str(media),"files":{identity:identity}}})
            self.assertTrue((frozen/"catalogue.sqlite").is_file())
            self.assertFalse((frozen/"payloads").exists())
            self.assertEqual(bound["media"]["attachments"]["root"],str(media))
            read_manifest(bound)
            (media/identity).write_bytes(b"corrupt")
            with self.assertRaises(LibraryError):read_manifest(bound)

    def test_referenced_external_proof_requires_exact_binding(self):
        from library.primitives import digest
        import yaml
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);frozen=source(root/'source');media=root/'proofs';media.mkdir()
            proof={'asset':'a'*64,'pages':[]};identity=digest(proof);raw=yaml.safe_dump(proof).encode()
            (media/(identity+'.yaml')).write_bytes(raw)
            with closing(sqlite3.connect(frozen/'catalogue.sqlite')) as db:
                db.execute('CREATE TABLE decision_pieces(piece TEXT,decision TEXT)')
                db.execute('INSERT INTO decision_pieces VALUES(?,?)',(identity,'retained'));db.commit()
            with self.assertRaises(LibraryError):
                prepare_manifest(frozen,candidate='fixture',media={'pieces':{'root':str(media),'files':{}}})
            binding={'pieces':{'root':str(media),'files':{identity+'.yaml':hashlib.sha256(raw).hexdigest()}}}
            bound=prepare_manifest(frozen,candidate='fixture',media=binding)
            (media/(identity+'.yaml')).write_text('changed: true\n')
            with self.assertRaises(LibraryError):read_manifest(bound)
