"""Offline source preservation and safe per-package document behavior."""
import tempfile
import unittest
from pathlib import Path

from booksys import CollectionError, extract_page
from fiches import write_fiche
from postgres_fixture import create_store
from library.primitives import empty
from library.operational_state import CollectorState

SOURCE = "https://www7.bookys-ebooks.com/bandes-dessinees/42-example"
HTML = """<h1>Example</h1><div itemprop="mainEntity">
<img id="item-cover" src="/storage/items/example.jpg">
<ul><li><b>Catégorie</b>: <a>Comics</a></li><li><b>Date d'ajout</b>: 08/11/2026</li></ul>
<p itemprop="abstract">A description.<br>Second paragraph &lt;script&gt;.</p></div>"""
JPEG = b"\xff\xd8\xff" + b"example"

class FicheTests(unittest.TestCase):
    def test_real_page_structure_preserves_description_and_local_cover(self):
        page = extract_page(HTML, SOURCE)
        self.assertEqual(page["details"]["Catégorie"], "Comics")
        self.assertIn("Second paragraph", page["description"])
        self.assertEqual(page["cover_url"], "https://www7.bookys-ebooks.com/storage/items/example.jpg")
        record = {"title": "Example", "page": page, "downloads": {"x": {"metadata": "CBZ Français 10 MB"}}}
        package = {"folder": "example", "pid": 8, "links": [{"fid": 9, "name": "example.cbz"}]}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = write_fiche(SOURCE, record, package, root, lambda u, s: (JPEG, ".jpg"))
            text = (root / "example/fiche.md").read_text()
            self.assertIn("A description.", text)
            self.assertIn("Comics", text)
            self.assertIn("example.cbz", text)
            self.assertIn("![Couverture](couverture.jpg)", text)
            self.assertNotIn("<script>", text)
            self.assertEqual((root / "example/couverture.jpg").read_bytes(), JPEG)
            self.assertEqual(result, "example")
            def forbidden(u, s): self.fail("A saved cover must not be fetched again")
            write_fiche(SOURCE, record, package, root, forbidden)

    def test_refuses_unsafe_folder_and_existing_personal_fiche(self):
        record = {"title": "Example", "page": extract_page(HTML, SOURCE), "downloads": {}}
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for folder in ["../outside", "/tmp/outside", "", "."]:
                with self.assertRaises(CollectionError):
                    write_fiche(SOURCE, record, {"folder": folder, "pid": 1, "links": []}, root, None)
            (root/"example").mkdir();(root/"example/fiche.md").write_text("My notes")
            with self.assertRaises(CollectionError):
                write_fiche(SOURCE, record, {"folder":"example","pid":1,"links":[]}, root, None)
            self.assertEqual((root/"example/fiche.md").read_text(), "My notes")

    def test_existing_queue_enrichment_resumes_cover_without_recollecting_or_writes(self):
        import yaml
        from unittest.mock import patch
        from pipeline import run_fiche_once, save
        package = {"folder": "example", "pid": 8, "links": [{"fid": 9, "name": "example.cbz", "statusmsg":"finished", "plugin":"Host", "size":3}]}
        class Api:
            def call(self, method, **kwargs):
                assert method in ("get_queue_data", "get_collector_data") and not kwargs, "Enrichment must never write to pyLoad"
                return [package] if method == "get_queue_data" else []
        class Browser:
            calls = 0
            image_calls = 0
            def __call__(self, source):
                self.calls += 1
                return HTML
            def cover(self, url, source):
                self.image_calls += 1
                if self.image_calls == 1: raise OSError("temporary image failure")
                return JPEG, ".jpg"
        browser = Browser()
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp)/"state";state.mkdir();root=Path(tmp)/"downloads";root.mkdir()
            (root/"example").mkdir();archive=root/"example/example.cbz";archive.write_bytes(b"abc")
            create_store(self,state,empty())
            CollectorState(state).capture(SOURCE,{"title":"Example","downloads":{}})
            CollectorState(state).submission(SOURCE,{"status":"queued","package_id":8})
            self.assertTrue(run_fiche_once(state,browser,Api(),root,minimum_free=0))
            catalog=yaml.safe_load((state/"catalogue.yaml").read_text())
            self.assertEqual(catalog["items"][SOURCE]["fiche_relative"],"example/fiche.md")
            self.assertIn("A description",(root/"example/fiche.md").read_text())
            with patch("pipeline.time.time",return_value=99999999999):
                self.assertTrue(run_fiche_once(state,browser,Api(),root,minimum_free=0))
            self.assertEqual(browser.calls,1)
            self.assertEqual(archive.read_bytes(),b"abc")
            catalog=yaml.safe_load((state/"catalogue.yaml").read_text())
            self.assertEqual(catalog["items"][SOURCE]["cover_relative"],"example/couverture.jpg")
            self.assertEqual(catalog["items"][SOURCE]["fiche_status"],"complete")
            package["links"][0]["name"]="renamed.cbz"
            self.assertTrue(run_fiche_once(state,browser,Api(),root,minimum_free=0))
            self.assertIn("renamed.cbz",(root/"example/fiche.md").read_text())
            self.assertEqual(browser.image_calls,2)
            self.assertFalse(run_fiche_once(state,browser,Api(),root,minimum_free=0))
            saved_cover=(root/"example/couverture.jpg").read_bytes()
            (root/"example/fiche.md").unlink()
            self.assertTrue(run_fiche_once(state,browser,Api(),root,minimum_free=0))
            self.assertEqual((root/"example/couverture.jpg").read_bytes(),saved_cover)
            self.assertEqual(browser.image_calls,2)
            (root/"example/fiche.md").unlink()
            with patch("fiches.atomic_bytes",side_effect=OSError("temporary write failure")):
                self.assertTrue(run_fiche_once(state,browser,Api(),root,minimum_free=0))
            progress=CollectorState(state).fiches()
            self.assertEqual(progress["items"][SOURCE]["cover_relative"],"example/couverture.jpg")
            with patch("pipeline.time.time",return_value=99999999999):
                self.assertTrue(run_fiche_once(state,browser,Api(),root,minimum_free=0))
            self.assertEqual((root/"example/couverture.jpg").read_bytes(),saved_cover)
            self.assertEqual(browser.image_calls,2)

    def test_cover_fetch_uses_solved_identity_and_rejects_other_hosts(self):
        from unittest.mock import patch
        from email.message import Message
        from booksys import Byparr
        browser=Byparr("http://byparr:8191/v1")
        browser.session={"url":SOURCE,"userAgent":"Browser UA","cookies":[
            {"name":"clearance","value":"local-test","domain":".bookys-ebooks.com"},
            {"name":"unrelated","value":"secret","domain":"other.example"}]}
        class Response:
            headers=Message()
            headers["Content-Type"]="image/jpeg"
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self,limit): return JPEG
        with patch("booksys_core.browser.build_opener") as build:
            build.return_value.open.return_value=Response()
            data,ext=browser.cover("https://www7.bookys-ebooks.com/storage/items/a.jpg",SOURCE)
            self.assertEqual((data,ext),(JPEG,".jpg"))
            req=build.return_value.open.call_args.args[0]
            self.assertEqual(req.get_header("User-agent"),"Browser UA")
            self.assertEqual(req.get_header("Cookie"),"clearance=local-test")
            handler=build.call_args.args[0]
            with self.assertRaises(CollectionError):
                handler.redirect_request(None,None,302,"",{},"https://127.0.0.1/private.jpg")
            build.reset_mock()
            for url in ["https://attacker.example/a.jpg","https://www7.bookys-ebooks.com/admin"]:
                with self.assertRaises(CollectionError):browser.cover(url,SOURCE)
            build.assert_not_called()

    def test_future_package_gets_offline_fiche_before_download_queue_transfer(self):
        from test_pipeline import MemoryDownloader
        class Downloader(MemoryDownloader):
            def call(self,method,*,write=False,**params):
                if method=="move_package":
                    assert (self.downloads/"example/fiche.md").is_file()
                    assert (self.downloads/"example/couverture.jpg").read_bytes()==JPEG
                result=super().call(method,write=write,**params)
                if method=="get_package_data":
                    result.update(pid=1,folder="example")
                    result["links"]=[dict(f,fid=i,name="example.cbz") for i,f in enumerate(result["links"],1)]
                return result
        with tempfile.TemporaryDirectory() as tmp:
            create_store(self,Path(tmp),empty())
            downloader=Downloader();downloader.downloads=Path(tmp)
            downloader.fetch_image=lambda u,s:(JPEG,".jpg")
            record={"title":"Example","page":extract_page(HTML,SOURCE),
                    "downloads":{"x":{"hoster":"fixture","metadata":"CBZ","urls":["https://host.example/file"]}}}
            from pipeline import save
            journal={"submissions":{}}
            self.assertEqual(downloader.submit(SOURCE,record,journal,Path(tmp)/"monitor.yaml"),1)
            self.assertEqual(journal["submissions"][SOURCE]["status"],"queued")

    def test_browser_failure_in_page_or_cover_stops_idle_work(self):
        from booksys import BrowserError
        from pipeline import run_fiche_once, save
        class Api:
            def call(self,method):
                return [{"pid":8,"folder":"example","links":[]}] if method == "get_queue_data" else []
        class Browser:
            def __init__(self,stage):self.stage=stage
            def __call__(self,source):return '<h1>Unexpected page</h1>' if self.stage=="page" else HTML
            def cover(self,url,source):raise BrowserError("Browser challenge did not clear")
        for stage in ["page","cover"]:
            with self.subTest(stage=stage),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)/"downloads";root.mkdir();state=Path(tmp)/"state";state.mkdir()
                create_store(self,state,empty())
                CollectorState(state).capture(SOURCE,{"title":"Example","downloads":{}})
                CollectorState(state).submission(SOURCE,{"status":"queued","package_id":8})
                self.assertFalse(run_fiche_once(state,Browser(stage),Api(),root,minimum_free=0))
                if stage=="cover":self.assertTrue((root/"example/fiche.md").exists())

    def test_failed_monitor_does_not_start_fiche_browser_work(self):
        from unittest.mock import patch
        from pipeline import main
        from booksys import BrowserError
        with tempfile.TemporaryDirectory() as tmp:
            credential=Path(tmp)/"credential.json";credential.write_text('{"api_key":"isolated-test"}')
            argv=["pipeline","--directory",tmp,"--credentials",str(credential),"--downloads",tmp,"--interval","60"]
            with patch.dict('os.environ', {'BOOKSYS_COLLECTION_DIR': tmp}), \
                 patch("sys.argv",argv),patch("pipeline.run_backfill",return_value=None), \
                 patch("pipeline.run_once",side_effect=BrowserError("challenge")), \
                 patch("pipeline.run_fiche_once") as enrich,patch("pipeline.os.umask"), \
                 patch("pipeline.time.sleep",side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):main()
                enrich.assert_not_called()
