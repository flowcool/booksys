"""Indexed operator rows exclude payload media and change with their owners."""
from pathlib import Path
import tempfile
import unittest
from library.catalog import empty
from postgres_fixture import create_store
from library.store import Change


class RowTests(unittest.TestCase):
    def test_group_pagination_filter_and_affected_rebuild(self):
        with tempfile.TemporaryDirectory() as folder:
            state = empty()
            asset = 'a' * 64
            state['assets'][asset] = {'sha256': asset, 'format': 'cbz', 'size': 5,
                                     'origins': [], 'captured_hints': {'hint': {'title': 'Album'}}}
            for index in range(61):
                key = 'o' + str(index).zfill(3)
                source = 'https://bookys.invalid/source-' + str(index // 2)
                state['origins'][key] = {'source': source, 'pid': index, 'fid': index,
                    'path': key + '.cbz', 'sha256': asset, 'availability': 'validated'}
                state['assets'][asset]['origins'].append(key)
            store = create_store(self,Path(folder),state,authority='rows')
            page = store.operator_page(limit=10, search='album')
            self.assertEqual(len(page['groups']), 10)
            self.assertEqual(page['counts']['sources'], 31)
            self.assertEqual(sum(len(g['rows']) for g in page['groups']), 20)
            self.assertNotIn('cover', str(page))
            second = store.operator_page(limit=10, search='album', cursor=page['cursor'])
            self.assertTrue(set(g['id'] for g in page['groups']).isdisjoint(g['id'] for g in second['groups']))
            current = store.get('assets', asset)
            current['captured_hints']['hint']['title'] = 'Changed'
            store.commit(Change(upserts={'assets': {asset: current}}), expected_revision=0)
            self.assertEqual(store.operator_page(search='album')['groups'], [])
            self.assertEqual(store.operator_page(search='changed')['counts']['sources'], 31)
