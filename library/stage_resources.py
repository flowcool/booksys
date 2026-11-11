"""Persistent task ownership; optional legacy capacity admission."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TYPE_CHECKING

import json
from contextlib import contextmanager
from copy import deepcopy
from typing import Iterator
from library.postgres import Database, guards

from library.primitives import LibraryError
from library.stage_model import Task, instant, QUEUES

if TYPE_CHECKING:
    from library.stage_tasks import StageTasks


@dataclass(frozen=True)
class Demand:
    memory_mib: int = 0
    cpu_millis: int = 0
    disk: int = 0
    browser: int = 0
    native: int = 0
    output_bytes: int = 0

    def __post_init__(self) -> None:
        if any(type(value) is not int or value < 0 for value in asdict(self).values()):
            raise LibraryError('Invalid resource demand')


CAPACITY = Demand(memory_mib=2304, cpu_millis=2000, disk=1, browser=1, native=1)
RESERVE = 20 * 1024**3


class Resources:
    def __init__(self, path: Path, *, database: Database | None = None):
        self.path = path
        self._database = database or Database.configured(path.parent)
        self.pool = path.name

    @classmethod
    def create(cls, path: Path, *, capacity: Demand = CAPACITY, reserve_bytes: int = RESERVE,
               external_policy: str = 'bounded', database: Database | None = None) -> Resources:
        if (external_policy not in ('bounded', 'lab-reserve', 'unreserved') or capacity.output_bytes
                or any(asdict(capacity)[key] > value for key, value in asdict(CAPACITY).items())
                or type(reserve_bytes) is not int or reserve_bytes < (0 if external_policy == 'unreserved' else RESERVE)):
            raise LibraryError('Resource configuration exceeds qualified envelope')
        result = cls(path, database=database)
        with result._database.connection(write=True) as db:
            db.execute('INSERT INTO resource_pools VALUES (%s,%s,%s,%s)',
                       (result.pool, json.dumps(asdict(capacity)), reserve_bytes, external_policy))
        return result

    @contextmanager
    def _transaction(self, *, write: bool = False, key: str = '') -> Iterator[dict[str, Any]]:
        with self._database.connection(write=write) as db:
            row = db.execute('SELECT capacity,reserve_bytes,policy FROM resource_pools WHERE pool=%s', (self.pool,)).fetchone()
            if row is None:
                raise LibraryError('Resource authority unavailable; retain all grants')
            if write:
                # Accepted unreserved policy excludes only this exact owner.
                guards(db, {'resource-owner:' + self.pool + ':' + key} if row[2] == 'unreserved'
                       else {'resource-pool:' + self.pool})
            value: dict[str, Any] = {'schema': 1, 'capacity': json.loads(row[0]),
                'reserve_bytes': row[1], 'external_policy': row[2]}
            for table in ('grants', 'waiters'):
                value[table] = {ident: {'age': age, 'demand': json.loads(demand), **({'queue': queue} if queue else {})}
                    for ident, age, queue, demand in db.execute(
                        f'SELECT key,age,queue,demand FROM resource_{table} WHERE pool=%s ORDER BY key', (self.pool,))}
            capacity = Demand(**value['capacity'])
            if (capacity.output_bytes or value['external_policy'] not in ('bounded','lab-reserve','unreserved')
                    or any(asdict(capacity)[name] > ceiling for name,ceiling in asdict(CAPACITY).items())
                    or value['reserve_bytes'] < (0 if value['external_policy'] == 'unreserved' else RESERVE)):
                raise LibraryError('Resource authority contains an invalid envelope')
            for table in ('grants','waiters'):
                for record in value[table].values():
                    Demand(**record['demand'])
                    instant(datetime.fromisoformat(record['age']))
                    if record.get('queue','') not in ('',*QUEUES):
                        raise LibraryError('Resource authority contains an invalid queue')
            before = deepcopy(value)
            yield value
            if write:
                for table in ('grants', 'waiters'):
                    for ident in before[table].keys() - value[table].keys():
                        old = before[table][ident]
                        db.execute(f'DELETE FROM resource_{table} WHERE pool=%s AND key=%s AND age=%s AND queue=%s AND demand=%s',
                                   (self.pool,ident,old['age'],old.get('queue',''),json.dumps(old['demand'])))
                    for ident, record in value[table].items():
                        if before[table].get(ident) != record:
                            db.execute(f'INSERT INTO resource_{table} VALUES (%s,%s,%s,%s,%s) '
                                'ON CONFLICT(pool,key) DO UPDATE SET age=excluded.age,queue=excluded.queue,demand=excluded.demand',
                                (self.pool,ident,record['age'],record.get('queue',''),json.dumps(record['demand'])))

    def capacity(self) -> Demand:
        with self._transaction() as value:
            return Demand(**value['capacity'])

    def admit(self, key: str, *, age: datetime, demand: Demand, free_bytes: int,
              external_bytes: int | None, queue: str = '') -> str:
        """Return a stable wait reason or a complete durable grant. Never sleep."""
        fields = asdict(demand)
        if queue not in ('', *QUEUES) or not key or len(key) > 256 or free_bytes < 0 or (external_bytes is not None and external_bytes < 0):
            raise LibraryError('Invalid admission input')
        with self._transaction(write=True, key=key) as value:
            capacity, reserve = value['capacity'], value['reserve_bytes']
            output_allowed = external_bytes is not None or value.get('external_policy') == 'lab-reserve'
            record = {'age': instant(age), 'demand': fields}
            if queue:
                record['queue'] = queue
            for table in ('grants', 'waiters'):
                previous = value[table].get(key)
                if previous is not None and queue and 'queue' not in previous:
                    previous = dict(previous, queue=queue)
                if previous is not None and previous != record:
                    raise LibraryError('Grant identity changed' if table == 'grants' else 'Waiting demand changed')
            if key in value['grants']:
                return ''
            if value.get('external_policy') == 'unreserved':
                # These records authorize one exact acting owner and its reader.
                # They do not reserve CPU, memory, disk I/O or output space.
                value['waiters'].pop(key, None)
                value['grants'][key] = record
                return ''
            reason = ''
            if any(fields[name] > maximum for name, maximum in capacity.items() if name != 'output_bytes'):
                reason = 'blocked_capacity'
            # Disk I/O is not output growth. The explicit lab policy uses measured
            # free space and whole output grants; it does not bound pyLoad writes.
            elif demand.output_bytes and not output_allowed:
                reason = 'external_capacity_unknown'
            else:
                used = {name: sum(grant['demand'][name] for grant in value['grants'].values()) for name in fields}
                if demand.output_bytes and free_bytes - used['output_bytes'] - demand.output_bytes - (external_bytes or 0) < reserve:
                    reason = 'output_capacity'
                else:
                    older = [waiting for other, waiting in value['waiters'].items()
                             if (not queue or waiting.get('queue') == queue)
                             and (waiting['age'], other) < (record['age'], key)
                             and (output_allowed or not waiting['demand']['output_bytes'])
                             and (not waiting['demand']['output_bytes'] or
                                  output_allowed and free_bytes - used['output_bytes']
                                  - waiting['demand']['output_bytes'] - (external_bytes or 0) >= reserve)]
                    # Reserve the oldest waiters' required capacity, while
                    # allowing surplus that cannot delay their next release.
                    if any((fields['output_bytes'] and waiter['demand']['output_bytes']
                               and free_bytes - used['output_bytes'] - fields['output_bytes']
                               - waiter['demand']['output_bytes'] - (external_bytes or 0) < reserve)
                           or any(fields[name] and waiter['demand'][name]
                               and max(used[name], waiter['demand'][name]) + fields[name] > maximum
                               for name, maximum in capacity.items() if name != 'output_bytes')
                           for waiter in older):
                        reason = 'older_waiter'
                    elif any(used[name] + fields[name] > maximum for name, maximum in capacity.items() if name != 'output_bytes'):
                        reason = 'resource_wait'
            if reason in ('resource_wait', 'older_waiter', 'output_capacity'):
                value['waiters'][key] = record
            elif reason:
                value['waiters'].pop(key, None)
            else:
                value['waiters'].pop(key, None)
                value['grants'][key] = record
            return reason

    def cancel_wait(self, key: str) -> None:
        with self._transaction(write=True, key=key) as value:
            value['waiters'].pop(key, None)

    def grants(self) -> list[str]:
        with self._transaction() as value:
            return list(value['grants'])

    def reservation(self, key: str) -> Demand | None:
        """Read one complete retained grant without creating or extending it."""
        with self._transaction() as value:
            record = value['grants'].get(key)
            return Demand(**record['demand']) if record else None

    def waiters(self) -> list[str]:
        with self._transaction() as value:
            return list(value['waiters'])

    def prune_waiters(self, tasks: StageTasks, queue: str, *, now: datetime) -> Task | None:
        """Resolve this queue's journals outside the shared resource lock."""
        with self._transaction() as value:
            waiting = value['waiters']
        owners = tasks._waiter_tasks({grant.rsplit(':', 1)[0] for grant in waiting}, queue)
        stale = {}
        oldest = None
        for grant, record in waiting.items():
            key, generation = grant.rsplit(':', 1)
            task = owners.get(key)
            if task is None and key in owners:
                continue  # This key belongs to another queue, whose payload was not read.
            if (task is None or task.status != 'pending' or task.blocked or task.superseded_by
                    or int(generation) != task.generation + 1 or task.due_at > instant(now)):
                stale[grant] = record
            elif task.wait_until is None or task.wait_until <= instant(now):
                if oldest is None or (task.first_eligible_at or task.due_at, task.key) < (oldest.first_eligible_at or oldest.due_at, oldest.key):
                    oldest = task
        if not stale:
            return oldest
        with self._transaction(write=True, key='prune') as value:
            changed = False
            for grant, record in stale.items():
                if value['waiters'].get(grant) == record:
                    del value['waiters'][grant]
                    changed = True
        return oldest

    def release(self, key: str, *, stopped: bool, children_stopped: bool) -> None:
        if not stopped or not children_stopped:
            raise LibraryError('Acting owner and decoder cleanup must be proved')
        with self._transaction(write=True, key=key) as value:
            value['grants'].pop(key, None)
