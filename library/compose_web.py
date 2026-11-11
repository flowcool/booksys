"""HTTP-only service. It has no canonical state, NAS, secrets or engine access."""
from __future__ import annotations

import argparse
from pathlib import Path
import socket

from library.compose_transport import ReconnectingRemote
from library.http_child import BoundedServer
from library.http import public_origins


class PublicServer(BoundedServer):
    public_host: str
    public_origin: str | None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', type=Path, required=True)
    parser.add_argument('--listen', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8768)
    parser.add_argument('--public-host', required=True)
    parser.add_argument('--public-origin')
    args = parser.parse_args()
    try:
        public_origins(args.public_host, args.public_origin)
    except ValueError as exc:
        parser.error(str(exc))
    remote = ReconnectingRemote(args.socket)
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((args.listen, args.port))
    listener.listen(16)
    try:
        with PublicServer(listener, remote) as server:
            server.public_host = args.public_host
            server.public_origin = args.public_origin
            server.serve_forever(poll_interval=.1)
    finally:
        listener.close()
        remote.close()


if __name__ == '__main__':
    main()
