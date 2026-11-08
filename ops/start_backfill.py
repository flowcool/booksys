"""Record a bounded catalogue request and retain completed request evidence."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from booksys import CollectionError, DEFAULT_CATEGORY


def record_request(directory: Path, pages: int, *, archive_completed: bool = False) -> dict[str, Any]:
    if type(pages) is not int or not 1 <= pages <= 50:
        raise CollectionError('Page count must be between 1 and 50')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / 'pipeline.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        target = directory / 'backfill.yaml'
        if target.is_symlink():
            raise CollectionError('Backfill request must not be a symlink')
        archived = None
        if target.exists():
            previous = target.read_bytes()
            job = yaml.safe_load(previous)
            if (not archive_completed or not isinstance(job, dict) or job.get('version') != 1
                    or job.get('status') != 'complete'):
                raise CollectionError('Existing request requires inspection; only completed requests can be archived')
            history = directory / 'backfill-history'
            if history.is_symlink():
                raise CollectionError('Backfill history must not be a symlink')
            history.mkdir(mode=0o700, exist_ok=True)
            archived = history / (hashlib.sha256(previous).hexdigest() + '.yaml')
            if archived.is_symlink():
                raise CollectionError('Archived request must not be a symlink')
            if archived.exists():
                if archived.read_bytes() != previous:
                    raise CollectionError('Archived request evidence differs')
            else:
                _write_atomic(archived, previous, exclusive=True)
        request = {'version': 1, 'pages_requested': pages, 'next_page': DEFAULT_CATEGORY,
                   'requested_at': datetime.now(timezone.utc).isoformat(), 'status': 'pending',
                   'pages': {}, 'items': {}, 'submitted': 0}
        _write_atomic(target, yaml.safe_dump(request, sort_keys=False).encode())
        return {'status': 'recorded', 'pages_requested': pages, 'request': str(target),
                'previous_request': str(archived) if archived else None}


def _write_atomic(target: Path, content: bytes, *, exclusive: bool = False) -> None:
    fd, name = tempfile.mkstemp(prefix='.' + target.name, dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        if exclusive:
            temporary.chmod(0o400)
            os.link(temporary, target)
        else:
            os.replace(temporary, target)
        parent = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=Path('/data/booksys/state'))
    parser.add_argument('--pages', type=int, default=10)
    parser.add_argument('--archive-completed', action='store_true')
    args = parser.parse_args()
    try:
        print(json.dumps(record_request(args.directory, args.pages, archive_completed=args.archive_completed)))
        return 0
    except (CollectionError, OSError, ValueError, yaml.YAMLError) as exc:
        print(json.dumps({'status': 'blocked', 'reason': str(exc)}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
