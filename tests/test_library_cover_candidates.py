"""The qualified cover seam proposes candidates; it never assigns an identity."""
import unittest
from PIL import Image, ImageDraw
from library.cover_candidates import cover_hash, compare_covers


class CoverCandidateTests(unittest.TestCase):
    def test_matching_cover_ties_remain_candidates_and_blank_input_abstains(self):
        image = Image.new('RGB', (96, 128), 'white')
        draw = ImageDraw.Draw(image)
        draw.rectangle((5, 8, 70, 35), fill='black')
        draw.ellipse((30, 55, 85, 118), fill='navy')
        fingerprint = cover_hash(image)
        self.assertIsNotNone(fingerprint)
        self.assertEqual(compare_covers(fingerprint, {'edition-a': fingerprint, 'edition-b': fingerprint}),
            [{'reference_id': 'edition-a', 'distance': 0}, {'reference_id': 'edition-b', 'distance': 0}])
        self.assertIsNone(cover_hash(Image.new('RGB', (96, 128), 'white')))
        self.assertEqual(compare_covers(None, {'edition-a': fingerprint}), [])

    def test_different_cover_is_ranked_without_a_fabricated_confidence(self):
        image = Image.new('RGB', (96, 128), 'white')
        draw = ImageDraw.Draw(image)
        draw.rectangle((4, 7, 51, 45), fill='black')
        draw.ellipse((28, 58, 81, 119), fill='navy')
        local = cover_hash(image)
        different = cover_hash(image.transpose(Image.Transpose.FLIP_LEFT_RIGHT))
        ranked = compare_covers(local, {'different-work': different, 'same-work': local, 'missing': None})
        self.assertEqual(ranked[0], {'reference_id': 'same-work', 'distance': 0})
        self.assertGreater(ranked[1]['distance'], 0)
        self.assertEqual(set(ranked[0]), {'reference_id', 'distance'})
        self.assertIsNone(cover_hash(Image.new('RGB', (2, 2), 'red')))
