import copy
import tempfile
import unittest
from pathlib import Path

from library.metadata import Providers


class PrintedIdentityTests(unittest.TestCase):
    def test_bnf_resolves_exact_ean_in_isbn10_record_without_inventing_revision(self):
        body = (Path(__file__).parent / 'fixtures/bnf-prince.xml').read_text()
        with tempfile.TemporaryDirectory() as folder:
            p = Providers(Path(folder), fetch=lambda url: (200, body), interval=0)
            result = p.source('bnf', '9782871297833')
            self.assertEqual(result['status'], 'ok', result)
            candidate = result['candidate']
            self.assertEqual(candidate['isbn'], '9782871297833')
            self.assertEqual(candidate['title'], 'Les crochets de la vipère')
            self.assertEqual(candidate['number'], '2')
            self.assertEqual(candidate['medium'], 'print')
            self.assertEqual(candidate['revision'], '')
            self.assertEqual(candidate['edition_ref'], 'ark:/12148/cb39973127n')

    def test_printed_exact_isbn_requires_readable_content_and_preserves_unknown_revision(self):
        from library.printed import identify_printed
        body = (Path(__file__).parent / 'fixtures/bnf-prince.xml').read_text()
        with tempfile.TemporaryDirectory() as folder:
            candidate = Providers(Path(folder), fetch=lambda url: (200, body), interval=0).source('bnf', '9782871297833')['candidate']
        printed = {'status': 'ok', 'asset': 'a' * 64, 'pages': [
            {'name': '001.jpg', 'sha256': 'b' * 64, 'text': 'Takeshi Konomi\n9"782871 297833'},
            {'name': '004.jpg', 'sha256': 'c' * 64, 'text': 'Volume 2 — LES CROCHETS\nDE LA VIPERE'}],
            'tool': 'tesseract 5.5.0', 'window': 'first4_last4'}
        result = identify_printed(printed, {}, [candidate])
        self.assertEqual(result['status'], 'identified', result)
        self.assertEqual(result['candidate']['revision'], '')
        self.assertEqual(result['evidence']['representation'], 'scan_of_print_edition')
        for field, wrong in [('title', 'La vipère rouge'), ('author', 'Other Writer'), ('number', '3'),
                             ('medium', 'ebook'), ('isbn', '9782373494730')]:
            with self.subTest(field=field):
                self.assertEqual(identify_printed(printed, {}, [{**candidate, field: wrong}])['status'], 'pending')
        conflicting = copy.deepcopy(printed)
        conflicting['pages'][0]['text'] += '\nISBN 9782373494730'
        self.assertEqual(identify_printed(conflicting, {}, [candidate])['status'], 'pending')
        for embedded in [{'LanguageISO': 'en'}, {'Publisher': 'Other'}, {'Number': '3'},
                         {'ISBN': '9782373494730'}, {'EditionRevision': 'Revised'}]:
            self.assertEqual(identify_printed(printed, embedded, [candidate])['status'], 'pending')
        bad = copy.deepcopy(printed)
        bad['pages'][0]['text'] = bad['pages'][0]['text'].replace('297833', '297834')
        self.assertEqual(identify_printed(bad, {}, [candidate])['status'], 'pending')
        self.assertEqual(identify_printed(printed, {}, [candidate, {**candidate, 'edition_ref': 'other'}])['status'], 'pending')

        advert = copy.deepcopy(printed)
        advert['pages'] = [{'name': '001.jpg', 'sha256': 'd' * 64, 'text': 'Another album'}] + [
            {'name': f'{n:03}.jpg', 'sha256': 'e' * 64, 'text': ''} for n in (2, 3, 4)] + printed['pages']
        self.assertEqual(identify_printed(advert, {}, [candidate])['status'], 'pending')
        other_volume = copy.deepcopy(printed)
        other_volume['pages'][0]['text'] += '\nVolume 3'
        self.assertEqual(identify_printed(other_volume, {}, [candidate])['status'], 'pending')

    def test_isolated_ocr_binds_text_to_validated_images_and_rejects_source_change(self):
        import io
        import os
        import zipfile
        from PIL import Image, ImageDraw, ImageFont
        from library.readers import inspect_file
        from library.printed import read_printed
        from library.catalog import LibraryError
        native = Path(__file__).resolve().parents[1] / 'tmp/qualification/native/ocr-root'
        if native.exists() and not os.environ.get('BOOKSYS_READER_IMAGE'):
            os.environ['BOOKSYS_TESSERACT'] = str(native / 'usr/bin/tesseract')
            os.environ['BOOKSYS_TESSDATA'] = str(native / 'usr/share/tesseract-ocr/5/tessdata')
            os.environ['BOOKSYS_OCR_LIBDIR'] = str(native / 'usr/lib/x86_64-linux-gnu')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'album.cbz'
            image = Image.new('RGB', (1000, 400), 'white')
            ImageDraw.Draw(image).multiline_text((20, 20), 'ISBN 9782871297833\nTakeshi Konomi\nVolume 2\nLes crochets de la vipere',
                font=ImageFont.truetype('DejaVuSans.ttf', 35), fill='black')
            data = io.BytesIO()
            image.save(data, format='PNG')
            with zipfile.ZipFile(path, 'w') as archive:
                for n in range(1, 11):
                    archive.writestr(f'{n:03}.png', data.getvalue())
            inspection = inspect_file(path, stable_seconds=.01)
            result = read_printed(path, inspection)
            self.assertEqual(result['status'], 'ok', result)
            self.assertEqual([p['name'] for p in result['pages']],
                             ['001.png', '002.png', '003.png', '004.png', '007.png', '008.png', '009.png', '010.png'])
            self.assertIn('9782871297833', result['pages'][0]['text'])
            self.assertEqual(result['pages'][0]['sha256'], inspection['page_manifest'][0]['sha256'])
            if not os.environ.get('BOOKSYS_READER_IMAGE'):
                from unittest.mock import patch
                with patch.dict(os.environ, {'BOOKSYS_TESSERACT': '/missing-ocr-binary'}):
                    missing = read_printed(path, inspection)
                self.assertEqual(missing['status'], 'unavailable')
                self.assertNotIn('pages', missing)
            path.write_bytes(b'changed')
            with self.assertRaises(LibraryError):
                read_printed(path, inspection)

    def test_enrichment_uses_bound_printed_assertions_without_fabricating_embedded_metadata(self):
        from library.metadata import enrich
        from library.catalog import LibraryError
        from library.review import report
        from test_library_publish import setup_library
        body = (Path(__file__).parent / 'fixtures/bnf-prince.xml').read_text()
        with tempfile.TemporaryDirectory() as folder:
            store, _, _, asset = setup_library(Path(folder))
            state = store.read()
            state['works'], state['editions'], state['variants'] = {}, {}, {}
            state['assets'][asset].pop('identity')
            state['assets'][asset]['embedded'] = {}
            state['assets'][asset]['printed'] = {'status': 'ok', 'asset': asset,
                'pages': [{'name': '001.jpg', 'sha256': 'b' * 64,
                           'text': 'ISBN 9782871297833\nTakeshi Konomi\nVolume 2\nLes crochets de la vipere'}],
                'tool': 'tesseract fixture', 'window': 'first4_last4'}
            state['assets'][asset]['captured_hints'] = {}
            store.save(state, expected=state['revision'])
            p = Providers(Path(folder) / 'cache', fetch=lambda url: (200, body), interval=0)
            result = enrich(store, asset, p, [])
            self.assertEqual(result['assets'][asset]['identity_result']['status'], 'identified')
            self.assertEqual(result['assets'][asset]['embedded'], {})
            self.assertIn('bnf', next(iter(report(store)['cases'].values()))['candidates'][0]['source'])
            self.assertEqual(next(iter(result['editions'].values()))['revision'], '')

    def test_bnf_rejects_ambiguous_wrong_identifier_medium_and_unsafe_xml(self):
        body = (Path(__file__).parent / 'fixtures/bnf-prince.xml').read_text()
        cases = [body.replace('>1</srw:numberOfRecords>', '>2</srw:numberOfRecords>'),
                 body.replace('9782871297833', '9782373494730'),
                 body.replace('Texte imprimé', 'Ressource électronique'),
                 '<!DOCTYPE root [<!ENTITY a SYSTEM "file:///etc/passwd">]>' + body]
        for content in cases:
            with self.subTest(case=content[:40]), tempfile.TemporaryDirectory() as folder:
                result = Providers(Path(folder), fetch=lambda url: (200, content), interval=0).source('bnf', '9782871297833')
                self.assertEqual(result['status'], 'unavailable', result)
