"""Canonical stable identities shared without application dependencies."""
import hashlib
import json
from typing import Any


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def seconds(value: Any) -> float:
    from datetime import datetime
    from booksys_core.errors import CollectionError
    if value is None:
        return 0
    if not isinstance(value, str):
        raise CollectionError('Invalid schedule time')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    offset = parsed.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise CollectionError('Invalid schedule timezone')
    return parsed.timestamp()
