"""One verified input resolver for direct and bundle-derived assets."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from library.catalog import Catalog, LibraryError
from library.readers import fingerprint, sha256


def resolve_asset_origin(store: Catalog, reception: Path, state: dict[str, Any], asset_id: str) -> tuple[str, Path]:
    from library.intake import reception_path
    from library.bundles import verify_directory
    errors = []
    for oid in state['assets'][asset_id]['origins']:
        origin = state['origins'][oid]
        if origin.get('availability') != 'validated' or origin.get('sha256') != asset_id:
            continue
        try:
            if origin.get('parent_origin'):
                parent = state['origins'][origin['parent_origin']]
                relative = Path(parent['path'])
                parent_path = reception_path(reception, str(relative.parent), relative.name)
                if (parent.get('availability') != 'bundle' or parent.get('sha256') != origin['parent_sha256']
                        or list(fingerprint(parent_path)) != parent.get('fingerprint')
                        or sha256(parent_path) != origin['parent_sha256']):
                    raise LibraryError('Bundle parent changed or unavailable')
                directory = store.directory / 'bundle-inputs' / str(origin['parent_sha256'])
                manifest = verify_directory(directory, origin['parent_sha256'])
                if manifest['members'] != parent.get('bundle', {}).get('members'):
                    raise LibraryError('Retained bundle manifest changed from canonical parent')
                member = next((item for item in manifest['members'] if item['member'] == origin['member']), None)
                if (member is None or member['sha256'] != asset_id or member['size'] != origin['size']
                        or origin['path'] != str(Path('bundle-inputs') / origin['parent_sha256'] / member['file'])):
                    raise LibraryError('Derived bundle provenance changed')
                path = directory / str(member['file'])
            else:
                relative = Path(origin['path'])
                path = reception_path(reception, str(relative.parent), relative.name)
            if sha256(path) != asset_id:
                raise LibraryError('Origin bytes changed')
            return oid, path
        except (LibraryError, OSError, KeyError) as exc:
            errors.append(str(exc))
    raise LibraryError('; '.join(errors) if errors else 'No validated origin with matching asset hash is available')


def resolve_asset(store: Catalog, reception: Path, state: dict[str, Any], asset_id: str) -> Path:
    return resolve_asset_origin(store, reception, state, asset_id)[1]
