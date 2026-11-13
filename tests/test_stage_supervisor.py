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
