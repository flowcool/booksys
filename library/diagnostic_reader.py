"""Bounded fixed-source event search. Cursors carry no paths or executable input."""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, asdict
import copy
import base64
import fcntl
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import time
from typing import Any, Callable, Protocol

from booksys_core.diagnostics import SERVICES, STAGES, REASONS, REF_KINDS, opaque, validate


@dataclass(frozen=True)
class Filter:
    start: float
    end: float
    service: str | None = None
    stage: str | None = None
    reason: str | None = None
    ref_kind: str | None = None
    ref: str | None = None
    cycle_id: str | None = None

    def check(self, timestamp: float, *, older: bool) -> None:
        if (type(self.start) not in (int, float) or type(self.end) not in (int, float)
                or not 0 <= self.start <= self.end <= timestamp or self.end - self.start > 86400
                or not older and self.start < timestamp - 86400):
            raise ValueError('A bounded retained UTC window is required')
        for value, registry in ((self.service, SERVICES), (self.stage, STAGES), (self.reason, REASONS),
                                (self.ref_kind, REF_KINDS)):
            if value is not None and value not in registry:
                raise ValueError('Invalid diagnostic filter')
        if (self.ref_kind is None) != (self.ref is None) or self.ref is not None and not opaque(self.ref):
            raise ValueError('Invalid typed diagnostic reference')
        if self.cycle_id is not None and not opaque(self.cycle_id):
            raise ValueError('Invalid cycle identity')

    def matches(self, value: dict[str, Any]) -> bool:
        return (all(wanted is None or value.get(key) == wanted for key, wanted in
                    (('service', self.service), ('stage', self.stage), ('reason', self.reason), ('cycle_id', self.cycle_id)))
                and (self.ref is None or {'kind': self.ref_kind, 'id': self.ref} in value['refs']))


class Source(Protocol):
    name: str
    generation: str

    def scan(self, start: float, end: float, position: Any, *, lines: int,
             byte_limit: int, deadline: float) -> dict[str, Any]: ...


class MemorySource:
    """Deterministic retained source adapter for isolated acceptance fixtures."""
    def __init__(self, name: str, generation: str, records: list[tuple[float, dict[str, Any]]]):
        self.name, self.generation, self.records = name, generation, records

    def scan(self, start: float, end: float, position: Any, *, lines: int,
             byte_limit: int, deadline: float) -> dict[str, Any]:
        offset = position or 0
        rows, used, scanned, frontier = [], 0, 0, start
        while offset < len(self.records) and scanned < lines and time.monotonic() < deadline:
            at, record = self.records[offset]
            size = len(json.dumps(record).encode())
            if used + size > byte_limit:
                break
            used += size
            scanned += 1
            offset += 1
            frontier = max(frontier, at)
            if start <= at <= end:
                rows.append((at, record))
        done = offset == len(self.records)
        return {'rows': rows, 'position': offset, 'frontier': end if done else min(end, frontier),
                'done': done, 'bytes': used, 'lines': scanned, 'gaps': []}


class DiagnosticReader:
    def __init__(self, sources: Sequence[Source], lock_path: Path, *, clock: Callable[[], float] = time.time,
                 line_limit: int = 10000, byte_limit: int = 2 * 1024**2):
        if not sources or len({s.name for s in sources}) != len(sources):
            raise ValueError('Distinct fixed diagnostic sources required')
        self.sources, self.lock_path, self.clock = list(sources), lock_path, clock
        self.line_limit, self.byte_limit = line_limit, byte_limit
        self.key = secrets.token_bytes(32)
        self.cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.responses: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()

    def _sign(self, value: dict[str, Any]) -> str:
        raw = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
        return base64.urlsafe_b64encode(raw + hmac.digest(self.key, raw, 'sha256')).decode()

    def _decode(self, token: str) -> dict[str, Any]:
        try:
            if not isinstance(token, str) or len(token) > 8192:
                raise ValueError('bound')
            data = base64.b64decode(token, altchars=b'-_', validate=True)
            raw, signature = data[:-32], data[-32:]
            if not hmac.compare_digest(hmac.digest(self.key, raw, 'sha256'), signature):
                raise ValueError('signature')
            value = json.loads(raw)
            if set(value) != {'id', 'serial', 'shape', 'generations'} or type(value['serial']) is not int:
                raise ValueError('shape')
            return dict(value)
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError('Invalid diagnostic cursor') from exc

    def query(self, query: Filter, *, cursor: str | None = None, older: bool = False,
              stacks: bool = False) -> dict[str, Any]:
        stamp = self.clock()
        query.check(stamp, older=older)
        shape = hashlib.sha256(json.dumps([asdict(query), stacks], sort_keys=True).encode()).hexdigest()
        for key in list(self.cache):
            if stamp - self.cache[key]['created'] > 1200:
                del self.cache[key]
        generations = {s.name: s.generation for s in self.sources}
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 8192):
            raise ValueError('Invalid diagnostic cursor')
        response_key = hashlib.sha256(json.dumps([shape, cursor, generations], sort_keys=True).encode()).hexdigest()
        for key in list(self.responses):
            if stamp - self.responses[key][0] > 1200:
                del self.responses[key]
        if response_key in self.responses:
            return copy.deepcopy(self.responses[response_key][1])
        if cursor:
            token = self._decode(cursor)
            if token['shape'] != shape:
                raise ValueError('Cursor belongs to another query')
            if token['generations'] != generations:
                return {'state': 'partial', 'events': [], 'cursor': None,
                        'gaps': [{'reason': 'generation_changed', 'source': name} for name in generations
                                 if token['generations'].get(name) != generations[name]]}
            state = self.cache.get(token['id'])
            if state is None or state['serial'] != token['serial']:
                return {'state': 'partial', 'events': [], 'cursor': None,
                        'gaps': [{'source': 'reader', 'reason': 'continuation_expired'}]}
            ident = token['id']
        else:
            ident = secrets.token_hex(16)
            state = {'created': stamp, 'serial': 0, 'positions': {s.name: None for s in self.sources},
                     'frontiers': {s.name: query.start for s in self.sources},
                     'next_source': 0, 'done': set(), 'pending': [], 'seen': set(), 'gaps': [], 'unsupported': {}}
        self.lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.lock_path.open('a') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {'state': 'busy', 'events': [], 'cursor': cursor, 'gaps': [], 'retry_after': 1}
            deadline = time.monotonic() + .25
            order=[(state['next_source']+offset)%len(self.sources) for offset in range(len(self.sources))]
            for index in order:
                source=self.sources[index]
                if source.name in state['done']:
                    continue
                if time.monotonic() >= deadline-.005:
                    break
                state['next_source']=(index+1)%len(self.sources)
                try:
                    part = source.scan(query.start, query.end, state['positions'][source.name],
                        lines=max(1, self.line_limit // len(self.sources)),
                        byte_limit=self.byte_limit // len(self.sources),
                        deadline=deadline-.005)
                    state['positions'][source.name] = part['position']
                    state['frontiers'][source.name] = part['frontier']
                    state['gaps'].extend(part['gaps'])
                    if part.get('rejected'):
                        state['unsupported'][source.name] = state['unsupported'].get(source.name, 0) + part['rejected']
                    if part['done']:
                        state['done'].add(source.name)
                    for at, raw in part['rows']:
                        try:
                            value = validate(raw)
                        except (ValueError, TypeError):
                            continue
                        if query.matches(value) and value['event_id'] not in state['seen']:
                            state['seen'].add(value['event_id'])
                            if not stacks:
                                value.pop('stack', None)
                            state['pending'].append({**value, 'ingested_at': at})
                except TimeoutError:
                    # Keep its log position and rotate the next page's first source.
                    continue
                except (OSError, ValueError):
                    state['gaps'].append({'source': source.name, 'reason': 'unavailable'})
                    state['done'].add(source.name)
                    state['frontiers'][source.name] = query.end
            frontier = min(state['frontiers'].values())
            pending = sorted(state['pending'], key=lambda value: (value['ingested_at'], value['event_id']))
            events: list[dict[str, Any]] = []
            retained: list[dict[str, Any]] = []
            used = 0
            for value in pending:
                size = len(json.dumps(value).encode())
                if value['ingested_at'] <= frontier and len(events) < 50 and used + size <= 128 * 1024:
                    events.append(value)
                    used += size
                else:
                    retained.append(value)
            state['pending'] = retained
            complete = len(state['done']) == len(self.sources) and not retained
            status = 'partial' if state['gaps'] else 'complete' if complete else 'continued'
            state['serial'] += 1
            self.cache[ident] = state
            self.cache.move_to_end(ident)
            # Include seen IDs in the memory budget; bounded cache eviction is explicit.
            def weight() -> int:
                return sum(len(json.dumps({**s, 'done': list(s['done']), 'seen': list(s['seen'])}).encode())
                           for s in self.cache.values())
            while self.cache and weight() > 6 * 1024**2:
                self.cache.popitem(last=False)
            if not complete and ident not in self.cache:
                status = 'partial'
                state['gaps'].append({'source': 'reader', 'reason': 'continuation_evicted'})
            continuation = None if complete or ident not in self.cache else self._sign(
                {'id': ident, 'serial': state['serial'], 'shape': shape, 'generations': generations})
            result = {'state': status, 'events': events, 'cursor': continuation, 'gaps': list(state['gaps']),
                      'unsupported_records': dict(state['unsupported'])}
            self.responses[response_key] = stamp, copy.deepcopy(result)
            while self.responses and sum(len(json.dumps(saved).encode()) for saved in self.responses.values()) > 2 * 1024**2:
                self.responses.popitem(last=False)
            return result
