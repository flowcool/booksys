import os
from pathlib import Path
import sys
import tempfile
import unittest

from library.process import bounded_run


class NativeSandboxTests(unittest.TestCase):
    def test_only_input_and_declared_runtime_are_visible_to_native_worker(self):
        from library.sandbox import native_prefix, PYTHON
        with tempfile.TemporaryDirectory(prefix='booksys-private-sandbox-') as folder:
            root = Path(folder)
            private = root / 'controller-secret'
            private.write_text('controller-private-sentinel')
            private.chmod(0o600)
            sibling = root / 'other-input'
            sibling.write_text('other-unexposed-input')
            source = root / 'input.cbz'
            source.write_text('the-only-input')
            code = ('import os,pathlib; '
                    'assert pathlib.Path("/tmp/input").read_text()=="the-only-input"; '
                    f'assert not pathlib.Path({str(private)!r}).exists(); '
                    f'assert not pathlib.Path({str(sibling)!r}).exists(); '
                    'assert "PRIVATE_CONTROLLER_TOKEN" not in os.environ; '
                    'assert not pathlib.Path("/home/operator").exists(); '
                    'assert not pathlib.Path("/data/booksys/state").exists(); '
                    'assert not pathlib.Path("/var/run/docker.sock").exists(); '
                    f'assert not pathlib.Path("/proc/1/root"+{str(private)!r}).exists(); '
                    'print("input-only; controller paths absent")')
            from unittest.mock import patch
            with patch.dict(os.environ, {'PRIVATE_CONTROLLER_TOKEN': 'must-not-leak'}):
                result = bounded_run(native_prefix(source) + [PYTHON, '-c', code], timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertIn(b'input-only', result.stdout)

    def test_native_transform_can_write_only_the_declared_output(self):
        from library.sandbox import native_prefix, PYTHON
        with tempfile.TemporaryDirectory(prefix='booksys-native-output-') as folder:
            root = Path(folder)
            source = root / 'input.cbz'
            source.write_text('immutable input')
            output = root / 'owned-staging'
            output.mkdir()
            code = ('from pathlib import Path\n'
                    'Path("/tmp/output/result").write_text("owned output")\n'
                    'for p in [Path("/tmp/input"),Path("/app/library/catalog.py")]:\n'
                    ' try: p.write_text("unsafe overwrite")\n'
                    ' except OSError: pass\n'
                    ' else: raise RuntimeError("Read-only grant became writable")\n')
            result = bounded_run(native_prefix(source, output=output) + [PYTHON, '-c', code], timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual((output / 'result').read_text(), 'owned output')
            self.assertEqual(source.read_text(), 'immutable input')
