"""Sandboxed CBR/CBZ to CBZ transformation using UnRAR and Python ZipFile."""
from __future__ import annotations

import base64
import json
import hashlib
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from library.catalog import LibraryError, sync_dir
from library.readers import fingerprint, inspect_file, sha256
from library.xmlmetadata import validate_xml

VERSION = 'booksys-cbz-1'


def transform_archive(source: Path, destination: Path, xml: bytes, asset_hash: str,
                      *, check_capacity: Callable[[], None] = lambda: None) -> dict[str, Any]:
    validate_xml(xml)
    source, destination = source.absolute(), destination.absolute()
    before = fingerprint(source)
    if sha256(source) != asset_hash or source.suffix.lower() not in ('.cbr', '.cbz'):
        raise LibraryError('Transform source differs from identified comic asset')
    if any(path.is_symlink() for path in (source, destination, *source.parents, *destination.parents)):
        raise LibraryError('Symlink in transformation paths')
    if list(destination.iterdir()):
        raise LibraryError('Transformation destination must be empty')
    image = os.environ.get('BOOKSYS_READER_IMAGE')
    if image:
        if not re.fullmatch(r'[a-z0-9./:_@-]+', image) or ',' in str(source) + str(destination):
            raise LibraryError('Unsafe Docker reader argument')
        command = ['docker', 'run', '--rm', '-i', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges', '--user', f'{os.getuid()}:{os.getgid()}',
            '--memory', '1536m', '--cpus', '1', '--pids-limit', '32',
            '--mount', f'type=bind,src={source},dst=/input,readonly',
            '--mount', f'type=bind,src={destination},dst=/output',
            '--entrypoint', '/app/.venv/bin/python', image, '-m', 'library.reader_worker',
            '/input', source.suffix.lower(), '--transform', '/output/book.cbz']
    else:
        from library.sandbox import native_prefix, PYTHON
        command = native_prefix(source, output=destination) + [PYTHON, '-m', 'library.reader_worker',
            '/tmp/input', source.suffix.lower(), '--transform', '/tmp/output/book.cbz']
    request = json.dumps({'comicinfo': base64.b64encode(xml).decode()}).encode()
    import time
    started = time.monotonic()
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        check_capacity()
        payload: bytes | None = request
        while True:
            try:
                stdout, stderr = process.communicate(payload, timeout=.1)
                break
            except subprocess.TimeoutExpired:
                payload = None
                check_capacity()
                if time.monotonic() - started > 60:
                    raise LibraryError('Comic transformation exceeded time limit')
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    if process.returncode:
        raise LibraryError('Isolated comic transformation failed')
    outcome: dict[str, Any] = json.loads(stdout)
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
