"""Child events arrive before completion; raw stderr never becomes diagnostics."""
import json
import sys
import threading
import time
import unittest
from booksys_core.diagnostics import event
from booksys_core.process import run_child


class StreamingTests(unittest.TestCase):
    def test_immediate_event_and_machine_result(self):
        first = threading.Event()
        completed = threading.Event()
        value = event(service='library', stage='local', code='started', reason='none')
        script = 'import sys,time; print(' + repr(json.dumps(value)) + ',file=sys.stderr,flush=True); time.sleep(.5); print(' + repr('{"status":"ok"}') + ')'
        seen = []
        def receive(item):
            seen.append(item)
            first.set()
        result = []
        def run():
            result.append(run_child([sys.executable, '-c', script], on_event=receive, timeout=3))
            completed.set()
        worker = threading.Thread(target=run); worker.start()
        self.assertTrue(first.wait(2))
        self.assertFalse(completed.is_set())
        worker.join(3)
        self.assertEqual(seen, [value])
        self.assertEqual(json.loads(result[0].stdout), {'status': 'ok'})

    def test_raw_stderr_is_discarded_and_stdout_overflow_fails(self):
        result = run_child([sys.executable, '-c', 'import sys; print("password=CANARY",file=sys.stderr); print("{}")'], on_event=lambda e: None, timeout=3)
        self.assertEqual(result.rejected, 1)
        self.assertNotIn('CANARY', str(result))
        with self.assertRaisesRegex(ValueError, 'stdout'):
            run_child([sys.executable, '-c', 'print("x" * 1048577)'], on_event=lambda e: None, timeout=3)
