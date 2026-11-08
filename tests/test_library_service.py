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
            root = Path(folder);setup_library(root)
            with patch('sys.argv', ['service', '--root', str(root), '--credentials', str(root/'secret'),
                                   '--listen', '127.0.0.1', '--port', '0']), \
                 patch.object(module.subprocess, 'run', side_effect=OSError('fixture launch failure')) as launch:
                with self.assertRaisesRegex(RuntimeError, 'cycle thread failed'):
                    module.main()
            self.assertIn('--route-mirrors', launch.call_args.args[0])
            self.assertEqual(module.read_yaml(root/'evidence/cycle.yaml')['status'], 'blocked')
