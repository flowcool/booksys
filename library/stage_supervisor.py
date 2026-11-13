"""systemd owns process death. These helpers neither install nor start units."""
from __future__ import annotations

from contextlib import contextmanager
import os
import signal
import socket
import threading
from typing import Any, Iterator

from library.primitives import LibraryError


@contextmanager
def deadline(seconds: float) -> Iterator[None]:
    def expired(*_: Any) -> None:
        raise LibraryError('Stage enclosing deadline exceeded')
    previous = signal.signal(signal.SIGALRM, expired)
    prior_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    # If Python cannot unwind, ask the existing supervisor to kill the cgroup.
    def force_stop() -> None:
        if os.environ.get('BOOKSYS_SUPERVISOR') == 'compose':
            os.kill(os.getpid(), signal.SIGKILL)
        else:
            notify('WATCHDOG=trigger')
    hard_stop = threading.Timer(seconds + 5, force_stop)
    hard_stop.daemon = True
    hard_stop.start()
    try:
        yield
    finally:
        hard_stop.cancel()
        signal.setitimer(signal.ITIMER_REAL, *prior_timer)
        signal.signal(signal.SIGALRM, previous)


def notify(message: str) -> None:
    address = os.environ.get('NOTIFY_SOCKET')
    if address:
        if address.startswith('@'):
            address = '\0' + address[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
            channel.connect(address)
            channel.sendall(message.encode())
