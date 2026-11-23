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
            ('Archie #54 - Cow-boy (Part Two)', 'Cow-boy (Part Two)'),
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

    def test_elided_articles_agree_with_slug_tokens_in_title_and_series(self) -> None:
        from library.work_identity import normalized

        for field in ('title', 'series'):
            for label, slug in (("L'eau enchantée", 'L eau enchantee'),
                                ("D’une rive", 'D une rive'),
                                ('L_eau enchantée', 'L eau enchantee'),
                                ('L-Eau-enchantee', 'L eau enchantee')):
                with self.subTest(field=field, label=label):
                    self.assertEqual(normalized(label, field), normalized(slug, field))
                    self.assertNotIn(normalized(slug, field).split()[0], ('l', 'd'))
        self.assertNotEqual(normalized('D-Day', 'title'), normalized('Day', 'title'))

    def test_release_annotations_do_not_extend_a_title_or_issue(self) -> None:
        for label in (
            'Archie #54 - Cow-boy [Digital 1920].cbz',
            'Archie #54 - Cow-boy (TONER).cbz',
            '[BD FR] Archie #54 - Cow-boy.cbz',
            'Archie #54 - Cow-boy (Digital-1920).cbz',
            'Archie #54 - Cow-boy - Mythic.cbz',
            'Archie #54 - Cow-boy [Digital] [BD FR] (TONER).cbz',
        ):
            with self.subTest(label=label):
                self.assertEqual(parse_name(label), {'series': 'Archie', 'number': '54', 'title': 'Cow-boy'})
        self.assertEqual(parse_name('Archie #54 (Digital-1920).cbz'), {'series': 'Archie', 'number': '54'})
        self.assertEqual(parse_name('Archie #54 - Mythic.cbz')['title'], 'Mythic')

    def test_generic_captions_do_not_veto_a_numbered_album(self) -> None:
        from library.bdnex import identity_for

        key = 'a' * 64
        for caption in ('Issue 6', 'Untitled', 'N° 6'):
            with self.subTest(caption=caption):
                state = {'assets': {key: {'sha256': key, 'embedded': {
                    'Title': caption, 'Series': 'Archie', 'Number': '6'}}}}
                self.assertNotIn('title', identity_for(state, key))
                self.assertFalse(any(a['field'] == 'title' for a in asset_assertions(bind_evidence(state, key))))
        state['assets'][key]['embedded']['Title'] = 'Issue at hand'
        self.assertEqual(identity_for(state, key)['title'], ['issue at hand'])

    def test_parenthesized_creator_requires_album_credits(self) -> None:
        from library.bdnex import identity_for
        from library.work_enrichment import qualify_filename_titles

        label = "Freaks' Squeele Rouge - 03 - Que Sera Sera (Maudoux-Sourya) [Digital] [BD FR]"
        parsed = parse_name(label)
        self.assertEqual(parsed['title'], 'Que Sera Sera')
        self.assertEqual(parsed['title_suffix_hint'], 'Maudoux-Sourya')
        key = 'a' * 64
        state = {'assets': {key: {'sha256': key, 'origins': ['o']}},
                 'origins': {'o': {'sha256': key, 'source': 'source', 'path': label + '.cbz'}}}
        evidence = bind_evidence(state, key)
        self.assertNotIn('title', identity_for(state, key))
        reference = {'title': 'Que Sera Sera', 'writers': ['Another Writer'], 'artists': [],
                     'url': 'https://www.bedetheque.com/retained-exact-album'}
        qualify_filename_titles(evidence, reference)
        titles = evidence['provenance']['title']
        self.assertEqual(titles[0]['value'], 'Que Sera Sera (Maudoux-Sourya)')
        self.assertNotIn('creator_reference', titles[0])
        reference.update(writers=['Maudoux'], artists=['Sourya'])
        qualify_filename_titles(evidence, reference)
        self.assertEqual(titles[0]['value'], 'Que Sera Sera')
        self.assertEqual(titles[0]['creator_reference'], reference['url'])
