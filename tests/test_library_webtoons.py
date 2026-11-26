from copy import deepcopy
import unittest
from xml.etree import ElementTree

from library.admission import projection, publication_comicinfo
from library.metadata import prepare_enrichment
from library.primitives import empty
from library.work_identity import accepted_work


KEY = 'a' * 64
CHANNEL = 1288796629


class Snapshot:
    def __init__(self, *documents):
        self.state = empty()
        self.state['assets'][KEY] = {'origins': [], 'sha256': KEY, 'format': 'pdf'}
        for i, document in enumerate(documents):
            oid = str(i)
            self.state['assets'][KEY]['origins'].append(oid)
            path = document.get('original_name', 'Other.pdf')
            self.state['origins'][oid] = {'telegram': document, 'sha256': KEY, 'path': path, 'source': 'external:' + path}

    def result_set(self, *_):
        return deepcopy(self.state)


class NoProviders:
    def __getattr__(self, name):
        raise AssertionError(f'Unexpected provider access: {name}')


def document(name, title='  Série   de test !  '):
    return {'channel': CHANNEL, 'topic': None, 'original_name': name, 'index_title': title}


class WebtoonTests(unittest.TestCase):
    def test_chapter_uses_index_title_and_receipt_filename(self):
        for name, chapter in (('Series Chapitre 07.pdf', '7'), ('Series chapitre 23.pdf', '23'),
                              ('Sa Place 70.pdf', '70'), ('Series_Chapitre_04_@WEBTOONS_LISTS.pdf', '4')):
            with self.subTest(name=name):
                store = Snapshot(document(name))
                state = prepare_enrichment(store, KEY, NoProviders(), [])
                result = state['assets'][KEY]['work_result']
                self.assertEqual(result['status'], 'webtoon')
                self.assertEqual(result['work'], {'series': 'Série de test !', 'number': chapter})
                self.assertEqual({p['field'] for p in result['provenance']}, {'series', 'number'})

    def test_missing_or_conflicting_provenance_requires_review(self):
        cases = (([document('Series.pdf')], 'webtoon_missing_chapter'),
                 ([document('Series 7.pdf', '')], 'webtoon_missing_index_title'),
                 ([document('Series 7.pdf'), document('Other 7.pdf', 'Other')], 'webtoon_conflicting_series'),
                 ([document('Series 7.pdf'), document('Series 8.pdf')], 'webtoon_conflicting_chapter'))
        for documents, reason in cases:
            with self.subTest(reason=reason):
                result = prepare_enrichment(Snapshot(*documents), KEY, NoProviders(), [])['assets'][KEY]['work_result']
                self.assertEqual((result['status'], result['collection'], result['reason_code']),
                                 ('review', 'webtoon', reason))
                self.assertEqual(result['work'], {})

    def test_mixed_origins_keep_the_existing_identification(self):
        store = Snapshot(document('Series 7.pdf'), {'channel': 202})
        previous = {'status': 'identified', 'work': {'series': 'Previously identified'}}
        store.state['assets'][KEY]['work_result'] = previous
        self.assertEqual(prepare_enrichment(store, KEY, NoProviders(), [])['assets'][KEY]['work_result'], previous)

    def test_admission_uses_chapter_path_and_comicinfo_without_a_work_identity(self):
        state = prepare_enrichment(Snapshot(document('Sa Place 70.pdf', 'Sa Place')), KEY, NoProviders(), [])
        asset = state['assets'][KEY]
        desired = projection(state, KEY)
        self.assertIsNone(accepted_work(asset))
        self.assertEqual(desired['status'], 'webtoon')
        self.assertTrue(desired['target'].startswith('webtoon/Sa Place/70/'))
        tags = ElementTree.fromstring(publication_comicinfo(asset, desired, KEY))
        self.assertEqual({field.tag: field.text for field in tags}, {'Series': 'Sa Place', 'Number': '70'})

    def test_copy_metadata_changes_only_for_an_accepted_series_reference(self):
        state = prepare_enrichment(Snapshot(document('Sa Place 70.pdf', 'Sa Place')), KEY, NoProviders(), [])
        asset = state['assets'][KEY]
        reference = {'decision': 'accepted', 'title': 'Her Seat',
                     'url': 'https://www.mangaupdates.com/series/abc/her-seat',
                     'authors': [{'name': 'Sample Writer', 'type': 'Author'},
                                 {'name': 'Sample Artist', 'type': 'Artist'}]}
        plain = projection(state, KEY)
        review = projection(state, KEY, references={'sa place': {**reference, 'decision': 'review'}})
        self.assertEqual(plain, review)
        enriched = projection(state, KEY, references={'sa place': reference})
        self.assertNotEqual(plain['signature'], enriched['signature'])
        tags = ElementTree.fromstring(publication_comicinfo(asset, enriched, KEY))
        self.assertEqual({field.tag: field.text for field in tags}, {'Series': 'Sa Place', 'Number': '70',
            'AlternateSeries': 'Her Seat', 'Writer': 'Sample Writer', 'Penciller': 'Sample Artist',
            'Manga': 'Yes', 'Web': reference['url']})

    def test_queue_reports_webtoon_chapters_and_reviews_outside_identification_yield(self):
        from library.projection import identification_counts, operator_row

        grouped = prepare_enrichment(Snapshot(document('Sa Place 70.pdf')), KEY, NoProviders(), [])['assets'][KEY]
        review = prepare_enrichment(Snapshot(document('Sa Place.pdf')), KEY, NoProviders(), [])['assets'][KEY]
        origin = {'availability': 'validated', 'source': 'external:test.pdf', 'path': 'test.pdf'}
        self.assertEqual(operator_row('chapter', origin, grouped, {})['identity'], 'webtoon')
        self.assertEqual(operator_row('review', origin, review, {})['identity'], 'webtoon review')
        counts = identification_counts([grouped['work_result'], review['work_result'], {'status': 'identified'},
                                        {'status': 'review'}, {'status': 'periodical'}, {}])
        self.assertEqual(counts, {'identified_assets': 1, 'identification_attempted_assets': 2,
                                  'webtoon_assets': 1, 'webtoon_review_assets': 1})
