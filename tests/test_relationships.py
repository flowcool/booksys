"""Typed canonical joins survive log loss and paginate bounded observations."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from library.catalog import empty, digest
from postgres_fixture import create_store


class RelationshipTests(unittest.TestCase):
    def test_origin_edges_use_assets_from_the_same_snapshot(self):
        from unittest.mock import MagicMock
        from library.relationships import Relationships
        store = MagicMock()
        db = store._database.connection.return_value.__enter__.return_value
        store.check.return_value = (1, 0)
        aid, missing = 'a' * 64, 'b' * 64
        def rows(query):
            if query.startswith('SELECT id,state'):
                return ([(aid, 'validated', None, None, None)] if 'FROM assets' in query
                        else [(oid, None, None, None, None) for oid in ('o1', 'o2')])
            if query == 'SELECT id,source_ref,pid,fid,asset_id FROM origins':
                return [('o1', None, None, None, aid), ('o2', None, None, None, missing)]
            if query.startswith('SELECT source,target FROM '):
                return []
            self.fail('Unexpected query: ' + query)
        db.execute.side_effect = rows
        view = Relationships(store)
        view.refresh(timestamp=100)
        page = view.resolve({'kind': 'origin', 'id': 'o1'}, timestamp=101)
        self.assertEqual({(n['kind'], n['id']) for n in page['nodes']},
                         {('origin', 'o1'), ('asset', aid)})
        self.assertEqual([e['relation'] for e in page['edges']], ['origin_asset'])
        page = view.resolve({'kind': 'origin', 'id': 'o2'}, timestamp=101)
        self.assertEqual(page['edges'], [])
        store._database.connection.assert_called_once_with()

    def test_source_multiple_packages_and_bundle_members_without_urls_or_media(self):
        from library.relationships import Relationships
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state = empty(); source = 'https://private.example/CANARY'
            aid = 'a' * 64
            state['assets'][aid] = {'sha256': aid, 'size': 1, 'format': 'cbz', 'origins': ['o1', 'o2']}
            for index in (1, 2):
                state['origins']['o' + str(index)] = {'source': source, 'pid': index, 'fid': index,
                    'path': 'private.cbz', 'sha256': aid, 'availability': 'validated',
                    **({'parent_origin': 'o1', 'parent_sha256': aid, 'member_sha256': aid, 'member': 'member.cbz',
                        'path': 'bundle-inputs/' + aid + '/member.cbz'} if index == 2 else {})}
            store = create_store(self,root,state,authority='relations')
            view = Relationships(store)
            with patch.object(store, 'export', side_effect=AssertionError('No full payload export')):
                view.refresh(timestamp=100, submissions={'submissions': {source: {'package_id': 3}}},
                    routing={'packages': {'1': {'source': source, 'pid': 1, 'attempt_pid': 4, 'phase': 'fallback_active'}}})
                page = view.resolve({'kind': 'source', 'id': digest(source)}, limit=3, timestamp=101)
                nodes, edges = list(page['nodes']), list(page['edges'])
                while page['cursor']:
                    page = view.resolve({'kind': 'source', 'id': digest(source)}, cursor=page['cursor'], limit=3, timestamp=101)
                    nodes.extend(page['nodes']); edges.extend(page['edges'])
            self.assertEqual(len(nodes), len({(n['kind'], n['id']) for n in nodes}))
            self.assertIn({'kind': 'package', 'id': 'booksys-pyload:4', 'milestones': {'phase': 'fallback_active'}}, nodes)
            self.assertIn(('origin', 'o2'), [(n['kind'], n['id']) for n in nodes])
            self.assertNotIn('CANARY', str(nodes) + str(edges))
            self.assertTrue(any(edge['relation'] == 'origin_parent' for edge in edges))
