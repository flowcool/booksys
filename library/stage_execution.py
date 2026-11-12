"""Acting-process ownership and domain fences. No heartbeat or lease expiry."""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
import fcntl
import os
from pathlib import Path
from library.postgres import DeadlineConnection
from contextvars import ContextVar
from typing import Any
from typing import IO, Iterator

from library.locking import effect_guard, commit_guard, mutation_guard
from booksys_core.network_admission import before_native_write
from library.primitives import LibraryError, digest, lock
from library.stage_model import Task, QUEUES
from library.stage_tasks import StageTasks


class QueueOwner:
    def __init__(self, directory: Path, queue: str):
        if queue not in QUEUES:
            raise LibraryError('Unknown actor queue')
        self.directory, self.queue = directory, queue
        self.stream: IO[str] | None = None
        self.pid = os.getpid()

    def __enter__(self) -> QueueOwner:
        if self.stream is not None:
            raise LibraryError('Owner already acquired')
        self.directory.mkdir(parents=True, exist_ok=True)
        stream = (self.directory / ('stage-' + self.queue + '.lock')).open('a')
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            stream.close()
            raise LibraryError('Queue acting process still owns exclusion') from exc
        self.stream = stream
        self.pid = os.getpid()
        return self

    def check(self, queue: str) -> None:
        if self.stream is None or self.stream.closed or self.pid != os.getpid() or self.queue != queue:
            raise LibraryError('Acting process does not own queue exclusion')

    def __exit__(self, *args: object) -> None:
        if self.stream is not None:
            self.stream.close()
            self.stream = None


@contextmanager
def acting(owner: QueueOwner, tasks: StageTasks, task: Task) -> Iterator[None]:
    """The actual process retains its OS lock across effects and commits."""
    def check(db: DeadlineConnection) -> None:
        owner.check(task.stage)
        tasks._check(db)
        current = tasks._get(db, task.key)
        if (current is None or current.generation != task.generation or current.status != 'active'
                or current.blocked or current.superseded_by):
            raise LibraryError('Stale acting attempt')
        tasks._verify_source(db,current)

    def before_effect() -> None:
        with tasks.store._database.connection() as db:
            check(db)

    before_effect()
    effect_token = effect_guard.set(before_effect)
    commit_token = commit_guard.set(check)
    native_token = before_native_write.set(before_effect)
    mutation_token = mutation_guard.set(lambda change: effect_scopes(tasks.store.directory, change_scopes(change)))
    try:
        yield
    finally:
        mutation_guard.reset(mutation_token)
        before_native_write.reset(native_token)
        commit_guard.reset(commit_token)
        effect_guard.reset(effect_token)


@contextmanager
def effect_scopes(directory: Path, scopes: list[str]) -> Iterator[None]:
    """Acquire the complete sorted entity/destination set without waiting."""
    held = held_scopes.get()
    names = {str(directory.absolute() / 'stage-scopes' / (digest(scope) + '.lock')) for scope in scopes}
    with ExitStack() as stack:
        for name in sorted(names - held):
            stack.enter_context(lock(Path(name), blocking=False))
        token = held_scopes.set(held | names)
        try:
            yield
        finally:
            held_scopes.reset(token)


held_scopes: ContextVar[set[str]] = ContextVar('held_scopes', default=set())


def record_scopes(state: dict[str, Any]) -> list[str]:
    return [kind.removesuffix('s') + ':' + ident for kind in ('works', 'editions', 'variants', 'assets', 'artifacts', 'origins')
            for ident in state.get(kind, {})]


def change_scopes(change: Any) -> list[str]:
    scopes = [kind.removesuffix('s') + ':' + ident for kind in set(change.upserts) | set(change.deletes)
              for ident in set(change.upserts.get(kind, {})) | set(change.deletes.get(kind, []))]
    for kind, records in change.upserts.items():
        for record in records.values():
            for field, prefix in (('work', 'work'), ('edition', 'edition'), ('asset', 'asset'), ('parent_origin', 'origin')):
                if isinstance(record.get(field), str):
                    scopes.append(prefix + ':' + record[field])
            if kind == 'origins' and record.get('sha256'):
                scopes.append('asset:' + record['sha256'])
            for oid in record.get('origins', []):
                scopes.append('origin:' + oid)
    return scopes
