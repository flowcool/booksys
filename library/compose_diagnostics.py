"""Fixed diagnostic mediation. The controller never receives Docker authority."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
from typing import Any
import uuid
import yaml

from library.log_sources import COMPOSE_ROLES, LiveSource
from library.reader_client import request
from library.primitives import LibraryError
from library.locking import bounded_acquisition


class RemoteSource:
    def __init__(self, name: str, state: Path):
        if name not in COMPOSE_ROLES:
            raise ValueError('Unknown fixed Compose source')
        self.name, self.state = name, state
        self.generation = 'unavailable'

    def refresh(self) -> None:
        refresh_sources([self])

    def scan(self, start: float, end: float, position: Any, *, lines: int,
             byte_limit: int, deadline: float) -> dict[str, Any]:
        reference = uuid.uuid4().hex
        value = {'role': self.name, 'start': start, 'end': end, 'position': position,
                 'lines': min(lines, 10000), 'byte_limit': min(byte_limit, 2*1024**2),
                 'generation': self.generation}
        from library.operational_state import ComposeDiagnostics
        from library.postgres import Database, CommitUnknown
        if time.monotonic() >= deadline:
            raise TimeoutError('Diagnostic page acquisition expired')
        try:
            with bounded_acquisition(deadline):
                ComposeDiagnostics(Database.configured(self.state)).retain(reference,value)
            if time.monotonic() >= deadline:
                raise TimeoutError('Diagnostic page acquisition expired')
            return request({'operation':'diagnostics','job':reference},timeout=deadline-time.monotonic())
        except LibraryError as exc:
            if not isinstance(exc,CommitUnknown) and (str(exc)=='storage_busy'
                    or isinstance(exc.__cause__,TimeoutError) or time.monotonic()>=deadline):
                raise TimeoutError('Diagnostic page acquisition expired') from exc
            raise OSError('Compose diagnostic source unavailable') from exc


def refresh_sources(sources: list[RemoteSource]) -> None:
    """Bind all fixed sources to one canonical observer snapshot."""
    if not sources:
        return
    from library.operational_state import ComposeDiagnostics
    from library.postgres import Database
    if any(source.state != sources[0].state for source in sources):
        raise ValueError('Diagnostic sources require one authority')
    try:
        with bounded_acquisition(time.monotonic()+.5):
            value=ComposeDiagnostics(Database.configured(sources[0].state)).sources()
        if not 0 <= time.time()-value['observed_at'] <= 15:
            raise ValueError('Stale Compose observations')
        generations={source.name:value['sources'][source.name] for source in sources}
    except (LibraryError,OSError,ValueError,KeyError,TypeError):
        generations={source.name:'unavailable' for source in sources}
    for source in sources:
        source.generation=generations[source.name]


def read_request(state: Path, reference: str, project: str) -> dict[str, Any]:
    deadline=time.monotonic()+.25
    import re
    if not isinstance(reference, str) or not re.fullmatch('[0-9a-f]{32}', reference):
        raise ValueError('Invalid diagnostic request reference')
    from library.operational_state import ComposeDiagnostics
    from library.postgres import Database, CommitUnknown
    try:
        with bounded_acquisition(deadline):
            value=ComposeDiagnostics(Database.configured(Path(os.environ['BOOKSYS_LAUNCHER_DATABASE_DIR']))).request(reference)
    except LibraryError as exc:
        if not isinstance(exc,CommitUnknown) and (str(exc)=='storage_busy' or time.monotonic()>=deadline):
            raise TimeoutError('Diagnostic server acquisition expired') from exc
        raise
    if (set(value) != {'role', 'start', 'end', 'position', 'lines', 'byte_limit', 'generation'}
            or value['role'] not in COMPOSE_ROLES or type(value['lines']) is not int
            or not 0 < value['lines'] <= 10000 or type(value['byte_limit']) is not int
            or not 0 < value['byte_limit'] <= 2*1024**2
            or type(value['start']) not in (int, float) or type(value['end']) not in (int, float)
            or not 0 <= value['start'] <= value['end'] <= time.time()
            or value['end']-value['start'] > 86400):
        raise ValueError('Invalid bounded diagnostic query')
    position = value['position']
    if position is not None and (not isinstance(position, list) or len(position) != 2
            or type(position[0]) not in (float, int) or not value['start'] <= position[0] <= value['end']
            or type(position[1]) is not int or not 0 <= position[1] <= 10000):
        raise ValueError('Invalid diagnostic continuation')
    source = LiveSource(value['role'], project=project)
    source.refresh(deadline=deadline)
    if not source.available or source.generation != value['generation']:
        raise ValueError('Compose diagnostic source changed; refresh its observation')
    return source.scan(value['start'], value['end'], position, lines=value['lines'],
        byte_limit=value['byte_limit'], deadline=deadline)


def observe_sources(state: Path, project: str, stopped: Any) -> None:
    """Fixed read-only discovery in the privileged service, independent of jobs."""
    from library.operational_state import ComposeDiagnostics
    from library.postgres import Database
    retained=ComposeDiagnostics(Database.configured(Path(os.environ["BOOKSYS_LAUNCHER_DATABASE_DIR"])))
    while not stopped.is_set():
        sources = {}
        for name in COMPOSE_ROLES:
            source = LiveSource(name, project=project)
            source.refresh()
            sources[name] = source.generation if source.available else 'unavailable'
        retained.publish({'schema': 1, 'observed_at': time.time(), 'sources': sources})
        stopped.wait(5)
