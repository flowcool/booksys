"""Decoder capacity stays reserved until exact named-container termination."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from library.decoder_ownership import DecoderOwnership
from library.process import bounded_run

class DecoderOwnershipTests(unittest.TestCase):
    def initialize(self,directory):
        from tests.postgres_fixture import create_store
        from library.primitives import empty
        return create_store(self,Path(directory),empty())

    def test_owned_name_is_retained_before_launch_and_cleanup_checks_exact_container(self):
        with tempfile.TemporaryDirectory() as directory:
            self.initialize(directory)
            owner = DecoderOwnership.create(Path(directory)/'children.yaml')
            with owner.track():
                command = owner.prepare(['docker', 'run', '--rm', 'fixture'])
            name = command[command.index('--name')+1]
            recovered = DecoderOwnership(owner.path)
            seen = []
            def invoke(cmd, **kwargs):
                seen.append(cmd)
                if cmd[1] == 'inspect':
                    return subprocess.CompletedProcess(cmd, 0, b'false\n', b'')
                return subprocess.CompletedProcess(cmd, 0, b'', b'')
            with patch('library.decoder_ownership.subprocess.run', side_effect=invoke):
                self.assertTrue(recovered.cleanup())
            self.assertEqual(seen[0], ['docker', 'stop', '--time', '1', name])
            self.assertEqual(seen[1][-1], name)

    def test_unproved_container_exit_keeps_registration_and_refuses_release(self):
        with tempfile.TemporaryDirectory() as directory:
            self.initialize(directory)
            owner = DecoderOwnership.create(Path(directory)/'children.yaml')
            owner.prepare(['docker', 'run', '--rm', 'fixture'])
            with patch('library.decoder_ownership.subprocess.run', return_value=subprocess.CompletedProcess([], 0, b'true\n', b'')):
                self.assertFalse(owner.cleanup())
            self.assertEqual(len(DecoderOwnership(owner.path).names()),1)

    def test_qualified_decoder_joins_shared_pool_and_other_parent_is_rejected(self):
        from library.primitives import LibraryError
        with tempfile.TemporaryDirectory() as directory:
            self.initialize(directory)
            owner = DecoderOwnership.create(Path(directory)/'children.yaml')
            with patch.dict('os.environ', {'BOOKSYS_DECODER_CGROUP_PARENT': '/qualified/booksys'}):
                command = owner.prepare(['docker','run','--rm','fixture'])
                self.assertEqual(command[command.index('--cgroup-parent')+1],'/qualified/booksys')
                with self.assertRaises(LibraryError):
                    owner.prepare(['docker','run','--cgroup-parent','/other','fixture'])
