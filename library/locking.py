"""A bounded acquisition context for controller decision lanes."""
from __future__ import annotations
from contextlib import contextmanager, AbstractContextManager
from contextvars import ContextVar
from collections.abc import Iterator, Callable
from typing import Any, TYPE_CHECKING
if TYPE_CHECKING:
    from library.postgres import DeadlineConnection

acquisition_deadline: ContextVar[float | None] = ContextVar('acquisition_deadline', default=None)


@contextmanager
def bounded_acquisition(deadline: float) -> Iterator[None]:
    token = acquisition_deadline.set(deadline)
    try:
        yield
    finally:
        acquisition_deadline.reset(token)

# These callbacks carry the current domain attempt into existing adapters.
# They are process-local fences, never ownership or a renewable lease.
effect_guard: ContextVar[Callable[[], None] | None] = ContextVar('effect_guard', default=None)
commit_guard: ContextVar[Callable[[DeadlineConnection], None] | None] = ContextVar('commit_guard', default=None)

def check_effect() -> None:
    guard = effect_guard.get()
    if guard is not None:
        guard()

mutation_guard: ContextVar[Callable[[Any], AbstractContextManager[None]] | None] = ContextVar('mutation_guard', default=None)


@contextmanager
def defer_contention() -> Iterator[None]:
    """Retain claims and receipts for the next pass without replaying effects."""
    from booksys_core.errors import LibraryError
    try:
        yield
    except LibraryError as exc:
        if str(exc) != 'storage_busy':
            raise
        from booksys_core.diagnostics import emit, exception_details
        emit(service='library',stage='diagnostics',code='waiting',
             **{**exception_details(exc),'reason':'storage_busy'})
