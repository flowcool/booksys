"""Retired SQLite runtime capabilities. Offline source reading lives in pg_import."""
from typing import Any
from library.primitives import LibraryError


def _retired(*args: Any, **kwargs: Any) -> Any:
    raise LibraryError("SQLite runtime is retired; use the selected PostgreSQL authority")


connection = _retired
writer = _retired
require_runtime = _retired
private_directory = _retired
passive_checkpoint = _retired
defer_contention = _retired
