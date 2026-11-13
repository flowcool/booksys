"""HTTP server in the controller process."""
from __future__ import annotations

import socket
import threading
from typing import Any

from library.http_child import BoundedServer


class HTTPRuntime:
    def __init__(self, address: tuple[str, int], api: Any):
        self.api = api
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(address)
        self.listener.listen(16)
        self.address = self.listener.getsockname()
        self.api.host = api.host or str(self.address[0]) + ':' + str(self.address[1])
        self.server = BoundedServer(self.listener, api)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name='http')

    def start(self) -> None:
        self.thread.start()

    def check(self) -> None:
        if not self.thread.is_alive():
            raise RuntimeError('HTTP server stopped')

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
