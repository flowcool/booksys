"""Best-effort PostgreSQL observations, independent from processing authority."""
from __future__ import annotations

from collections.abc import Callable
import hashlib
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
MAX_EVENTS = 8192


def selected_store(root: Path) -> Any:
    """One PostgreSQL authority; absence never enables a YAML fallback."""
    configured = os.environ.get('BOOKSYS_ACTIVITY_DIR')
    directory = Path(configured) if configured else root
    from library.activity_store import ActivityStore
    try:
        selector = directory / 'activity-authority.yaml'
        if selector.is_symlink():
            raise ValueError('Unsafe Activity selector')
        authority = yaml.safe_load(selector.read_text())['authority_id']
        return ActivityStore(directory, authority=authority)
    except Exception:
        raise ValueError('Activity PostgreSQL authority is not selected') from None


def read_activity(root: Path, stage: str) -> dict[str, Any]:
    store = selected_store(root)
    if store is not None:
        producers = [value for value in store.producers().values() if value['stage'] == stage]
        primary = [p for p in producers if p['producer_scope'] != 'observer']
        current: dict[str, Any] = max(primary or producers, key=lambda p: p['observed_at'], default={})
        def legacy(value: dict[str, Any]) -> dict[str, Any]:
            return {**value, 'last_progress_at': value.get('progress_at'), 'item': value.get('item_ref', ''),
                    'session_started': value.get('session_started', 0), 'operation':value.get('operation_label') or value.get('operation')}
        events, cursor = {}, None
        for _ in range(41):
            page = store.events(after=cursor, stage=stage, limit=200)
            events.update({e['id']: legacy(e) for e in page['items']})
            cursor = page['cursor']
            if cursor is None:
                break
        return {'schema': 1, 'current': legacy(current) if current else {},
                'sources': [legacy(p) for p in producers], 'events': events}
    raise ValueError('Activity PostgreSQL authority unavailable')


def read_yaml(path: Path, *, max_bytes: int = 32 * 1024 * 1024) -> dict[str, Any]:
    try:
        if path.is_symlink() or path.stat().st_size > max_bytes:
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
            yaml.dump(value, out, Dumper=getattr(yaml, 'CSafeDumper', yaml.SafeDumper), allow_unicode=True)
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
        store = selected_store(root)
        if store is not None:
            store.prune(timestamp=stamp)
            return
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
    def __init__(self, root: Path, stage: str, *, clock: Callable[[], float] = time.time, scope: str | None = None):
        if stage not in STAGES:
            raise ValueError('Unknown activity stage')
        self.root, self.stage, self.clock = root, stage, clock
        self.session = reference(str(os.getpid()) + ':' + str(time.time_ns()))
        self.started = clock()
        self.scope = scope or ('collector' if stage in ('collection', 'byparr') else 'controller')
        self.dropped = 0
        self.selection_failed = False
        try:
            self.store = selected_store(root)
        except Exception:
            self.store = None
            self.selection_failed = True
            self.dropped += 1

    def update(self, value: dict[str, Any] | None) -> None:
        try:
            if self.selection_failed:
                self.store = selected_store(self.root)
                self.selection_failed = False
                if self.store is not None:
                    self.store.dropped += self.dropped
                    self.dropped = 0
            if self.store is not None:
                if value is None:
                    self.store.heartbeat(self.stage, self.scope, self.session, timestamp=self.clock())
                else:
                    from booksys_core.diagnostics import REASONS
                    state = value['state']
                    operation = {'working': 'started', 'waiting': 'waiting', 'sleeping': 'completed',
                                 'stopped': 'completed', 'blocked': 'blocked', 'unknown': 'degraded'}[state]
                    reason = value.get('reason', '')
                    reason = reason if reason in REASONS else 'none' if not reason else 'unknown'
                    self.store.report(self.stage, self.scope, self.session, state=state,
                        operation=operation, reason=reason, timestamp=self.clock(),
                        title=value.get('title', ''), item_ref=value.get('item', ''),
                        progressed=value.get('progressed', False), next_attempt=value.get('next_attempt'),
                        process_start=Path('/proc/self/stat').read_text().split()[21],
                        process_id=os.getpid(), session_started=self.started,operation_label=value.get('operation',''))
                return
            raise ValueError('Activity PostgreSQL authority unavailable')
        except Exception:
            self.dropped = min(self.dropped + 1, 2**31 - 1)
            from booksys_core.diagnostics import emit
            emit(service='collector' if self.scope == 'collector' else 'library', stage=self.stage, code='degraded',
                 reason='diagnostic_unavailable', dropped_count=self.dropped)

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


def degradation() -> int:
    """Bounded in-process observation losses, independent of the stderr sink."""
    return sum(value.dropped + (value.store.dropped if value.store is not None else 0)
               for value in _active.values())
