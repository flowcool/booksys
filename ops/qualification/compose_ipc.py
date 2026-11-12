"""Qualify the delivered typed IPC across a read-only Unix socket bind.

Uses an empty synthetic controller root, not the selected lab authority.
This proves transport compatibility, not the future web service entry point.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import socket
import sys
import tempfile
import threading
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from library.api import PublicAPI
from library.ipc import Channel, Controller
from ops.qualification.compose_probe import owned_probe

CLIENT = '''
import json, socket
from pathlib import Path
from library.ipc import Channel, Remote, IPCError
assert not Path('/data/booksys').exists()
assert not Path('/var/run/docker.sock').exists()
connection = socket.socket(socket.AF_UNIX)
connection.connect('/run/booksys-ipc/control.sock')
remote = Remote(Channel(connection))
envelope = {'payload': {}, 'host': 'synthetic:8768', 'origin': '', 'token': ''}
try:
    assert isinstance(remote.call('token', envelope)['token'], str)
    activity = remote.call('activity', envelope)
    assert isinstance(activity, dict)
    try:
        remote.call('confirm', envelope)
    except IPCError as error:
        assert str(error) == 'Forbidden request envelope'
    else:
        raise RuntimeError('Invalid decision envelope was accepted')
    print(json.dumps({'typed_token_read': True, 'activity_transfer': True,
                      'invalid_decision_denied': True, 'state_socket_absent': True}))
finally:
    remote.close()
'''


def qualify(image: str, evidence: Path) -> dict[str, Any]:
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
        raise ValueError('Immutable local image required')
    evidence.mkdir(parents=True, exist_ok=False)
    root = Path(tempfile.mkdtemp(prefix='booksys-ipc-controller-'))
    ipc = Path(tempfile.mkdtemp(prefix='booksys-ipc-transport-'))
    api = PublicAPI(root, host='synthetic:8768', reception=root/'unmounted')
    controllers: list[Controller] = []
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(ipc/'control.sock'))
        (ipc/'control.sock').chmod(0o600)
        listener.listen(1)
        listener.settimeout(.2)
        closing = threading.Event()
        def serve() -> None:
            while not closing.is_set():
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                controller = Controller(Channel(connection), api.invoke, validate=api.validate)
                controllers.append(controller)
                controller.serve()
                break
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        command = ['docker', 'create', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges', '--user', '1000:1000', '--memory', '256m',
            '--memory-swap', '256m', '--cpus', '.5', '--pids-limit', '32',
            '--mount', f'type=bind,src={ipc},dst=/run/booksys-ipc,readonly',
            '--entrypoint', '/app/.venv/bin/python', image, '-c', CLIENT]
        try:
            report = owned_probe(command, evidence, timeout=25)
        finally:
            closing.set()
            for controller in controllers:
                controller.close()
            thread.join(1)
        if report['exit_code']:
            raise RuntimeError('Transport qualification failed; inspect retained result')
        return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    qualify(args.image, args.evidence)
    print('PASS: delivered typed IPC crosses the restricted container boundary')


if __name__ == '__main__':
    main()
