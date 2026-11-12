"""Locked durable network starts, shared across producers and process restarts."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Callable
import fcntl
import math
import os
from pathlib import Path
import time
from typing import Any, Iterator

from library.postgres import Database, guards
from library.codec import encode, decode

from booksys_core.errors import CollectionError


class BudgetWait(CollectionError):
    def __init__(self, due: float):
        self.due = due
        super().__init__('Request budget exhausted')


@contextmanager
def lane(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CollectionError('Shared network lane busy') from exc
        yield


class RollingBudget:
    def __init__(self, path: Path):
        self.path = path
        self._database = Database.configured(path.parent)
        self.scope = path.name

    @classmethod
    def create(cls, path: Path, *, requests: int, period: float, spacing: float = 0) -> RollingBudget:
        if type(requests) is not int or not 1 <= requests <= 1000 or not math.isfinite(period) or period <= 0 or not math.isfinite(spacing) or spacing < 0:
            raise CollectionError('Invalid network budget')
        result=cls(path)
        with result._database.connection(write=True) as db:
            db.execute('INSERT INTO network_budgets VALUES (%s,%s,%s,%s,false,0,0)',
                       (result.scope,requests,period,spacing))
        if path.name == 'native-admission.yaml':
            NativeMutation.create(path.parent)
        return result

    @contextmanager
    def _transaction(self, *, write: bool = False) -> Iterator[dict[str,Any]]:
        with self._database.connection(write=write) as db:
            if write:
                guards(db,{'network-budget:'+self.scope})
            row=db.execute('SELECT requests,period,spacing,review_block,last,not_before FROM network_budgets WHERE scope=%s',(self.scope,)).fetchone()
            if row is None:
                raise CollectionError('Network authority unavailable')
            value=dict(zip(('requests','period','spacing','review_block','last','not_before'),row))
            value.update(schema=1,starts=[row[0] for row in db.execute('SELECT at FROM network_starts WHERE scope=%s ORDER BY position',(self.scope,))])
            self._validate(value)
            before=encode(value)
            yield value
            if write and encode(value)!=before:
                db.execute('UPDATE network_budgets SET review_block=%s,last=%s,not_before=%s WHERE scope=%s',
                           (value['review_block'],value['last'],value['not_before'],self.scope))
                db.execute('DELETE FROM network_starts WHERE scope=%s',(self.scope,))
                db.cursor().executemany('INSERT INTO network_starts(scope,at) VALUES (%s,%s)',[(self.scope,start) for start in value['starts']])

    @staticmethod
    def _validate(value: dict[str,Any]) -> None:
        if (value['schema'] != 1 or type(value['requests']) is not int or not 1 <= value['requests'] <= 1000
                or value['period'] <= 0 or value['spacing'] < 0 or len(value['starts']) > value['requests']
                or any(not math.isfinite(x) or x < 0 for x in
                       [value['period'],value['spacing'],value['last'],value['not_before'],*value['starts']])):
            raise CollectionError('Network authority unavailable')

    def _read(self) -> dict[str,Any]:
        with self._transaction() as value:
            return value

    def next_due(self, *, timestamp: float | None = None, reserve: int = 1) -> float:
        """Read when `reserve` requests fit the window, without spending the allowance.

        An operation that needs several calls passes its count so it does not start
        with one free slot and fail midway.
        """
        stamp = time.time() if timestamp is None else timestamp
        if not math.isfinite(stamp) or stamp < 0:
            raise CollectionError('Invalid request time')
        if type(reserve) is not int or reserve < 1:
            raise CollectionError('Invalid request reserve')
        with self._transaction(write=True) as value:
            if value.get('review_block', False):
                return math.inf
            starts = sorted(start for start in value['starts'] if start > stamp - value['period'])
            due = max(value['last'], value['not_before'],
                      value['last'] + value['spacing'] if value['starts'] else 0)
            wanted = min(reserve, value['requests'])
            overflow = len(starts) + wanted - value['requests']
            if overflow > 0:
                due = max(due, starts[overflow - 1] + value['period'])
            return float(due)

    def debit(self, *, timestamp: float | None = None) -> None:
        stamp = time.time() if timestamp is None else timestamp
        if not math.isfinite(stamp) or stamp < 0:
            raise CollectionError('Invalid request time')
        with self._transaction(write=True) as value:
            if value.get('review_block', False):
                raise BudgetWait(math.inf)
            if stamp < value['last']:
                raise BudgetWait(value['last'])
            starts = [s for s in value['starts'] if s > stamp - value['period']]
            due = max(value['not_before'], value['last'] + value['spacing'] if value['starts'] else 0)
            if len(starts) >= value['requests']:
                due = max(due, min(starts) + value['period'])
            if stamp < due:
                raise BudgetWait(due)
            value.update(starts=[*starts, stamp], last=stamp)

    def defer(self, due: float) -> None:
        if not math.isfinite(due) or due < 0:
            raise CollectionError('Invalid remote deadline')
        with self._transaction(write=True) as value:
            value['not_before'] = max(due, value['not_before'])

    def remote_deadline(self, header: str | None) -> None:
        from booksys_core.browser_outcomes import retry_after
        stamp = time.time()
        state, delay = retry_after(header, received_at=stamp)
        if state in ('invalid', 'unrepresentable'):
            with self._transaction(write=True) as value:
                value['review_block'] = True
        else:
            self.defer(stamp + (delay if delay is not None else 60))


def configured_budget(name: str) -> RollingBudget | None:
    directory = os.environ.get('BOOKSYS_ADMISSION_STATE')
    return RollingBudget(Path(directory) / (name + '-admission.yaml')) if directory else None


before_native_write: ContextVar[Callable[[], None] | None] = ContextVar('before_native_write', default=None)


current_native: ContextVar[NativeMutation | None] = ContextVar('current_native', default=None)


class NativeMutation:
    """One shared native effect pointer; domain journals own intent and receipt."""
    def __init__(self, directory: Path, owner: str, journal: Path):
        import hashlib
        self.directory = directory
        self.owner = hashlib.sha256(owner.encode()).hexdigest()
        self.journal = journal
        self.path = directory / 'native-effects.yaml'
        self._database = Database.configured(directory)

    @classmethod
    def create(cls, directory: Path) -> None:
        with Database.configured(directory).connection(write=True) as db:
            db.execute("INSERT INTO native_effect VALUES (1,%s)",(encode({'schema':1,'state':'clear'}),))

    def read(self) -> dict[str, Any]:
        with self._database.connection() as db:
            row=db.execute('SELECT payload FROM native_effect WHERE singleton=1').fetchone()
            if row is None:
                raise CollectionError('Native effect authority unavailable')
            value=decode(row[0])
            if value.get('schema')!=1 or value.get('state') not in ('clear','uncertain','resolved'):
                raise CollectionError('Native effect authority unavailable')
            return dict(value)

    def _retain(self, value: dict[str,Any]) -> None:
        # The caller holds the process-native lane through the effect/readback.
        with self._database.connection(write=True) as db:
            guards(db,{'native-pyload-effect'})
            if db.execute('UPDATE native_effect SET payload=%s WHERE singleton=1 RETURNING singleton',(encode(value),)).fetchone() is None:
                raise CollectionError('Native effect authority unavailable')

    @contextmanager
    def hold(self) -> Iterator[NativeMutation]:
        with lane(self.directory / 'native-mutation.lock'):
            self.read()
            token = current_native.set(self)
            try:
                yield self
            finally:
                current_native.reset(token)

    def prepare(self) -> None:
        value = self.read()
        if value['state'] == 'uncertain':
            raise CollectionError('Native mutation needs exact domain readback; no resend')
        self._retain({'schema': 1, 'state': 'uncertain',
            'owner': self.owner, 'journal': str(self.journal.absolute())})

    def resolve(self, proof: str) -> None:
        if not proof:
            raise CollectionError('Native receipt is required')
        value = self.read()
        if value['state'] == 'uncertain' and (value.get('owner') != self.owner or value.get('journal') != str(self.journal.absolute())):
            raise CollectionError('Another native mutation needs reconciliation')
        self._retain({'schema': 1, 'state': 'resolved',
            'owner': self.owner, 'journal': str(self.journal.absolute()), 'proof': proof})


def native_receipt(proof: str) -> None:
    mutation = current_native.get()
    if mutation is not None:
        mutation.resolve(proof)


@contextmanager
def native_operation(client: Any, owner: str, journal: Path) -> Iterator[None]:
    operation = getattr(client, 'mutation', None)
    if operation is None:
        yield
    else:
        with operation(owner, journal):
            yield
