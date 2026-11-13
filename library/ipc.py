"""Response types used by the HTTP server."""
from __future__ import annotations

from dataclasses import dataclass


class IPCError(ValueError):
    pass


@dataclass(frozen=True)
class EncodedJSON:
    """A complete bounded controller response relayed by the restricted child."""
    body: bytes
    compressed: str | None = None
