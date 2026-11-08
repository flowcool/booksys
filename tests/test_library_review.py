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

    def test_source_hint_discovers_reviewable_editions_without_embedded_identity(self):
        import json
        from urllib.parse import urlsplit
        from library.metadata import Providers, enrich
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store, reception, _, asset = setup_library(root)
            state = store.read()
            state['works'], state['editions'], state['variants'] = {}, {}, {}
            state['assets'][asset].pop('identity')
            state['assets'][asset]['captured_hints'] = {'https://bookys-ebooks.com/item': {'title': 'One Piece'}}
            store.save(state, expected=state['revision'])
            calls = []
            def fetch(url):
                calls.append(url)
                path = urlsplit(url).path
                if path == '/search.json':
                    return 200, json.dumps({'docs': [{'title': 'One Piece', 'edition_key': ['OL8692999M']}]})
                if path == '/books/OL8692999M.json':
                    return 200, json.dumps({'type': {'key': '/type/edition'}, 'key': '/books/OL8692999M',
                        'title': 'One Piece, Vol. 1', 'authors': [{'key': '/authors/OL123A'}],
                        'languages': [{'key': '/languages/eng'}], 'publishers': ['SHONEN JUMP'],
                        'publish_date': '2003', 'edition_name': '1 edition',
                        'works': [{'key': '/works/OL8215661W'}]})
                if path == '/authors/OL123A.json':
                    return 200, json.dumps({'name': 'Eiichiro Oda'})
                self.fail('Unexpected provider request: ' + url)
            providers = Providers(root / 'cache', fetch=fetch, interval=0, budget=4)
            state = enrich(store, asset, providers, [])
            self.assertNotIn('identity', state['assets'][asset])
            case = next(v for v in report(store)['cases'].values() if v.get('asset') == asset)
            candidate = case['candidates'][0]
            self.assertEqual(candidate['edition_ref'], '/books/OL8692999M')
            self.assertEqual(len(calls), 3)
            request = {'request_id': 'review-discovered', 'case': case['id'], 'version': case['version'],
                'action': 'identify', 'values': {'work_id': 'review-work', 'edition_id': 'review-edition',
                'work': {k: candidate[k] for k in ('title', 'author', 'number')},
                'edition': {**{k: candidate[k] for k in ('language', 'publisher', 'date', 'revision')},
                    'evidence': {'operator': 'flowcool', 'source': candidate['url'],
                                 'basis': 'fixture operator checked source against title page'}}}}
            self.assertEqual(decide(store, [request], author='flowcool')[0]['status'], 'applied')
            case = next(v for v in report(store)['cases'].values() if v.get('asset') == asset)
            self.assertEqual(decide(store, [{'request_id': 'review-select', 'case': case['id'],
                'version': case['version'], 'action': 'select', 'values': {}}], author='flowcool')[0]['status'], 'applied')
            state = enrich(store, asset, providers, [])
            self.assertFalse(state['assets'][asset].get('contested', False))
            from library.review import render_report
            rendered = render_report(report(store))
            for text in ('One Piece, Vol. 1', 'Eiichiro Oda', 'SHONEN JUMP', '2003', '/books/OL8692999M'):
                self.assertIn(text, rendered)
            Publisher(store, reception, root / 'library', isolated=True, capacity=Capacity(reserve=0)).publish(asset)
            class ChangedEdition(Providers):
                def edition(self, reference):
                    result = super().edition(reference)
                    if result['status'] == 'ok':
                        result['candidate']['publisher'] = 'Conflicting publisher'
                    return result
            changed = ChangedEdition(root / 'cache', fetch=fetch, interval=0)
            self.assertTrue(enrich(store, asset, changed, [])['assets'][asset]['contested'])

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
