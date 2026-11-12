"""Restricted HTTP child. It receives sockets, trusted code and no state mount."""
from __future__ import annotations

import argparse
from http.server import ThreadingHTTPServer
import socket
import threading
from typing import Any
from pathlib import Path

from library.http import handler
from library.ipc import Channel, Remote


class BoundedServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, listener: socket.socket, remote: Any):
        super().__init__(listener.getsockname(), handler(Path('/unmounted'), api=remote), bind_and_activate=False)
        self.socket.close()
        self.socket = listener
        self.server_address = listener.getsockname()
        self.server_name = str(self.server_address[0])
        self.server_port = int(self.server_address[1])
        self.slots = threading.BoundedSemaphore(16)

    def get_request(self) -> tuple[socket.socket, Any]:
        connection, address = super().get_request()
        connection.settimeout(5)
        return connection, address

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(b'HTTP/1.1 429 Too Many Requests\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--listen-fd', type=int, required=True)
    parser.add_argument('--ipc-fd', type=int, required=True)
    args = parser.parse_args()
    remote = Remote(Channel(socket.socket(fileno=args.ipc_fd)), raw_compatibility=True)
    listener = socket.socket(fileno=args.listen_fd)
    try:
        with BoundedServer(listener, remote) as server:
            server.serve_forever(poll_interval=.1)
    finally:
        remote.close()


if __name__ == '__main__':
    main()
