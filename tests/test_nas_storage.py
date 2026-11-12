"""Mount admission must reject an empty local substitute and changed export."""
from pathlib import Path
import unittest
import tempfile
import subprocess
import sys
import fcntl

from library.nas_storage import MountIdentity, verify_mounts
from library.primitives import LibraryError


class NASStorageTests(unittest.TestCase):
    def test_old_probe_lock_refuses_new_probe_before_any_nas_access(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with (root/'probe.lock').open('a') as old:
                fcntl.flock(old, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = subprocess.run([sys.executable, '-m', 'library.nas_storage',
                    str(root/'absent-contract.json'), '--owner-lock', str(root/'probe.lock')],
                    capture_output=True, timeout=3)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b'BlockingIOError', result.stderr)
            self.assertNotIn(b'FileNotFoundError', result.stderr)

    def test_shared_export_superblock_is_counted_once_and_distinct_devices_add(self):
        from library.stage_supervisor import nfs_counters
        mounts = ('42 31 0:51 / /files rw - nfs nas:/files rw,hard\n'
                  '43 31 0:51 / /library rw - nfs nas:/library rw,hard\n')
        stats = ('device nas:/files mounted on /files with fstype nfs statvers=1.1\n'
                 'bytes: 10 20 30 40 100 200 0 0\n'
                 'device nas:/library mounted on /library with fstype nfs statvers=1.1\n'
                 'bytes: 10 20 30 40 100 200 0 0\n')
        self.assertEqual(nfs_counters(mounts, stats, {'nas:/files', 'nas:/library'}), 100)
        self.assertEqual(nfs_counters(mounts.replace('43 31 0:51', '43 31 0:52'), stats,
                                     {'nas:/files', 'nas:/library'}), 200)

    def test_exact_export_root_and_readonly_contract_are_required(self):
        expected = MountIdentity(Path('/downloads'), 'nas:/volume2/storage/booksys', '/downloads', True)
        correct = '42 31 0:51 /downloads /downloads ro,nosuid - nfs nas:/volume2/storage/booksys rw,vers=3,hard\n'
        verify_mounts([expected], correct)
        for changed in (correct.replace(' - nfs ', ' - ext4 '),
                        correct.replace('nas:/volume2/storage/booksys', 'nas:/wrong'),
                        correct.replace(' /downloads /downloads ', ' /other /downloads '),
                        correct.replace(' ro,nosuid ', ' rw,nosuid '), ''):
            with self.subTest(changed=changed), self.assertRaises(LibraryError):
                verify_mounts([expected], changed)

    def test_parent_mount_does_not_prove_the_declared_child_bind(self):
        with self.assertRaises(LibraryError):
            verify_mounts([MountIdentity(Path('/downloads'), 'nas:/files', '/downloads', True)],
                          '42 31 0:51 / / ro - nfs nas:/files rw,vers=3,hard\n')
