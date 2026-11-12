"""Explicit isolated import/export tooling. This module alone creates stores."""
from __future__ import annotations

from collections.abc import Callable
import json
import ctypes
import os
from pathlib import Path
from typing import Any

from library.primitives import TABLES, LibraryError, digest, validate, atomic_yaml, sync_dir
from library.codec import encode, state_digest


def _retired(*args: Any, **kwargs: Any) -> Any:
    raise LibraryError('Legacy SQLite conversion and restore are retired; use the qualified PostgreSQL importer and forward recovery')


activate_catalogue = _retired
recover_yaml = _retired
import_catalogue = _retired
compact_catalogue = _retired
manifest = _retired
