"""Retain exact out-of-cgroup decoder identities before launch, never expire them."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import re
import os
import subprocess
from typing import Iterator
import uuid

from library.postgres import Database, guards

from library.primitives import LibraryError

from library.decoder_context import current_decoder as current_decoder


def valid_name(name: str) -> bool:
    return bool(re.fullmatch(r'(?:booksys-[a-z]+-|booksys-reader-)[0-9a-f]{32}', name))


class DecoderOwnership:
    def __init__(self, path: Path, *, database: Database | None = None):
        self.path = path
        root = path.parent.parent if path.parent.name == 'stage-children' else path.parent
        self._database = database or Database.configured(root)
        self.owner = path.name

    @classmethod
    def create(cls, path: Path, *, database: Database | None = None) -> DecoderOwnership:
        result = cls(path,database=database)
        with result._database.connection(write=True) as db:
            db.execute('INSERT INTO decoder_owners VALUES (%s)',(result.owner,))
        return result

    def exists(self) -> bool:
        with self._database.connection() as db:
            return db.execute('SELECT 1 FROM decoder_owners WHERE owner=%s',(self.owner,)).fetchone() is not None

    def names(self) -> list[str]:
        with self._database.connection() as db:
            if db.execute('SELECT 1 FROM decoder_owners WHERE owner=%s',(self.owner,)).fetchone() is None:
                raise LibraryError('Decoder ownership missing; cleanup is unproved')
            names = [row[0] for row in db.execute('SELECT name FROM decoder_children WHERE owner=%s AND NOT stopped ORDER BY position',(self.owner,))]
            if len(names)>128 or any(not valid_name(name) for name in names):
                raise LibraryError('Decoder ownership invalid')
            return names

    def register(self, name: str) -> None:
        if not valid_name(name):
            raise LibraryError('Decoder has no exact owned identity')
        with self._database.connection(write=True) as db:
            guards(db, {'decoder-owner:' + self.owner})
            prior = db.execute('SELECT stopped FROM decoder_children WHERE owner=%s AND name=%s',(self.owner,name)).fetchone()
            if prior and prior[0]:
                raise LibraryError('Stopped decoder identity cannot be reused')
            count = db.execute('SELECT count(*) FROM decoder_children WHERE owner=%s AND NOT stopped',(self.owner,)).fetchone()
            if count is not None and count[0] >= 128:
                if db.execute('SELECT 1 FROM decoder_children WHERE owner=%s AND name=%s',(self.owner,name)).fetchone() is None:
                    raise LibraryError('Decoder ownership capacity')
            db.execute('INSERT INTO decoder_children(owner,name) VALUES (%s,%s) ON CONFLICT DO NOTHING',(self.owner,name))

    def reset_stopped(self) -> None:
        """After exact external cleanup proof, start a new observation generation."""
        with self._database.connection(write=True) as db:
            guards(db, {'decoder-owner:' + self.owner})
            db.execute('UPDATE decoder_children SET stopped=true WHERE owner=%s',(self.owner,))

    def prepare(self, command: list[str]) -> list[str]:
        if len(command) < 2 or command[:2] not in (['docker', 'run'], ['docker', 'create']):
            return command
        command = list(command)
        parent = os.environ.get('BOOKSYS_DECODER_CGROUP_PARENT')
        if parent:
            if '--cgroup-parent' in command and command[command.index('--cgroup-parent') + 1] != parent:
                raise LibraryError('Decoder cgroup differs from the shared pool')
            if '--cgroup-parent' not in command:
                command[2:2] = ['--cgroup-parent', parent]
        if '--name' not in command:
            command[2:2] = ['--name', 'booksys-stage-' + uuid.uuid4().hex]
        name = command[command.index('--name') + 1]
        if not re.fullmatch(r'booksys-[a-z]+-[0-9a-f]{32}', name):
            raise LibraryError('Decoder has no exact owned identity')
        self.register(name)
        return command

    @contextmanager
    def track(self) -> Iterator[None]:
        token = current_decoder.set(self)
        try:
            yield
        finally:
            current_decoder.reset(token)

    def cleanup(self) -> bool:
        """Call only after the old execution path cannot launch more children.

        On takeover, require old supervisor cgroup termination first. On normal
        return, synchronous adapters have unwound and reaped their launchers.

        Inspect after stop, including failed stop. Never remove a container,
        staging, or evidence. Missing --rm containers are the expected outcome.
        """
        for name in self.names():
            if os.environ.get('BOOKSYS_READER_SOCKET'):
                from library.reader_client import request
                from library.reader_inputs import ReaderUnavailable
                if not name.startswith('booksys-reader-'):
                    return False  # Old direct-engine ownership needs explicit migration.
                try:
                    result = request({'operation': 'reconcile', 'job': name.removeprefix('booksys-reader-')}, timeout=30)
                except ReaderUnavailable:
                    return False
                if result.get('stopped') is not True:
                    return False
                continue
            try:
                subprocess.run(['docker', 'stop', '--time', '1', name], capture_output=True, timeout=15)
                proof = subprocess.run(['docker', 'inspect', '--format', '{{.State.Running}}', name],
                                       capture_output=True, timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                return False
            if proof.returncode == 0 and proof.stdout.strip() == b'false':
                continue
            if proof.returncode != 0 and (b'no such object:' in proof.stderr.lower()
                                         or b'no such container:' in proof.stderr.lower()):
                continue
            return False
        return True


def prepare(command: list[str]) -> list[str]:
    owner = current_decoder.get()
    return owner.prepare(command) if owner is not None else command
