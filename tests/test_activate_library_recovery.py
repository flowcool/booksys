"""Exercise activation's public CLI with isolated units and OS command adapters."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ops import activate_library


class RecoveryActivationTests(unittest.TestCase):
    def test_missing_reader_recovery_changes_only_image(self) -> None:
        self.exercise('missing', recover=True, expected_error=None)

    def test_recovery_refuses_existing_reader(self) -> None:
        self.exercise('present', recover=True, expected_error=RuntimeError)

    def test_daemon_failure_is_not_missing_image(self) -> None:
        self.exercise('daemon_error', recover=True, expected_error=subprocess.CalledProcessError)

    def test_ordinary_activation_still_requires_previous_image(self) -> None:
        self.exercise('missing', recover=False, expected_error=subprocess.CalledProcessError)

    def exercise(self, previous_image: str, *, recover: bool,
                 expected_error: type[Exception] | None) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unit = root / 'installed.service'
            original = ('[Service]\nEnvironment=BOOKSYS_READER_IMAGE=sha256:old\n'
                        'ExecStart=/owned/controller --activity --retire-packages\n'
                        'Restart=on-failure\n')
            unit.write_text(original)
            evidence = root / 'proof'
            containers = [{'Name': name, 'Id': name + '-id'}
                          for name in ('booksys-pyload', 'booksys-worker-1', 'booksys-byparr-1')]

            def command(*args: str) -> str:
                if args == ('hostname',) or args[:2] == ('docker', 'info'):
                    return 'booksys-host'
                if args == ('docker', 'image', 'inspect', 'sha256:old'):
                    if previous_image != 'present':
                        message = ('No such image: sha256:old' if previous_image == 'missing'
                                   else 'Cannot connect to the Docker daemon')
                        raise subprocess.CalledProcessError(1, args, stderr=message)
                    return json.dumps([{'Id': 'sha256:old'}])
                if args == ('docker', 'image', 'inspect', 'qualified-reader'):
                    return json.dumps([{'Id': 'sha256:new'}])
                if args[:2] == ('docker', 'inspect'):
                    return json.dumps(containers)
                if args[0] == 'git':
                    return '' if 'status' in args else 'committed-candidate'
                if args == ('systemctl', '--user', 'is-active', 'booksys-library.service'):
                    return 'active'
                if args[:2] == ('systemctl', '--user'):
                    return ''
                raise AssertionError('Unexpected OS command: ' + repr(args))

            arguments = ['activate_library.py', '--image', 'qualified-reader',
                         '--expected-unit-sha256', hashlib.sha256(unit.read_bytes()).hexdigest(),
                         '--evidence', str(evidence), '--apply']
            if recover:
                arguments.append('--recover-missing-reader')
            with patch.object(activate_library, 'ROOT', root), \
                    patch.object(activate_library, 'UNIT', unit), \
                    patch.object(activate_library, 'run', side_effect=command), \
                    patch('ops.activate_library.subprocess.run',
                          return_value=subprocess.CompletedProcess([], 3, stdout='inactive')), \
                    patch('sys.argv', arguments), patch('builtins.print'):
                if expected_error is not None:
                    with self.assertRaises(expected_error):
                        activate_library.main()
                    self.assertEqual(unit.read_text(), original)
                    self.assertFalse(evidence.exists())
                    return
                activate_library.main()
            self.assertEqual(unit.read_text(), original.replace('sha256:old', 'sha256:new'))
            self.assertEqual((evidence / 'previous.service').read_text(), original)
            proof = json.loads((evidence / 'activation.json').read_text())
            self.assertTrue(proof['active'])
            self.assertEqual(proof['image'], 'sha256:new')


if __name__ == '__main__':
    unittest.main()
