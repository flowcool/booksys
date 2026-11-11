"""Lossless, non-executable JSON representation of accepted legacy YAML values."""
from __future__ import annotations

import base64
from datetime import date, datetime
import json
import math
from typing import Any


class CodecError(ValueError):
    """The value cannot be represented without changing its meaning."""


TAG = '$booksys'


def encode(value: Any) -> str:
    active: set[int] = set()

    def visit(item: Any, depth: int = 0) -> Any:
        if depth > 128:
            raise CodecError('Value nesting exceeds codec limit')
        if item is None or type(item) in (bool, int, str):
            return item
        if type(item) is float:
            return item if math.isfinite(item) else {TAG: ['float', repr(item)]}
        if type(item) is bytes:
            return {TAG: ['binary', base64.b64encode(item).decode('ascii')]}
        if type(item) in (date, datetime):
            return {TAG: [type(item).__name__, item.isoformat()]}
        if type(item) not in (dict, list, set):
            raise CodecError('Unsupported legacy value type')
        ident = id(item)
        if ident in active:
            raise CodecError('Recursive aliases are unsupported')
        active.add(ident)
        try:
            if type(item) is dict:
                if not all(type(key) is str for key in item):
                    raise CodecError('Legacy map keys must be strings')
                pairs = [[key, visit(item[key], depth + 1)] for key in sorted(item)]
                return {TAG: ['map', pairs]} if TAG in item else dict(pairs)
            values = [visit(child, depth + 1) for child in item]
            if type(item) is set:
                values.sort(key=lambda child: json.dumps(child, sort_keys=True))
                return {TAG: ['set', values]}
            return values
        finally:
            active.remove(ident)

    try:
        return json.dumps(visit(value), ensure_ascii=False, allow_nan=False,
                          sort_keys=True, separators=(',', ':'))
    except (ValueError, TypeError, RecursionError) as exc:
        raise CodecError('Cannot encode legacy value') from exc


def decode(payload: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise CodecError('Duplicate encoded key')
            result[key] = value
        return result

    def invalid_constant(value: str) -> Any:
        raise CodecError('Nonfinite JSON constant')

    def visit(item: Any, depth: int = 0) -> Any:
        if depth > 128:
            raise CodecError('Value nesting exceeds codec limit')
        if isinstance(item, list):
            return [visit(child, depth + 1) for child in item]
        if not isinstance(item, dict):
            return item
        if TAG not in item:
            return {key: visit(child, depth + 1) for key, child in item.items()}
        tag = item.get(TAG)
        if set(item) != {TAG} or not isinstance(tag, list) or len(tag) != 2:
            raise CodecError('Invalid encoded tag')
        kind, value = tag
        if kind == 'map' and isinstance(value, list):
            if not all(isinstance(pair, list) and len(pair) == 2 and type(pair[0]) is str for pair in value):
                raise CodecError('Invalid escaped map')
            return pairs([(key, visit(child, depth + 1)) for key, child in value])
        if kind == 'set' and isinstance(value, list):
            return set(visit(child, depth + 1) for child in value)
        if kind == 'float' and value in ('inf', '-inf', 'nan'):
            return float(value)
        if type(value) is str:
            if kind == 'binary':
                return base64.b64decode(value, validate=True)
            if kind == 'date':
                return date.fromisoformat(value)
            if kind == 'datetime':
                return datetime.fromisoformat(value)
        raise CodecError('Unknown encoded tag')

    try:
        return visit(json.loads(payload, object_pairs_hook=pairs, parse_constant=invalid_constant))
    except (ValueError, TypeError, RecursionError, OverflowError) as exc:
        raise CodecError('Invalid encoded legacy value') from exc


def state_digest(state: dict[str, Any]) -> str:
    """Streaming semantic digest; media strings are never concatenated globally."""
    import hashlib
    from library.primitives import TABLES
    result = hashlib.sha256()
    def add(value: Any) -> None:
        raw = encode(value).encode()
        result.update(len(raw).to_bytes(8, 'big')); result.update(raw)
    add({key: state[key] for key in ('schema', 'revision', 'ledger')})
    for kind in TABLES:
        add(kind)
        for ident in sorted(state[kind]):
            add([ident, state[kind][ident]])
    return result.hexdigest()
