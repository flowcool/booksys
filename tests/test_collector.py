"""Exercise public collection boundaries with minimized HTML and synthetic outgoing links and a local API."""

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from booksys import Byparr, CollectionError, DEFAULT_CATEGORY, catalogue, collect

FIXTURES = Path(__file__).parent / "fixtures"
FIRST = "https://www7.bookys-ebooks.com/bandes-dessinees/358548-star-wars-histoires-de-lhyperespace-tome-06-la-decrypteuse-2026"
SECOND = "https://www7.bookys-ebooks.com/bandes-dessinees/358545-les-amis-de-jacobs-tome-23"
PAGES = {
    DEFAULT_CATEGORY: "catalogue-1.html",
    DEFAULT_CATEGORY + "?page=2": "catalogue-2.html",
    FIRST: "item-358548.html", SECOND: "item-358545.html",
    FIRST + "/dl/2435949": "download-2435949.html",
    FIRST + "/dl/2435951": "download-2435951.html",
    SECOND + "/dl/2435940": "download-2435940.html",
    SECOND + "/dl/2435942": "download-2435942.html",
}


def captured(url):
    return (FIXTURES / PAGES[url]).read_text()


class CollectorTests(unittest.TestCase):
    def test_fixture_catalogues_have_40_distinct_items_and_next_page(self):
        first, next_url = catalogue(captured(DEFAULT_CATEGORY), DEFAULT_CATEGORY)
        second, _ = catalogue(captured(next_url), next_url)
        self.assertEqual((len(first), len(second), len(first.keys() | second.keys())), (20, 20, 40))
        self.assertEqual(next_url, DEFAULT_CATEGORY + "?page=2")

    def test_collect_exports_synthetic_links_and_skips_completed_items_on_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            counts = collect([DEFAULT_CATEGORY], captured, directory,
                             {"1fichier", "nitroflare"}, max_items=2)
            self.assertEqual(counts["collected"], 2)
            self.assertEqual(set((directory / "nitroflare.txt").read_text().splitlines()), {
                "https://files.example.invalid/fixture-0b011ea44ebc45f9",
                "https://files.example.invalid/fixture-c74ce0a976f7b459"})
            self.assertEqual(len((directory / "1fichier.txt").read_text().splitlines()), 2)
            calls = []

            def restart(url):
                calls.append(url)
                if url == DEFAULT_CATEGORY:
                    return f'<h1>Catalogue</h1><div class="bys-items-container"><a class="bys-item" href="{FIRST}">First</a><a class="bys-item" href="{SECOND}">Second</a></div>'
                self.fail("Restart requested an already completed item")

            counts = collect([DEFAULT_CATEGORY], restart, directory, {"1fichier", "nitroflare"})
            self.assertEqual(counts, {"pages": 1, "collected": 0, "skipped": 2})
            self.assertEqual(calls, [DEFAULT_CATEGORY])

    def test_collect_follows_pagination_and_filters_hosters(self):
        def fetch(url):
            if url in {DEFAULT_CATEGORY, DEFAULT_CATEGORY + "?page=2"}:
                item = FIRST if url == DEFAULT_CATEGORY else SECOND
                next_link = '<a href="?page=2">Next</a>' if item == FIRST else ""
                return f'<h1>Catalogue</h1><div class="bys-items-container"><a class="bys-item" href="{item}">Item</a></div>{next_link}'
            self.assertNotIn(url, {FIRST + "/dl/2435951", SECOND + "/dl/2435942"})
            return captured(url)

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            counts = collect([DEFAULT_CATEGORY], fetch, directory, {"1fichier"}, pages=0)
            self.assertEqual(counts["pages"], 2)
            self.assertEqual(counts["collected"], 2)
            self.assertFalse((directory / "nitroflare.txt").exists())

    def test_challenge_stops_but_keeps_prior_item_and_exports(self):
        def fetch(url):
            if url == SECOND:
                return "<title>Just a moment...</title>"
            return captured(url)

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with self.assertRaises(CollectionError):
                collect([DEFAULT_CATEGORY], fetch, directory, {"nitroflare"})
            self.assertEqual((directory / "nitroflare.txt").read_text(),
                             "https://files.example.invalid/fixture-0b011ea44ebc45f9\n")
            self.assertIn(FIRST, (directory / "state.yaml").read_text())
            self.assertNotIn(SECOND, (directory / "state.yaml").read_text())

    def test_off_site_link_is_rejected_before_fetch(self):
        calls = []

        def fetch(url):
            calls.append(url)
            return '<h1>Catalogue</h1><div class="bys-items-container"><a class="bys-item" href="https://evil.example/123-book">Book</a></div>'

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(CollectionError):
                collect([DEFAULT_CATEGORY], fetch, Path(temporary), {"1fichier"})
        self.assertEqual(calls, [DEFAULT_CATEGORY])

    def test_all_hosters_and_every_host_link_are_kept(self):
        page = '<h1>Book</h1><table><tr><td><a href="/book/dl/99">NewHost</a></td></tr></table>'
        outgoing = ('<h1>Download</h1><a class="bys-link bys-host" href="https://files.example/a">Part 1</a>'
                    '<a class="bys-link bys-host" href="https://files.example/b">Part 2</a>'
                    '<a href="https://advert.example/">Advertisement</a>')
        def fetch(url):
            if url == DEFAULT_CATEGORY:
                return f'<h1>Catalogue</h1><div class="bys-items-container"><a class="bys-item" href="{FIRST}">Book</a></div>'
            return page if url == FIRST else outgoing
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            collect([DEFAULT_CATEGORY], fetch, directory)
            self.assertEqual((directory / "all-links.txt").read_text(),
                             "https://files.example/a\nhttps://files.example/b\n")
            self.assertTrue((directory / "newhost.txt").exists())

    def test_watermark_excludes_existing_catalogue_without_fetching_details(self):
        calls = []
        def fetch(url):
            calls.append(url)
            return captured(url)
        with tempfile.TemporaryDirectory() as temporary:
            counts = collect([DEFAULT_CATEGORY], fetch, Path(temporary), pages=0, after_id=358548)
        self.assertEqual(counts["collected"], 0)
        self.assertEqual(calls, [DEFAULT_CATEGORY])

    def test_missing_pagination_before_watermark_stops_incremental_scan(self):
        def fetch(url):
            if url == DEFAULT_CATEGORY:
                return f'<h1>Catalogue</h1><div class="bys-items-container"><a class="bys-item" href="{FIRST}">Book</a></div>'
            return captured(url)
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(CollectionError, "before reaching"):
                collect([DEFAULT_CATEGORY], fetch, Path(temporary), {"1fichier"}, pages=0, after_id=358000)


class ByparrTests(unittest.TestCase):
    def test_real_api_contract_and_error_handling(self):
        responses = [
            {"status": "ok", "solution": {"status": 200, "url": DEFAULT_CATEGORY,
                                           "response": captured(DEFAULT_CATEGORY)}},
            {"status": "error", "message": "Challenge timeout"},
            {"status": "ok", "solution": {"status": 200, "url": DEFAULT_CATEGORY,
                                           "response": "<title>Just a moment...</title>"}},
        ]
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                body = json.dumps(responses.pop(0)).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = Byparr(f"http://127.0.0.1:{server.server_port}/v1", delay=0)
            self.assertIn("bys-items-container", client(DEFAULT_CATEGORY))
            for _ in range(2):
                with self.assertRaises(CollectionError):
                    client(DEFAULT_CATEGORY)
            self.assertEqual(received[0], {"cmd": "request.get", "url": DEFAULT_CATEGORY,
                                            "maxTimeout": 60000, "blockMedia": False})
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
