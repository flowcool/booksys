"""Queue-owned reader requests. The client has no Docker authority."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
from typing import Any
import uuid

from library.decoder_context import current_decoder
from library.ipc import Channel, IPCError
from library.primitives import LibraryError, sync_dir
from library.reader_inputs import ReaderBusy, ReaderUnavailable, sha256, fingerprint


def endpoint() -> Path | None:
    value = os.environ.get('BOOKSYS_READER_SOCKET')
    return Path(value) if value else None


def request(value: dict[str, str], *, timeout: float = 1900) -> dict[str, Any]:
    path = endpoint()
    if path is None:
        raise ReaderUnavailable('Reader service is not configured')
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(timeout)
        try:
            connection.connect(str(path))
            channel = Channel(connection)
            channel.send(value)
            response = channel.receive()
        except (OSError, IPCError) as exc:
            raise ReaderUnavailable('Reader result uncertain; retain job and grant; do not replay') from exc
    if value.get('operation')=='diagnostics' and response.get('error')=='diagnostic_budget_exhausted':
        raise TimeoutError('Diagnostic page acquisition expired')
    if response.get('error') == 'reader_busy':
        raise ReaderBusy('Reader busy; request did not start')
    if response.get('error') is not None or not isinstance(response.get('result'), dict):
        raise ReaderUnavailable('Reader unavailable; retain job and grant for reconciliation')
    return dict(response['result'])


def available(*, idle: bool = False) -> bool:
    try:
        result = request({'operation': 'probe', 'job': '0'*32}, timeout=2)
        return result.get('available') is True and (not idle or result.get('idle', True) is True)
    except ReaderUnavailable:
        return False


def execute(operation: str, source: Path, payload: dict[str, Any] | None = None, *,
            destination: Path | None = None) -> dict[str, Any]:
    owner = current_decoder.get()
    if owner is None:
        raise ReaderUnavailable('Reader requires a retained domain task and resource grant')
    inputs = Path(os.environ['BOOKSYS_READER_INPUTS'])
    jobs = Path(os.environ['BOOKSYS_READER_JOBS'])
    source = source.absolute()
    if not source.is_relative_to(inputs) or any(p.is_symlink() for p in (source, *source.parents)):
        raise LibraryError('Reader source is outside declared NAS input namespace')
    before = fingerprint(source)
    checksum = sha256(source)
    reference = uuid.uuid4().hex
    job = jobs/reference
    requests = Path(os.environ['BOOKSYS_READER_REQUESTS'])
    requests.mkdir(mode=0o700, exist_ok=True)
    pending = requests/reference
    pending.mkdir(mode=0o700)
    manifest = {'operation': operation, 'input': str(source.relative_to(inputs)),
                'sha256': checksum, 'payload': payload or {}}
    from library.operational_state import ReaderJobs
    from library.postgres import Database
    ReaderJobs(Database.configured(owner.path.parent.parent)).request(reference,owner.path.stem,manifest)
    for name, value in (('manifest.json', manifest), ('grant.json', {'key': owner.path.stem})):
        with (pending/name).open('x') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
    sync_dir(pending)
    owner.register('booksys-reader-'+reference)
    result = request({'operation': operation, 'job': reference})
    if not result.get('stopped'):
        raise ReaderUnavailable('Reader stop is unproved; retain all reservations')
    if fingerprint(source) != before or sha256(source) != checksum or result.get('status') == 'input_changed':
        raise LibraryError('Reader source changed during execution')
    if result.get('status') != 'completed' or not isinstance(result.get('outcome'), dict):
        raise LibraryError('Isolated reader failed; retained receipt identifies the worker')
    if destination is not None:
        if list(destination.iterdir()):
            raise LibraryError('Reader destination must be empty')
        for child in (job/'work').iterdir():
            if child.is_symlink() or not child.is_file():
                raise LibraryError('Reader output contains undeclared files')
            shutil.copyfile(child, destination/child.name)
            with (destination/child.name).open('rb') as stream:
                os.fsync(stream.fileno())
        sync_dir(destination)
    return dict(result['outcome'])


def bundle_payload(parts: list[Path]) -> dict[str, Any]:
    inputs = Path(os.environ['BOOKSYS_READER_INPUTS'])
    return {'parts': [{'input': str(path.absolute().relative_to(inputs)), 'sha256': sha256(path)} for path in parts]}
