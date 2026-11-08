"""Read-only exact-origin reader/CBZ qualification. No catalogue or identity write."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from library.catalog import Catalog, LibraryError, atomic_yaml, now
from library.intake import reception_path
from library.readers import fingerprint, inspect_file, sha256
from library.transform import transform_archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--reception', type=Path, required=True)
    parser.add_argument('--origin', action='append', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if len(args.origin) > 3 or len(set(args.origin)) != len(args.origin):
        parser.error('Select one to three distinct exact origins')
    state = Catalog(args.state).read()
    proof: dict[str, Any] = {'created_at': now(), 'purpose': 'Technical qualification only; no identity admission', 'origins': {}}
    for oid in args.origin:
        origin = state['origins'][oid]
        relative = Path(origin['path'])
        source = reception_path(args.reception, str(relative.parent), relative.name)
        stamp, before = fingerprint(source), sha256(source)
        if origin.get('sha256') and origin['sha256'] != before:
            raise ValueError('Source differs from canonical origin hash')
        result = inspect_file(source, stable_seconds=.01)
        record: dict[str, Any] = {'source_sha256': before, 'validation': {k: v for k, v in result.items() if k != 'cover'}}
        if result['status'] == 'validated':
            # This XML describes an isolated probe, never a publishable catalogue identity.
            with tempfile.TemporaryDirectory(prefix='booksys-format-readback-') as folder:
                try:
                    transformed = transform_archive(source, Path(folder),
                        b'<ComicInfo><Title>Isolated format qualification</Title></ComicInfo>', before)
                    record['transformation'] = transformed
                    record['artifact_readback'] = {k: v for k, v in inspect_file(Path(folder) / 'book.cbz', stable_seconds=.01).items() if k != 'cover'}
                except LibraryError as exc:
                    record['transform_failure'] = str(exc)

        record['source_unchanged'] = fingerprint(source) == stamp and sha256(source) == before
        proof['origins'][oid] = record
        atomic_yaml(args.output, proof)
        if not record['source_unchanged']:
            raise ValueError('Source changed during qualification')
    good = all(r['validation']['status'] == 'validated' and r.get('artifact_readback', {}).get('status') == 'validated'
               for r in proof['origins'].values())
    print(json.dumps({'origins': len(proof['origins']), 'all_validated': good, 'evidence': str(args.output)}))
    return 0 if good else 2


if __name__ == '__main__':
    raise SystemExit(main())
