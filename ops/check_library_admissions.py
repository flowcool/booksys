"""Read back managed CBZ pages/XML and exact original hashes without mutation."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from library.admission import AdmissionPublisher
from library.catalog import Catalog, LibraryError, lock
from library.origins import resolve_asset
from library.publish import verify
from library.readers import inspect_file, sha256
from library.xmlmetadata import validate_xml


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--reception', type=Path, default=Path('/data/booksys/downloads'))
    parser.add_argument('--asset', action='append', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    store = Catalog(args.root/'state')
    rows = []
    with lock(store.directory/'library-copy.lock', blocking=False):
        state = store.read()
        writer = AdmissionPublisher(store, args.reception, args.root/'library', lab=True)
        for key in args.asset:
            asset = state['assets'][key]
            record = asset['admission']
            source = resolve_asset(store, args.reception, state, key)
            if sha256(source) != key:
                raise LibraryError('Original changed during readback')
            target = writer.target_path(record['target'])
            verify(target, record['manifest'])
            readback = inspect_file(target/'book.cbz', stable_seconds=.01)
            if (readback['status'] != 'validated' or
                    readback['page_manifest'] != record['transformation']['page_manifest']):
                raise LibraryError('Independent output page readback differs')
            with zipfile.ZipFile(target/'book.cbz') as archive:
                xml = archive.read('ComicInfo.xml')
            validate_xml(xml)
            if hashlib.sha256(xml).hexdigest() != record['transformation']['xml_sha256']:
                raise LibraryError('Independent ComicInfo readback differs')
            rows.append({'asset': key, 'status': record['status'], 'target': record['target'],
                'original_unchanged': True, 'pages': readback['pages'],
                'cbz_sha256': readback['sha256'], 'xml_sha256': hashlib.sha256(xml).hexdigest(),
                'xml_base64': base64.b64encode(xml).decode(), 'operation': record['operation']})
    args.output.write_text(json.dumps({'schema': 1, 'readback': rows}, indent=2))
    print(json.dumps({'verified': len(rows), 'statuses': sorted({r['status'] for r in rows})}))


if __name__ == '__main__':
    main()
