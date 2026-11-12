"""Existing service's sandboxed HTTP child and fixed parent mediation."""
from __future__ import annotations

from collections import deque
from pathlib import Path
import socket
import subprocess
import threading
import time
from typing import Any

from library.ipc import Channel, Controller
from library.sandbox import native_prefix, PYTHON
from booksys_core.diagnostics import emit


class HTTPRuntime:
    def __init__(self, address: tuple[str, int], api: Any):
        self.api = api
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(address)
        self.listener.listen(16)
        self.listener.set_inheritable(True)
        self.address = self.listener.getsockname()
        self.api.host = str(self.address[0]) + ':' + str(self.address[1])
        self.child: subprocess.Popen[bytes] | None = None
        self.controller: Controller | None = None
        self.thread: threading.Thread | None = None
        self.restarts: deque[float] = deque()
        self.closed = False

    def start(self) -> None:
        now = time.monotonic()
        while self.restarts and now - self.restarts[0] > 300:
            self.restarts.popleft()
        restarting = self.child is not None
        if restarting and len(self.restarts) >= 3:
            emit(service='library', stage='diagnostics', code='blocked', reason='diagnostic_unavailable')
            return
        if restarting:
            self.restarts.append(now)
        parent, child = socket.socketpair()
        prefix = native_prefix(Path(__file__))
        command = prefix + [PYTHON, '-m', 'library.http_child', '--listen-fd', str(self.listener.fileno()),
                            '--ipc-fd', str(child.fileno())]
        try:
            self.child = subprocess.Popen(command, pass_fds=(self.listener.fileno(), child.fileno()),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            parent.close(); child.close()
            raise
        child.close()
        self.controller = Controller(Channel(parent), self.api.invoke, validate=getattr(self.api, 'validate', None))
        self.thread = threading.Thread(target=self.controller.serve, daemon=True, name='http-mediation')
        self.thread.start()
        # Starting bwrap can fail asynchronously. No privileged fallback exists.
        try:
            status = self.child.wait(timeout=.05)
        except subprocess.TimeoutExpired:
            return
        self.controller.close()
        raise RuntimeError('Restricted HTTP child failed to start: ' + str(status))

    def check(self) -> None:
        if not self.closed and self.child is not None and self.child.poll() is not None:
            if self.controller:
                self.controller.close()
            self.start()

    def close(self) -> None:
        self.closed = True
        if self.controller:
            self.controller.close()
        if self.child and self.child.poll() is None:
            self.child.terminate()
            try:
                self.child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.child.kill(); self.child.wait()
        self.listener.close()
        if self.thread:
            self.thread.join(1)
