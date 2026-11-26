from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import tempfile
from threading import Barrier
import unittest
from unittest.mock import MagicMock, patch

from library.primitives import telegram_origin_id
from postgres_fixture import create_catalog


@unittest.skipUnless(os.environ.get("BOOKSYS_TEST_PG_DSN_FILE"), "requires disposable PostgreSQL")
class TelegramPullTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"BOOKSYS_WORK_STAGES": "intake,metadata,copies"}))
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.feed = self.root / "receipts"
        self.catalog = create_catalog(self, self.root / "state")
        self.now = datetime.now(timezone.utc)

    def head(self, scope, *, stream="initial", ready=1):
        folder = self.feed / scope
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "head.json").write_text(json.dumps({
            "schema": 1, "stream_id": stream, "ready_page": ready,
        }))

    def page(self, scope, page, *rows):
        (self.feed / scope / f"{page:08d}.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows)
        )

    def row(self, scope="BD_X18", message=9100, *, name="Album.cbz", data=b"comics"):
        peer, topic = {"BD_X18": (1235693416, None), "bd_fr": (1219883119, 96714),
                       "webtoons-vf": (1288796629, None)}[scope]
        prefix = f"{peer}_{message}" if scope == "webtoons-vf" else str(message)
        return {"schema": 1, "peer": peer, "topic": topic, "message": message,
                "path": f"{scope}/{prefix}_{name}", "original_name": name, "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(), "completion_proof": "native",
                "completion_ref": f"receipts/{message}.json"}

    def test_poll_one_exact_page_and_resume_from_postgresql(self):
        from library.telegram_pull import TelegramPull
        self.head("BD_X18", ready=2)
        self.page("BD_X18", 1, self.row())
        self.page("BD_X18", 2, self.row(message=9101))
        self.head("bd_fr")
        self.page("bd_fr", 1, self.row("bd_fr"))
        pull = TelegramPull(self.catalog, ("BD_X18", "bd_fr"), root=self.feed)
        first = telegram_origin_id(1235693416, 9100)
        second = telegram_origin_id(1235693416, 9101)
        other = telegram_origin_id(1219883119, 9100)
        pull.poll(now=self.now)
        self.assertEqual(self.catalog.get("origins", first)["path"], "telegram-archive/BD_X18/9100_Album.cbz")
        self.assertIsNone(self.catalog.get("origins", second))
        self.assertIsNone(self.catalog.get("origins", other))
        pull.poll(now=self.now)
        self.assertIsNotNone(self.catalog.get("origins", other))
        self.assertIsNone(self.catalog.get("origins", second))
        TelegramPull(self.catalog, ("BD_X18",), root=self.feed).poll(now=self.now)
        self.assertIsNotNone(self.catalog.get("origins", second))
        with self.catalog.backend()._database.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM work_items WHERE stage=\x27intake\x27").fetchone()[0], 3)

    def test_missing_archive_waits_and_disabled_worker_can_read_queued_source(self):
        from io import BytesIO
        from zipfile import ZipFile
        from library.origins import resolve_asset_origin
        from library.stage_runtime import StageRuntime
        from library.telegram_pull import TelegramPull
        from test_library_readers import image_bytes

        buf = BytesIO()
        with ZipFile(buf, "w") as comic:
            comic.writestr("001.png", image_bytes())
        data = buf.getvalue()
        row = self.row(data=data)
        second = self.row(message=9101, data=data)
        self.head("BD_X18")
        self.page("BD_X18", 1, row, second)
        TelegramPull(self.catalog, ("BD_X18",), root=self.feed).poll(now=self.now)

        archive = self.root / "archive"
        originals = self.root / "originals"
        substitute = originals / "telegram-archive" / row["path"]
        substitute.parent.mkdir(parents=True)
        substitute.write_bytes(data)
        (self.root / "work").mkdir()
        self.enterContext(patch("library.origins.ARCHIVE_ROOT", archive))
        self.enterContext(patch("library.intake.EXTERNAL_ROOT", originals))
        self.enterContext(patch("library.stage_runtime.require_nas"))
        self.enterContext(patch("library.decode.WORK_ROOT", str(self.root / "work")))
        self.enterContext(patch.dict(os.environ, {"BOOKSYS_TELEGRAM_PULL": "0"}))

        runtime = StageRuntime(self.root, self.root / "downloads", self.root / "collector",
                               providers=MagicMock(), client=None, browser=None, isolated=True)
        self.assertIsNone(TelegramPull.configured(runtime.catalog))
        actor = runtime.actor("intake")
        self.addCleanup(actor.work.close)
        self.assertEqual(actor.step(now=self.now), "recovery_wait")
        self.assertFalse(self.catalog.read()["assets"])
        self.assertFalse(archive.exists())
        self.assertEqual(substitute.read_bytes(), data)

        actor.clock = None
        for receipt in (row, second):
            source = archive / receipt["path"]
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(data)
        stamp = self.now + timedelta(seconds=35)
        unreadable = archive / row["path"]
        unreadable.chmod(0)
        self.assertEqual(actor.step(now=stamp), "recovery_wait")
        with patch("library.readers.sha256", side_effect=PermissionError(errno.EACCES, "Fixture access loss")):
            self.assertEqual(actor.step(now=stamp), "defer")
        self.assertFalse(self.catalog.read()["assets"])
        unreadable.chmod(0o600)
        stamp += timedelta(seconds=35)
        self.assertEqual(actor.step(now=stamp), "done")
        self.assertEqual(actor.step(now=stamp), "done")
        state = self.catalog.read()
        key, = state["assets"]
        self.assertEqual(key, row["sha256"])
        self.assertEqual(len(state["assets"][key]["origins"]), 2)
        oid, source = resolve_asset_origin(runtime.catalog, runtime.reception, state, key)
        self.assertEqual(source, archive / state["origins"][oid]["path"].removeprefix("telegram-archive/"))
        self.assertEqual(runtime.discover_business(now=stamp), 2)

        self.head("BD_X18", ready=2)
        self.page("BD_X18", 2, row)
        TelegramPull(self.catalog, ("BD_X18",), root=self.feed).poll(now=stamp)
        self.assertEqual(self.catalog.read()["origins"], state["origins"])
        self.assertEqual(actor.work.counts("intake"), {"intake": {"done": 2}})

    def test_webtoon_receipts_reach_review_or_publish_cbz_through_normal_queues(self):
        from PIL import Image
        from library.api import PublicAPI
        from library.stage_runtime import StageRuntime
        from library.telegram_pull import TelegramPull

        archive = self.root / "archive"
        scope = "webtoons-vf"
        (archive / scope).mkdir(parents=True)
        (self.root / "work").mkdir()
        rows = []
        refs = [{"title": "Sample Series", "index": "WEBTOONS_LISTS/8", "anchor": 4}]
        for index, evidence in enumerate((
            {"origins": refs, "index_title": "Sample Series", "reposts": [87]},
            {"index_review": "missing-valid-index-evidence"},
            {"origins": [*refs, {"title": "Other Series", "index": "WEBTOONS_LISTS/12", "anchor": 5}],
             "index_review": "conflicting-index-titles"},
        )):
            message = 50 + index
            with tempfile.TemporaryFile() as handle:
                Image.new("RGB", (100 + index, 100), "white").save(handle, format="PDF")
                handle.seek(0)
                data = handle.read()
            row = self.row(scope, message, name=f"Sample Chapitre 0{7 + index}.pdf", data=data)
            (archive / row["path"]).write_bytes(data)
            rows.append({**row, **evidence})
        self.head(scope)
        self.page(scope, 1, *rows)
        TelegramPull(self.catalog, (scope,), root=self.feed).poll(now=self.now)
        providers = MagicMock(events=[])
        providers.bdnex.match.side_effect = AssertionError("Webtoons use index evidence")
        runtime = StageRuntime(self.root, self.root / "downloads", self.root / "collector",
                               providers=providers, client=None, browser=None, isolated=True)
        self.enterContext(patch("library.origins.ARCHIVE_ROOT", archive))
        self.enterContext(patch("library.stage_runtime.require_nas"))
        self.enterContext(patch("library.decode.WORK_ROOT", str(self.root / "work")))
        intake, metadata, copies = (runtime.actor(stage) for stage in ("intake", "metadata", "copies"))
        for actor in (intake, metadata, copies):
            self.addCleanup(actor.work.close)
        for _ in rows:
            self.assertEqual(intake.step(now=self.now), "done")
        self.assertEqual(runtime.discover_business(now=self.now), 3)
        for _ in rows:
            self.assertEqual(metadata.step(now=self.now), "done")
            self.assertEqual(copies.step(now=self.now), "done")
        indexed, missing, conflict = (self.catalog.get("assets", row["sha256"]) for row in rows)
        self.assertEqual(indexed["work_result"]["work"], {"series": "Sample Series", "number": "7"})
        target = indexed["admission"]["target"]
        self.assertTrue(target.startswith("webtoon/Sample Series/7/"))
        self.assertTrue((self.root / "library" / target / "book.cbz").is_file())
        self.assertEqual(missing["work_result"]["reason_code"], "webtoon_missing_index_title")
        self.assertEqual(conflict["work_result"]["reason_code"], "webtoon_conflicting_series")
        api = PublicAPI(self.root, host="localhost", reception=runtime.reception)
        counts = api.invoke("status", {"host": "localhost", "origin": "", "token": "", "payload": {}})["counts"]
        self.assertEqual((counts["webtoon_assets"], counts["webtoon_review_assets"], counts["webtoon_copies"]), (1, 2, 1))
        providers.bdnex.match.assert_not_called()

    def test_concurrent_registration_and_conflict_keep_one_origin_and_task(self):
        from library.operational_state import CollectorState
        from library.telegram_pull import TelegramPull, receipt_origin
        from library.work_items import WorkItems

        row = self.row()
        pull = TelegramPull(self.catalog, ("BD_X18",), root=self.feed)
        oid, origin = receipt_origin(row, "BD_X18")
        backend = self.catalog.backend()
        commit = backend.commit
        barrier = Barrier(2)

        def race(*args, **kwargs):
            barrier.wait(timeout=10)
            return commit(*args, **kwargs)

        def register():
            pull.register("BD_X18", 1, oid, origin, now=self.now)

        # Each submitter has already passed its local file lock. Exercise the
        # common PostgreSQL transaction guard as if they used separate hosts.
        with (patch.object(backend, "commit", side_effect=race),
              patch("library.stage_execution.effect_scopes", return_value=nullcontext()),
              ThreadPoolExecutor(max_workers=2) as pool):
            first, second = pool.submit(register), pool.submit(register)
            first.result(timeout=15)
            second.result(timeout=15)
        work = WorkItems(backend._database)
        self.addCleanup(work.close)
        self.assertEqual(work.counts("intake"), {"intake": {"ready": 1}})
        self.assertEqual(dict(self.catalog.records("origins")), {oid: origin})
        self.head("BD_X18")
        self.page("BD_X18", 1, {**row, "sha256": "f" * 64})
        pull.poll(now=self.now)
        state = CollectorState(self.catalog.directory, database=backend._database).telegram_checkpoint("BD_X18")
        self.assertEqual(state["page"], 0)
        self.assertIn("bytes conflict", state["blocked"])
        self.assertEqual(dict(self.catalog.records("origins")), {oid: origin})

    def test_blocked_page_requires_selected_replacement_and_does_not_block_other_scopes(self):
        from booksys_core.errors import LibraryError
        from library.operational_state import CollectorState
        from library.stage_runtime import StageRuntime
        from library.telegram_pull import TelegramPull

        runtime = StageRuntime(self.root, self.root / "downloads", self.root / "collector",
                               providers=MagicMock(), client=None, browser=None, isolated=True)
        with self.assertRaisesRegex(LibraryError, "Native observation unavailable"):
            runtime.native.discover_intake(runtime.tasks, now=self.now)
        self.head("BD_X18", ready=2)
        self.head("bd_fr")
        self.page("bd_fr", 1, self.row("bd_fr"))
        pull = TelegramPull(self.catalog, ("BD_X18", "bd_fr"), root=self.feed)
        state = CollectorState(self.catalog.directory, database=self.catalog.backend()._database)
        self.page("BD_X18", 2, self.row(message=9101))
        with patch("library.telegram_pull.emit") as activity:
            pull.poll(now=self.now)
            self.assertIn("BD_X18 page 1", activity.call_args.kwargs["detail"])
            self.assertEqual(activity.call_args.kwargs["code"], "waiting")
            pull.poll(now=self.now)
            self.assertEqual(state.telegram_checkpoint("bd_fr")["page"], 1)
            self.assertEqual(state.telegram_checkpoint("BD_X18")["page"], 0)
            self.page("BD_X18", 1, *[self.row(message=msg) for msg in range(1, 34)])
            pull.poll(now=self.now)
            self.assertIn("blocked", state.telegram_checkpoint("BD_X18"))
            self.assertIn("BD_X18 page 1", activity.call_args.kwargs["detail"])
            self.head("BD_X18", stream="replacement")
            self.page("BD_X18", 1, self.row())
            TelegramPull(self.catalog, ("BD_X18",), root=self.feed).poll(now=self.now)
            self.assertIn("blocked", state.telegram_checkpoint("BD_X18"))
            TelegramPull(self.catalog, ("BD_X18",), root=self.feed,
                         streams={"BD_X18": "replacement"}).poll(now=self.now)
        self.assertEqual(state.telegram_checkpoint("BD_X18"), {"stream_id": "replacement", "page": 1})
        self.assertIsNone(self.catalog.get("origins", telegram_origin_id(1235693416, 9101)))

    def test_bad_receipts_cannot_register_a_partial_page(self):
        from library.operational_state import CollectorState
        from library.telegram_pull import TelegramPull

        state = CollectorState(self.catalog.directory, database=self.catalog.backend()._database)
        invalid_rows = (
            {"extra": "X" * 65536},
            {"extra": float("nan")},
            {"topic": 96714},
            {"path": "BD_X18/../9100_Album.cbz"},
            {"size": 2**63},
        )
        for index, invalid in enumerate(invalid_rows):
            with self.subTest(invalid=list(invalid)):
                stream = f"replacement-{index}"
                self.head("BD_X18", stream=stream)
                self.page("BD_X18", 1, self.row(message=9101), {**self.row(), **invalid})
                TelegramPull(self.catalog, ("BD_X18",), root=self.feed,
                             streams={"BD_X18": stream}).poll(now=self.now)
                self.assertEqual(state.telegram_checkpoint("BD_X18")["page"], 0)
                self.assertIn("blocked", state.telegram_checkpoint("BD_X18"))
        self.assertFalse(self.catalog.read()["origins"])

    def test_nonregular_page_blocks_only_its_scope(self):
        from library.operational_state import CollectorState
        from library.telegram_pull import TelegramPull

        self.head("BD_X18")
        (self.feed / "BD_X18" / "00000001.jsonl").mkdir()
        self.head("bd_fr")
        self.page("bd_fr", 1, self.row("bd_fr"))
        pull = TelegramPull(self.catalog, ("BD_X18", "bd_fr"), root=self.feed)
        pull.poll(now=self.now)
        pull.poll(now=self.now)
        state = CollectorState(self.catalog.directory, database=self.catalog.backend()._database)
        self.assertIn("regular file", state.telegram_checkpoint("BD_X18")["blocked"])
        self.assertEqual(state.telegram_checkpoint("bd_fr")["page"], 1)

    def test_oversize_archive_is_retained_and_refused_by_intake(self):
        from library.stage_runtime import StageRuntime
        from library.telegram_pull import TelegramPull

        archive = self.root / "archive"
        row = {**self.row(), "size": 1024 ** 3 + 1}
        source = archive / row["path"]
        source.parent.mkdir(parents=True)
        with source.open("wb") as stream:
            stream.truncate(row["size"])
        self.head("BD_X18")
        self.page("BD_X18", 1, row)
        TelegramPull(self.catalog, ("BD_X18",), root=self.feed).poll(now=self.now)
        runtime = StageRuntime(self.root, self.root / "downloads", self.root / "collector",
                               providers=MagicMock(), client=None, browser=None, isolated=True)
        self.enterContext(patch("library.origins.ARCHIVE_ROOT", archive))
        self.enterContext(patch("library.stage_runtime.require_nas"))
        self.enterContext(patch("library.intake.sha256", side_effect=AssertionError("Oversize file hashed")))
        self.enterContext(patch("library.readers.sha256", side_effect=AssertionError("Oversize file hashed")))
        actor = runtime.actor("intake")
        self.addCleanup(actor.work.close)
        self.assertEqual(actor.step(now=self.now), "done")
        origin = self.catalog.get("origins", telegram_origin_id(1235693416, 9100))
        self.assertEqual(origin["availability"], "rejected")
        self.assertIn("File size limit", origin["reason"])
        self.assertEqual(source.stat().st_size, row["size"])
        self.assertFalse(self.catalog.read()["assets"])

    def test_reconciles_commit_readback_and_resumes_partial_page(self):
        from library.operational_state import CollectorState
        from library.postgres import CommitUnknown
        from library.telegram_pull import TelegramPull
        from library.work_items import WorkItems

        self.head("BD_X18")
        first, second = self.row(), self.row(message=9101)
        self.page("BD_X18", 1, first, second)
        pull = TelegramPull(self.catalog, ("BD_X18",), root=self.feed)
        backend = self.catalog.backend()
        commit = backend.commit
        state = CollectorState(self.catalog.directory, database=backend._database)
        live_work = WorkItems(backend._database)
        self.addCleanup(live_work.close)
        calls = 0

        def lose_commit_ack(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                commit(*args, **kwargs)
            raise CommitUnknown("commit_unresolved")

        with patch.object(backend, "commit", side_effect=lose_commit_ack):
            pull.poll(now=self.now)
        self.assertEqual(calls, 2)
        self.assertEqual(live_work.counts("intake"), {"intake": {"ready": 1}})
        self.assertEqual(state.telegram_checkpoint("BD_X18")["page"], 0)
        pull.poll(now=self.now)
        self.assertEqual(state.telegram_checkpoint("BD_X18")["page"], 1)
        self.assertEqual(live_work.counts("intake"), {"intake": {"ready": 2}})

    def test_preserves_historical_trial_with_exact_path_and_hash(self):
        from library.primitives import digest
        from library.telegram_pull import TelegramPull

        scope, message = "BD_X18", 5321
        name = "Historical trial.cbz"
        path = f"telegram/1235693416_{message}/{name}"
        size, checksum = 678, "a" * 64
        proof = digest({"path": path, "size": size, "sha256": checksum})
        oid = digest({"external": path, "size": size, "mtime_ns": 1234})
        record = {"source": "external:" + path, "path": path, "size": size, "mtime_ns": 1234,
                  "pid": None, "fid": None, "sha256": checksum, "availability": "validated"}
        state = self.catalog.result_set("retire", {"origins": [oid]}, media=False)
        state["origins"][oid] = record
        self.catalog.save(state, expected=state["revision"])
        self.head(scope)
        row = {**self.row(scope, message, name=name), "size": size, "sha256": checksum}
        self.page(scope, 1, row)
        with patch.dict("library.telegram_pull.TRIALS", {(1235693416, message): proof}):
            TelegramPull(self.catalog, (scope,), root=self.feed).poll(now=self.now)
        self.assertEqual(dict(self.catalog.records("origins")), {oid: record})
