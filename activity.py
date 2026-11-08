"""Best-effort observations, never a processing authority. One bounded file per stage."""
from __future__ import annotations

from collections.abc import Callable
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Any

import yaml

STAGES = ('collection', 'byparr', 'downloads', 'local', 'identity', 'retirement')
STATES = ('working', 'waiting', 'sleeping', 'stopped', 'blocked', 'unknown')
WINDOW = 86400
MAX_EVENTS = 512


def read_activity(root: Path, stage: str) -> dict[str, Any]:
    return read_yaml(root / (stage + '.yaml'))


def read_yaml(path: Path) -> dict[str, Any]:
    try:
        if path.is_symlink() or path.stat().st_size > 32 * 1024 * 1024:
            return {}
        value = yaml.load(path.read_text(), Loader=getattr(yaml, 'CSafeLoader', yaml.SafeLoader))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, yaml.YAMLError):
        return {}


def write_yaml(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            yaml.safe_dump(value, out, allow_unicode=True)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def prune(root: Path, stage: str, stamp: float) -> None:
    """Observation-only lock. Never changes source freshness or producer ownership."""
    try:
        with (root / (stage + '.lock')).open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            saved = read_activity(root, stage)
            events = saved.get('events', {})
            retained = {k: v for k, v in events.items() if v.get('at', 0) >= stamp - WINDOW}
            if len(retained) != len(events):
                write_yaml(root / (stage + '.yaml'), {**saved, 'events': retained})
    except (OSError, ValueError, TypeError):
        pass


def text(value: Any) -> str:
    # Titles are display text. Free-form errors never cross this boundary.
    clean = re.sub(r'(?i)(?:https?://|www\.)\S+', '[private URL]', str(value or ''))
    clean = re.sub(r'(?i)(?:password|api[_-]?key|token|secret)\s*[:=]\s*\S+', '[private field]', clean)
    return clean[:240]


def reference(value: Any) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()


class Activity:
    def __init__(self, root: Path, stage: str, *, clock: Callable[[], float] = time.time):
        if stage not in STAGES:
            raise ValueError('Unknown activity stage')
        self.root, self.stage, self.clock = root, stage, clock
        self.session = reference(str(os.getpid()) + ':' + str(time.time_ns()))
        self.started = clock()

    def update(self, value: dict[str, Any] | None) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with (self.root / (self.stage + '.lock')).open('a') as handle:
                # A busy observation writer must never delay business work.
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                saved = read_activity(self.root, self.stage)
                current = saved.get('current', {})
                stamp = self.clock()
                if value is None and current.get('session') != self.session:
                    return
                events = {k: e for k, e in saved.get('events', {}).items()
                          if e.get('at', 0) >= stamp - WINDOW}
                if value is not None:
                    changed = any(current.get(k) != value.get(k) for k in ('state', 'operation', 'item', 'reason'))
                    if changed:
                        value['since'] = stamp
                    else:
                        value['since'] = current.get('since', stamp)
                    progressed = value.pop('progressed', False)
                    value['last_progress_at'] = stamp if progressed else current.get('last_progress_at')
                    if changed or progressed:
                        event = {**value, 'at': stamp}
                        eid = reference(self.session + ':' + str(stamp) + ':' + json.dumps(event, sort_keys=True))
                        events[eid] = event
                    current = value
                # Heartbeats retain the original producer identity after restart.
                if value is not None:
                    current.update(session=self.session, session_started=self.started, pid=os.getpid(),
                                   process_start=Path('/proc/self/stat').read_text().split()[21])
                current['observed_at'] = stamp
                write_yaml(self.root / (self.stage + '.yaml'), {'schema': 1, 'current': current,
                    'events': dict(sorted(events.items(), key=lambda x: x[1]['at'])[-MAX_EVENTS:])})
        except (OSError, ValueError, TypeError):
            pass  # Observability cannot stop work or expose source exceptions.

    def report(self, state: str, operation: str, *, item: str = '', title: str = '',
               reason: str = '', next_attempt: float | None = None, progressed: bool = False) -> None:
        if state not in STATES:
            raise ValueError('Unknown activity state')
        self.update({'state': state, 'operation': text(operation), 'item': reference(item) if item else '',
                     'title': text(title), 'reason': text(reason), 'next_attempt': next_attempt,
                     'progressed': progressed})

    def heartbeat(self) -> None:
        self.update(None)

    def start_heartbeat(self) -> threading.Event:
        stop = threading.Event()
        def beat() -> None:
            while not stop.wait(15):
                self.heartbeat()
        threading.Thread(target=beat, daemon=True, name='activity-' + self.stage).start()
        return stop


# Runtime-only binding; absent in isolated commands unless their owner enables it.
_active: dict[str, Activity] = {}


def bind(root: Path, *stages: str) -> list[threading.Event]:
    stops = []
    for stage in stages:
        _active[stage] = Activity(root, stage)
        stops.append(_active[stage].start_heartbeat())
    return stops


def report(stage: str, state: str, operation: str, **values: Any) -> None:
    if stage in _active:
        _active[stage].report(state, operation, **values)
