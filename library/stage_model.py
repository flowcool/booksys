"""Stable domain task records shared by SQL claims and source policy."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from typing import Any, TYPE_CHECKING
if TYPE_CHECKING:
    from library.operational_state import OperationalCompletion

from library.primitives import LibraryError, digest

QUEUES = ('collection', 'intake', 'metadata', 'copies', 'routing', 'retirement')
# Bump only the method whose useful evidence or unsuccessful outcomes changed.
# Repository revisions and process restarts are deliberately absent.
METHODS = dict(metadata='metadata-step-v1', copies='admission-step-v2-nfs',
               intake='intake-step-v1', collection='collection-step-v1', routing='routing-step-v1',
               retirement='retirement-step-v2-entry-wait', manual='manual-apply-v1')


def instant(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise LibraryError('Task time must include UTC offset')
    return value.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class Obligation:
    stage: str
    entity: str
    input_revision: str
    method: str
    due_at: datetime | None = None
    blocked: str = ''
    dependencies: dict[str, Any] | None = None
    source: str = ''
    replay: bool = False

    @property
    def key(self) -> str:
        if self.stage not in QUEUES or any(not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:/@+-]{1,256}', value) for value in (self.entity, self.input_revision, self.method)):
            raise LibraryError('Invalid domain obligation')
        return digest({'schema': 1, 'stage': self.stage, 'entity': self.entity,
                       'input_revision': self.input_revision})


@dataclass
class Task:
    key: str
    stage: str
    entity: str
    input_revision: str
    method: str
    first_eligible_at: str | None
    due_at: str
    blocked: str = ''
    dependencies: dict[str, Any] | None = None
    source: str = ''
    generation: int = 0
    attempts: int = 0
    prestart_failures: int = 0
    status: str = 'pending'
    episode: int = 0
    reconciliation: str = ''
    policy_sequence: int = 0
    superseded_by: str = ''
    technical_dependency: str = ''
    dependency_failed_at: float = 0
    recovery_version: str = ''
    completed_at: str | None = None
    wait_reason: str = ''
    wait_until: str | None = None
    source_guard: str = ''
    native_attempt: dict[str, Any] | None = None


    def __post_init__(self) -> None:
        if self.key != Obligation(self.stage, self.entity, self.input_revision, self.method).key:
            raise LibraryError('Invalid task identity')
        if self.status not in ('pending', 'active', 'done', 'blocked', 'superseded'):
            raise LibraryError('Invalid task status')
        if any(type(value) is not int or value < 0 for value in
               (self.generation, self.attempts, self.prestart_failures, self.episode, self.policy_sequence)):
            raise LibraryError('Invalid task allowance')
        for value in (self.first_eligible_at, self.due_at):
            if value is not None and instant(datetime.fromisoformat(value)) != value:
                raise LibraryError('Noncanonical task time')


@dataclass
class Selection:
    task: Task | None = None
    waits: dict[str, str] = field(default_factory=dict)
    eligible: int = 0
    continued: bool = False
    # Set only when a bounded window held no selectable task and more candidates
    # remain: the caller resumes after the last examined candidate, never from the head.
    after: list[str] | None = None


@dataclass(frozen=True)
class StepResult:
    outcome: str = 'done'
    due_at: datetime | None = None
    prepared: PreparedResult | None = None
    successors: list[Obligation] = field(default_factory=list)
    source_progress: SourceProgress | None = None


@dataclass
class Change:
    upserts: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    deletes: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class PreparedResult:
    """Detached domain result. Providers and media work have already finished."""
    change: Change
    conflict: dict[str, Any] | None
    revision: int
    completions: list[OperationalCompletion] = field(default_factory=list)


@dataclass(frozen=True)
class SourceProgress:
    source: str
    entity: str
    input_revision: str
    method: str
    guard: str
    result: dict[str, Any]
    completions: list[OperationalCompletion] = field(default_factory=list)



SOURCE_PAGE = 256

@dataclass(frozen=True)
class Scan:
    processed: int = 0
    continued: bool = False
    blocked: str = ''
