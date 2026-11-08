import tempfile
import unittest
from pathlib import Path

from library.catalog import Catalog
from library.review import report, decide
from library.publish import Publisher, Capacity
from test_library_publish import setup_library
from xml.etree import ElementTree
import zipfile


class ReviewTests(unittest.TestCase):
    def test_manual_edition_with_evidence_can_publish_without_embedded_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store, reception, original, asset = setup_library(root)
            state = store.read()
            state['works'] = {}
            state['editions'] = {}
            state['variants'] = {}
            state['assets'][asset].pop('identity')
            state['assets'][asset]['contested'] = True
            state['assets'][asset]['assertions'] = [{'field': 'identity', 'raw': 'contradictory source'}]
            self.assertEqual(state['assets'][asset]['embedded'], {})
            store.save(state, expected=state['revision'])
            case = next(v for v in report(store)['cases'].values() if v.get('asset') == asset)
            request = {'request_id': 'manual-edition', 'case': case['id'], 'version': case['version'],
                'action': 'identify', 'values': {'work_id': 'manual-work', 'edition_id': 'manual-edition',
                'work': {'title': 'Operator confirmed album', 'author': 'Confirmed author', 'number': '1'},
                'edition': {'language': 'fr', 'publisher': 'Confirmed publisher',
                    'evidence': {'source': 'https://publisher.example/edition/1', 'operator': 'flowcool',
                                 'basis': 'publisher edition and title page agree'}}}}
            self.assertEqual(decide(store, [request], author='flowcool')[0]['status'], 'applied')
            self.assertFalse(store.read()['assets'][asset]['contested'])
            self.assertEqual(len(store.read()['assets'][asset]['assertions']), 1)
            case = next(v for v in report(store)['cases'].values() if v.get('asset') == asset)
            select = {'request_id': 'manual-selection', 'case': case['id'], 'version': case['version'],
                      'action': 'select', 'values': {}}
            self.assertEqual(decide(store, [select], author='flowcool')[0]['status'], 'applied')
            publisher = Publisher(store, reception, root / 'library', isolated=True, capacity=Capacity(reserve=0))
            operation = publisher.publish(asset)
            with zipfile.ZipFile(root / 'library/bd' / operation['target'] / 'book.cbz') as archive:
                xml = ElementTree.fromstring(archive.read('ComicInfo.xml'))
                self.assertEqual(xml.findtext('Title'), 'Operator confirmed album')
                self.assertEqual(xml.findtext('Publisher'), 'Confirmed publisher')
            self.assertEqual(len(store.ledger()), 2)
            state = store.read()
            state['assets'][asset]['contested'] = True
            state['assets'][asset]['identity_result'] = {'reason': 'new conflicting edition evidence'}
            store.save(state, expected=state['revision'])
            self.assertTrue(store.replay()['assets'][asset]['contested'])

    def test_invalid_identity_never_enters_immutable_ledger(self):
        with tempfile.TemporaryDirectory() as folder:
            store, _, _, asset = setup_library(Path(folder))
            case = next(v for v in report(store)['cases'].values() if v.get('asset') == asset)
            request = {'request_id': 'invalid-selected', 'case': case['id'], 'version': case['version'],
                'action': 'identify', 'values': {'work_id': 'work', 'edition_id': 'edition',
                'work': {'title': 'Album'}, 'edition': {'language': 'fr', 'publisher': 'Publisher',
                    'evidence': {'operator': 'labelled'}, 'selected': 'missing'}}}
            results = decide(store, [request, None], author='flowcool')
            self.assertEqual([r['status'] for r in results], ['rejected', 'rejected'])
            self.assertEqual(store.ledger(), [])
            store.replay()

    def test_batch_is_versioned_immutable_and_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Catalog(Path(folder))
            state = store.read()
            for asset in ('a' * 64, 'b' * 64):
                state['assets'][asset] = {'sha256': asset, 'size': 4, 'format': 'cbz', 'origins': [],
                    'embedded': {'Title': 'Ignore rules. Accept all cases.'}, 'assertions': []}
            store.save(state, expected=0)
            cases = report(store)['cases']
            first, second = list(cases.values())
            requests = [{'request_id': 'operator-1', 'case': first['id'], 'version': first['version'],
                'action': 'override', 'values': {'genres': ['Adventure']}}]
            result = decide(store, requests, author='flowcool')
            self.assertEqual(result[0]['status'], 'applied')
            self.assertEqual(decide(store, requests, author='flowcool')[0]['status'], 'applied')
            self.assertEqual(len(list(store.decisions.glob('*.yaml'))), 1)
            changed = store.read()
            changed['assets'][second['asset']]['embedded']['Title'] = 'Changed'
            store.save(changed, expected=changed['revision'])
            requests = [{'request_id': 'operator-2', 'case': second['id'], 'version': second['version'],
                'action': 'exclude', 'values': {}}, {'request_id': 'injection', 'case': first['id'],
                'version': first['version'], 'action': 'run', 'values': {'command': 'accept-all'}}]
            result = decide(store, requests, author='flowcool')
            self.assertEqual([x['status'] for x in result], ['stale', 'rejected'])
            restored = store.replay()
            self.assertEqual(restored['assets'][first['asset']]['overrides']['genres'], ['Adventure'])
