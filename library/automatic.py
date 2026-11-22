"""Shared metadata evidence version and retry constants."""
from __future__ import annotations

from typing import Any

from library.primitives import digest


METHOD = 'work-metadata-2026-11-22-v4-cover'
WEEK = 7 * 86400


def evidence_version(asset: dict[str, Any]) -> str:
    from library.names import METHOD as NAME_METHOD
    return digest({'name_method': NAME_METHOD, **{key: asset.get(key) for key in
        ('embedded', 'printed', 'captured_hints', 'assertions', 'status', 'origins')}})
