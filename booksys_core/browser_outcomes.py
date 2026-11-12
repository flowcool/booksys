"""Versioned browser outcomes. No header, body, cookie or exception text escapes."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import re

from booksys_core.errors import BrowserError


def retry_after(value: str | None, *, received_at: float) -> tuple[str, int | None]:
    if value is None:
        return 'absent', None
    value = value.strip()
    if re.fullmatch('[0-9]+', value):
        if len(value.lstrip('0')) > 12:
            return 'unrepresentable', None
        delay = int(value.lstrip('0') or '0')
    else:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                return 'invalid', None
            delay = max(0, math.ceil(parsed.timestamp() - received_at))
        except (ValueError, TypeError, OverflowError):
            return 'invalid', None
    try:
        datetime.fromtimestamp(received_at + delay, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return 'unrepresentable', None
    return 'valid', delay


@dataclass(frozen=True)
class Outcome:
    code: str
    origin: str
    retry_after_state: str = 'absent'
    retry_after_seconds: int | None = None


class BrowserOutcome(BrowserError):
    def __init__(self, outcome: Outcome):
        super().__init__(outcome.code)
        self.outcome = outcome
