import copy
import tempfile
import unittest
from pathlib import Path

from postgres_fixture import create_store
from library.primitives import empty

from library.metadata import Providers


class PrintedIdentityTests(unittest.TestCase):
    def test_bnf_resolves_exact_ean_in_isbn10_record_without_inventing_revision(self):
        body = (Path(__file__).parent / 'fixtures/bnf-prince.xml').read_text()
        with tempfile.TemporaryDirectory() as folder:
            create_store(self, Path(folder), empty())
            p = Providers(Path(folder)/"cache", fetch=lambda url: (200, body), interval=0)
            result = p.source('bnf', '9782871297833')
            self.assertEqual(result['status'], 'ok', result)
            candidate = result['candidate']
            self.assertEqual(candidate['isbn'], '9782871297833')
            self.assertEqual(candidate['title'], 'Les crochets de la vipère')
            self.assertEqual(candidate['number'], '2')
            self.assertEqual(candidate['medium'], 'print')
            self.assertEqual(candidate['revision'], '')
            self.assertEqual(candidate['edition_ref'], 'ark:/12148/cb39973127n')




    def test_bnf_rejects_ambiguous_wrong_identifier_medium_and_unsafe_xml(self):
        body = (Path(__file__).parent / 'fixtures/bnf-prince.xml').read_text()
        cases = [body.replace('>1</srw:numberOfRecords>', '>2</srw:numberOfRecords>'),
                 body.replace('9782871297833', '9782373494730'),
                 body.replace('Texte imprimé', 'Ressource électronique'),
                 '<!DOCTYPE root [<!ENTITY a SYSTEM "file:///etc/passwd">]>' + body]
        for content in cases:
            with self.subTest(case=content[:40]), tempfile.TemporaryDirectory() as folder:
                create_store(self, Path(folder), empty())
                result = Providers(Path(folder)/"cache", fetch=lambda url: (200, content), interval=0).source('bnf', '9782871297833')
                self.assertEqual(result['status'], 'unavailable', result)

    def test_bounded_process_rejects_stdout_stderr_and_timeout(self):
        import subprocess
        import sys
        from library.process import bounded_run
        from library.catalog import LibraryError
        for stream in ['stdout', 'stderr']:
            with self.subTest(stream=stream), self.assertRaises(LibraryError):
                bounded_run([sys.executable, '-c', f'import sys; sys.{stream}.write("x"*10000)'],
                            timeout=5, stdout_limit=128, stderr_limit=128)
        with self.assertRaises(subprocess.TimeoutExpired):
            bounded_run([sys.executable, '-c', 'import time; time.sleep(10)'], timeout=.05)


    def test_decoder_timeout_stops_descendant_after_parent_exits(self):
        import os
        import signal
        import subprocess
        import sys
        from library.process import bounded_run
        with tempfile.TemporaryDirectory() as folder:
            pid_file = Path(folder) / 'child.pid'
            code = ('import subprocess,sys; from pathlib import Path; '
                    'child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]); '
                    'Path(sys.argv[1]).write_text(str(child.pid))')
            with self.assertRaises(subprocess.TimeoutExpired):
                bounded_run([sys.executable, '-c', code, str(pid_file)], timeout=.3)
            pid = int(pid_file.read_text())
            proc = Path(f'/proc/{pid}/stat')
            import time
            def running():
                try:
                    return proc.read_text().split()[2] != 'Z'
                except FileNotFoundError:
                    return False
            deadline = time.monotonic() + .5
            while running() and time.monotonic() < deadline:
                time.sleep(.01)
            try:
                self.assertFalse(running(), 'Owned descendant survived its group timeout')
            finally:
                if running():
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_current_bnf_publication_and_contributor_fields_are_retained(self):
        from library.bnf import parse_bnf
        fixture=Path(__file__).parent/'fixtures'
        bird=parse_bnf((fixture/'bnf-oiseau.xml').read_text(),'9782368468425','https://fixture','now')
        self.assertEqual((bird['publisher'],bird['date']),('Steinkis','DL 2026'))
        self.assertEqual(bird['author'],'Désirée Frappier')
        self.assertTrue(any(c['name']=='Alain Frappier' and 'Illustrateur' in c['roles'] for c in bird['contributors']))
        for name, code, number in [('geeks1','9782302000599','1'),('geeks4','9782302008168','4')]:
            geeks=parse_bnf((fixture/('bnf-'+name+'.xml')).read_text(),code,'https://fixture','now')
            self.assertEqual(geeks['author'],'Gang')
            self.assertEqual(geeks['number'],number)
            self.assertTrue(any(c['name']=='Thomas Labourot' for c in geeks['contributors']))
            self.assertNotIn('Labourot',geeks['author'])
