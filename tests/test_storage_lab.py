"""A busy business boundary must prevent every service stop."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from library.primitives import lock, LibraryError
from ops import storage_lab


class LabStorageTests(unittest.TestCase):
    def test_busy_cycle_refuses_quiesce_before_service_or_collector_stop(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'lab';collector=Path(folder)/'collector'
            (root/'state').mkdir(parents=True);collector.mkdir()
            observed={'booksys-worker-1':{'id':'owned-worker','running':True}}
            with patch.object(storage_lab,'ROOT',root), patch.object(storage_lab,'COLLECTOR',collector), \
                    patch.object(storage_lab,'targets',return_value=observed), \
                    patch.object(storage_lab,'run') as run, \
                    patch('sys.argv',['storage-lab','quiesce','--authority','fixture','--evidence',str(root/'proof'),
                                      '--expected-worker','owned-worker']), \
                    lock(root/'state/library-cycle.lock'):
                with self.assertRaises(LibraryError):storage_lab.main()
                run.assert_not_called()
            self.assertFalse((root/'proof').exists())
