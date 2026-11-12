"""Bounded immutable sort keys shared by indexed operator queries."""
from functools import lru_cache
import re

_parts = re.compile(r'(\d+)')


@lru_cache(maxsize=16384)
def natural(value: str) -> tuple[tuple[int, int | str], ...]:
    return tuple((1, int(part)) if part.isdecimal() else (0, part.casefold()) for part in _parts.split(value))


def compare(first: str, second: str) -> int:
    a, b = natural(first), natural(second)
    return (a > b) - (a < b)


def storage_key(value: str) -> bytes:
    """Byte ordering equivalent to natural(), independent of database locale."""
    result = bytearray()
    for kind, part in natural(value):
        if kind:
            digits = str(part).encode('ascii')
            result.extend(b'\x02' + len(digits).to_bytes(8, 'big') + digits)
        else:
            result.extend(b'\x01' + str(part).encode('utf-8').replace(b'\x00', b'\x00\xff') + b'\x00\x00')
    return bytes(result)
