import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_library_publish import setup_library


class ServiceTests(unittest.TestCase):
    def test_cycle_thread_failure_stops_the_service_instead_of_silent_dead_scheduler(self):
        spec = importlib.util.spec_from_file_location('service_test', 'ops/library_service.py')
        module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder);setup_library(root,self)
            from library.operational_state import StatusObservations
            from library.postgres import Database
            observations=StatusObservations(Database.configured(root/'state'))
            observations.save_cycle( {'status': 'blocked', 'reason': 'old error',
                'finished_at': 'old finish', 'error_source': 'old stage'})
            def launch_failure(*args, **kwargs):
                value = observations.cycle()
                self.assertEqual(value['stage'], 'starting')
                self.assertNotIn('reason', value)
                self.assertNotIn('finished_at', value)
                self.assertNotIn('error_source', value)
                raise OSError('fixture launch failure')
            with patch.dict('os.environ'), patch('sys.argv', ['service', '--root', str(root), '--credentials', str(root/'secret'),
                                   '--listen', '127.0.0.1', '--port', '0']), \
                 patch.object(module, 'run_child', side_effect=launch_failure) as launch, \
                 patch('library.log_sources.LiveSource.refresh'):
                with self.assertRaisesRegex(RuntimeError, 'cycle thread failed'):
                    module.main()
            self.assertIn('--route-mirrors', launch.call_args.args[0])
            self.assertEqual(observations.cycle()['status'], 'blocked')
