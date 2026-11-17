"""Startup accepts code changes while retaining the selected authority."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml


class StartupCodeDigestTests(unittest.TestCase):
    def test_verify_activation_accepts_different_code_digest(self):
        from library.stage_worker import verify_activation
        value = {'schema': 1, 'code_digest': 'previous-image', 'mode': 'independent',
                 'supervisor': 'compose', 'qualification_receipt': 'qualified',
                 'cgroup_pool': '/sys/fs/cgroup/booksys', 'io_device': '8:0'}
        files = {'/proc/self/cgroup': '0::/booksys/worker\n',
                 '/sys/fs/cgroup/booksys/memory.max': str(4608 * 1024**2),
                 '/sys/fs/cgroup/booksys/cpu.max': '200000 100000'}
        with patch('library.postgres.Database.configured'), \
                patch('library.operational_state.RuntimeObservation.activation', return_value=value), \
                patch.object(Path, 'is_symlink', return_value=False), \
                patch.object(Path, 'read_text', lambda path: files[str(path)]):
            self.assertEqual(verify_activation(Path('/unused')), value)

    @unittest.skipUnless(os.environ.get('BOOKSYS_TEST_PG_DSN_FILE'), 'isolated PostgreSQL required')
    def test_read_manifest_accepts_different_code_digest(self):
        from library.authority import read_manifest
        from library.primitives import atomic_yaml, empty
        from postgres_fixture import create_store
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            create_store(self, root, empty())
            manifest = root / 'storage-authority.yaml'
            value = yaml.safe_load(manifest.read_text())
            value['code_sha256'] = 'previous-image'
            atomic_yaml(manifest, value)
            manifest.chmod(0o400)
            self.assertEqual(read_manifest(root), value)
            self.assertEqual(read_manifest(root, expected_code='current-image'), value)
