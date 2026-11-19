"""Strict technical events and bounded, nonblocking JSONL emission."""
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from datetime import datetime, timezone
from functools import lru_cache
import json
import os
import queue
import re
import threading
import time
from typing import Any
import uuid

SERVICES = ('collector', 'byparr', 'library', 'retirement', 'diagnostic-reader')
STAGES = ('collection', 'byparr', 'downloads', 'local', 'identity', 'publication', 'retirement', 'diagnostics')
CODES = ('started', 'completed', 'waiting', 'attempt', 'failed', 'recovered', 'progress', 'degraded', 'blocked')
REASONS = frozenset(('none', 'unknown', 'browser_busy', 'remote_rate_limited', 'remote_http_failure',
    'browser_timeout', 'invalid_content', 'cleanup_failed', 'unknown_browser_failure', 'processing_lock_busy',
    'provider_unavailable', 'provider_timeout', 'operation_timeout', 'provider_http_failure', 'provider_cooldown', 'provider_request_invalid', 'provider_budget', 'identity_unknown', 'identity_conflict', 'transfer_wait',
    'transfer_failed', 'invalid_archive', 'unsupported_input', 'decoder_timeout', 'source_drift',
    'ownership_conflict', 'ledger_invalid', 'uncertain_external_write', 'disk_reserve', 'disk_full',
    'schema_invalid', 'authority_mismatch', 'storage_busy', 'diagnostic_unavailable', 'unknown_effect', 'submission_refused',
    'downloader_paused', 'catalogue_order_invalid', 'pagination_invalid', 'idle', 'resource_wait', 'recovery_wait', 'missing_dependency',
    'unexpected_exception', 'provider_wait', 'inputs_changed', 'operator_decision_pending',
    'domain_prerequisites', 'source_policy_changed', 'nas_unavailable', 'conflict_wait',
    'blocked_capacity', 'external_capacity_unknown', 'output_capacity', 'older_waiter',
    'queue_snapshot_unavailable', 'native_package_unavailable', 'routing_prerequisites',
    'retirement_prerequisites', 'native_prerequisites_unavailable', 'reconciliation_required'))
REF_KINDS = ('source', 'package', 'file', 'origin', 'asset', 'decision', 'operation', 'task')
ERRORS = frozenset(('TimeoutError', 'OSError', 'ValueError', 'TypeError', 'KeyError', 'LibraryError',
                   'CollectionError', 'Deferred', 'BrowserOutcome', 'NativeRefusal', 'HTTPError', 'URLError', 'OperationalError', 'IntegrityError', 'RuntimeError', 'FileNotFoundError', 'Exception', 'ReaderUnavailable', 'PermissionError', 'NotADirectoryError', 'IsADirectoryError', 'BrokenPipeError', 'ConnectionError', 'ConnectionResetError', 'ConnectionRefusedError', 'ImportError', 'ModuleNotFoundError', 'MemoryError', 'TimeoutExpired'))
FIELDS = frozenset(('schema', 'event_id', 'at', 'service', 'stage', 'code', 'level', 'reason', 'cycle_id',
    'request_id', 'operation_id', 'refs', 'title', 'duration_ms', 'wait_ms', 'attempt', 'repeat_count',
    'dropped_count', 'next_attempt', 'exception_type', 'message', 'cause_type', 'detail', 'stack', 'measurements', 'queue', 'outcome', 'rejected_count', 'dependency', 'http_status', 'task_state'))
REQUIRED = frozenset(('schema', 'event_id', 'at', 'service', 'stage', 'code', 'level', 'reason', 'refs'))


_urls = re.compile(r'(?i)(?:https?://|www\.)\S+')
_secrets = re.compile(r'(?i)(?:authorization\s*:\s*(?:bearer\s+)?|(?:password|api[_-]?key|token|secret)\s*[:=]\s*)\S+')


def _filter_text(value: str, limit: int) -> str:
    value = _urls.sub('[private URL]', value)
    value = _secrets.sub('[private field]', value)
    if not value.isprintable():
        value = ''.join(char if char.isprintable() else ' ' for char in value)
    return value[:limit]


@lru_cache(maxsize=4096)
def _short_text(value: str, limit: int) -> str:
    # At most512 input/output characters per cache entry. Long source text is
    # never retained in this bounded metadata optimization.
    return _filter_text(value, limit)


def safe_text(value: str, limit: int = 240) -> str:
    return _short_text(value, limit) if len(value) <= 512 else _filter_text(value, limit)


# Messages are untrusted. Keep /data paths only, with lexical traversal checks.
_message_paths = re.compile(
    r"'(?:/|[A-Za-z]:[\\/])[^']*'|\"(?:/|[A-Za-z]:[\\/])[^\"]*\"|"
    r"(?<![\w/])(?:/|[A-Za-z]:[\\/])[^\s'\"<>,;)\]}]*")
_message_secrets = re.compile(
    r'(?i)(?:authorization\s*:\s*(?:(?:bearer|basic)\s+)?|bearer\s+|'
    r'(?:password|api[_-]?key|(?:access[_-]?|refresh[_-]?)?token|secret)\s*[:=]\s*)'
    r"(?:'[^']*'|\"[^\"]*\"|[^\s,;]+)")


def _exception_message(value: str) -> str:
    from posixpath import normpath

    def path(match: re.Match[str]) -> str:
        raw = match.group().strip("'\"")
        normalized = normpath(raw)
        return match.group() if (raw == '/data' or raw.startswith('/data/')) and (
            normalized == '/data' or normalized.startswith('/data/')) else '[private path]'

    value = _urls.sub('[private URL]', value)
    value = _message_secrets.sub('[private field]', value)
    value = safe_text(_message_paths.sub(path, value), 300)
    # Truncation can leave an incomplete /data prefix. Filter that tail again.
    return safe_text(_message_paths.sub(path, value), 300)


def utc(value: Any) -> bool:
    if type(value) is not str or len(value) > 40:
        return False
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        offset = stamp.utcoffset()
        return offset is not None and offset.total_seconds() == 0
    except ValueError:
        return False


def opaque(value: Any) -> bool:
    return type(value) is str and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value) is not None


def validate(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - FIELDS or REQUIRED - set(value):
        raise ValueError('Unknown or missing event fields')
    if type(value['schema']) is not int or value['schema'] != 1 or not utc(value['at']):
        raise ValueError('Invalid event schema or time')
    if not isinstance(value['event_id'], str) or not re.fullmatch(
            r'(?:[0-9a-f]{64}|[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})', value['event_id']):
        raise ValueError('Invalid event identity')
    for key, options in (('service', SERVICES), ('stage', STAGES), ('code', CODES),
                         ('level', ('INFO', 'WARNING', 'ERROR', 'CRITICAL')), ('reason', REASONS)):
        if type(value[key]) is not str or value[key] not in options:
            raise ValueError('Invalid controlled event field')
    for key, task_options in (('queue', ('collection', 'intake', 'ocr', 'metadata', 'copies', 'routing', 'retirement', 'discovery', 'serial')),
                         ('outcome', ('done', 'retry', 'defer', 'uncertain', 'stopped', 'absent'))):
        if key in value and value[key] not in task_options:
            raise ValueError('Invalid controlled task field')
    if 'dependency' in value and value['dependency'] not in ('bdnex', 'www.bedetheque.com', 'catalogue.bnf.fr', 'openlibrary.org'):
        raise ValueError('Invalid provider dependency')
    if 'http_status' in value and (type(value['http_status']) is not int or not 100 <= value['http_status'] <= 599):
        raise ValueError('Invalid provider HTTP status')
    if 'task_state' in value and value['task_state'] not in ('ready', 'claimed', 'waiting', 'blocked', 'done', 'failed', 'superseded'):
        raise ValueError('Invalid task state')
    refs = value['refs']
    if not isinstance(refs, list) or len(refs) > 32 or any(
            not isinstance(ref, dict) or set(ref) != {'kind', 'id'} or ref['kind'] not in REF_KINDS
            or not opaque(ref['id']) for ref in refs):
        raise ValueError('Invalid typed event references')
    for key in ('cycle_id', 'request_id', 'operation_id'):
        if key in value and not opaque(value[key]):
            raise ValueError('Invalid correlation reference')
    for key in ('duration_ms', 'wait_ms', 'attempt', 'repeat_count', 'dropped_count', 'rejected_count'):
        if key in value and (type(value[key]) is not int or not 0 <= value[key] <= 2 ** 63 - 1):
            raise ValueError('Invalid event count')
    if 'next_attempt' in value and not utc(value['next_attempt']):
        raise ValueError('Invalid retry timestamp')
    if 'exception_type' in value and value['exception_type'] not in ERRORS:
        raise ValueError('Unrecognized exception type')
    if 'cause_type' in value and value['cause_type'] is not None and value['cause_type'] not in ERRORS:
        raise ValueError('Unrecognized cause type')
    if 'message' in value and (type(value['message']) is not str or value['message'] != _exception_message(value['message'])):
        raise ValueError('Unfiltered exception message')
    if 'detail' in value and (type(value['detail']) is not str or value['detail'] != safe_text(value['detail'], 300)):
        raise ValueError('Unfiltered event detail')
    if 'title' in value and (type(value['title']) is not str or value['title'] != safe_text(value['title'])):
        raise ValueError('Unfiltered event title')
    if 'measurements' in value:
        measures = value['measurements']
        if not isinstance(measures, dict) or set(measures) - {'bytes_done', 'bytes_total', 'free_bytes', 'memory_bytes'} or any(
                type(number) is not int or not 0 <= number <= 2 ** 63 - 1 for number in measures.values()):
            raise ValueError('Invalid event measurements')
    if 'stack' in value:
        stack = value['stack']
        if not isinstance(stack, list) or len(stack) > 32 or any(
                not isinstance(frame, dict) or set(frame) != {'module', 'function', 'line'}
                or type(frame['module']) is not str or not re.fullmatch(
                    r'(?:library|booksys_core|ops)(?:\.[a-z_][a-z_0-9]*)+|booksys|pipeline|fiches|activity', frame['module'])
                or type(frame['function']) is not str or not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,127}', frame['function'])
                or type(frame['line']) is not int or not 1 <= frame['line'] <= 1000000 for frame in stack):
            raise ValueError('Invalid project stack')
    # JSON scalars cannot carry literal control characters. Strict JSON parsing
    # also excludes NaN and duplicate keys before this validation.
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > 8192:
        raise ValueError('Event exceeds record bound')
    return dict(value)


def provider_diagnostic(events: list[dict[str, Any]], *, deferred: bool = False) -> dict[str, Any]:
    """Keep one representative cause; never copy provider text or URLs into logs."""
    failures = [event for event in events if event.get('status') == 'unavailable'
                and (event.get('reason') == 'Request budget exhausted' if deferred else
                     event.get('http_status', 0) == 429 or event.get('http_status', 0) >= 500
                     or 'reason' in event and event['reason'] != 'Request budget exhausted')]
    if not failures:
        return {}
    event = failures[0]
    status = event.get('http_status')
    reason = ('provider_budget' if event.get('reason') == 'Request budget exhausted' else
              'provider_cooldown' if event.get('reason') == 'BDneX cooldown' else
              'provider_request_invalid' if event.get('reason') == 'BDneX request invalid' else
              'remote_rate_limited' if status == 429 else
              'provider_http_failure' if type(status) is int and status >= 400 else
              'provider_timeout' if event.get('exception_type') == 'TimeoutError' or event.get('failure_reason') == 'provider_timeout' else 'provider_unavailable')
    result: dict[str, Any] = {'reason': reason}
    if type(event.get('reason')) is str:
        result['detail'] = _exception_message(event['reason'])
    if event.get('dependency') in ('bdnex', 'www.bedetheque.com', 'catalogue.bnf.fr', 'openlibrary.org'):
        result['dependency'] = event['dependency']
    if type(status) is int and 100 <= status <= 599:
        result['http_status'] = status
    if event.get('exception_type') in ERRORS:
        result['exception_type'] = event['exception_type']
    return result


def diagnostic_fields(value: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if value.get('reason') in REASONS:
        result['reason'] = value['reason']
    if value.get('dependency') in ('bdnex', 'www.bedetheque.com', 'catalogue.bnf.fr', 'openlibrary.org'):
        result['dependency'] = value['dependency']
    status = value.get('http_status')
    if type(status) is int and 100 <= status <= 599:
        result['http_status'] = status
    if value.get('exception_type') in ERRORS:
        result['exception_type'] = value['exception_type']
    if type(value.get('detail')) is str:
        result['detail'] = _exception_message(value['detail'])
    return result


def diagnostic_text(value: dict[str, Any]) -> str:
    fields = diagnostic_fields(value)
    return ' '.join([str(fields.pop('reason', 'unknown'))] + [f'{key}={item}' for key, item in fields.items()])[:200]


def severity(reason: str, code: str) -> str:
    if reason in ('cleanup_failed', 'authority_mismatch'):
        return 'CRITICAL'
    if code in ('failed', 'blocked') and reason not in ('identity_unknown', 'provider_budget', 'transfer_wait'):
        return 'ERROR'
    if reason in ('diagnostic_unavailable', 'storage_busy', 'disk_reserve', 'remote_rate_limited', 'provider_unavailable', 'provider_timeout', 'operation_timeout', 'provider_http_failure'):
        return 'WARNING'
    return 'INFO'


_context: ContextVar[dict[str, Any]] = ContextVar('diagnostic_context', default={})


@contextmanager
def context(**fields: Any) -> Iterator[None]:
    token = _context.set({**_context.get(), **fields})
    try:
        yield
    finally:
        _context.reset(token)


def event(*, service: str, stage: str, code: str, reason: str, **fields: Any) -> dict[str, Any]:
    fields = {**_context.get(), **fields}
    if 'title' in fields and isinstance(fields['title'], str):
        fields['title'] = safe_text(fields['title'])
    return validate({'schema': 1, 'event_id': str(uuid.uuid4()),
                     'at': datetime.now(timezone.utc).isoformat(), 'service': service,
                     'stage': stage, 'code': code, 'level': severity(reason, code),
                     'reason': reason, 'refs': [], **fields})


def parse_event(line: str) -> dict[str, Any]:
    if len(line.encode()) > 8192:
        raise ValueError('Oversized event')
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                raise ValueError('Duplicate event key')
            value[key] = item
        return value
    def constant(value: str) -> Any:
        raise ValueError('Nonfinite event constant')
    return validate(json.loads(line, object_pairs_hook=pairs, parse_constant=constant))


class Emitter:
    def __init__(self, fd: int = 2):
        self.fd = os.dup(fd)
        os.set_blocking(self.fd, False)
        self.queue: queue.Queue[bytes] = queue.Queue(maxsize=256)
        self.dropped = 0
        self.rejected = 0
        self.closed = False
        self.reported_loss = (0, 0)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._drain, daemon=True, name='diagnostic-emitter')
        self.thread.start()

    def emit(self, value: dict[str, Any]) -> bool:
        try:
            record = json.dumps(validate(value), ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode() + b'\n'
        except (ValueError, TypeError, OverflowError):
            self.rejected += 1
            return False
        if self.closed:
            self.dropped += 1
            return False
        try:
            self.queue.put_nowait(record)
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def degradation(self) -> dict[str, int]:
        return {'dropped_count': self.dropped, 'rejected_count': self.rejected, 'queued_count': self.queue.qsize()}

    def _drain(self) -> None:
        while not self.stop.is_set() or not self.queue.empty():
            deadline = time.monotonic() + 0.1
            while time.monotonic() < deadline:
                try:
                    record = self.queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    loss = (self.dropped, self.rejected)
                    if loss != self.reported_loss:
                        notice = json.dumps(event(service='library', stage='diagnostics', code='recovered',
                            reason='diagnostic_unavailable', dropped_count=loss[0], rejected_count=loss[1]),
                            separators=(',', ':')).encode()+b'\n'
                        if os.write(self.fd, notice) == len(notice):
                            self.reported_loss = loss
                    if os.write(self.fd, record) != len(record):
                        self.dropped += 1
                except OSError:
                    self.dropped += 1
            self.stop.wait(0.01)
        os.close(self.fd)

    def close(self) -> None:
        self.closed = True
        self.stop.set()
        self.thread.join(0.1)


# One bounded emitter per process. Importing the module starts no thread.
_default: Emitter | None = None
_failed_emissions = 0
_emit_lock = threading.Lock()
_conditions: dict[tuple[str, str, str], tuple[str, str, int]] = {}


def emit(*, service: str, stage: str, code: str, reason: str = 'none', **fields: Any) -> bool:
    global _default, _failed_emissions
    try:
        value = event(service=service, stage=stage, code=code, reason=reason, **fields)
        key = (service, stage, json.dumps([value.get('queue'), value['refs']], sort_keys=True))
        with _emit_lock:
            if _default is None:
                import atexit
                _default = Emitter()
                atexit.register(_default.close)
            condition = json.dumps([reason, *[value.get(field) for field in
                ('dependency', 'http_status', 'exception_type', 'cause_type', 'detail', 'task_state')]], sort_keys=True)
            prior = _conditions.get(key)
            if code == 'waiting' and prior and prior[:2] == (code, condition):
                _conditions[key] = (code, condition, prior[2] + 1)
                return True
            if prior:
                value['repeat_count'] = prior[2]
            if len(_conditions) >= 256 and key not in _conditions:
                _conditions.pop(next(iter(_conditions)))
            _conditions[key] = (code, condition, 0)
            return _default.emit(value)
    except (ValueError, TypeError, OSError):
        _failed_emissions += 1
        return False


def relay(value: dict[str, Any]) -> bool:
    """Relay a validated child record without replacing its identity or time."""
    global _default, _failed_emissions
    try:
        checked = validate(value)
        with _emit_lock:
            if _default is None:
                import atexit
                _default = Emitter()
                atexit.register(_default.close)
            return _default.emit(checked)
    except (ValueError, TypeError, OSError):
        _failed_emissions += 1
        return False


def degradation() -> dict[str, int]:
    value = _default.degradation() if _default else {'dropped_count': 0, 'rejected_count': 0, 'queued_count': 0}
    return {**value, 'rejected_count': value['rejected_count']+_failed_emissions}


# Third-party log messages are not a safe event source. Never call getMessage()
# or copy log args. Exception messages pass through the dedicated sanitizer.
import logging


class SafeLoggingHandler(logging.Handler):
    def __init__(self, service: str):
        super().__init__(logging.WARNING)
        if service not in SERVICES:
            raise ValueError('Invalid logging service')
        self.service = service

    def emit(self, record: logging.LogRecord) -> None:
        details = exception_details(record.exc_info[1]) if record.exc_info and record.exc_info[1] else {}
        emit(service=self.service, stage='diagnostics', code='degraded',
             level='ERROR' if record.levelno >= logging.ERROR else 'WARNING', **({'reason': 'diagnostic_unavailable'} if not details else details))


def exception_details(exc: BaseException) -> dict[str, Any]:
    # Preserve reason mapping and trusted project frames; sanitize unexpected messages.
    import sqlite3
    reason = 'unexpected_exception'
    if isinstance(exc, sqlite3.OperationalError) and getattr(exc, 'sqlite_errorcode', 0) & 0xff in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        reason = 'storage_busy'
    elif type(exc).__name__ == 'LibraryError' and exc.args == ('storage_busy',):
        reason = 'storage_busy'
    elif isinstance(exc, FileNotFoundError) or type(exc).__name__ == 'ReaderUnavailable':
        reason = 'missing_dependency'
    elif isinstance(exc, ValueError):
        reason = 'invalid_content'
    kind = type(exc).__name__
    from booksys_core.browser_outcomes import BrowserOutcome
    from booksys_core.errors import ResultInputsChanged, NativeRefusal
    import errno
    import subprocess
    from urllib.error import URLError
    if isinstance(exc, URLError) and isinstance(exc.reason, TimeoutError):
        reason = 'provider_timeout'
    elif isinstance(exc, BrowserOutcome) and exc.outcome.code in REASONS:
        reason = exc.outcome.code
    elif kind == 'Deferred' and getattr(exc, 'record', {}).get('wait_reason') in REASONS:
        reason = exc.record['wait_reason']  # type: ignore[attr-defined]
    elif kind == 'BudgetWait':
        reason = 'provider_budget'
    elif isinstance(exc, ResultInputsChanged):
        reason = 'inputs_changed'
    elif isinstance(exc, NativeRefusal):
        reason = 'submission_refused'
    elif isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
        reason = 'operation_timeout'
    elif isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        reason = 'disk_full'
    elif kind == 'LibraryError' and str(exc) == 'Decoder timed out':
        reason = 'decoder_timeout'
    elif kind == 'LibraryLockBusy':
        reason = 'processing_lock_busy'
    stack = []
    tb = exc.__traceback__
    while tb is not None:
        module = tb.tb_frame.f_globals.get('__name__', '')
        if module == '__main__':
            from pathlib import Path
            try:
                module = '.'.join(Path(tb.tb_frame.f_code.co_filename).resolve().relative_to(
                    Path(__file__).resolve().parents[1]).with_suffix('').parts)
            except (OSError, ValueError):
                module = ''
        function = tb.tb_frame.f_code.co_name
        if (isinstance(module, str) and re.fullmatch(
                r'(?:library|booksys_core|ops)(?:\.[a-z_][a-z_0-9]*)+|booksys|pipeline|fiches|activity', module)
                and re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,127}', function)
                and 1 <= tb.tb_lineno <= 1000000):
            stack.append({'module': module, 'function': function, 'line': tb.tb_lineno})
        tb = tb.tb_next
    from booksys_core.errors import ResultInputsChanged
    details: dict[str, Any] = {'reason': reason, 'exception_type': kind if kind in ERRORS else 'Exception', 'stack': stack[-32:]}
    if reason == 'unexpected_exception':
        details['message'] = _exception_message(str(exc))
        cause = exc.__cause__ if exc.__cause__ is not None else (
            None if exc.__suppress_context__ else exc.__context__)
        cause_kind = type(cause).__name__ if cause is not None else None
        details['cause_type'] = cause_kind if cause_kind is None or cause_kind in ERRORS else 'Exception'
    if isinstance(exc, ResultInputsChanged):
        details['detail'] = safe_text(str(exc), 300)
    return details


def install_logging(service: str) -> None:
    import sys
    def uncaught(kind: Any, exc: BaseException, tb: Any) -> None:
        emit(service=service, stage='diagnostics', code='failed', **exception_details(exc))
        if _default is not None:
            _default.close()
    sys.excepthook = uncaught
    def thread_uncaught(args: Any) -> None:
        emit(service=service, stage='diagnostics', code='failed', **exception_details(args.exc_value))
    threading.excepthook = thread_uncaught
    root = logging.getLogger()
    root.handlers[:] = [SafeLoggingHandler(service)]
    root.setLevel(logging.WARNING)
    # Uvicorn handlers can bypass root propagation and print request paths.
    for name in ('uvicorn', 'uvicorn.error', 'uvicorn.access'):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
