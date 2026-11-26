"""One verified input resolver for direct and bundle-derived assets."""
from __future__ import annotations

from pathlib import Path
from typing import Any
import os
import re
from stat import S_ISREG

from library.catalog import Catalog
from library.relocation import matches_retained
from library.primitives import LibraryError, digest
from library.readers import fingerprint, sha256
from library.reception import reception_path
from library.telegram_paths import ARCHIVE_PREFIX, ARCHIVE_ROOT, archive_relative


class TelegramSourceUnavailable(OSError):
    """The archive source is temporarily unavailable."""


def is_archive_origin(origin: dict[str, Any]) -> bool:
    return "telegram" in origin and origin["path"].startswith(ARCHIVE_PREFIX)


def external_source_path(origin: dict[str, Any]) -> Path:
    from library.intake import EXTERNAL_ROOT

    if is_archive_origin(origin):
        telegram = origin["telegram"]
        relative = archive_relative(origin["path"], telegram["channel"], telegram["topic"],
                                    telegram["message"], telegram["original_name"])
        root = ARCHIVE_ROOT
    else:
        relative = origin["path"]
        root = EXTERNAL_ROOT
    path = Path(relative)
    return reception_path(root, str(path.parent), path.name)


def archive_source_wait(origin: dict[str, Any]) -> bool:
    try:
        path = external_source_path(origin)
        mode = path.lstat().st_mode
        if not S_ISREG(mode):
            raise LibraryError("Telegram archive source is not a regular file")
        return not mode & 0o444 or not os.access(path, os.R_OK)
    except OSError:
        return True


def archive_source_stamp(origin: dict[str, Any]) -> tuple[Path, tuple[int, int, int, int, int]]:
    try:
        path = external_source_path(origin)
        stamp = fingerprint(path)
        if stamp[2] != origin["size"]:
            raise LibraryError("Telegram archive file size differs from the receipt")
    except OSError as exc:
        raise TelegramSourceUnavailable("Telegram archive source unavailable") from exc
    return path, stamp


def verify_source_parts(reception: Path, parent: dict[str, Any], manifest: dict[str, Any]) -> None:
    parts = parent.get('source_parts')
    if not parts:
        if manifest.get('source_parts'):
            raise LibraryError('Multipart canonical provenance is missing')
        return
    expected = [{'name': Path(p['path']).name, 'sha256': p['sha256'], 'size': p['fingerprint'][2]} for p in parts]
    names = [re.fullmatch(r'(.+)\.part([0-9]{1,3})\.rar', p['name'], re.I) for p in expected]
    if (not 2 <= len(parts) <= 64 or any(m is None for m in names)
            or len({m[1] for m in names if m}) != 1
            or [int(m[2]) for m in names if m] != list(range(1, len(parts) + 1))
            or len({p['fid'] for p in parts}) != len(parts)
            or manifest.get('volume_end_verified') is not True
            or manifest.get('source_parts') != expected
            or parent.get('source_set_sha256') != digest(expected)
            or parts[0]['path'] != parent['path'] or parts[0]['sha256'] != parent['sha256']):
        raise LibraryError('Multipart complete-set or container-end proof is missing')
    for part in parts:
        relative = Path(part['path'])
        path = reception_path(reception, str(relative.parent), relative.name)
        if not matches_retained(path, part['fingerprint'], part['sha256']) or sha256(path) != part['sha256']:
            raise LibraryError('Multipart source part changed')


def resolve_asset_origin(store: Catalog, reception: Path, state: dict[str, Any], asset_id: str) -> tuple[str, Path]:
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
                # Native package absence does not invalidate retained bundle bytes.
                # Existing package_missing parents still need every proof below.
                if (parent.get('availability') not in ('bundle', 'package_missing')
                        or parent.get('sha256') != origin['parent_sha256']
                        or not matches_retained(parent_path, parent.get('fingerprint', []), origin['parent_sha256'])
                        or sha256(parent_path) != origin['parent_sha256']):
                    raise LibraryError('Bundle parent changed or unavailable')
                from library.media_paths import bundle_inputs
                directory = bundle_inputs(store.directory) / str(origin['parent_sha256'])
                manifest = verify_directory(directory, origin['parent_sha256'])
                verify_source_parts(reception, parent, manifest)
                if manifest['members'] != parent.get('bundle', {}).get('members'):
                    raise LibraryError('Retained bundle manifest changed from canonical parent')
                member = next((item for item in manifest['members'] if item['member'] == origin['member']), None)
                if (member is None or member['sha256'] != asset_id or member['size'] != origin['size']
                        or origin['path'] != str(Path('bundle-inputs') / origin['parent_sha256'] / member['file'])):
                    raise LibraryError('Derived bundle provenance changed')
                path = directory / str(member['file'])
            elif is_archive_origin(origin):
                path, stamp = archive_source_stamp(origin)
                if sha256(path) != asset_id or fingerprint(path) != stamp:
                    raise LibraryError('Telegram archive file SHA-256 differs from the receipt or the file changed')
                return oid, path
            elif origin['source'].startswith('external:'):
                path = external_source_path(origin)
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
