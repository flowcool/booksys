"""Work-level exports preserve source annotations without an edition claim."""
import base64
import unittest
from xml.etree import ElementTree as ET
from library.xmlmetadata import work_comicinfo

class WorkComicInfoTests(unittest.TestCase):
    def test_preserves_annotations_and_maps_work_without_fabricating_age(self):
        source = b'<ComicInfo><Title>Old</Title><Notes>Manual note</Notes><Writer>Old writer</Writer><Pages><Page Image="0" Bookmark="Keep" /></Pages><PrivateUnknown>central only</PrivateUnknown></ComicInfo>'
        asset = {'source_comicinfo_base64': base64.b64encode(source).decode(), 'pages': 2,
                 'overrides': {'summary': 'Manual summary'}}
        data = work_comicinfo(asset, {'title': 'New title', 'adult': True, 'genres': ['Aventure'], 'summary': 'Reference'})
        root = ET.fromstring(data)
        self.assertEqual(root.findtext('Notes'), 'Manual note')
        self.assertEqual(root.findtext('Summary'), 'Manual summary')
        self.assertEqual(root.findtext('Title'), 'New title')
        self.assertEqual(root.find('Pages/Page').get('Bookmark'), 'Keep')
        self.assertIsNone(root.find('AgeRating'))
        self.assertIsNone(root.find('PrivateUnknown'))
        self.assertEqual(base64.b64decode(asset['source_comicinfo_base64']), source)

    def test_explicit_conflict_removes_stale_metadata_but_manual_override_wins(self):
        asset = {'source_comicinfo_base64': base64.b64encode(b'<ComicInfo><Title>Old</Title><Publisher>Stale</Publisher></ComicInfo>').decode()}
        work = {'title': 'Known', 'field_conflicts': {'publisher': []}}
        self.assertIsNone(ET.fromstring(work_comicinfo(asset, work)).find('Publisher'))
        asset['overrides'] = {'publisher': 'Manual'}
        self.assertEqual(ET.fromstring(work_comicinfo(asset, work)).findtext('Publisher'), 'Manual')

    def test_external_entities_are_rejected(self):
        from library.catalog import LibraryError
        asset = {'source_comicinfo_base64': base64.b64encode(b'<!DOCTYPE x [<!ENTITY x SYSTEM "file:///etc/passwd">]><ComicInfo/>').decode()}
        with self.assertRaises(LibraryError):
            work_comicinfo(asset, {'title': 'Known'})

    def test_legacy_adult_flag_does_not_fabricate_age(self):
        from library.xmlmetadata import comicinfo
        state = {'assets': {'a': {'identity': {'work': 'w', 'edition': 'e'}}},
                 'works': {'w': {'title': 'Known', 'public': 'adult'}},
                 'editions': {'e': {'publisher': 'P', 'language': 'fr'}}}
        self.assertIsNone(ET.fromstring(comicinfo(state, 'a')).find('AgeRating'))

    def test_work_export_through_archive_transform_preserves_page_hashes(self):
        import tempfile
        import zipfile
        from pathlib import Path
        from library.readers import inspect_file
        from library.transform import transform_archive
        from test_library_readers import image_bytes
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source.cbz'
            with zipfile.ZipFile(source, 'w') as archive:
                archive.writestr('001.png', image_bytes())
            inspected = inspect_file(source, stable_seconds=.01)
            (root / 'out').mkdir()
            xml = work_comicinfo({'pages': 1}, {'title': 'Known', 'adult': True})
            result = transform_archive(source, root / 'out', xml, inspected['sha256'])
            self.assertEqual(result['page_manifest'], inspected['page_manifest'])
            with zipfile.ZipFile(root / 'out/book.cbz') as archive:
                self.assertEqual(archive.read('001.png'), image_bytes())
                self.assertIsNone(ET.fromstring(archive.read('ComicInfo.xml')).find('AgeRating'))

    def test_partial_date_does_not_invent_day_and_roles_follow_schema_order(self):
        root = ET.fromstring(work_comicinfo({}, {'title': 'Known', 'date': '2020-03',
            'contributors': {'writer': ['A', 'B'], 'penciller': ['C']}}))
        self.assertEqual(root.findtext('Year'), '2020')
        self.assertEqual(root.findtext('Month'), '3')
        self.assertIsNone(root.find('Day'))
        self.assertEqual(root.findtext('Writer'), 'A, B')
        self.assertEqual(root.findtext('Penciller'), 'C')
