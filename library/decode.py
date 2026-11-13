"""Repeatable decoding in a stage subprocess."""
from __future__ import annotations

import json
import os
from pathlib import Path
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
from typing import Any

from library.primitives import LibraryError, sync_dir
from library.reader_inputs import fingerprint

WORK_ROOT = '/data/booksys/work'


def _limits() -> None:
    # Bound decoder virtual address space independently of the container memory cap.
    resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024**2, 1536 * 1024**2))
    # Bound each decoder output file to 1 GiB of local work storage.
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024**3, 1024**3))


def run(operation: str, source: Path, payload: dict[str, Any] | None = None,
        destination: Path | None = None) -> dict[str, Any]:
    """Run one decode. A crash or a timeout raises LibraryError (the stage counts the attempt); the worker reports
    its own pending or rejected outcomes in its JSON."""
    source = source.absolute()
    before = fingerprint(source)
    # Bound decoding wall time, including slow input reads (plus 1 s per 2 MiB of source); the process group is killed on expiry.
    timeout = {'validate': 45, 'bundle': 180,
               'transform': 1800 if source.suffix.lower() == '.pdf' else 60}[operation]
    work = Path(tempfile.mkdtemp(dir=WORK_ROOT))
    try:
        process = subprocess.Popen([sys.executable, '-m', 'library.reader_worker', str(source), source.suffix.lower(), operation, str(work)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True, preexec_fn=_limits,
            env={'PATH': os.environ.get('PATH', os.defpath), 'HOME': str(work), 'TMPDIR': str(work)})
        try:
            stdout, _ = process.communicate(json.dumps(payload or {}).encode(), timeout=timeout + before[2] // (2 * 1024**2))
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise LibraryError('Decoder timed out') from None
        if fingerprint(source) != before:
            raise LibraryError('Reader source changed during execution')
        if process.returncode:
            raise LibraryError('Decoder failed')
        outcome = json.loads(stdout)
        if not isinstance(outcome, dict):
            raise LibraryError('Decoder returned an invalid result')
        if destination is not None and outcome.get('status') in ('validated', 'extracted'):
            if list(destination.iterdir()):
                raise LibraryError('Reader destination must be empty')
            for child in work.iterdir():
                if child.is_symlink() or not child.is_file():
                    raise LibraryError('Reader output contains undeclared files')
                shutil.copyfile(child, destination / child.name)
                with (destination / child.name).open('rb') as stream:
                    os.fsync(stream.fileno())
            sync_dir(destination)
        return outcome
    finally:
        shutil.rmtree(work, ignore_errors=True)
