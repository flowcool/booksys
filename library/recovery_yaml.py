"""Retired reverse exporter. Frozen SQLite readers belong to migration tooling."""
from typing import Any
from library.primitives import LibraryError


def _retired(*args: Any, **kwargs: Any) -> Any:
    raise LibraryError('Reverse conversion and old-state restore are retired; recover PostgreSQL forward')


exported_records = _retired
verify_export = _retired
write_export = _retired
