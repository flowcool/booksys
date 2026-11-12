"""One-level bundle extraction; original member names are data only."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import resource
import stat
import struct
import subprocess
import sys
from typing import Any, Protocol


class ByteReader(Protocol):
    def read(self, size: int = -1) -> bytes: ...
import zipfile

from library.primitives import LibraryError

POLICY = 'comic-bundle-2'
MAX_SOURCE = 4 * 1024**3
MAX_CHILD = 1024**3
MAX_TOTAL = 4 * 1024**3
MAX_COMICS = 64
MAX_ENTRIES = 256


def extract(source: Path, output: Path, parts: list[Path] | None = None) -> dict[str, Any]:
    with source.open('rb') as handle:
        magic = handle.read(8)
    seen: set[str] = set()
    entries: list[dict[str, Any]] = []
    total = 0
    comics = 0

    def entry(name: str, size: int, directory: bool, linked: bool) -> dict[str, Any] | None:
        nonlocal total, comics
        path = PurePosixPath(name)
        if (not name or len(name) > 1024 or str(path) != name.rstrip('/') or path.is_absolute() or '..' in path.parts
                or '\\' in name or ':' in name or any(ord(c) < 32 for c in name)
                or name.casefold().rstrip('/') in seen or linked):
            raise LibraryError('Unsafe, duplicate or linked bundle member')
        seen.add(name.casefold().rstrip('/'))
        if len(seen) > MAX_ENTRIES:
            raise LibraryError('Bundle entry count limit')
        if directory:
            return None
        suffix = path.suffix.lower()
        ancillary = {'ComicInfo.xml': 1024**2, 'Thumbs.db': 4*1024**2, '.picasa.ini': 64*1024}
        if suffix in ('.pdf', '.cbz', '.cbr'):
            comics += 1
            if comics > MAX_COMICS or size <= 0 or size > MAX_CHILD:
                raise LibraryError('Bundle comic count or size limit')
            target = hashlib.sha256(name.encode()).hexdigest() + suffix
        elif path.name in ancillary and 0 <= size <= ancillary[path.name]:
            target = None
        else:
            raise LibraryError('Bundle contains unknown, recursive or mixed loose content')
        total += size
        if total > MAX_TOTAL:
            raise LibraryError('Bundle expansion limit')
        item = {'member': name, 'size': size, 'file': target, 'kind': 'comic' if target else 'ancillary'}
        entries.append(item)
        return item

    def consume(item: dict[str, Any], stream: ByteReader) -> None:
        remaining = item['size']
        digest = hashlib.sha256()
        header = b''
        destination = (output / item['file']).open('xb') if item['file'] else None
        try:
            while remaining:
                data = stream.read(min(1024**2, remaining))
                if not data:
                    raise LibraryError('Bundle member shorter than declared')
                header = (header + data[:8])[:8]
                remaining -= len(data)
                digest.update(data)
                if destination:
                    destination.write(data)
            if destination:
                destination.flush()
                os.fsync(destination.fileno())
        finally:
            if destination:
                destination.close()
        item['sha256'] = digest.hexdigest()
        if item['file']:
            suffix = Path(item['file']).suffix
            valid = (header.startswith(b'%PDF-') if suffix == '.pdf' else
                     header.startswith(b'PK\x03\x04') if suffix == '.cbz' else
                     header.startswith((b'Rar!\x1a\x07\x00', b'Rar!\x1a\x07\x01\x00')))
            if not valid:
                raise LibraryError('Bundle member signature differs from suffix')

    if parts and not magic.startswith((b'Rar!\x1a\x07\x00', b'Rar!\x1a\x07\x01\x00')):
        raise LibraryError('Unsupported multipart container; numbered RAR volumes only')
    if magic.startswith(b'PK\x03\x04'):
        with source.open('rb') as handle:
            handle.seek(max(0, source.stat().st_size - 65557))
            tail = handle.read(65557)
        end = tail.rfind(b'PK\x05\x06')
        if end < 0 or end + 22 > len(tail):
            raise LibraryError('Missing bundle ZIP end record')
        disk, central_disk, local, count = struct.unpack_from('<HHHH', tail, end+4)
        if disk or central_disk or local != count:
            raise LibraryError('Multipart bundle ZIP')
        with zipfile.ZipFile(source) as archive:
            checked = []
            for member in archive.infolist():
                mode = member.external_attr >> 16
                if member.flag_bits & 1 or member.volume:
                    raise LibraryError('Encrypted or multipart bundle ZIP')
                item = entry(member.filename, member.file_size, member.is_dir(),
                             stat.S_ISLNK(mode) or bool(stat.S_IFMT(mode) and not
                             (stat.S_ISREG(mode) or stat.S_ISDIR(mode))))
                if item:
                    checked.append((member, item))
            if not comics:
                raise LibraryError('Bundle has no complete comic members')
            for member, item in checked:
                with archive.open(member) as stream:
                    consume(item, stream)
                    if stream.read(1):
                        raise LibraryError('Bundle ZIP member exceeds declared size')
    elif magic.startswith((b'Rar!\x1a\x07\x00', b'Rar!\x1a\x07\x01\x00')):
        import rarfile  # type: ignore[import-untyped]
        tool = os.environ.get('BOOKSYS_UNRAR', 'unrar')
        endings: dict[Path, int] = {}
        def record_header(header: Any) -> None:
            if header.type == rarfile.RAR_BLOCK_ENDARC:
                endings[Path(header.volume_file)] = header.flags
        with rarfile.RarFile(source, info_callback=record_header if parts else None,
                             errors='strict' if parts else 'stop') as archive:
            members = archive.infolist()
            if archive.needs_password() or (not parts and len(archive.volumelist()) != 1):
                raise LibraryError('Encrypted or multipart bundle RAR')
            if parts:
                if set(endings) != set(parts) or any(
                        bool(endings[part] & rarfile.RAR_ENDARC_NEXT_VOLUME) != (index < len(parts)-1)
                        for index,part in enumerate(parts)):
                    raise LibraryError('Missing part or missing final RAR container end')
                if {Path(name) for name in archive.volumelist()} != set(parts):
                    raise LibraryError('Missing part or untracked RAR volume')
            rar_entries: list[dict[str, Any]] = []
            for member in members:
                if not parts and member.flags & (rarfile.RAR_FILE_SPLIT_BEFORE | rarfile.RAR_FILE_SPLIT_AFTER):
                    raise LibraryError('Multipart bundle RAR')
                item = entry(member.filename, member.file_size, member.is_dir(),
                             bool(member.is_symlink() or member.file_redir or
                                  not (member.is_file() or member.is_dir())))
                if item:
                    rar_entries.append(item)
            if not comics:
                raise LibraryError('Bundle has no complete comic members')
            if parts:
                # Native integrity check proves CRCs and final volume termination.
                integrity = subprocess.run([tool, 't', '-inul', '-p-', '--', str(source)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
                if integrity.returncode:
                    raise LibraryError('Missing part or RAR volume CRC/end validation failed')
            process = subprocess.Popen([tool, 'p', '-inul', '-p-', '--', str(source)],
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            try:
                assert process.stdout is not None
                for rar_item in rar_entries:
                    consume(rar_item, process.stdout)
                if process.stdout.read(1) or process.wait(timeout=20) != 0:
                    raise LibraryError('Bundle RAR CRC or decoded length failed')
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
    else:
        raise LibraryError('Unrecognized bundle container signature')
    result = {'schema': 1, 'policy': POLICY, 'members': entries, 'expanded_size': total}
    if parts:
        result['volume_end_verified'] = True
    return result


def main() -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    try:
        source, output = Path(sys.argv[1]), Path(sys.argv[2])
        parts = [Path(value) for value in sys.argv[3:]] or None
        if not 0 < sum(path.stat().st_size for path in (parts or [source])) <= MAX_SOURCE:
            raise LibraryError('Bundle source size limit')
        result = extract(source, output, parts)
        from library.readers import sha256
        result['parent_sha256'] = sha256(source)
        print(json.dumps({'status': 'extracted', **result}))
    except Exception as exc:
        print(json.dumps({'status': 'pending' if isinstance(exc, (subprocess.TimeoutExpired, TimeoutError, MemoryError)) else 'rejected',
                          'reason': type(exc).__name__ + ': ' + str(exc)}))


if __name__ == '__main__':
    main()
