"""Invalidate exact obsolete rejection caches; normal intake owns validation."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import fcntl
import json
from pathlib import Path
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from library.catalog import Catalog
from library.primitives import LibraryError, atomic_yaml, digest
from library.reader_inputs import fingerprint, sha256
from library.stage_execution import effect_scopes, record_scopes

REASONS = {'Unsupported Docker bind source syntax', 'RarCannotExec: Cannot find working tool'}
COMMENT_REASON = "FileNotFoundError: [Errno 2] No usable temporary directory found in ['/tmp', '/var/tmp', '/usr/tmp', '/app']"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--reception', type=Path, required=True)
    parser.add_argument('--selection', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--include-rar-comment', action='store_true')
    args = parser.parse_args()
    entries = json.loads(args.selection.read_text())['origins']
    ids = [entry['id'] for entry in entries]
    if not 1 <= len(ids) <= 32 or len(set(ids)) != len(ids):
        raise LibraryError('Select one to 32 distinct origins')
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    catalog = Catalog(args.state)
    allowed = REASONS | ({COMMENT_REASON} if args.include_rar_comment else set())
    with ExitStack() as stack:
        for name in ('library-cycle.lock', 'stage-checkpoint.lock'):
            handle = stack.enter_context((args.state/name).open('a'))
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        state = catalog.result_set('retire', {'origins': ids}, media=False)
        stack.enter_context(effect_scopes(args.state, record_scopes(state)))
        before: dict[str, Any] = {}
        proof: list[dict[str, Any]] = []
        for entry in entries:
            origin = state['origins'][entry['id']]
            if (digest(origin) != entry['record_digest'] or origin.get('availability') != 'rejected'
                    or origin.get('reason') not in allowed or 'fingerprint' not in origin):
                raise LibraryError('Selected origin changed or is not an obsolete technical rejection')
            path = args.reception/origin['path']
            if (not path.is_relative_to(args.reception) or '..' in path.parts
                    or any(parent.is_symlink() for parent in (path, *path.parents))):
                raise LibraryError('Unsafe selected source path')
            stamp = list(fingerprint(path))
            checksum = sha256(path)
            if (stamp != entry['fingerprint'] or checksum != entry['sha256']
                    or list(fingerprint(path)) != stamp
                    or (origin.get('sha256') and origin['sha256'] != checksum)):
                raise LibraryError('Selected source changed')
            before[entry['id']] = dict(origin)
            proof.append({'id': entry['id'], 'fingerprint': stamp, 'sha256': checksum})
        atomic_yaml(args.evidence/'before.yaml', {'origins': before, 'revision': state['revision'], 'sources': proof})
        for oid in ids:
            state['origins'][oid].pop('fingerprint')
        atomic_yaml(args.evidence/'intent.yaml', {'state': 'prepared', 'origins': ids})
        catalog.save(state, expected=state['revision'])
        after = {oid: catalog.get('origins', oid) for oid in ids}
        if any(after[oid] != {k: v for k, v in before[oid].items() if k != 'fingerprint'} for oid in ids):
            raise LibraryError('Cache invalidation readback differs; retain evidence and reconcile')
        atomic_yaml(args.evidence/'after.yaml', {'origins': after})
        atomic_yaml(args.evidence/'intent.yaml', {'state': 'completed', 'origins': ids})
    print(json.dumps({'invalidated': len(ids), 'validation': 'normal intake actor'}))


if __name__ == '__main__':
    main()
