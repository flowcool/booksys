"""Owned child supervision with immediate filtered stderr and bounded stdout."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import os
import selectors
import signal
import subprocess
import time
from typing import Any

from booksys_core.diagnostics import parse_event


@dataclass(frozen=True)
class ChildResult:
    returncode: int
    stdout: bytes
    rejected: int


def run_child(command: list[str], *, on_event: Callable[[dict[str, Any]], None],
              timeout: float | None = None, stdout_limit: int = 1024**2) -> ChildResult:
    child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, start_new_session=True)
    output, line = bytearray(), bytearray()
    rejected, oversized = 0, False
    deadline = time.monotonic() + timeout if timeout is not None else float('inf')
    try:
        with selectors.DefaultSelector() as selector:
            assert child.stdout is not None and child.stderr is not None
            for pipe, name in ((child.stdout, 'stdout'), (child.stderr, 'stderr')):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, name)
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise TimeoutError('Owned child deadline exceeded')
                for key, _ in selector.select(min(.1, max(0, deadline - time.monotonic()))):
                    stream: Any = key.fileobj
                    data = os.read(stream.fileno(), 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        stream.close()
                        continue
                    if key.data == 'stdout':
                        if len(output) + len(data) > stdout_limit:
                            raise ValueError('Machine stdout limit exceeded; reconcile uncertain effects')
                        output.extend(data)
                        continue
                    # Oversized lines are discarded through newline without retaining raw text.
                    for segment in data.splitlines(keepends=True):
                        ended = segment.endswith(b'\n')
                        if len(line) + len(segment) > 8193:
                            oversized = True
                        if not oversized:
                            line.extend(segment)
                        if ended:
                            try:
                                if oversized:
                                    raise ValueError('oversized')
                                value = parse_event(line.decode('utf-8').rstrip('\n'))
                            except (ValueError, UnicodeError, TypeError):
                                rejected += 1
                            else:
                                on_event(value)
                            line.clear()
                            oversized = False
            if line or oversized:
                rejected += 1  # Unterminated records are never trusted.
        return ChildResult(child.wait(), bytes(output), rejected)
    finally:
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        child.wait()
        for final_pipe in (child.stdout, child.stderr):
            if final_pipe is not None and not final_pipe.closed:
                final_pipe.close()
