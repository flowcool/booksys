"""The deployed web transport preserves typed calls and never replays decisions."""
from pathlib import Path
import tempfile
import time
import unittest

from library.compose_transport import UnixController, ReconnectingRemote
from library.ipc import IPCError


class ComposeTransportTests(unittest.TestCase):
    def test_web_reconnects_for_later_requests_without_replaying_confirmation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'controller.sock'
            calls = []
            def invoke(action, value):
                calls.append(action)
                return {'ok': action}
            remote = ReconnectingRemote(path)
            with self.assertRaises(IPCError):
                remote.call('confirm', {})
            with UnixController(path, invoke):
                self.assertEqual(remote.call('token', {}), {'ok': 'token'})
            with self.assertRaises(IPCError):
                remote.call('confirm', {})
            with UnixController(path, invoke):
                # A failed call can observe the disconnect after the new listener
                # exists. It still must not retry that call.
                for _ in range(10):
                    try:
                        result = remote.call('token', {})
                        break
                    except IPCError:
                        time.sleep(.02)
                self.assertEqual(result, {'ok': 'token'})
            remote.close()
            self.assertNotIn('confirm', calls)

    def test_second_controller_cannot_replace_owned_socket(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'controller.sock'
            with UnixController(path, lambda a, v: {}):
                with self.assertRaises((OSError, ValueError)):
                    with UnixController(path, lambda a, v: {}):
                        self.fail('Duplicate controller started')

    def test_envelope_validation_still_precedes_dispatch(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'controller.sock'
            def validate(action, value):
                if value != {'allowed': True}:
                    raise ValueError('Denied')
            with UnixController(path, lambda a, v: {'ok': True}, validate=validate):
                remote = ReconnectingRemote(path)
                try:
                    with self.assertRaises(IPCError):
                        remote.call('confirm', {'command': 'docker'})
                    self.assertEqual(remote.call('token', {'allowed': True}), {'ok': True})
                finally:
                    remote.close()
