"""Public pipeline behavior: baseline, all-link submission, recovery and readback."""

import tempfile
import unittest
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


from booksys import CollectionError, DEFAULT_CATEGORY
from pipeline import Pyload, run_once, run_backfill
from postgres_fixture import create_store
from library.primitives import empty
from library.operational_state import CollectorState

def seed_monitor(directory,value):
    state=CollectorState(directory)
    for source,record in value.get("categories",{}).items():state.category(source,record)
    for source,record in value.get("submissions",{}).items():state.submission(source,record)

def seed_backfill(directory,value):
    state=CollectorState(directory)
    state.start_backfill(value)
    for url,sources in value.get("pages",{}).items():
        state.backfill_page(value,url,{source:value["items"][source] for source in sources})

FIRST = "https://www7.bookys-ebooks.com/bandes-dessinees/10-old"
NEW = "https://www7.bookys-ebooks.com/bandes-dessinees/11-new"


class MemoryDownloader(Pyload):
    def __init__(self):
        self.packages = []
        self.creates = 0
        self.lose_response = False
        self.modify_links = False

    def host_availability(self):
        return {'hosts': {'fixture': {'status': True, 'domains': ['1fichier.com', 'nitroflare.com',
            'rapidgator.net', 'example.com', 'example.org', 'host.test', 'host.example']}}}

    def call(self, method, *, write=False, **parameters):
        if method == "status_server":
            return {"pause": False, "download": True}
        if method in {"get_queue", "get_collector"}:
            dest = int(method == "get_queue")
            return [p.copy() for p in self.packages if p["dest"] == dest]
        if method == "add_package":
            self.creates += 1
            self.packages.append({"pid": self.creates, "name": parameters["name"], "dest": 0,
                                  "links": [{"url": u} for u in parameters["links"]]})
            if self.lose_response:
                self.lose_response = False
                raise OSError("Lost create response")
            return self.creates
        if method == "get_package_data":
            result = next(p.copy() for p in self.packages if p["pid"] == parameters["package_id"])
            if self.modify_links:
                result["links"] = [{"url": "https://wrong.example/file"}]
            return result
        if method == "move_package":
            next(p for p in self.packages if p["pid"] == parameters["package_id"])["dest"] = parameters["destination"]
            return None
        raise AssertionError(method)


class PipelineTests(unittest.TestCase):
    def test_backfill_recovers_progress_counter_from_confirmed_submission(self):
        downloader = MemoryDownloader()
        def fetch(url):
            self.fail("Confirmed item must not be fetched again")
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            create_store(self,directory,empty())
            seed_monitor(directory, {"version": 1, "categories": {},
                "submissions": {FIRST: {"status": "queued", "package_id": 42}}})
            seed_backfill(directory, {"version": 1, "pages_requested": 1, "next_page": None,
                "pages": {DEFAULT_CATEGORY: [FIRST]}, "items": {FIRST: "Old"}, "status": "collecting", "submitted": 0})
            result = run_backfill(directory, fetch, downloader, minimum_free=0)
            self.assertEqual(result["queued"], 1)
            self.assertEqual(CollectorState(directory).backfill()["submitted"], 1)
            self.assertEqual(downloader.creates, 0)

    def test_backfill_resumes_saved_pages_and_does_not_resubmit_first_item(self):
        downloader = MemoryDownloader()
        fail_second = [True]
        catalogue_calls = []
        def fetch(url):
            if url in {DEFAULT_CATEGORY, DEFAULT_CATEGORY + "?page=2"}:
                catalogue_calls.append(url)
                source = FIRST if url == DEFAULT_CATEGORY else NEW
                return f'<h1>Catalogue</h1><div class="bys-items-container"><a class="bys-item" href="{source}">Item</a></div><a href="?page=2">Next</a>'
            if url == NEW and fail_second[0]:
                fail_second[0] = False
                raise CollectionError("Transient page failure")
            if "/dl/" in url:
                return '<h1>Download</h1><a class="bys-link bys-host" href="https://host.example/a">Get</a>'
            return '<h1>Item</h1><a href="/item/dl/1">Host</a>'
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            create_store(self,directory,empty())
            seed_monitor(directory, {"version": 1, "categories": {}, "submissions": {}})
            seed_backfill(directory, {"version": 1, "pages_requested": 2, "next_page": DEFAULT_CATEGORY,
                "pages": {}, "items": {}, "status": "pending", "submitted": 0})
            with self.assertRaises(CollectionError):
                run_backfill(directory, fetch, downloader, minimum_free=0)
            self.assertEqual(downloader.creates, 1)
            result = run_backfill(directory, fetch, downloader, minimum_free=0)
            self.assertEqual(result["queued"], 2)
            self.assertEqual(downloader.creates, 2)
            self.assertEqual(len(catalogue_calls), 2)

    def test_ten_page_backfill_submits_all_links_and_keeps_watermark(self):
        downloader = MemoryDownloader()
        catalogue_calls = []
        def fetch(url):
            if "/dl/" in url:
                return '<h1>Download</h1><a class="bys-link bys-host" href="https://host.example/a">Get</a><a class="bys-link bys-host" href="https://host.example/b">Get</a>'
            if url == DEFAULT_CATEGORY or "?page=" in url:
                catalogue_calls.append(url)
                page = int(url.split("?page=")[1]) if "?page=" in url else 1
                return f'<h1>Catalogue</h1><div class="bys-items-container"><a class="bys-item" href="/bandes-dessinees/{20-page}-item">Item</a></div><a href="?page={page+1}">Next</a>'
            return '<h1>Item</h1><a href="/item/dl/1">AnyHost</a>'
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            create_store(self,directory,empty())
            monitor = {"version": 1, "categories": {DEFAULT_CATEGORY: {"after_id": 999}}, "submissions": {}}
            seed_monitor(directory, monitor)
            seed_backfill(directory, {"version": 1, "pages_requested": 10, "next_page": DEFAULT_CATEGORY,
                "pages": {}, "items": {}, "status": "pending", "submitted": 0})
            result = run_backfill(directory, fetch, downloader, minimum_free=0, max_items=3)
            self.assertEqual(result, {"pages": 10, "items": 10, "queued": 3, "pending": 1})
            from library.catalog import lock
            with lock(directory / "pipeline.lock", blocking=False):
                pass  # Other processing can acquire the collector lock between batches.
            self.assertEqual(downloader.creates, 3)
            result = run_backfill(directory, fetch, downloader, minimum_free=0)
            self.assertEqual(result, {"pages": 10, "items": 10, "queued": 10})
            self.assertEqual(len(catalogue_calls), 10)
            self.assertEqual(downloader.creates, 10)
            self.assertTrue(all(p["dest"] == 1 and len(p["links"]) == 2 for p in downloader.packages))
            self.assertEqual(CollectorState(directory).monitor()["categories"], monitor["categories"])
            self.assertIsNone(run_backfill(directory, fetch, downloader, minimum_free=0))
            self.assertEqual(downloader.creates, 10)

    def test_fifty_page_backfill_deduplicates_prior_batch_and_preserves_watermark(self):
        downloader = MemoryDownloader()
        catalogue_calls = []
        def fetch(url):
            if url == FIRST:
                self.fail("Previously queued source must not be resolved again")
            if "/dl/" in url:
                return '<h1>Download</h1><a class="bys-link bys-host" href="https://host.example/a">Get</a>'
            if url == DEFAULT_CATEGORY or "?page=" in url:
                catalogue_calls.append(url)
                page = int(url.split("?page=")[1]) if "?page=" in url else 1
                return (f'<h1>Catalogue</h1><div class="bys-items-container">'
                        f'<a class="bys-item" href="{FIRST}">Old</a>'
                        f'<a class="bys-item" href="/bandes-dessinees/{1000-page}-item">Item</a></div>'
                        f'<a href="?page={page+1}">Next</a>')
            return '<h1>Item</h1><a href="/item/dl/1">Host</a>'
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            create_store(self,directory,empty())
            monitor = {"version": 1, "categories": {DEFAULT_CATEGORY: {"after_id": 9999}},
                       "submissions": {FIRST: {"status": "queued", "package_id": 100}}}
            seed_monitor(directory, monitor)
            seed_backfill(directory, {"version": 1, "pages_requested": 50, "next_page": DEFAULT_CATEGORY,
                 "pages": {}, "items": {}, "status": "pending", "submitted": 0})
            result = run_backfill(directory, fetch, downloader, minimum_free=0)
            self.assertEqual(result, {"pages": 50, "items": 51, "queued": 51})
            self.assertEqual(len(catalogue_calls), 50)
            self.assertEqual(downloader.creates, 50)
            current = CollectorState(directory).monitor()
            self.assertEqual(current['categories'], monitor['categories'])
            self.assertEqual(current['submissions'][FIRST], monitor['submissions'][FIRST])
            self.assertIsNone(run_backfill(directory, fetch, downloader, minimum_free=0))
            self.assertEqual(downloader.creates, 50)

    def test_pyload_get_has_no_json_content_type_and_keeps_query_parameters(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append((self.path, self.headers.get("Content-Type")))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"dest": 0}).encode())
            def log_message(self, *_args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            api = Pyload(f"http://127.0.0.1:{server.server_port}", "fixture-key")
            api.call("get_package_data", package_id=12)
            self.assertEqual(received, [("/api/get_package_data?package_id=12", None)])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_initialize_then_queue_new_entry_with_primary_host_set_once(self):
        current = [FIRST]
        calls = []
        def fetch(url):
            calls.append(url)
            if url == DEFAULT_CATEGORY:
                return '<h1>Catalogue</h1><div class="bys-items-container">' + ''.join(
                    f'<a class="bys-item" href="{u}">Item</a>' for u in current) + '</div>'
            if url == NEW:
                return '<h1>New</h1><a href="/book/dl/1">Host A</a><a href="/book/dl/2">Host B</a>'
            return f'<h1>Download</h1><a class="bys-link bys-host" href="https://host.example/{url[-1]}">Get</a>'
        downloader = MemoryDownloader()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            create_store(self,directory,empty())
            result = run_once(directory, fetch, downloader, minimum_free=0)
            self.assertEqual(result["after_id"], 10)
            self.assertEqual(downloader.creates, 0)
            current.insert(0, NEW)
            result = run_once(directory, fetch, downloader, minimum_free=0)
            self.assertEqual(result["queued"], 1)
            self.assertEqual(downloader.packages[0]["dest"], 1)
            self.assertEqual(len(downloader.packages[0]["links"]), 1)
            run_once(directory, fetch, downloader, minimum_free=0)
            self.assertEqual(downloader.creates, 1)
            self.assertNotIn(FIRST, calls)

    def test_lost_create_response_is_reconciled_without_resend(self):
        downloader = MemoryDownloader()
        record = {"title": "New", "downloads": {"x": {"hoster": "fixture", "urls": ["https://host.example/a"]}}}
        with tempfile.TemporaryDirectory() as temporary:
            create_store(self,Path(temporary),empty())
            path = Path(temporary) / "monitor.yaml"
            journal = {"submissions": {}}
            downloader.lose_response = True
            with self.assertRaises(OSError):
                downloader.submit(NEW, record, journal, path)
            recovered = CollectorState(Path(temporary)).monitor()
            downloader.submit(NEW, record, recovered, path)
            self.assertEqual(downloader.creates, 1)
            self.assertEqual(recovered["submissions"][NEW]["status"], "queued")

    def test_mismatched_readback_does_not_start_download(self):
        downloader = MemoryDownloader()
        downloader.modify_links = True
        record = {"title": "New", "downloads": {"x": {"hoster": "fixture", "urls": ["https://host.example/a"]}}}
        with tempfile.TemporaryDirectory() as temporary:
            create_store(self,Path(temporary),empty())
            with self.assertRaises(CollectionError):
                downloader.submit(NEW, record, {"submissions": {}}, Path(temporary) / "journal.yaml")
            self.assertEqual(downloader.packages[0]["dest"], 0)


if __name__ == "__main__":
    unittest.main()
