"""Private Unix transport for the existing bounded controller protocol.

One web process multiplexes requests through one connection. A disconnected
request is never replayed. Old dispatched work drains before a new connection
can consume the same lane capacity.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import socket
import stat
import threading
from typing import Any, Callable, IO

from library.ipc import Channel, Controller, Remote, IPCError


class UnixController:
    def __init__(self, path: Path, invoke: Callable[[str, dict[str, Any]], Any], *,
                 validate: Callable[[str, dict[str, Any]], None] | None = None):
        self.path, self.invoke, self.validate = path, invoke, validate
        self.listener: socket.socket | None = None
        self.controller: Controller | None = None
        self.owner: IO[bytes] | None = None
        self.thread: threading.Thread | None = None
        self.closed = threading.Event()
        self.guard = threading.Lock()

    def __enter__(self) -> UnixController:
        self.start()
        return self

    def start(self) -> None:
        if self.owner is not None or self.closed.is_set():
            raise ValueError('Controller already started or closed')
        if any(p.is_symlink() for p in (self.path, *self.path.parents)):
            raise ValueError('Symlink in private IPC path')
        parent = self.path.parent.stat()
        if parent.st_uid != os.getuid() or parent.st_mode & 0o077:
            raise ValueError('IPC directory must be private and owned')
        fd = os.open(self.path.with_suffix('.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        owner = os.fdopen(fd, 'r+b')
        try:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.path.exists():
                if not stat.S_ISSOCK(self.path.lstat().st_mode):
                    raise ValueError('IPC target is not a retained socket')
                self.path.unlink()  # Prior OS owner is absent; replace only its socket.
            listener = socket.socket(socket.AF_UNIX)
            try:
                listener.bind(str(self.path))
                os.chmod(self.path, 0o600)
                listener.listen(1)
                listener.settimeout(.2)
            except BaseException:
                listener.close()
                raise
            self.owner, self.listener = owner, listener
        except BaseException:
            owner.close()
            raise
        self.thread = threading.Thread(target=self._serve, name='web-controller', daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        assert self.listener is not None
        while not self.closed.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            controller = Controller(Channel(connection), self.invoke, validate=self.validate)
            with self.guard:
                if self.closed.is_set():
                    controller.close()
                    return
                self.controller = controller
            try:
                controller.serve()
            finally:
                controller.close()
                # Preserve the existing global lane bounds through reconnect.
                for pool in controller.pools.values():
                    pool.shutdown(wait=True, cancel_futures=True)
                with self.guard:
                    self.controller = None

    def check(self) -> None:
        if self.thread is None or not self.thread.is_alive():
            raise RuntimeError('Controller transport stopped')

    def close(self) -> None:
        self.closed.set()
        if self.listener:
            self.listener.close()
        with self.guard:
            if self.controller:
                self.controller.close()
        if self.thread:
            self.thread.join()  # Do not release ownership while effects can finish.
        if self.owner:
            self.owner.close()
            self.owner = None

    def __exit__(self, *args: object) -> None:
        self.close()


class ReconnectingRemote:
    def __init__(self, path: Path):
        self.path = path
        self.remote: Remote | None = None
        self.guard = threading.Lock()

    def call(self, action: str, value: dict[str, Any]) -> Any:
        with self.guard:
            if self.remote is None or self.remote.closed.is_set():
                if self.remote:
                    self.remote.close()
                connection = socket.socket(socket.AF_UNIX)
                connection.settimeout(1)
                try:
                    connection.connect(str(self.path))
                except OSError as exc:
                    connection.close()
                    raise IPCError('Controller unavailable; no request dispatched') from exc
                connection.settimeout(None)
                self.remote = Remote(Channel(connection), raw_compatibility=True)
            remote = self.remote
        try:
            return remote.call(action, value)
        except OSError as exc:
            # This call may have reached the controller. Only a later request
            # may reconnect. The confirmation must be reconciled by its ID.
            remote.close()
            raise IPCError('Controller disconnected; completion unknown; do not resend confirmation') from exc

    def close(self) -> None:
        with self.guard:
            if self.remote:
                self.remote.close()
