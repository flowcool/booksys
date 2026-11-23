from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from xml.etree import ElementTree
from zipfile import ZipFile

from library.admission import AdmissionPublisher, projection, publication_comicinfo
from library.metadata import prepare_enrichment
from library.primitives import empty, validate
from test_library_publish import setup_library


KEY = 'a' * 64


class Snapshot:
    def __init__(self, embedded):
        self.state = empty()
        self.state['assets'][KEY] = {
            'sha256': KEY, 'size': 1, 'format': 'cbz', 'origins': [], 'embedded': embedded,
            'overrides': {'series': 'Other series', 'number': '99'},
        }

    def result_set(self, *_):
        return deepcopy(self.state)


class NoProviders:
    def __getattr__(self, name):
        raise AssertionError(f'Unexpected provider access: {name}')


class PeriodicalTests(unittest.TestCase):
    def test_periodical_uses_file_fields_and_separate_publication(self):
        store = Snapshot({'Series': 'Fluide Glacial', 'Number': '006', 'Title': 'An album'})
        state = prepare_enrichment(store, KEY, NoProviders(), [])
        asset = state['assets'][KEY]
        result = asset['work_result']
        self.assertEqual(result['status'], 'periodical')
        self.assertEqual(result['work'], {'series': 'Fluide Glacial', 'number': '6'})
        self.assertEqual({claim['field'] for claim in result['provenance']}, {'series', 'number'})
        desired = projection(state, KEY)
        self.assertEqual(desired['status'], 'periodical')
        self.assertTrue(desired['target'].startswith('Périodiques/Fluide Glacial/6/'))
        comicinfo = ElementTree.fromstring(publication_comicinfo(asset, desired, KEY))
        self.assertEqual([(field.tag, field.text) for field in comicinfo], [('Series', 'Fluide Glacial'), ('Number', '6')])
        asset['admission'] = {'status': desired['status'], 'target': desired['target'],
                              'signature': desired['signature'], 'operation': 'b' * 32,
                              'manifest': {'book.cbz': 'c' * 64, 'fiche.md': 'd' * 64}}
        validate(state)

    def test_missing_or_conflicting_issue_stays_in_review_without_using_album_candidates(self):
        for embedded in ({'Series': 'Fillette'}, {'Series': 'Fillette', 'Number': '3'}):
            with self.subTest(embedded=embedded):
                store = Snapshot(embedded)
                if 'Number' in embedded:
                    asset = store.state['assets'][KEY]
                    asset['origins'] = ['file']
                    store.state['origins']['file'] = {'sha256': KEY, 'path': 'Fillette - T04.cbz', 'source': 'external:x'}
                result = prepare_enrichment(store, KEY, NoProviders(), [])['assets'][KEY]['work_result']
                self.assertEqual(result['status'], 'review')
                self.assertEqual(result['work'], {})

    def test_a_title_removed_from_the_list_returns_to_normal_metadata(self):
        from library.periodicals import PERIODICAL_TITLES
        from library.automatic import evidence_version

        store = Snapshot({'Series': 'Nouveau Tintin', 'Number': '14'})
        before = prepare_enrichment(store, KEY, NoProviders(), [])
        asset = before['assets'][KEY]
        self.assertEqual(asset['work_result']['status'], 'periodical')
        old_version = evidence_version(asset)
        class NoReference:
            bdnex = None
            events = []
            def get(self, _):
                return {'status': 'unavailable'}

        store.state = before
        with patch.dict(PERIODICAL_TITLES, {'fillette': 'Fillette'}, clear=True):
            self.assertNotEqual(evidence_version(asset), old_version)
            after = prepare_enrichment(store, KEY, NoReference(), [])
        self.assertEqual(after['assets'][KEY]['work_result']['status'], 'no_match')
        self.assertEqual(projection(after, KEY)['status'], 'holding')

    def test_persisted_periodical_copy_can_be_read_and_reconciled(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, reception, _, key = setup_library(root, self)
            state = catalog.read()
            state['assets'][key]['embedded'] = {'Series': 'Fillette', 'Number': '12', 'Title': 'Ignored'}
            catalog.save(state, expected=state['revision'])
            result = prepare_enrichment(catalog, key, NoProviders(), [])
            catalog.save(result, expected=result['revision'])
            publisher = AdmissionPublisher(catalog, reception, root / 'library', isolated=True)
            operation = publisher.publish_asset(key)
            target = publisher.root / operation['target']
            self.assertEqual(target.relative_to(publisher.root).parts[:3], ('Périodiques', 'Fillette', '12'))
            self.assertEqual(catalog.read()['assets'][key]['admission']['status'], 'periodical')
            self.assertEqual(publisher.reconcile(asset=key)[0]['phase'], 'committed')
            with ZipFile(target / 'book.cbz') as archive:
                xml = ElementTree.fromstring(archive.read('ComicInfo.xml'))
                self.assertEqual([(field.tag, field.text) for field in xml], [('Series', 'Fillette'), ('Number', '12')])
