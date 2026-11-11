"""Versioned supervisor output and task enclosing deadlines, without unit activation."""
from pathlib import Path
import signal
import tempfile
import time
import unittest

class StageSupervisorTests(unittest.TestCase):
    def test_enclosing_deadline_stops_a_stuck_step(self):
        from library.stage_supervisor import deadline
        from library.primitives import LibraryError
        before = signal.getsignal(signal.SIGALRM)
        with self.assertRaisesRegex(LibraryError, 'deadline'):
            with deadline(.02):
                time.sleep(1)
        self.assertEqual(signal.getsignal(signal.SIGALRM), before)

    def test_generated_units_share_limits_and_retain_process_ownership(self):
        from library.stage_supervisor import units
        output = units(code=Path('/data/code/booksys'), python=Path('/data/code/booksys/.venv/bin/python'),
            root=Path('/data/isolated/library'), collector=Path('/data/isolated/collector'),
            reception=Path('/data/isolated/reception'), credentials=Path('/data/isolated/secret.json'))
        self.assertEqual(len([k for k in output if k.endswith('.service')]), 9)
        service = output['booksys-stage-copies.service']
        for text in ('KillMode=control-group','Delegate=no','WatchdogSec=15','RestartSec=5','Slice=booksys.slice'):
            self.assertIn(text,service)
        self.assertIn('CPUQuota=200%',output['booksys.slice'])
        self.assertIn('MemoryMax=3584M',output['booksys.slice'])
        self.assertIn('User=', service)
        self.assertIn('WantedBy=multi-user.target', service)
        self.assertNotIn('library_cycle',service)
        self.assertNotIn('process_assets',service)

    def test_native_history_memory_is_reserved_in_independent_and_serial_modes(self):
        from library.stage_supervisor import units
        from library.stage_runtime import demand
        from library.stage_model import Obligation, Task
        output = units(code=Path('/data/code'), python=Path('/data/code/.venv/bin/python'),
            root=Path('/data/lab'), collector=Path('/data/collector'),
            reception=Path('/data/reception'), credentials=Path('/data/secret.json'))
        # Actual retained history needs332MiB with the safe C parser. Both
        # dispatch modes must admit that working set within the same task pool.
        for stage in ('collection', 'routing', 'retirement'):
            obligation = Obligation(stage, 'source', 'revision', 'method')
            task = Task(obligation.key, stage, 'source', 'revision', 'method', None,
                        '2026-11-09T00:00:00+00:00')
            self.assertGreaterEqual(demand(task).memory_mib, 512)
            self.assertIn('MemoryMax=512M', output['booksys-stage-'+stage+'.service'])
        self.assertIn('MemoryMax=512M', output['booksys-stage-serial.service'])

    def test_io_ceiling_checks_all_descendants_and_refuses_unknown_counters(self):
        from library.stage_supervisor import io_ceiling
        from library.primitives import LibraryError
        with tempfile.TemporaryDirectory() as directory:
            pool = Path(directory)
            (pool/'io.stat').write_text('8:0 rbytes=10 wbytes=20 rios=1 wios=1\n')
            with self.assertRaisesRegex(LibraryError, 'I/O ceiling'):
                with io_ceiling(pool, '8:0', 100):
                    (pool/'io.stat').write_text('8:0 rbytes=111 wbytes=20 rios=2 wios=1\n')
            (pool/'io.stat').write_text('8:0 invalid\n')
            with self.assertRaises(LibraryError):
                with io_ceiling(pool, '8:0', 100):
                    pass
