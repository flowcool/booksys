"""Select only known Booksys submission origins; never scan shared reception."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from library.catalog import Catalog, LibraryError, digest, now
from library.readers import inspect_file


def reception_path(root: Path, folder: str, name: str) -> Path:
    relative = Path(folder) / name
    if Path(folder).is_absolute() or Path(name).name != name or '..' in relative.parts:
        raise LibraryError('Unsafe pyLoad reception path')
    path = root / relative
    if any(p.is_symlink() for p in (path, *path.parents)) or not path.resolve().is_relative_to(root.resolve()):
        raise LibraryError('Reception escape or symlink')
    return path


def receive(store: Catalog, root: Path, captured: dict[str, Any], journal: dict[str, Any],
            queue: list[dict[str, Any]], *, stable_seconds: float = 2) -> dict[str, Any]:
    # The caller obtains immutable snapshots without holding a collector lock.
    # Parsing and hashing happen before the short catalogue compare-and-save.
    state = store.read()
    packages = {int(p['pid']): p for p in queue}
    for source, submission in journal.get('submissions', {}).items():
        host = urlsplit(source).hostname or ''
        if (source not in captured.get('items', {}) or submission.get('status') != 'queued'
                or not (host == 'bookys-ebooks.com' or host.endswith('.bookys-ebooks.com'))):
            continue
        pid = submission['package_id']
        package = packages.get(pid)
        if package is None:
            for origin in state['origins'].values():
                if origin['pid'] == pid and origin['source'] == source:
                    origin['availability'] = 'package_missing'
            continue
        if package['name'] != submission['name']:
            raise LibraryError('Tracked pyLoad package identity changed')
        for item in package.get('links', []):
            fid = item['fid']
            oid = digest({'source': source, 'pid': pid, 'fid': fid})
            name = item['name']
            if name == 'fiche.md' or name.startswith('couverture.'):
                continue
            origin = {'source': source, 'pid': pid, 'fid': fid,
                      'path': str(Path(package['folder']) / name), 'sha256': None,
                      'observed_at': now(), 'availability': 'pending'}
            try:
                path = reception_path(root, package['folder'], name)
                # Exact pinned pyLoad status: core/datatypes/pyfile.py: finished = 0.
                if type(item.get('status')) is not int or item['status'] != 0:
                    origin['reason'] = 'Transfer is not confirmed finished'
                elif not path.exists():
                    origin.update(availability='missing', reason='Finished origin is missing or renamed')
                elif path.stat().st_size != item.get('size'):
                    origin['reason'] = 'pyLoad size and reception size differ'
                else:
                    result = inspect_file(path, stable_seconds=stable_seconds)
                    origin.update(availability=result['status'], reason=result.get('reason', ''),
                                  sha256=result.get('sha256'))
                    if result['status'] == 'validated':
                        key = result['sha256']
                        old_origin = state['origins'].get(oid)
                        if old_origin and old_origin['sha256'] and old_origin['sha256'] != key:
                            raise LibraryError('Origin bytes changed after identification')
                        asset = state['assets'].setdefault(key, {**result, 'origins': [], 'assertions': [],
                            'overrides': {}, 'disposition': 'pending', 'captured_hints': {}})
                        if not asset['assertions']:
                            for field, raw_field in [('genres', 'Genre'), ('public', 'AgeRating')]:
                                raw = result.get('embedded', {}).get(raw_field)
                                if raw:
                                    asset['assertions'].append({'field': field, 'raw': raw,
                                        'normalized': [x.strip() for x in raw.split(',')] if field == 'genres' else raw,
                                        'source': 'embedded', 'upstream': 'embedded-unverified',
                                        'url': 'sha256:' + key, 'retrieved_at': now(), 'validation': 'asserted'})
                        if oid not in asset['origins']:
                            asset['origins'].append(oid)
                        asset['captured_hints'][source] = captured['items'][source]
            except (LibraryError, OSError) as exc:
                origin.update(availability='rejected', reason=str(exc))
            state['origins'][oid] = origin
    store.save(state, expected=state['revision'])
    return store.read()
