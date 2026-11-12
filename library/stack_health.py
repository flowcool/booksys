"""Local process readiness, separate from NAS and reader dependency readiness."""
from __future__ import annotations

import argparse
import http.client
import os
from pathlib import Path
import time

import yaml


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('roles', nargs='+')
    args = parser.parse_args()
    if args.roles == ['web']:
        client = http.client.HTTPConnection('127.0.0.1', 8768, timeout=2)
        try:
            client.request('HEAD', '/', headers={'Host': os.environ['BOOKSYS_PUBLIC_HOST']})
            if client.getresponse().status != 200:
                raise SystemExit(1)
        finally:
            client.close()
        return
    root = Path(os.environ.get('BOOKSYS_LIBRARY_ROOT', '/data/booksys/library-lab'))
    if args.roles == ['controller']:
        if not Path('/ipc/controller.sock').is_socket():
            raise SystemExit(1)
        return
    from library.operational_state import RuntimeObservation
    from library.postgres import Database
    pulses=RuntimeObservation(Database.configured(root/'state')).heartbeats()
    for role in args.roles:
        if role not in pulses or not 0 <= time.time() - pulses[role] <= 15:
            raise SystemExit(1)


if __name__ == '__main__':
    main()
