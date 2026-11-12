"""Work-level exports preserve source annotations without an edition claim."""
import base64
import unittest
from xml.etree import ElementTree as ET
from library.xmlmetadata import work_comicinfo

class WorkComicInfoTests(unittest.TestCase):
    def test_qualified_french_contributors_export_roles(self):
        root = ET.fromstring(work_comicinfo({}, {'title': 'Known', 'contributors': [
            {'name': 'A', 'role': 'Scénario'}, {'name': 'B', 'role': 'Dessin'},
            {'name': 'C', 'role': 'Couleurs'}]}))
        self.assertEqual(root.findtext('Writer'), 'A')
        self.assertEqual(root.findtext('Penciller'), 'B')
        self.assertEqual(root.findtext('Colorist'), 'C')

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


    def test_external_entities_are_rejected(self):
        from library.catalog import LibraryError
        asset = {'source_comicinfo_base64': base64.b64encode(b'<!DOCTYPE x [<!ENTITY x SYSTEM "file:///etc/passwd">]><ComicInfo/>').decode()}
        with self.assertRaises(LibraryError):
            work_comicinfo(asset, {'title': 'Known'})



    def test_partial_date_does_not_invent_day_and_roles_follow_schema_order(self):
        root = ET.fromstring(work_comicinfo({}, {'title': 'Known', 'date': '2020-03',
            'contributors': {'writer': ['A', 'B'], 'penciller': ['C']}}))
        self.assertEqual(root.findtext('Year'), '2020')
        self.assertEqual(root.findtext('Month'), '3')
        self.assertIsNone(root.find('Day'))
        self.assertEqual(root.findtext('Writer'), 'A, B')
        self.assertEqual(root.findtext('Penciller'), 'C')
