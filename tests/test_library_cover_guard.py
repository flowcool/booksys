"""Offline cover confirmation at the worker/reader boundary."""
import io
import copy
import hashlib
from pathlib import Path
import random
import tempfile
from typing import Any
import zipfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw
from library import decode
from library.primitives import LibraryError
from library.readers import READER_POLICY


def texture(seed=71):
    rng = random.Random(seed)
    image = Image.new('RGB', (500, 700), 'white')
    draw = ImageDraw.Draw(image)
    for _ in range(500):
        x, y = rng.randrange(480), rng.randrange(680)
        draw.rectangle((x, y, x + rng.randrange(2, 20), y + rng.randrange(2, 20)),
                       fill=tuple(rng.randrange(256) for _ in range(3)))
    return image


def encoded(image):
    buffer = io.BytesIO()
    image.save(buffer, 'PNG')
    return buffer.getvalue()


class CoverGuardTests(unittest.TestCase):
    def test_cover_threshold_uses_full_catalogue_work_count_and_album_id(self):
        from library.cover_confirmation import apply_cover_threshold
        from test_candidate_concordance import candidate, evidence

        row = candidate(series='Sky Patrol Press')
        asset = evidence('Sky Patrol T3')
        album = {'album_id': '123', 'series': 'Sky Patrol Press', 'number': '3', 'title': 'The Silent Island'}
        edition = dict(album, album_id='456')
        sibling = dict(album, album_id='789', series='Sky Patrol Other Press', title='The Last Gate')
        for score, albums, expected in (
            (40, None, True), (20, None, False), (19.999, [album], False),
            (20, [album, edition], True), (39.999, [album, edition], True),
            (20, [album, sibling], False), (20, [edition], False),
            (20, [dict(album, title=''), dict(edition, title='')], False),
        ):
            with self.subTest(score=score, albums=albums):
                def lookup(series, number):
                    return None if albums is None else {'index': 'a' * 64, 'albums': albums}

                receipt: dict[str, Any] = {
                    'score': score, 'threshold': 40, 'result': 'pass' if score >= 40 else 'review',
                    'reason': 'cover_agreement' if score >= 40 else 'below_threshold',
                }
                method = apply_cover_threshold(receipt, asset, row, lookup)
                self.assertEqual(receipt['result'] == 'pass', expected)
                if expected and score < 40:
                    self.assertEqual(method, 'catalogue-unique-cover-v1')
                    self.assertEqual(receipt['catalogue_family']['family_size'], 1)
                    self.assertEqual(receipt['catalogue_family']['matched_families'], ['sky patrol press'])
                    self.assertEqual(receipt['threshold'], 20)
                else:
                    self.assertIsNone(method)
                    self.assertEqual(receipt['threshold'], 40)

    def test_prefix_cover_pass_records_requeue_method_after_finalization(self):
        from types import SimpleNamespace
        from library.work_enrichment import save_work_result
        from library.work_identity import evaluate_work
        from test_candidate_concordance import evidence

        state, row, _ = self.fixture(series='Sky Patrol 2e serie Press', number='3')
        asset = state['assets']['a' * 64]
        labels = evidence('Sky Patrol - Série 2 - Tome 3')
        album = {'album_id': '123', 'series': row['series'], 'number': '3', 'title': row['title']}
        lookup = lambda series, issue: {'index': 'a' * 64, 'albums': [album]}
        providers = SimpleNamespace(bdnex=SimpleNamespace(lookup_family=lookup),
                                    get_binary=lambda url: {'status': 'ok', 'body': b'reference'})

        def extract(operation, source, payload, destination):
            (destination / 'first-page.img').write_bytes(b'original')
            return {'status': 'extracted', 'input_sha256': asset['sha256']}

        for score in (10, 20, 40):
            with self.subTest(score=score), tempfile.TemporaryDirectory() as directory:
                result = evaluate_work(copy.deepcopy(labels), [row], family_lookup=lookup)
                result['evidence'] = copy.deepcopy(labels)
                compared = {'score': score, 'result': 'pass' if score >= 40 else 'review',
                            'reason': 'cover_agreement' if score >= 40 else 'below_threshold'}
                providers._cover_confirmations = {}
                with patch.object(decode, 'WORK_ROOT', directory), patch.object(decode, 'run', side_effect=extract), \
                        patch('library.cover_confirmation.compare', return_value=compared):
                    save_work_result(asset, result, providers, lambda: Path('/unused'))
                receipt = result['evidence']['cover_confirmation']
                self.assertEqual(receipt['catalogue_family']['matched_families'], ['sky patrol 2e serie press'])
                self.assertEqual(receipt['threshold'], 20 if score == 20 else 40)
                self.assertEqual(result['status'], 'identified' if score >= 20 else 'review')
                self.assertEqual(result.get('identification_method'),
                                 'catalogue-unique-cover-v1' if score >= 20 else None)

    def test_real_child_compares_identical_textured_images(self):
        from library.cover_confirmation import compare
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(decode, 'WORK_ROOT', directory):
                result = compare(encoded(texture()), encoded(texture()))
        self.assertEqual(result['result'], 'pass', result)
        self.assertGreaterEqual(result['score'], 95)
        self.assertEqual(result['input_sha256'], result['reference_sha256'])

    def test_sift_rejects_unrelated_blank_corrupt_and_oversized_images(self):
        from library.cover_confirmation import compare
        with tempfile.TemporaryDirectory() as directory, patch.object(decode, 'WORK_ROOT', directory):
            for other in (encoded(texture(999)), encoded(Image.new('RGB', (500, 700))),
                          b'broken', encoded(Image.new('1', (5001, 5000)))):
                with self.subTest(size=len(other)):
                    result = compare(encoded(texture()), other)
                    self.assertEqual(result['result'], 'review', result)
            image = texture()
            cropped = image.crop((20, 30, 480, 670)).resize((345, 480))
            self.assertEqual(compare(encoded(image), encoded(cropped))['result'], 'pass')

    def test_crashes_timeouts_and_nonfinite_scores_stay_review(self):
        from library.cover_confirmation import compare
        for failure in (LibraryError('Decoder timed out'), LibraryError('signal 9')):
            with patch.object(decode, 'run', side_effect=failure):
                self.assertEqual(compare(b'a', b'b')['result'], 'review')
        for score in (float('nan'), float('inf'), -1, 101, True):
            with patch.object(decode, 'run', return_value={'status': 'compared', 'score': score}):
                self.assertEqual(compare(b'a', b'b')['result'], 'review')

    def test_first_page_uses_validated_natural_and_chapter_order_and_hashes(self):
        from library.reader_worker import inspect_archive
        png = encoded(texture())
        with tempfile.TemporaryDirectory() as directory, patch.object(decode, 'WORK_ROOT', directory):
            root = Path(directory)
            for names in (['10.png', '2.png'], ['chapter 2/1.png', 'chapter 1/2.png']):
                for order in (names, names[::-1]):
                    source = root / 'book.cbz'
                    with zipfile.ZipFile(source, 'w') as archive:
                        for name in order:
                            archive.writestr(name, png)
                    inspection = inspect_archive(source, '.cbz')
                    member = inspection['page_manifest'][0]
                    self.assertEqual(member['name'], names[1])
                    request = {'asset_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                               'member': member, 'reader_policy': READER_POLICY}
                    with tempfile.TemporaryDirectory(dir=root) as output:
                        result = decode.run('first_page', source, request, Path(output))
                        self.assertEqual(result['status'], 'extracted', result)
                        self.assertEqual((Path(output)/'first-page.img').read_bytes(), png)
                    for field in ('asset_sha256', 'reader_policy'):
                        changed = {**request, field: 'changed'}
                        self.assertEqual(decode.run('first_page', source, changed)['status'], 'pending')
                    changed = {**request, 'member': {**member, 'sha256': 'a'*64}}
                    self.assertEqual(decode.run('first_page', source, changed)['status'], 'pending')

    def fixture(self, *, title=None, embedded=None, series='Series', number='2'):
        from library.asset_evidence import bind_evidence
        from library.work_identity import evaluate_work
        key = 'a'*64
        state = {'assets': {key: {'sha256': key, 'origins': [], 'embedded': {
            'Series': series, 'Number': number, **({'Title': title} if title else {}), **(embedded or {})},
            'page_manifest': [{'name': '1.png', 'sha256': hashlib.sha256(b'original').hexdigest(), 'size': 8}],
            'reader_policy': READER_POLICY}}, 'origins': {}}
        url = 'https://www.bedetheque.com/BD-Series-Tome-2-123.html'
        row = {'source': 'bedetheque', 'edition_ref': '123', 'url': url,
               'title': 'Informative Album', 'series': series, 'number': number,
               'author': 'Jane Writer', 'contributors': [{'name': 'John Artist', 'role': 'Dessin'}],
               'cover_binding': {'album_id': '123', 'album_url': url,
                                 'cover_url': 'https://www.bedetheque.com/media/Couvertures/123.jpg'}}
        evidence = bind_evidence(state, key)
        result = evaluate_work(evidence, [row])
        result['evidence'] = evidence
        return state, row, result

    def test_missing_cover_stays_review_before_classification(self):
        from library.work_enrichment import save_work_result
        state, row, result = self.fixture()
        save_work_result(state['assets']['a'*64], result)
        self.assertEqual(result['status'], 'review')
        self.assertEqual(result['work'], {})
        self.assertNotIn('classification', result)
        self.assertEqual(result['reason_code'], 'cover_archive_unavailable')

    def test_real_archive_to_receipt_to_classification_and_exact_reuse(self):
        from types import SimpleNamespace
        from library.reader_worker import inspect_archive
        from library.work_enrichment import save_work_result
        original = encoded(texture())
        with tempfile.TemporaryDirectory() as directory, patch.object(decode, 'WORK_ROOT', directory):
            source = Path(directory)/'series.cbz'
            with zipfile.ZipFile(source, 'w') as archive:
                archive.writestr('2.png', encoded(texture(999)))
                archive.writestr('1.png', original)
            inspection = inspect_archive(source, '.cbz')
            state, row, result = self.fixture()
            asset = state['assets']['a'*64]
            asset.update(sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                         page_manifest=inspection['page_manifest'])
            result['evidence']['asset'] = asset['sha256']
            fresh = copy.deepcopy(result)
            providers = SimpleNamespace(get_binary=lambda url: {'status': 'ok', 'body': original,
                                                                'retrieved_at': '2026-11-22'})
            save_work_result(asset, result, providers, lambda: source)
            self.assertEqual(result['status'], 'identified', result)
            receipt = result['evidence']['cover_confirmation']
            self.assertEqual(receipt['member']['name'], '1.png')
            self.assertEqual(receipt['input_sha256'], hashlib.sha256(original).hexdigest())
            self.assertEqual(receipt['reference_sha256'], receipt['input_sha256'])
            self.assertIn('classification', result)
            # Same archive/evidence/reference/method can reuse a comparison.
            with patch.object(decode, 'run', side_effect=AssertionError('Must reuse')):
                save_work_result(asset, fresh, providers, lambda: source)
            self.assertEqual(fresh['status'], 'identified')
            self.assertEqual(fresh['evidence']['cover_confirmation']['score'], receipt['score'])

    def test_only_asset_title_or_explicit_same_role_creator_exempts(self):
        from library.work_enrichment import save_work_result
        cases = [({'title': 'Informative Album'}, 'identified', 'asset_title'),
                 ({'embedded': {'Writer': 'Jane Writer'}}, 'identified', 'asset_creator'),
                 ({'embedded': {'Penciller': 'John Artist'}}, 'identified', 'asset_creator'),
                 ({'embedded': {'Writer': 'John Artist'}}, 'review', 'creator_conflict'),
                 ({'embedded': {'Writer': 'Other Writer'}}, 'review', 'creator_conflict'),
                 ({'title': 'Series'}, 'no_match', None), ({}, 'review', 'archive_unavailable')]
        for kwargs, status, reason in cases:
            with self.subTest(kwargs=kwargs):
                state, row, result = self.fixture(**kwargs)
                save_work_result(state['assets']['a'*64], result)
                self.assertEqual(result['status'], status, result)
                if reason:
                    self.assertEqual(result['evidence']['cover_confirmation']['reason'], reason)

    def test_cover_threshold_and_three_factual_regression_routes(self):
        from library.work_enrichment import save_work_result
        from types import SimpleNamespace
        cases = [('8437', 0.04), ('419681', 7.55), ('403116', 0.0), ('123', 39.999),
                 ('123', 40.0), ('123', 40.001)]
        for album, score in cases:
            with self.subTest(album=album, score=score), tempfile.TemporaryDirectory() as directory:
                state, row, result = self.fixture()
                row = result['concordant_candidates'][0]
                row['edition_ref'] = album
                row['cover_binding']['album_id'] = album
                providers = SimpleNamespace(get_binary=lambda url: {'status': 'ok', 'body': b'reference'})
                def extract(operation, source, payload, destination):
                    (destination/'first-page.img').write_bytes(b'original')
                    return {'status': 'extracted', 'input_sha256': 'a'*64}
                compared = {'score': score, 'result': 'pass' if score >= 40 else 'review',
                            'reason': 'cover_agreement' if score >= 40 else 'below_threshold'}
                with patch.object(decode, 'WORK_ROOT', directory), patch.object(decode, 'run', side_effect=extract), \
                        patch('library.cover_confirmation.compare', return_value=compared):
                    save_work_result(state['assets']['a'*64], result, providers, lambda: Path('/unused'))
                self.assertEqual(result['status'], 'identified' if score >= 40 else 'review', result)
                self.assertEqual(result['evidence']['cover_confirmation']['album_id'], album)

    def test_dated_series_pure_result_and_failed_cover_followed_by_fallbacks(self):
        from library.work_enrichment import save_work_result
        from library.work_identity import evaluate_work
        state, row, result = self.fixture()
        row['series'] = 'Series 2011'
        proposed = evaluate_work(result['evidence'], [row])
        self.assertEqual(proposed['status'], 'identified')
        proposed['evidence'] = result['evidence']
        asset = state['assets']['a'*64]
        save_work_result(asset, proposed)
        self.assertEqual(proposed['status'], 'review')
        for route in ('hydration', 'aliases', 'old_candidates', 'heading', 'public_discovery', 'retained'):
            fallback = evaluate_work(copy.deepcopy(result['evidence']), [])
            fallback['evidence'] = copy.deepcopy(result['evidence'])
            fallback['evidence'].pop('cover_confirmation', None)
            with self.subTest(route=route):
                save_work_result(asset, fallback)
                self.assertEqual(asset['work_result']['status'], 'review')
                self.assertIn('cover_confirmation', asset['work_result']['evidence'])

    def test_canonical_creator_conflict_wins_over_title_and_repeated_series_is_not_a_title(self):
        from library.work_enrichment import save_work_result
        from library.work_identity import evaluate_work
        state, row, result = self.fixture(title='Informative Album', embedded={'Writer': 'Jane Writer'})
        result['concordant_candidates'][0]['canonical_identity'] = {**row, 'author': 'Other Writer'}
        save_work_result(state['assets']['a'*64], result)
        self.assertEqual(result['reason_code'], 'cover_creator_conflict')
        state, row, result = self.fixture(title='Series')
        row.update(title='Series', series='Series 2011')
        result = {**evaluate_work(result['evidence'], [row]), 'evidence': result['evidence']}
        save_work_result(state['assets']['a'*64], result)
        self.assertEqual(result['status'], 'review')
        self.assertNotEqual(result['evidence']['cover_confirmation']['result'], 'exempt')
