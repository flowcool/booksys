"""Fixed owned Docker/journal sources. No HTTP argument selects a command or path."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import time
from typing import Any

from booksys_core.diagnostics import parse_event

DOCKER_SOURCES = {'worker': ('booksys-worker-1', 'worker'), 'byparr': ('booksys-byparr-1', 'byparr')}
UNIT = 'booksys-library.service'
COMPOSE_ROLES = ('controller', 'discovery', 'collection', 'intake', 'ocr', 'metadata',
                 'copies', 'routing', 'retirement', 'byparr', 'pyload')


def timestamp(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.utcoffset() is None:
        raise ValueError('Missing trusted timestamp timezone')
    return parsed.timestamp()


def utc(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


class LiveSource:
    def __init__(self, name: str, *, project: str | None = None):
        if name not in (COMPOSE_ROLES if project else (*DOCKER_SOURCES, 'library')):
            raise ValueError('Unknown fixed source')
        if project and not re.fullmatch('[a-z][a-z0-9-]{0,62}', project):
            raise ValueError('Invalid configured Compose project')
        self.project = project
        self.retained: dict[str, str] | None = None
        configured = os.environ.get('BOOKSYS_RETAINED_SOURCES_FILE')
        if project and configured:
            bindings = json.loads(Path(configured).read_text())
            if not isinstance(bindings, dict) or set(bindings)-{'pyload', 'byparr'}:
                raise ValueError('Only retained dependencies can override configured sources')
            for role, binding in bindings.items():
                if (not isinstance(binding, dict) or set(binding) != {'id', 'project', 'service'}
                        or not isinstance(binding['id'], str) or not re.fullmatch('[0-9a-f]{64}', binding['id'])
                        or not isinstance(binding['project'], str) or not re.fullmatch('[a-z][a-z0-9-]{0,62}', binding['project'])
                        or binding['service'] != role):
                    raise ValueError('Invalid retained dependency identity')
            self.retained = bindings.get(name)
        self.name, self.generation = name, 'unavailable'
        self.container = ''
        self.available = False

    def refresh(self, *, deadline: float | None = None) -> None:
        """Called by the shared observer, never by individual viewers."""
        if self.name == 'library' and self.project is None:
            try:
                with open('/proc/sys/kernel/random/boot_id') as handle:
                    self.generation = 'journal:' + handle.read().strip()
                self.available = True
            except OSError:
                self.available = False
            return
        stop = deadline if deadline is not None else time.monotonic()+.3
        def remaining() -> float:
            value=min(.15,stop-time.monotonic())
            if value<=0:raise TimeoutError('Diagnostic source acquisition expired')
            return value
        name, service = (self.name, self.name) if self.project else DOCKER_SOURCES[self.name]
        try:
            if self.retained:
                name = self.retained['id']
            elif self.project:
                selected = subprocess.run(['docker', 'ps', '--all', '--quiet', '--no-trunc',
                    '--filter', 'label=com.docker.compose.project='+self.project,
                    '--filter', 'label=com.docker.compose.service='+service], capture_output=True, timeout=remaining())
                candidates = selected.stdout.decode().splitlines()
                if selected.returncode or len(candidates) != 1:
                    raise ValueError('Expected exactly one Compose source')
                name = candidates[0]
            result = subprocess.run(['docker', 'inspect', '--format', '{{json .}}', name],
                                    capture_output=True, timeout=remaining(), check=False)
            if result.returncode or len(result.stdout) > 256 * 1024:
                raise ValueError('Source unavailable')
            value = json.loads(result.stdout)
            labels = value.get('Config', {}).get('Labels', {})
            if ((self.retained and value.get('Id') != self.retained['id'])
                    or labels.get('com.docker.compose.project') != (self.retained['project'] if self.retained else self.project or 'booksys')
                    or labels.get('com.docker.compose.service') != service):
                raise ValueError('Unexpected source owner')
            self.container = value['Id']
            self.generation = self.container
            self.available = True
        except (TimeoutError,subprocess.TimeoutExpired) as exc:
            if deadline is not None:
                raise TimeoutError('Diagnostic source acquisition expired') from exc
            self.available=False
        except (OSError, ValueError, KeyError):
            self.available = False

    def scan(self, start: float, end: float, position: Any, *, lines: int,
             byte_limit: int, deadline: float) -> dict[str, Any]:
        if not self.available:
            raise OSError('Fixed source unavailable')
        since, skip = position or (start, 0)
        if self.name == 'library':
            if os.environ.get('BOOKSYS_STAGE_MODE'):
                command = ['journalctl', '--no-pager', '--output=json', '--since=@'+str(since), '--until=@'+str(end)]
                from library.stage_model import QUEUES
                for index, queue in enumerate((*QUEUES, 'discovery', 'serial', 'observer')):
                    if index:
                        command.append('+')
                    command.append('_SYSTEMD_UNIT=booksys-stage-'+queue+'.service')
            else:
                command = ['journalctl', '--user', '--no-pager', '--output=json',
                           '_SYSTEMD_USER_UNIT=' + UNIT, '--since=@' + str(since), '--until=@' + str(end)]
        else:
            command = ['docker', 'logs', '--timestamps', '--since', utc(since), '--until', utc(end), self.container]
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        # One pipe preserves Docker log demultiplexing order. Separate pipes can
        # move the continuation past a record that has not been consumed.
        buffers: dict[int, bytearray] = {}
        rows, used, count, skipped = [], 0, 0, 0
        frontier, boundary_count = since, skip
        done, cut, rejected = False, False, 0
        gaps: list[dict[str, str]] = []
        first_trusted = True
        try:
            with selectors.DefaultSelector() as selector:
                for pipe in (process.stdout, process.stderr):
                    if pipe is None:
                        continue
                    os.set_blocking(pipe.fileno(), False)
                    buffers[pipe.fileno()] = bytearray()
                    selector.register(pipe, selectors.EVENT_READ)
                while selector.get_map() and not cut:
                    remaining = deadline - time.monotonic()
                    if remaining <= .015:  # Reserve kill/reap time inside250ms.
                        break
                    for key, _ in selector.select(min(.01, remaining)):
                        stream: Any = key.fileobj
                        chunk = os.read(stream.fileno(), min(16384, byte_limit - used + 1))
                        if not chunk:
                            selector.unregister(stream)
                            continue
                        used += len(chunk)
                        if used > byte_limit:
                            cut = True
                            break
                        buffer = buffers[stream.fileno()]
                        buffer.extend(chunk)
                        while b'\n' in buffer and not cut:
                            raw, _, rest = buffer.partition(b'\n')
                            buffer[:] = rest
                            try:
                                if len(raw) > 32768:
                                    raise ValueError('Oversized source record')
                                if self.name == 'library':
                                    envelope = json.loads(raw)
                                    if os.environ.get('BOOKSYS_STAGE_MODE'):
                                        from library.stage_model import QUEUES
                                        owner = envelope.get('_SYSTEMD_UNIT') in {
                                            'booksys-stage-'+queue+'.service' for queue in (*QUEUES, 'discovery', 'serial', 'observer')}
                                    else:
                                        owner = envelope.get('_SYSTEMD_USER_UNIT') == UNIT
                                    if (not owner
                                            or str(envelope.get('_UID')) != str(os.getuid())
                                            or envelope.get('_TRANSPORT') != 'stdout'):
                                        raise ValueError('Untrusted journal unit')
                                    at = int(envelope['__REALTIME_TIMESTAMP']) / 1000000
                                    message = envelope['MESSAGE']
                                else:
                                    date, message = raw.decode('utf-8').split(' ', 1)
                                    at = timestamp(date)
                                if at == since and skipped < skip:
                                    skipped += 1
                                    continue
                                if at < since or at > end:
                                    continue
                                if first_trusted:
                                    if position is None and at > start:
                                        gaps.append({'source': self.name, 'reason': 'retention_before_first_record_unknown'})
                                    elif position is not None and at > since and skipped < skip:
                                        gaps.append({'source': self.name, 'reason': 'continuation_anchor_lost'})
                                    first_trusted = False
                                boundary_count = boundary_count + 1 if at == frontier else 1
                                frontier = at
                                count += 1
                                try:
                                    record = parse_event(message)
                                except (ValueError, TypeError, UnicodeError):
                                    rejected += 1
                                else:
                                    rows.append((at, record))
                                if count >= lines:
                                    cut = True
                            except (ValueError, TypeError, KeyError, UnicodeError):
                                rejected += 1
                        if len(buffer) > 65536:
                            # We cannot continue a source without a trusted timestamp.
                            raise ValueError('Invalid source envelope')
                try:
                    done = not selector.get_map() and process.wait(timeout=max(.001, deadline - time.monotonic())) == 0
                except subprocess.TimeoutExpired as exc:
                    raise TimeoutError('Source process exceeded scan budget') from exc
                if done and any(buffers.values()):
                    rejected += 1
            if not done and frontier == since and boundary_count == skip:
                raise TimeoutError('Source made no bounded scan progress')
            return {'rows': rows, 'position': [frontier, boundary_count], 'frontier': end if done else frontier,
                    'done': done, 'bytes': used, 'lines': count, 'gaps': gaps, 'rejected': rejected}
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait()
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()
