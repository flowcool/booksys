import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from library.xmlmetadata import comicinfo, validate_xml
from library.transform import transform_archive
from library.catalog import LibraryError
from library.readers import inspect_file
from test_library_publish import setup_library
from test_library_readers import image_bytes, rar_bytes


class TransformTests(unittest.TestCase):
    def test_catalogue_metadata_replaces_conflicting_source_xml_without_reencoding(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store, reception, original, asset = setup_library(root)
            state = store.read()
            state['assets'][asset]['overrides'] = {'title': 'Validated & corrected title', 'genres': ['Adventure']}
            source = root / 'source.cbz'
            with zipfile.ZipFile(source, 'w') as archive:
                archive.writestr('001.png', image_bytes())
                archive.writestr('ComicInfo.xml', '<ComicInfo><Title>Wrong source title</Title></ComicInfo>')
                archive.writestr('Thumbs.db', b'inert')
            inspected = inspect_file(source, stable_seconds=.01)
            xml = comicinfo(state, asset)
            validate_xml(xml)
            output = root / 'out'
            output.mkdir()
            result = transform_archive(source, output, xml, inspected['sha256'])
            self.assertEqual(result['page_manifest'], inspected['page_manifest'])
            with zipfile.ZipFile(output / 'book.cbz') as archive:
                self.assertEqual(archive.read('001.png'), image_bytes())
                self.assertEqual(archive.read('Thumbs.db'), b'inert')
                tree = ElementTree.fromstring(archive.read('ComicInfo.xml'))
                self.assertEqual(tree.findtext('Title'), 'Validated & corrected title')
                self.assertEqual(tree.findtext('Genre'), 'Adventure')
                self.assertEqual(len([n for n in archive.namelist() if n == 'ComicInfo.xml']), 1)
            parsed = inspect_file(output / 'book.cbz', stable_seconds=.01)
            self.assertEqual(parsed['status'], 'validated', parsed)
            self.assertNotEqual(result['artifact_sha256'], inspected['sha256'])

    def test_cbr_conversion_preserves_page_bytes_and_order(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source.cbr'
            source.write_bytes(rar_bytes('001.png', image_bytes()))
            checked = inspect_file(source, stable_seconds=.01)
            (root / 'out').mkdir()
            result = transform_archive(source, root / 'out', b'<ComicInfo><Title>Album</Title></ComicInfo>', checked['sha256'])
            self.assertEqual(result['page_manifest'], checked['page_manifest'])
            with zipfile.ZipFile(root / 'out/book.cbz') as archive:
                self.assertEqual(archive.read('001.png'), image_bytes())

    def test_invalid_xml_and_ambiguous_page_names_are_blocked(self):
        with self.assertRaises(LibraryError):
            validate_xml(b'<ComicInfo><PageCount>unknown</PageCount></ComicInfo>')
        with self.assertRaises(LibraryError):
            validate_xml(b'<!DOCTYPE x [<!ENTITY a SYSTEM "file:///etc/passwd">]><ComicInfo/>')
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'ambiguous.cbz'
            with zipfile.ZipFile(source, 'w') as archive:
                archive.writestr('1.png', image_bytes())
                archive.writestr('01.png', image_bytes())
            self.assertEqual(inspect_file(source, stable_seconds=.01)['status'], 'pending')
