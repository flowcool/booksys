"""Shared metadata evidence version and retry constants."""
from __future__ import annotations

from typing import Any

from library.periodicals import PERIODICAL_TITLES
from library.primitives import digest


METHOD = 'work-metadata-2026-11-25-v9-catalogue-unique-cover'
WEEK = 7 * 86400


def evidence_version(asset: dict[str, Any]) -> str:
    from library.names import METHOD as NAME_METHOD
    return digest({'name_method': NAME_METHOD, 'periodicals': PERIODICAL_TITLES, **{key: asset.get(key) for key in
        ('embedded', 'printed', 'captured_hints', 'assertions', 'status', 'origins')}})
