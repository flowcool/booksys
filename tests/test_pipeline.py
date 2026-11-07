"""Public pipeline behavior: baseline, all-link submission, recovery and readback."""

import tempfile
import unittest
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from booksys import CollectionError, DEFAULT_CATEGORY
from pipeline import Pyload, run_once

FIRST = "https://www7.bookys-ebooks.com/bandes-dessinees/10-old"
NEW = "https://www7.bookys-ebooks.com/bandes-dessinees/11-new"


class MemoryDownloader(Pyload):
    def __init__(self):
        self.packages = []
        self.creates = 0
        self.lose_response = False
        self.modify_links = False

    def call(self, method, *, write=False, **parameters):
        if method == "status_server":
            return {"pause": False, "download": True}
        if method in {"get_queue", "get_collector"}:
            dest = int(method == "get_queue")
            return [p.copy() for p in self.packages if p["dest"] == dest]
        if method == "add_package":
            self.creates += 1
            self.packages.append({"pid": 1, "name": parameters["name"], "dest": 0,
                                  "links": [{"url": u} for u in parameters["links"]]})
            if self.lose_response:
                self.lose_response = False
                raise OSError("Lost create response")
            return 1
        if method == "get_package_data":
            result = self.packages[0].copy()
            if self.modify_links:
                result["links"] = [{"url": "https://wrong.example/file"}]
            return result
        if method == "move_package":
            self.packages[0]["dest"] = parameters["destination"]
            return None
        raise AssertionError(method)


class PipelineTests(unittest.TestCase):
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

    def test_initialize_then_queue_new_entry_with_all_links_once(self):
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
            result = run_once(directory, fetch, downloader, minimum_free=0)
            self.assertEqual(result["after_id"], 10)
            self.assertEqual(downloader.creates, 0)
            current.insert(0, NEW)
            result = run_once(directory, fetch, downloader, minimum_free=0)
            self.assertEqual(result["queued"], 1)
            self.assertEqual(downloader.packages[0]["dest"], 1)
            self.assertEqual(len(downloader.packages[0]["links"]), 2)
            run_once(directory, fetch, downloader, minimum_free=0)
            self.assertEqual(downloader.creates, 1)
            self.assertNotIn(FIRST, calls)

    def test_lost_create_response_is_reconciled_without_resend(self):
        downloader = MemoryDownloader()
        record = {"title": "New", "downloads": {"x": {"urls": ["https://host.example/a"]}}}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "monitor.yaml"
            journal = {"submissions": {}}
            downloader.lose_response = True
            with self.assertRaises(OSError):
                downloader.submit(NEW, record, journal, path)
            recovered = yaml.safe_load(path.read_text())
            downloader.submit(NEW, record, recovered, path)
            self.assertEqual(downloader.creates, 1)
            self.assertEqual(recovered["submissions"][NEW]["status"], "queued")

    def test_mismatched_readback_does_not_start_download(self):
        downloader = MemoryDownloader()
        downloader.modify_links = True
        record = {"title": "New", "downloads": {"x": {"urls": ["https://host.example/a"]}}}
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(CollectionError):
                downloader.submit(NEW, record, {"submissions": {}}, Path(temporary) / "journal.yaml")
            self.assertEqual(downloader.packages[0]["dest"], 0)


if __name__ == "__main__":
    unittest.main()
