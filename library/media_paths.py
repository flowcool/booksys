"""Configurable immutable media roots, separate from local PostgreSQL metadata."""
from __future__ import annotations

import os
from pathlib import Path

from library.primitives import LibraryError


def media_root(variable: str, default: Path) -> Path:
    configured = os.environ.get(variable)
    root = Path(configured) if configured else default
    if not root.is_absolute() or any(p.is_symlink() for p in (root, *root.parents)):
        raise LibraryError('Media roots must be absolute without symlinks')
    return root


def pieces_root(directory: Path) -> Path:
    return media_root('BOOKSYS_PIECES_ROOT', directory/'library-resolution/pieces')


def bundle_inputs(directory: Path) -> Path:
    return media_root('BOOKSYS_BUNDLE_INPUT_ROOT', directory/'bundle-inputs')


def writer_uid(path: Path) -> int:
    """Apply the qualified squash UID only on the two declared NAS exports."""
    configured = os.environ.get('BOOKSYS_NAS_WRITER_UID')
    if not configured:
        return os.getuid()
    from library.nas_storage import unescape
    matches = []
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        row = line.split()
        mount = Path(unescape(row[4]))
        if path.absolute().is_relative_to(mount):
            separator = row.index('-')
            matches.append((len(mount.parts), row[separator+1], row[separator+2]))
    if matches:
        _, fstype, source = max(matches)
        sources = (os.environ['BOOKSYS_NFS_FILES_SOURCE'], os.environ['BOOKSYS_NFS_LIBRARY_SOURCE'])
        if fstype == 'nfs' and any(source == base or source.startswith(base+'/') for base in sources):
            if configured != '1003':
                raise LibraryError('NAS writer UID differs from qualified squash identity')
            return 1003
    return os.getuid()
