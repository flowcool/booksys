"""Title evidence sources must retain captions and reject volume placeholders."""
import unittest
from typing import Any

from library.asset_evidence import bind_evidence
from library.names import parse_name
from library.work_identity import asset_assertions


class TitleEvidenceTests(unittest.TestCase):
    def test_embedded_volume_placeholder_is_not_a_title_assertion(self) -> None:
        key = 'a' * 64
        for title in ('Tome 2', 'Tome 4', 'Volume 10', 'Vol. 2', 'T02', '#2'):
            with self.subTest(title=title):
                state: dict[str, Any] = {'assets': {key: {'sha256': key, 'embedded': {
                    'Title': title, 'Series': 'Ace Attorney Investigations', 'Number': '2'}}}}
                evidence = bind_evidence(state, key)
                self.assertNotIn('title', evidence)
                self.assertFalse(any(a['field'] == 'title' for a in asset_assertions(evidence)))
                self.assertEqual(evidence['number'], '2')

    def test_third_segment_rejects_volume_placeholders(self) -> None:
        for title in ('Vol. 2', 'T02', '#2'):
            with self.subTest(title=title):
                self.assertNotIn('title', parse_name('Archie #54 - ' + title))

    def test_bound_capture_rejects_volume_placeholders(self) -> None:
        key = 'a' * 64
        for title in ('Vol. 2', 'T02', '#2'):
            with self.subTest(title=title):
                label = 'Archie #54 - ' + title
                state: dict[str, Any] = {'assets': {key: {'sha256': key, 'origins': ['o'],
                    'captured_hints': {'source': {'title': label}}}},
                    'origins': {'o': {'sha256': key, 'source': 'source', 'path': 'Archie #54.cbz'}}}
                evidence = bind_evidence(state, key)
                self.assertFalse(any(a['field'] == 'title' for a in asset_assertions(evidence)))
                self.assertIn(label, evidence['labels'])

    def test_real_third_segment_titles_remain_assertions(self) -> None:
        for label, title in (
            ('Archie - 54 - Cow-boy', 'Cow-boy'),
            ('Les Vengeurs - 12 - Maintenant par ma main va mourir un scélérat',
             'Maintenant par ma main va mourir un scélérat'),
            ('Archie #54 - Tome 2: The return', 'Tome 2: The return'),
        ):
            with self.subTest(label=label):
                self.assertEqual(parse_name(label)['title'], title)

    def test_existing_exact_album_credits_qualify_filename_and_captured_creators(self) -> None:
        from library.work_enrichment import qualify_filename_titles
        key = 'a' * 64
        state: dict[str, Any] = {'assets': {key: {'sha256': key, 'origins': ['o'],
            'captured_hints': {'source': {'title': 'Gachiakuta T17 - Urana Kei'}}}},
            'origins': {'o': {'sha256': key, 'source': 'source',
                              'path': 'Gachiakuta T17 - Urana Kei.pdf'}}}
        evidence = bind_evidence(state, key)
        reference = {'title': 'Gachiakuta', 'writers': ['Urana Kei'], 'artists': [],
                     'url': 'https://www.bedetheque.com/retained-exact-album'}
        qualify_filename_titles(evidence, reference)
        titles = evidence['provenance']['title']
        self.assertEqual(len(titles), 2)
        self.assertTrue(all(a['scope'] == 'discovery' for a in titles))
        self.assertTrue(all(a['creator_reference'] == reference['url'] for a in titles))

    def test_unconfirmed_embedded_creator_and_real_title_remain_bound(self) -> None:
        # Missing reference credits cannot justify removing a creator-like caption.
        key = 'a' * 64
        for title in ('Urana Kei', 'Cow-boy', 'Tome 2: The return'):
            with self.subTest(title=title):
                state: dict[str, Any] = {'assets': {key: {'sha256': key, 'embedded': {'Title': title}}}}
                evidence = bind_evidence(state, key)
                self.assertTrue(any(a['field'] == 'title' and a['value'] == title
                                    for a in asset_assertions(evidence)))
