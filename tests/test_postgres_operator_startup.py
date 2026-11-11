"""Offline startup of the Compose controller with its selected PostgreSQL authority."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from library.primitives import empty
from ops import check_activity, library_service
from postgres_fixture import create_store


class OperatorStartupTests(unittest.TestCase):
    def test_controller_starts_with_postgresql_without_collector_selector(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            create_store(self, root / 'state', empty())
            collector = root / 'collector'
            collector.mkdir()
            self.assertFalse((collector / 'postgresql.json').exists())
            argv = ['library_service', '--root', str(root), '--collector', str(collector),
                    '--credentials', str(root / 'unused.json'), '--independent-queues',
                    '--ipc-socket', str(root / 'controller.sock'), '--public-host', 'localhost']
            server = MagicMock()
            server.check.side_effect = RuntimeError('stop after first server check')
            with patch.dict('os.environ'), patch('sys.argv', argv), patch('library.stage_worker.verify_activation'), \
                    patch('library.compose_transport.UnixController', return_value=server), \
                    patch('ops.library_service.threading.Thread'), patch('ops.library_service.signal.signal'), \
                    patch('ops.library_service.build_projection', return_value={}):
                with self.assertRaisesRegex(RuntimeError, 'stop after first server check'):
                    library_service.main()
            server.start.assert_called_once()
            server.close.assert_called_once()

    def test_activity_proof_refuses_unbound_container_observation(self):
        ident = '1' * 64
        with patch('ops.check_activity.subprocess.run') as inspect:
            with self.assertRaisesRegex(ValueError, 'full Docker container IDs'):
                check_activity.container_observations(['books-collection'])
            inspect.assert_not_called()
            inspect.return_value.stdout = '/collector ' + ('2' * 64) + ' sha256:image true\n'
            with self.assertRaisesRegex(RuntimeError, 'requested container IDs'):
                check_activity.container_observations([ident])
            self.assertEqual(inspect.call_args.args[0][-1], ident)
            inspect.return_value.stdout = '/collector ' + ident + ' sha256:image true\n'
            self.assertEqual(check_activity.container_observations([ident]), [inspect.return_value.stdout.strip()])
