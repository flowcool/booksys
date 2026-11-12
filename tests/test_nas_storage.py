"""Shared NFS exports count each device once."""
import unittest


class NASStorageTests(unittest.TestCase):
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

