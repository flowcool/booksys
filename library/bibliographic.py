"""Pure bibliographic normalization and ISBN validation."""
import re
import unicodedata
from typing import Any

def normalized(value: Any) -> str:
    return ' '.join(unicodedata.normalize('NFKC', str(value)).casefold().split())


def isbn13(value: str) -> str | None:
    value = re.sub(r'[-\s]', '', value)
    if not re.fullmatch(r'97[89]\d{10}', value):
        return None
    checksum = sum(int(char) * (1 if index % 2 == 0 else 3) for index, char in enumerate(value[:12]))
    return value if (10 - checksum % 10) % 10 == int(value[-1]) else None
