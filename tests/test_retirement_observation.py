"""Retirement observations cannot parse the evidence graph in the API process."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from activity import write_yaml
from postgres_fixture import create_store, create_activity
from library.primitives import empty
from library.operational_state import NativeHistory


class RetirementObservationTests(unittest.TestCase):
    def test_owned_child_returns_only_relationship_and_activity_fields(self):
        from library.retirement_observation import observe
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'package-retirement.yaml'
            create_store(self,Path(folder),empty())
            NativeHistory(Path(folder)).retire('source', {'phase': 'deleting',
                'private': 'CANARY', 'packages': {'280': {'pid': 280,
                    'name': 'CANARY', 'links': [{'fid': 41, 'url': 'CANARY'}, [42, 'CANARY', 'name', 0]]}}})
            with patch('library.views.read_yaml', side_effect=AssertionError('No parent YAML parse')):
                result = observe(path)
            self.assertEqual(result, {'acquisitions': {'source': {'phase': 'deleting',
                'packages': {'280': {'pid': 280, 'links': [[41], [42]]}}}}})
            self.assertNotIn('CANARY', json.dumps(result))

    def test_missing_corrupt_or_failed_child_is_unavailable_not_empty_history(self):
        from library.retirement_observation import observe
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'package-retirement.yaml'
            self.assertEqual(observe(path),{})
            create_store(self,Path(folder),empty())
            self.assertEqual(observe(path),{'acquisitions':{}})
            config=Path(folder)/'postgresql.json'
            saved=config.read_bytes()
            config.write_text('{invalid')
            self.assertEqual(observe(path),{})
            config.write_bytes(saved)
            with patch('library.retirement_observation.run_child',side_effect=TimeoutError):
                self.assertEqual(observe(path),{})

    def test_empty_unknown_and_unsafe_journals_remain_distinct(self):
        from library.retirement_observation import observe
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);create_store(self,root,empty())
            path=root/'package-retirement.yaml'
            self.assertEqual(observe(path),{'acquisitions':{}})
            NativeHistory(root).retire('source',{'phase':'unknown','packages':{}})
            self.assertEqual(observe(path)['acquisitions']['source']['phase'],'unknown')
            # Retired file contents are no longer authority. They cannot hide
            # canonical PostgreSQL history or supply attacker-controlled paths.
            path.write_text('acquisitions: [invalid')
            self.assertEqual(observe(path)['acquisitions']['source']['phase'],'unknown')
            link=root/'link.yaml';link.symlink_to(path)
            self.assertEqual(observe(link)['acquisitions']['source']['phase'],'unknown')
            with path.open('wb') as stream:stream.truncate(32*1024**2+1)
            self.assertEqual(observe(path)['acquisitions']['source']['phase'],'unknown')

    def test_activity_uses_supplied_observation_without_reloading_journal(self):
        from library.activity import Observer, read_yaml
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store=create_store(self,root/'state',empty())
            create_store(self,root/'collector',empty())
            from library.operational_state import StatusObservations
            StatusObservations(store._database).save_retirement({'results': []})
            create_activity(self,root/'evidence/activity',authority='test',timestamp=1000)
            create_activity(self,root/'collector/activity',authority='test',timestamp=1000)
            write_yaml(root/'evidence/retirement-cycle.yaml', {'results': []})
            observer = Observer(root, root/'collector', clock=lambda: 1000)
            observed = {'acquisitions': {'source': {'phase': 'retired', 'packages': {}}}}
            def guarded(path, **kwargs):
                self.assertNotEqual(path.name, 'package-retirement.yaml')
                return read_yaml(path, **kwargs)
            with patch('library.activity.read_yaml', side_effect=guarded):
                snapshot = observer.snapshot(retirement=observed)
                self.assertEqual(snapshot['stages']['retirement']['counts']['removed acquisitions'], 1)
                observer.saved = snapshot
                unavailable = observer.snapshot(retirement={})
                stage = unavailable['stages']['retirement']
                self.assertEqual(stage['counts']['removed acquisitions'], 1)
                self.assertEqual(stage['queue_freshness'], 'unavailable')
