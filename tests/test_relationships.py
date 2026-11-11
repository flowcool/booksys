"""Typed canonical joins survive log loss and paginate bounded observations."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from library.catalog import empty, digest
from postgres_fixture import create_store


class RelationshipTests(unittest.TestCase):
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
