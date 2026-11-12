"""pyLoad reuses a deleted package's id. A reused id must not freeze the pipeline."""
import unittest
from pathlib import Path
from unittest.mock import patch

from booksys_core.errors import CollectionError
from retirement_history import retained_snapshot, same_package


def package(pid, name, fid=1, status=0):
    return {'pid': pid, 'name': name, 'folder': name, 'links': [{'fid': fid, 'url': 'https://host.test/' + name, 'name': 'f.cbz', 'status': status}]}


def history(phase, retained):
    record = {'phase': phase, 'packages': {str(retained['pid']): retained}, 'created_at': '2026-11-12T00:00:00+00:00',
              'origins': {}, 'files': {}}
    return {'acquisitions': {'https://www.bookys-ebooks.com/x': record}}


class ReusedIdTests(unittest.TestCase):
    def snapshot(self, phase, retained, live):
        with patch('retirement_history.read_history', return_value=history(phase, retained)):
            return retained_snapshot([live], Path('/unused/retirement.yaml'))

    def test_new_package_with_the_id_of_a_retired_one_is_not_an_error(self):
        result = self.snapshot('retired', package(834, 'Booksys-old'), package(834, 'Booksys-new', fid=9))
        self.assertEqual([p['name'] for p in result], ['Booksys-new'])

    def test_new_package_with_the_id_of_a_deleting_one_is_not_an_error(self):
        result = self.snapshot('deleting', package(834, 'Booksys-old'), package(834, 'Booksys-new', fid=9))
        self.assertEqual([p['name'] for p in result], ['Booksys-new'])

    def test_the_retired_package_itself_reappearing_still_stops(self):
        same = package(834, 'Booksys-old')
        with self.assertRaisesRegex(CollectionError, 'reappeared'):
            self.snapshot('retired', same, package(834, 'Booksys-old'))

    def test_same_package_with_changed_state_still_stops_a_deleting_record(self):
        with self.assertRaisesRegex(CollectionError, 'target changed'):
            self.snapshot('deleting', package(834, 'Booksys-old', status=0), package(834, 'Booksys-old', status=12))

    def test_same_package_compares_name_and_links_not_status(self):
        self.assertTrue(same_package(package(1, 'a', status=0), package(1, 'a', status=12)))
        self.assertFalse(same_package(package(1, 'a'), package(1, 'b')))
        self.assertFalse(same_package(package(1, 'a', fid=1), package(1, 'a', fid=2)))


if __name__ == '__main__':
    unittest.main()


class SnapshotFailureLevelTests(unittest.TestCase):
    def test_failure_becomes_an_error_once_the_last_snapshot_is_old(self):
        from library.stage_worker import snapshot_failure_code
        with patch('library.operational_state.RuntimeObservation') as observation:
            observation.return_value.capacity.return_value = {'observed_at': 1000.0}
            self.assertEqual(snapshot_failure_code(object(), now=1100.0), 'degraded')
            self.assertEqual(snapshot_failure_code(object(), now=1400.0), 'failed')
            observation.return_value.capacity.side_effect = KeyError('missing')
            self.assertEqual(snapshot_failure_code(object(), now=1100.0), 'failed')
