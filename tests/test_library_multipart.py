from postgres_fixture import create_catalog
import unittest
from library.multipart import select_parts
from library.catalog import LibraryError

class MultipartTests(unittest.TestCase):
    def test_only_exact_finished_contiguous_host_set_is_selected(self):
        links = [dict(fid=i, name=f"Album.part{i}.rar", status=0, url=f"https://host.test/{i}") for i in (2,1)]
        self.assertEqual([x["fid"] for x in select_parts(links, links[0])], [1,2])
        links[1]["status"] = 1
        with self.assertRaisesRegex(LibraryError, "Awaiting part"):
            select_parts(links, links[0])
        links[1]["status"] = 0
        links[1]["url"] = "https://mirror.test/1"
        with self.assertRaisesRegex(LibraryError, "Missing part"):
            select_parts(links, links[0])

    def test_gaps_duplicate_numbers_and_zip_splits_do_not_form_a_set(self):
        for names in (("A.part1.rar","A.part3.rar"), ("A.part1.rar","A.part01.rar")):
            links=[dict(fid=i,name=n,status=0,url="https://h.test/"+str(i)) for i,n in enumerate(names)]
            with self.assertRaises(LibraryError): select_parts(links,links[0])
        with self.assertRaisesRegex(LibraryError,"Unsupported multipart"):
            select_parts([],dict(name="A.zip.001"))


    def test_intake_does_not_open_unfinished_or_missing_parts_or_follow_symlinks(self):
        import tempfile
        from pathlib import Path
        from library.catalog import Catalog
        from library.intake import receive
        source = 'https://www7.bookys-ebooks.com/bandes-dessinees/1-test'
        for mode in ('unfinished','missing','symlink','unsupported'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                root=Path(folder); reception=root/'reception'; reception.mkdir()
                links=[dict(fid=i,name=f'A.part{i}.rar',status=0,url=f'https://h.test/{i}') for i in (1,2)]
                (reception/links[0]['name']).write_bytes(b'first')
                if mode=='unfinished': links[1]['status']=1
                if mode=='symlink': (reception/links[1]['name']).symlink_to(reception/links[0]['name'])
                if mode=='unsupported': links=[dict(fid=3,name='A.zip.001',status=0)]
                state=receive(create_catalog(self,root/'state'),reception,{'items':{source:{}}},
                    {'submissions':{source:dict(status='queued',package_id=1,name='tracked')}},
                    [dict(pid=1,name='tracked',folder='.',links=links)],stable_seconds=0)
                self.assertFalse(state['assets'])
                reasons='; '.join(o.get('reason','') for o in state['origins'].values())
                self.assertIn({'unfinished':'Awaiting part','missing':'Missing part',
                               'symlink':'symlink','unsupported':'Unsupported multipart'}[mode],reasons)

