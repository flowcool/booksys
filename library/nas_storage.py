"""Check the two NAS export sentinels before dependent work."""
import os
import time

from library.primitives import LibraryError

_verified_at = float('-inf')


def require_nas() -> None:
    global _verified_at
    stamp = time.monotonic()
    if stamp - _verified_at < 30:
        return
    try:
        for path in ('/nas-identity/.nas-identity', '/nas-library-identity/.nas-identity'):
            if os.stat(path).st_size == 0:
                raise LibraryError('NAS unavailable')
    except OSError as exc:
        raise LibraryError('NAS unavailable') from exc
    _verified_at = stamp
