"""Synthetic jobs for isolated queue qualification. Never import business adapters."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time


def record(directory: str, name: str, duration: float) -> str:
    started = time.monotonic()
    path = Path(directory)
    (path / (name + '.started')).write_text(str(os.getpid()))
    time.sleep(duration)
    (path / (name + '.json')).write_text(json.dumps({
        'name': name, 'pid': os.getpid(), 'start': started, 'end': time.monotonic()}))
    return name
