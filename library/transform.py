"""PDF/CBR/CBZ to CBZ transformation using UnRAR and Python ZipFile."""
from __future__ import annotations

import base64
import hashlib
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from library.primitives import LibraryError, sync_dir
from library.readers import fingerprint, inspect_file, sha256
from library.xmlmetadata import validate_xml

VERSION = 'booksys-cbz-2'


def transform_archive(source: Path, destination: Path, xml: bytes, asset_hash: str,
                      *, check_capacity: Callable[[], None] = lambda: None) -> dict[str, Any]:
    validate_xml(xml)
    source, destination = source.absolute(), destination.absolute()
    before = fingerprint(source)
    if sha256(source) != asset_hash or source.suffix.lower() not in ('.cbr', '.cbz', '.pdf'):
        raise LibraryError('Transform source differs from identified comic asset')
    if any(path.is_symlink() for path in (source, destination, *source.parents, *destination.parents)):
        raise LibraryError('Symlink in transformation paths')
    if list(destination.iterdir()):
        raise LibraryError('Transformation destination must be empty')
    from library.decode import run
    check_capacity()
    outcome = run('transform', source, {'comicinfo': base64.b64encode(xml).decode()}, destination)
    check_capacity()
    if outcome['status'] != 'validated' or outcome.get('input_sha256') != asset_hash:
        raise LibraryError('Comic transformation blocked: ' + outcome.get('reason', 'input hash mismatch'))
    output = destination / 'book.cbz'
    if fingerprint(source) != before or sha256(source) != asset_hash or sha256(output) != outcome['artifact_sha256']:
        raise LibraryError('Transformation bytes or original changed')
    parsed = inspect_file(output, stable_seconds=.01)
    if (parsed['status'] != 'validated' or parsed['page_manifest'] != outcome['page_manifest']
            or parsed.get('ancillary_manifest') != outcome.get('ancillary_manifest')):
        raise LibraryError('Generated CBZ failed independent page/content readback')
    with output.open('rb') as handle:
        os.fsync(handle.fileno())
    sync_dir(destination)
    return {**outcome, 'transform_version': VERSION, 'xml_sha256': hashlib.sha256(xml).hexdigest(),
            'source_asset_sha256': asset_hash}
