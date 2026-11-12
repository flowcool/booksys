from __future__ import annotations

from contextlib import contextmanager, ExitStack
from pathlib import Path
from contextvars import ContextVar
from typing import Any
from typing import Iterator

from library.primitives import digest, lock


@contextmanager
def effect_scopes(directory: Path, scopes: list[str]) -> Iterator[None]:
    """Acquire the complete sorted entity/destination set without waiting."""
    held = held_scopes.get()
    names = {str(directory.absolute() / 'stage-scopes' / (digest(scope) + '.lock')) for scope in scopes}
    with ExitStack() as stack:
        for name in sorted(names - held):
            stack.enter_context(lock(Path(name), blocking=False))
        token = held_scopes.set(held | names)
        try:
            yield
        finally:
            held_scopes.reset(token)


held_scopes: ContextVar[set[str]] = ContextVar('held_scopes', default=set())


def record_scopes(state: dict[str, Any]) -> list[str]:
    return [kind.removesuffix('s') + ':' + ident for kind in ('assets', 'origins')
            for ident in state.get(kind, {})]


def change_scopes(change: Any) -> list[str]:
    scopes = [kind.removesuffix('s') + ':' + ident for kind in set(change.upserts) | set(change.deletes)
              for ident in set(change.upserts.get(kind, {})) | set(change.deletes.get(kind, []))]
    for kind, records in change.upserts.items():
        for record in records.values():
            for field, prefix in (('asset', 'asset'), ('parent_origin', 'origin')):
                if isinstance(record.get(field), str):
                    scopes.append(prefix + ':' + record[field])
            if kind == 'origins' and record.get('sha256'):
                scopes.append('asset:' + record['sha256'])
            for oid in record.get('origins', []):
                scopes.append('origin:' + oid)
    return scopes
