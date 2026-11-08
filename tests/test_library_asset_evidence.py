"""Public source/file binding must not turn a bundle label into child identity."""
import unittest

from library.asset_evidence import bind_evidence
from library.catalog import LibraryError


class AssetEvidenceTests(unittest.TestCase):
    def test_standalone_title_requires_filename_and_bound_capture_agreement(self) -> None:
        key = 'a' * 64
        state = {'assets': {key: {'sha256': key, 'origins': ['origin'],
            'captured_hints': {'https://source': {'title': "L'Oiseau Chanteur (2026)"}}}},
            'origins': {'origin': {'sha256': key, 'source': 'https://source',
                                  'path': 'package/ZzL_Oiseau Chanteur.cbz'}}}
        result = bind_evidence(state, key)
        self.assertEqual(result['title'], "L'Oiseau Chanteur")
        state['assets'][key]['captured_hints']['https://source']['title'] = 'Another title'
        self.assertNotIn('title', bind_evidence(state, key))

    def test_bundle_member_keeps_its_name_and_excludes_parent_identity(self) -> None:
        key = 'a' * 64
        state = {'assets': {key: {'sha256': key, 'embedded': {}, 'origins': ['child'],
            'captured_hints': {'https://source#bundle-member=child': {
                'title': 'Les Geeks - T03', 'parent_source': 'https://source',
                'parent_title': 'Les Geeks - Intégrale (11 Tomes + 2 Best Of)'}}}},
            'origins': {'child': {'sha256': key, 'source': 'https://source',
                'member': 'Les Geeks - T03.pdf', 'parent_origin': 'parent',
                'path': 'bundle-inputs/parent/' + key + '.pdf'},
                'parent': {'source': 'https://source', 'sha256': 'b' * 64}}}
        result = bind_evidence(state, key)
        self.assertEqual(result['labels'], ['Les Geeks - T03'])
        self.assertNotIn('title', result)
        self.assertIn('Les Geeks - Intégrale (11 Tomes + 2 Best Of)', result['discovery_queries'])
        self.assertEqual(result['provenance']['labels'][0]['scope'], 'asset')

    def test_embedded_fields_are_bound_but_unbound_parent_is_only_discovery(self) -> None:
        key = 'a' * 64
        state = {'assets': {key: {'sha256': key,
            'embedded': {'Title': 'Le colosse rouge', 'Series': 'Sorciers & Bourbiers'},
            'origins': ['child'], 'captured_hints': {'https://parent': {
                'title': 'Other anthology'}}}}, 'origins': {'child': {
                'sha256': key, 'source': 'https://parent', 'parent_origin': 'parent',
                'member': 'chapter.pdf', 'path': 'bundle-inputs/x/' + key + '.pdf'}}}
        result = bind_evidence(state, key)
        self.assertEqual(result['title'], 'Le colosse rouge')
        self.assertEqual(result['series'], 'Sorciers & Bourbiers')
        self.assertNotIn('Other anthology', result['labels'])
        self.assertIn('Other anthology', result['discovery_queries'])
        self.assertEqual(result['provenance']['title'][0]['source'], 'embedded:' + key)

    def test_mismatched_origin_cannot_supply_another_books_name(self) -> None:
        key = 'a' * 64
        state = {'assets': {key: {'sha256': key, 'origins': ['other']}},
                 'origins': {'other': {'sha256': 'b' * 64, 'path': 'Wrong book.cbz'}}}
        with self.assertRaises(LibraryError):
            bind_evidence(state, key)
