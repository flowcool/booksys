"""Generate exact container NAS contracts from resolved Compose and host mounts.

Writes a new local configuration directory only. Does not create NAS paths,
change mounts, select catalogue authority, stop services or start containers.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from library.nas_storage import unescape


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compose-json', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    services = json.loads(args.compose_json.read_text())['services']
    mounts = [line.split() for line in Path('/proc/self/mountinfo').read_text().splitlines()]
    contracts = {}
    for role, service in services.items():
        environment = service.get('environment', {})
        if not environment.get('BOOKSYS_NAS_CONTRACT'):
            continue
        identities, sentinels = [], []
        for volume in service.get('volumes', []):
            if volume['type'] != 'bind':
                continue
            source = Path(volume['source'])
            if any(p.is_symlink() for p in (source, *source.parents)):
                raise SystemExit('Symlink in declared bind source')
            matches = [row for row in mounts if source.is_relative_to(unescape(row[4]))]
            row = max(matches, key=lambda row: len(unescape(row[4])))
            at = row.index('-')
            if row[at+1] != 'nfs':
                continue
            if unescape(row[at+2]) not in (environment.get('BOOKSYS_NFS_FILES_SOURCE'),
                                           environment.get('BOOKSYS_NFS_LIBRARY_SOURCE')):
                raise SystemExit('NAS export differs from the qualified environment')
            if 'hard' not in row[at+3].split(','):
                raise SystemExit('Declared NAS mount is not hard')
            relative = str(source.relative_to(unescape(row[4])))
            suffix = '/'.join(part.strip('/') for part in (unescape(row[3]), relative) if part not in ('/', '.'))
            # Qualified Docker29 on booksys-host presents an NFS subtree as the
            # source export plus its suffix, with mountinfo root '/'.
            export = unescape(row[at+2]).rstrip('/')+('/'+suffix if suffix else '')
            identities.append({'target': volume['target'], 'source': export,
                               'root': '/', 'readonly': bool(volume.get('read_only'))})
            sentinel = source/'.nas-identity'
            if sentinel.is_file():
                expected = ('4f34ea560d283a641de4daf0a32b3ceea7ef34882e90f11ef1a6214b8fb4fc40'
                    if unescape(row[at+2]) == environment['BOOKSYS_NFS_FILES_SOURCE'] else
                    '4aa71008faa5d97de5be8f542a1af9081dcb682427d839d4f84b46f0f597c75a')
                if hashlib.sha256(sentinel.read_bytes()).hexdigest() != expected:
                    raise SystemExit('NAS sentinel differs from Infra qualification')
                sentinels.append({'target': volume['target']+'/.nas-identity',
                                  'sha256': hashlib.sha256(sentinel.read_bytes()).hexdigest()})
        if not identities or not sentinels:
            raise SystemExit('NAS identities and retained sentinel are required')
        name = Path(environment['BOOKSYS_NAS_CONTRACT']).name
        contracts[name] = {'schema': 1, 'mounts': identities, 'sentinels': sentinels,
                          'probe_directory': environment.get('BOOKSYS_READER_JOBS', '/nas-tmp')
                              if role == 'reader-launcher' else '/nas-tmp'}
    if 'processing' in services:
        # Both fixed children have exactly the processing service mounts.
        for role in ('ocr', 'metadata'):
            contracts['nas-'+role+'.json'] = contracts['nas-processing.json']
    if not args.output.is_absolute() or any(p.is_symlink() for p in (args.output, *args.output.parents)):
        raise SystemExit('Use a new absolute local configuration directory')
    args.output.mkdir(parents=True, mode=0o700, exist_ok=False)
    for name, value in contracts.items():
        (args.output/name).write_text(json.dumps(value, indent=2)+'\n')
    print(f'Generated {len(contracts)} exact NAS contracts; no runtime or NAS change')


if __name__ == '__main__':
    main()
