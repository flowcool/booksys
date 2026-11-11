"""Durable collection episode policy. The owner persists records under pipeline.lock.

No method sleeps, acquires a processing lock or contacts an external service.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
from typing import Any
import uuid
from collections.abc import Callable
from pathlib import Path
import time
import yaml

from booksys_core.browser_outcomes import Outcome, BrowserOutcome
from library.operational_state import CollectorState
from booksys_core.errors import BrowserError, CollectionError
from booksys_core.diagnostics import emit


def utc_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


from booksys_core.values import seconds as seconds


class Deferred(CollectionError):
    def __init__(self, record: dict[str, Any]):
        super().__init__(record.get('wait_reason') or 'processing_lock_busy')
        self.record = copy.deepcopy(record)


class Schedule:
    def __init__(self, record: dict[str, Any], *, timestamp: float):
        if record:
            required = {'schema', 'cycle_id', 'state', 'started_at', 'budget_renews_at', 'busy_used',
                        'not_before', 'wait_reason', 'wait_origin', 'remote_deadline', 'review_block',
                        'request_ref', 'phase', 'last_invalid_ref', 'repeat_count'}
            if not required <= set(record) <= required | {'cleanup_recovery'} or record['schema'] != 1 or type(record['busy_used']) is not int or not 0 <= record['busy_used'] <= 2:
                raise CollectionError('Invalid durable schedule')
            receipt = record.get('cleanup_recovery')
            if receipt is not None:
                if (not isinstance(receipt, dict) or set(receipt) != {'at', 'status', 'proof'}
                        or receipt['status'] not in ('checking', 'failed', 'qualified')
                        or not isinstance(receipt['proof'], str) or len(receipt['proof']) > 256):
                    raise CollectionError('Invalid browser recovery receipt')
                seconds(receipt['at'])
            if (type(record['review_block']) is not bool
                    or type(record['repeat_count']) is not int or record['repeat_count'] < 0
                    or record['state'] not in ('ready', 'working', 'waiting', 'blocked')
                    or record['wait_origin'] not in ('local', 'remote', 'unknown')
                    or record['phase'] not in (None, 'read')
                    or not isinstance(record['cycle_id'], str)
                    or not record['cycle_id'] or len(record['cycle_id']) > 128):
                raise CollectionError('Invalid durable schedule')
            for key in ('request_ref', 'last_invalid_ref'):
                ref = record[key]
                if ref is not None and (not isinstance(ref, str) or len(ref) != 64
                                        or any(c not in '0123456789abcdef' for c in ref)):
                    raise CollectionError('Invalid durable request reference')
            if record['remote_deadline'] != 'unrepresentable':
                seconds(record['remote_deadline'])
            self.record = copy.deepcopy(record)
            for key in ('started_at', 'budget_renews_at', 'not_before'):
                seconds(record[key])
        else:
            self.record = {}
            self._renew(timestamp)

    @property
    def next_due(self) -> float:
        return seconds(self.record['not_before'])

    def _renew(self, stamp: float) -> None:
        recovery = self.record.get('cleanup_recovery')
        last_invalid = self.record.get('last_invalid_ref')
        repeats = self.record.get('repeat_count', 0)
        self.record = {'schema': 1, 'cycle_id': str(uuid.uuid4()), 'state': 'ready',
            'started_at': utc_timestamp(stamp), 'budget_renews_at': utc_timestamp(stamp + 900),
            'busy_used': 0, 'not_before': None, 'wait_reason': 'none', 'wait_origin': 'local',
            'remote_deadline': None, 'review_block': False, 'request_ref': None,
            'phase': None, 'last_invalid_ref': last_invalid, 'repeat_count': repeats}
        if recovery is not None:
            self.record['cleanup_recovery'] = recovery

    def before(self, ref: str, *, timestamp: float, reconstruction_calls: int = 0) -> None:
        record = self.record
        if record['review_block'] or timestamp < self.next_due:
            raise Deferred(record)
        if record['request_ref']:
            wanted = hashlib.sha256(ref.encode()).hexdigest()
            if wanted != record['request_ref'] or reconstruction_calls + 1 > 2 - record['busy_used']:
                self._defer_episode(timestamp)
                raise Deferred(self.record)
            record['busy_used'] += reconstruction_calls + 1
        elif timestamp >= max(seconds(record['budget_renews_at']), self.next_due):
            self._renew(timestamp)
        self.record['state'] = 'working'

    def _defer_episode(self, stamp: float) -> None:
        self.record.update(not_before=utc_timestamp(max(self.next_due, stamp + 900)),
                           request_ref=None, phase=None, state='waiting', wait_reason='browser_busy', wait_origin='local')

    def record_outcome(self, ref: str, outcome: Outcome, *, timestamp: float) -> None:
        record = self.record
        ident = hashlib.sha256(ref.encode()).hexdigest()
        record.update(state='waiting', wait_reason=outcome.code, wait_origin=outcome.origin)
        if outcome.code == 'browser_busy' and outcome.origin == 'local':
            if record['busy_used'] >= 2:
                self._defer_episode(timestamp)
            else:
                record.update(request_ref=ident, phase='read', not_before=utc_timestamp(max(self.next_due, timestamp + 60)))
            return
        record.update(request_ref=None, phase=None)
        due = timestamp + 900
        if outcome.retry_after_state == 'unrepresentable':
            record.update(remote_deadline='unrepresentable', review_block=True, state='blocked')
        elif outcome.retry_after_state == 'valid' and outcome.retry_after_seconds is not None:
            deadline = timestamp + outcome.retry_after_seconds
            existing = record.get('remote_deadline')
            if existing != 'unrepresentable':
                record['remote_deadline'] = utc_timestamp(max(seconds(existing), deadline))
            due = max(due, deadline)
            if outcome.retry_after_seconds > 86400:
                record.update(review_block=True, state='blocked')
        if outcome.code == 'cleanup_failed':
            record.update(review_block=True, state='blocked')
        if outcome.code == 'invalid_content':
            record['repeat_count'] = record['repeat_count'] + 1 if record['last_invalid_ref'] == ident else 1
            record['last_invalid_ref'] = ident
        record['not_before'] = utc_timestamp(max(self.next_due, due))

    def success(self, ref: str) -> None:
        if self.record['request_ref'] == hashlib.sha256(ref.encode()).hexdigest():
            self.record.update(request_ref=None, phase=None)
        # Success cannot release an unrelated origin deadline or review block.
        self.record['state'] = 'blocked' if self.record['review_block'] else 'ready'

    def release_remote_block(self, *, timestamp: float, cleanup_proof: str = '') -> None:
        if self.record['remote_deadline'] == 'unrepresentable' or timestamp < self.next_due:
            raise CollectionError('Remote safe deadline is not known or has not expired')
        if self.record['wait_reason'] == 'cleanup_failed':
            if not cleanup_proof or len(cleanup_proof) > 256:
                raise CollectionError('Browser cleanup requires qualified service recovery')
            self.record['cleanup_recovery'] = {'at': utc_timestamp(timestamp), 'status': 'qualified', 'proof': cleanup_proof}
        self.record.update(review_block=False, state='ready')


class ScheduledCalls:
    """Caller owns pipeline.lock for the complete invocation and journal merge.

    An exception unwinds to the outer scheduler; there is no retry sleep here.
    """
    def __init__(self, directory: Path, fetch: Callable[[str], str], *, clock: Callable[[], float] = time.time):
        self.directory, self.fetch, self.clock = directory, fetch, clock

    def _load(self) -> tuple[dict[str, Any], Schedule]:
        path = self.directory / 'monitor.yaml'
        value = CollectorState(self.directory).monitor()
        if not isinstance(value, dict) or value.get('version') != 1:
            raise CollectionError('Invalid monitor journal')
        return value, Schedule(value.get('schedule', {}), timestamp=self.clock())

    def _save(self, value: dict[str, Any], schedule: Schedule) -> None:
        value['schedule'] = schedule.record
        CollectorState(self.directory).patch_monitor(schedule=value['schedule'])

    def recover_cleanup(self) -> bool:
        """Only the collection actor probes; reserve backoff before external I/O."""
        from booksys_core.network_admission import lane
        from library.primitives import lock
        check = getattr(self.fetch, 'check_session', None)
        if check is None:
            return False
        # A healthy schedule needs no recovery lane. Recheck under both locks
        # below before any probe or state transition.
        _, current = self._load()
        if (not current.record['review_block'] or current.record['wait_reason'] != 'cleanup_failed'
                or self.clock() < current.next_due or current.record['remote_deadline'] == 'unrepresentable'):
            return False
        with lock(self.directory/'pipeline.lock', blocking=False), lane(self.directory/'browser-session.lock'):
            value, schedule = self._load()
            stamp = self.clock()
            if (not schedule.record['review_block'] or schedule.record['wait_reason'] != 'cleanup_failed'
                    or stamp < schedule.next_due or schedule.record['remote_deadline'] == 'unrepresentable'):
                return False
            previous_due = schedule.record['not_before']
            schedule.record['not_before'] = utc_timestamp(stamp + 900)
            schedule.record['cleanup_recovery'] = {'at': utc_timestamp(stamp), 'status': 'checking', 'proof': ''}
            self._save(value, schedule)
            try:
                proof = check()
                if not isinstance(proof, str) or not proof or len(proof) > 256:
                    raise CollectionError('Invalid browser recovery proof')
            except BrowserOutcome as exc:
                # Preserve a remote Retry-After observed by the qualification
                # request. Recovery never bypasses that service-wide deadline.
                schedule.record_outcome('cleanup-recovery', exc.outcome, timestamp=self.clock())
                schedule.record['wait_reason'] = 'cleanup_failed'
                schedule.record['cleanup_recovery']['status'] = 'failed'
                self._save(value, schedule)
                return False
            except (BrowserError, CollectionError, OSError, ValueError):
                schedule.record['cleanup_recovery']['status'] = 'failed'
                self._save(value, schedule)
                return False
            schedule.record['not_before'] = previous_due
            schedule.release_remote_block(timestamp=self.clock(), cleanup_proof=proof)
            self._save(value, schedule)
            emit(service='collector', stage='byparr', code='recovered')
            return True

    def call(self, ref: str, operation: Callable[[], Any], *, reconstruction_calls: int = 0) -> Any:
        from booksys_core.network_admission import lane
        with lane(self.directory / 'browser-session.lock'):
            return self._call(ref, operation, reconstruction_calls=reconstruction_calls)

    def _call(self, ref: str, operation: Callable[[], Any], *, reconstruction_calls: int = 0) -> Any:
        value, schedule = self._load()
        try:
            schedule.before(ref, timestamp=self.clock(), reconstruction_calls=reconstruction_calls)
        except Deferred:
            self._save(value, schedule)
            raise
        self._save(value, schedule)  # Consumed allowance is durable before the call.
        from booksys_core.values import digest
        refs = [{'kind': 'source', 'id': digest(ref)}]
        emit(service='collector', stage='byparr', code='attempt', refs=refs,
             cycle_id=schedule.record['cycle_id'], attempt=schedule.record['busy_used'])
        try:
            result = operation()
        except BrowserOutcome as exc:
            schedule.record_outcome(ref, exc.outcome, timestamp=self.clock())
            self._save(value, schedule)
            emit(service='collector', stage='byparr', code='blocked' if schedule.record['review_block'] else 'waiting',
                 reason=exc.outcome.code, refs=refs, cycle_id=schedule.record['cycle_id'],
                 next_attempt=schedule.record['not_before'], repeat_count=schedule.record['repeat_count'])
            if exc.outcome.code in ('browser_busy', 'remote_rate_limited', 'cleanup_failed'):
                raise Deferred(schedule.record) from None
            raise
        except BrowserError:
            schedule.record_outcome(ref, Outcome('invalid_content', 'remote'), timestamp=self.clock())
            self._save(value, schedule)
            raise
        schedule.success(ref)
        self._save(value, schedule)
        return result

    def __call__(self, url: str) -> str:
        def invoke() -> str:
            typed = getattr(self.fetch, 'fetch_html', None)
            if typed:
                _, schedule = self._load()
                ref = hashlib.sha256(url.encode()).hexdigest()
                request_id = schedule.record['cycle_id'] + ':' + ref[:16] + ':' + str(schedule.record['busy_used'])
                result: str = typed(ref, url, request_id)
                return result
            return self.fetch(url)
        result: str = self.call(url, invoke)
        return result

    def cover(self, url: str, source: str) -> tuple[bytes, str]:
        fetch_cover = getattr(self.fetch, 'cover', None)
        if fetch_cover is None:
            raise CollectionError('Cover adapter unavailable')
        needs_session = getattr(self.fetch, 'cover_needs_session', lambda _: False)(source)
        def operation() -> tuple[bytes, str]:
            if needs_session:
                # The allowance for reconstruction and resumed cover was saved
                # together before either call. Do not nest schedule acquisitions.
                self.fetch(source)
            result: tuple[bytes, str] = fetch_cover(url, source)
            return result
        result: tuple[bytes, str] = self.call(url, operation, reconstruction_calls=int(needs_session))
        return result
