"""Bounded pipe I/O for owned decoder processes. No shell or output files."""
from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from typing import Any

from library.catalog import LibraryError


def bounded_run(command: list[str], *, input: bytes = b'', timeout: float,
                stdout_limit: int = 1024**2, stderr_limit: int = 65536,
                env: dict[str, str] | None = None) -> subprocess.CompletedProcess[bytes]:
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=env, start_new_session=True)
    output: dict[str, bytearray] = {'stdout': bytearray(), 'stderr': bytearray()}
    deadline = time.monotonic() + timeout
    offset = 0
    try:
        with selectors.DefaultSelector() as selector:
            assert process.stdin is not None and process.stdout is not None and process.stderr is not None
            for pipe, name in [(process.stdout, 'stdout'), (process.stderr, 'stderr')]:
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, name)
            if input:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, 'stdin')
            else:
                process.stdin.close()
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                for key, _ in selector.select(min(remaining, .5)):
                    stream: Any = key.fileobj
                    if key.data == 'stdin':
                        try:
                            offset += os.write(stream.fileno(), input[offset:offset+65536])
                        except BrokenPipeError:
                            offset = len(input)
                        if offset == len(input):
                            selector.unregister(stream)
                            stream.close()
                    else:
                        chunk = os.read(stream.fileno(), 65536)
                        if not chunk:
                            selector.unregister(stream)
                            stream.close()
                            continue
                        target = output[key.data]
                        limit = stdout_limit if key.data == 'stdout' else stderr_limit
                        if len(target) + len(chunk) > limit:
                            raise LibraryError('Decoder output limit exceeded')
                        target.extend(chunk)
            result = process.wait(timeout=max(.001, deadline-time.monotonic()))
        return subprocess.CompletedProcess(command, result, bytes(output['stdout']), bytes(output['stderr']))
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        for final_stream in (process.stdin, process.stdout, process.stderr):
            if final_stream and not final_stream.closed:
                final_stream.close()
