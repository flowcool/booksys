"""Bind the stage activation record and the selected authority to the code of THIS image.

Run as a one-shot before the application roles (`python -m library.activate`): a role refuses to start while the
database row runtime_activation(role='stage') or storage-authority.yaml names other code than its image.
Environment: BOOKSYS_ACTIVATION_DSN (libpq key/value DSN of the role that may write runtime_activation, never printed),
BOOKSYS_CGROUP_POOL (pool path as the containers see it), BOOKSYS_STATE (state directory, read-write, owned by the
roles' user). Running roles check the binding only at their start, so recreate them after this step.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

from library.authority import code_digest
from library.codec import decode
from library.operational_state import RuntimeObservation
from library.postgres import Database
from library.primitives import LibraryError, atomic_yaml


def main() -> None:
    state = Path(os.environ.get('BOOKSYS_STATE', '/data/booksys/library-lab/state'))
    pool = os.environ.get('BOOKSYS_CGROUP_POOL', '/sys/fs/cgroup/memory/booksys')
    digest = code_digest()
    database = Database(os.environ['BOOKSYS_ACTIVATION_DSN'], 'booksys')
    with database.connection() as db:
        row = db.execute('SELECT payload FROM runtime_activation WHERE role=%s', ('stage',)).fetchone()
    if row is None:
        raise LibraryError('No stage activation record to update')
    stage = decode(row[0])
    receipt = state / 'activation-receipt.json'
    receipt.write_text(json.dumps({'application': digest, 'cgroup_pool': pool,
                                   'previous_code_digest': stage.get('code_digest')}, indent=1))
    RuntimeObservation(database).activate(
        {**stage, 'code_digest': digest, 'qualification_receipt': str(receipt), 'cgroup_pool': pool}, role='stage')
    authority = state / 'storage-authority.yaml'
    manifest = yaml.safe_load(authority.read_text())
    authority.chmod(0o600)
    atomic_yaml(authority, {**manifest, 'code_sha256': digest})
    authority.chmod(0o400)
    print('activation bound to', digest[:16], 'pool', pool)


if __name__ == '__main__':
    main()
