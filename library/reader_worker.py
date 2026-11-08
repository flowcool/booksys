"""Internal sandbox entry point. Never extract or execute archive contents."""
from __future__ import annotations

import io
import base64
import json
import hashlib
import os
import resource
import re
import stat
import struct
import sys
import warnings
import zipfile
import subprocess
import platform
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree

import pikepdf
from PIL import Image

from library.catalog import LibraryError
from library.readers import READER_POLICY

MAX_MEMBER = 64 * 1024 ** 2
MAX_EXPANDED = 2 * 1024 ** 3
MAX_PAGES = 2000
Image.MAX_IMAGE_PIXELS = 50_000_000


class PendingFile(LibraryError):
    """A recognized representation outside the V1 single-work scope."""


def metadata(data: bytes) -> dict[str, str]:
    if len(data) > 1024 ** 2 or b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise LibraryError('Unsafe or oversized XML')
    root = ElementTree.fromstring(data)
    if root.tag != 'ComicInfo':
        raise LibraryError('Unknown embedded metadata schema')
    fields = ('Title', 'Series', 'Number', 'Volume', 'Writer', 'Penciller', 'Publisher',
              'Year', 'LanguageISO', 'ISBN', 'GTIN', 'Genre', 'AgeRating', 'Summary', 'Web',
              'EditionDate', 'EditionRevision')
    return {key: str(root.findtext(key)) for key in fields if root.findtext(key)}


def member(name: str, data: bytes, output: dict[str, Any]) -> None:
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name:
        raise LibraryError('Unsafe archive path')
    if path.name == '.picasa.ini':
        if len(data) > 64 * 1024:
            raise LibraryError('Oversized Picasa ancillary file')
        output.setdefault('ignored_ancillary', []).append(name)
        return
    if path.name == 'Thumbs.db' and len(data) <= 4 * 1024**2:
        if any(PurePosixPath(n).name == 'Thumbs.db' for n in output.get('ignored_ancillary', [])):
            raise LibraryError('Several ancillary database files')
        output.setdefault('ignored_ancillary', []).append(name)
        return
    if path.name == 'ComicInfo.xml':
        if output['embedded']:
            raise LibraryError('Several ComicInfo records')
        output['embedded'] = metadata(data)
        output['source_comicinfo_base64'] = base64.b64encode(data).decode('ascii')
    elif path.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp', '.gif'):
        with warnings.catch_warnings():
            warnings.simplefilter('error')
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in ('JPEG', 'PNG', 'WEBP', 'GIF'):
                    raise LibraryError('Misleading image suffix')
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                if not output.get('cover'):
                    image.thumbnail((320, 480))
                    cover = io.BytesIO()
                    image.convert('RGB').save(cover, format='PNG')
                    output['cover'] = base64.b64encode(cover.getvalue()).decode('ascii')
        output['pages'] += 1
        output['folders'].add(str(path.parent))
    else:
        if path.suffix.lower() in ('.pdf', '.zip', '.rar', '.cbz', '.cbr'):
            raise PendingFile('Nested container or multi-work batch')
        raise LibraryError('Nested container or unauthorized archive content')


def inspect_archive(path: Path, suffix: str, *, sink: Callable[[str, bytes], None] | None = None) -> dict[str, Any]:
    output: dict[str, Any] = {'embedded': {}, 'pages': 0, 'folders': set()}
    expanded = 0
    count = 0
    seen: set[str] = set()
    output['page_manifest'] = []
    output['ancillary_manifest'] = {}
    output['omitted_ancillary_manifest'] = {}
    def consume(name: str, data: bytes) -> None:
        if name.casefold() in seen:
            raise LibraryError('Duplicate or case-ambiguous archive member')
        seen.add(name.casefold())
        before = output['pages']
        member(name, data, output)
        if output['pages'] > before:
            output['page_manifest'].append({'name': name, 'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)})
        elif PurePosixPath(name).name == '.picasa.ini':
            output['omitted_ancillary_manifest'][name] = hashlib.sha256(data).hexdigest()
        elif PurePosixPath(name).name != 'ComicInfo.xml':
            output['ancillary_manifest'][name] = hashlib.sha256(data).hexdigest()
        if sink and PurePosixPath(name).name not in ('ComicInfo.xml', '.picasa.ini'):
            sink(name, data)
    if suffix == '.cbz':
        with path.open('rb') as handle:
            handle.seek(max(0, path.stat().st_size - 65557))
            tail = handle.read(65557)
        end = tail.rfind(b'PK\x05\x06')
        if end < 0 or end + 22 > len(tail):
            raise LibraryError('Missing ZIP end record')
        disk, directory_disk, on_disk, total = struct.unpack_from('<HHHH', tail, end + 4)
        if disk or directory_disk or on_disk != total:
            raise PendingFile('Multipart ZIP')
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_PAGES + 100:
                raise LibraryError('Archive member count limit')
            for entry in entries:
                if entry.volume:
                    raise PendingFile('Multipart ZIP')
                mode = entry.external_attr >> 16
                if stat.S_ISLNK(mode) or entry.flag_bits & 1:
                    raise LibraryError('Encrypted archive or symlink')
                if entry.is_dir():
                    if '..' in PurePosixPath(entry.filename).parts:
                        raise LibraryError('Unsafe archive directory')
                    continue
                expanded += entry.file_size
                if entry.file_size > MAX_MEMBER or expanded > MAX_EXPANDED:
                    raise LibraryError('Archive expansion limit')
                consume(entry.filename, archive.read(entry))
    else:
        import rarfile  # type: ignore[import-untyped]
        rarfile.UNRAR_TOOL = os.environ.get('BOOKSYS_UNRAR', 'unrar')
        rarfile.USE_EXTRACT_HACK = False
        rarfile.tool_setup(unrar=True, unar=False, bsdtar=False, sevenzip=False, sevenzip2=False)
        try:
            with rarfile.RarFile(path) as archive:
                if archive.needs_password():
                    raise LibraryError('Encrypted RAR')
                if len(archive.volumelist()) != 1:
                    raise PendingFile('Multipart RAR')
                if any(x.flags & (rarfile.RAR_FILE_SPLIT_BEFORE | rarfile.RAR_FILE_SPLIT_AFTER)
                       for x in archive.infolist()):
                    raise PendingFile('Multipart RAR')
                entries = archive.infolist()
                for entry in entries:
                    count += 1
                    if count > MAX_PAGES + 100 or entry.is_symlink() or entry.file_redir:
                        raise LibraryError('RAR count limit or links')
                    if entry.is_dir():
                        continue
                    if not entry.is_file() or entry.file_size > MAX_MEMBER:
                        raise LibraryError('RAR member type or size limit')
                    expanded += entry.file_size
                    if expanded > MAX_EXPANDED:
                        raise LibraryError('RAR expansion limit')
                # One sequential UnRAR process avoids decoding each solid block
                # once per page. UnRAR validates CRCs and writes only to stdout.
                process = subprocess.Popen([rarfile.UNRAR_TOOL, 'p', '-inul', '-p-', '--', str(path)],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                try:
                    assert process.stdout is not None
                    for entry in entries:
                        if entry.is_dir():
                            continue
                        data = process.stdout.read(entry.file_size)
                        if len(data) != entry.file_size:
                            raise LibraryError('RAR member length mismatch')
                        consume(entry.filename, data)
                    if process.stdout.read(1) or process.wait(timeout=10) != 0:
                        raise LibraryError('RAR decoder failed CRC or length validation')
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait()
        except rarfile.NeedFirstVolume as exc:
            raise PendingFile('Multipart RAR') from exc
    if not output['pages'] or output['pages'] > MAX_PAGES:
        raise LibraryError('Empty archive or page count limit')
    chapter_numbers: dict[str, int] = {}
    if len(output['folders']) > 1:
        folders = [PurePosixPath(folder) for folder in output['folders']]
        chapters = [re.fullmatch(r'(?:chapitre|chapter)[ _.-]+([0-9]+)', folder.name, re.I)
                    for folder in folders]
        numbers = [int(match.group(1)) for match in chapters if match]
        if (len({str(folder.parent) for folder in folders}) != 1
                or len(numbers) != len(folders) or len(set(numbers)) != len(numbers)
                or sorted(numbers) != list(range(min(numbers), max(numbers) + 1))):
            return {'status': 'pending', 'reason': 'Multiple image groups: possible multi-work archive'}
        chapter_numbers = {str(folder): number for folder, number in zip(folders, numbers)}
        output['layout'] = 'single-volume-numbered-chapters'

    names = [entry['name'] for entry in output['page_manifest']]
    def natural(name: str) -> tuple[Any, ...]:
        page = PurePosixPath(name)
        value = page.name if chapter_numbers else name
        parts = tuple(int(part) if part.isdigit() else part.casefold() for part in re.split(r'(\d+)', value))
        return (chapter_numbers[str(page.parent)], *parts) if chapter_numbers else parts
    ordered_names: dict[tuple[Any, ...], str] = {}
    for name in names:
        key = natural(name)
        if key in ordered_names:
            raise PendingFile('Ambiguous page naming/order: ' +
                repr(ordered_names[key])[:180] + ' and ' + repr(name)[:180])
        ordered_names[key] = name
    output['page_manifest'].sort(key=lambda entry: natural(entry['name']))
    del output['folders']
    return {'status': 'validated', 'format': suffix[1:], **output}


def inspect_pdf(path: Path) -> dict[str, Any]:
    with pikepdf.open(path, attempt_recovery=False) as pdf:
        if pdf.is_encrypted:
            raise LibraryError('Encrypted PDF')
        warnings_found = pdf.check_pdf_syntax()
        fatal_warnings = [warning for warning in warnings_found if not re.fullmatch(
            r'WARNING: [^\n]+: incorrect object count in outline hint table', warning)]
        if not 0 < len(pdf.pages) <= MAX_PAGES or fatal_warnings:
            raise LibraryError('Unreadable PDF or page limit')
        forbidden = {'/AA', '/JS', '/JavaScript', '/EmbeddedFiles',
                     '/EF', '/RichMedia', '/AcroForm', '/XFA', '/Launch', '/Collection'}
        for obj in pdf.objects:
            if isinstance(obj, (pikepdf.Dictionary, pikepdf.Stream)):
                if forbidden.intersection(str(key) for key in obj.keys()):
                    raise LibraryError('Active PDF content or attachment')
                for action_key in ('/A', '/OpenAction'):
                    if action_key not in obj:
                        continue
                    action = obj[action_key]
                    if (not isinstance(action, pikepdf.Dictionary) or str(action.get('/S', '')) != '/GoTo'
                            or set(map(str, action.keys())) - {'/S', '/D', '/Type'}):
                        raise LibraryError('Active or external PDF action')
                    destination = action.get('/D')
                    if (not isinstance(destination, pikepdf.Array) or len(destination) < 2
                            or destination[0].objgen not in {page.obj.objgen for page in pdf.pages}
                            or str(destination[1]) not in ('/Fit', '/FitB', '/XYZ', '/FitH', '/FitV', '/FitR', '/FitBH', '/FitBV')):
                        raise LibraryError('Unresolved or external PDF destination')
                if str(obj.get('/Type', '')) == '/Filespec':
                    raise LibraryError('PDF attachment')
        # Decode every stream. Syntax alone does not check compressed page bytes.
        expanded = 0
        for obj in pdf.objects:
            if isinstance(obj, pikepdf.Stream):
                try:
                    if str(obj.get('/Subtype', '')) == '/Image':
                        width, height = int(obj.get('/Width', 0)), int(obj.get('/Height', 0))
                        if width <= 0 or height <= 0 or width * height > 50_000_000:
                            raise LibraryError('PDF image pixel limit')
                        image = pikepdf.PdfImage(obj).as_pil_image()
                        if image.width * image.height > 50_000_000:
                            raise LibraryError('PDF image pixel limit')
                        image.load()
                        size = image.width * image.height * len(image.getbands())
                    else:
                        size = len(obj.read_bytes())
                    expanded += size
                    if size > MAX_MEMBER or expanded > MAX_EXPANDED:
                        raise LibraryError('PDF stream size limit')
                except pikepdf.PdfError as exc:
                    raise LibraryError('PDF stream cannot be validated') from exc
        for page in pdf.pages:
            list(pikepdf.parse_content_stream(page))
        embedded = {str(k).lstrip('/'): str(v) for k, v in pdf.docinfo.items()}
        return {'status': 'validated', 'format': 'pdf', 'pages': len(pdf.pages),
                'embedded': embedded, 'reader_warnings': warnings_found}


def main() -> None:
    pdf_transform = '--transform' in sys.argv and len(sys.argv) > 2 and sys.argv[2] == '.pdf'
    cpu_limit = 1800 if pdf_transform else 30
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_limit))
    resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024 ** 2, 1536 * 1024 ** 2))
    transform = len(sys.argv) > 3 and sys.argv[3] == '--transform'
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_EXPANDED if transform else 0, MAX_EXPANDED if transform else 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    path, suffix = Path(sys.argv[1]), sys.argv[2]
    output: dict[str, Any]
    try:
        file_hash = hashlib.sha256()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as handle:
            while chunk := handle.read(1024 * 1024):
                file_hash.update(chunk)
        with path.open('rb') as handle:
            magic = handle.read(8)
        real = '.pdf' if magic.startswith(b'%PDF-') else '.cbz' if magic.startswith(b'PK\x03\x04') else '.cbr' if magic.startswith(b'Rar!\x1a\x07') else None
        if suffix != real:
            output = {'status': 'pending', 'reason': 'Suffix and actual format differ'}
        else:
            if transform:
                from library.xmlmetadata import validate_xml
                request = json.loads(sys.stdin.buffer.read(2 * 1024**2))
                xml = base64.b64decode(request['comicinfo'], validate=True)
                validate_xml(xml)
                destination = Path(sys.argv[4])
                with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_STORED) as archive:
                    def write_member(name: str, data: bytes) -> None:
                        info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                        info.external_attr = 0o100600 << 16
                        archive.writestr(info, data)
                    if suffix == '.pdf':
                        from library.pdf_pages import convert_pdf_pages
                        output = convert_pdf_pages(path, write_member)
                    else:
                        output = inspect_archive(path, suffix, sink=write_member)
                    write_member('ComicInfo.xml', xml)
                if output['status'] != 'validated':
                    raise PendingFile(output['reason'])
                from library.readers import sha256
                output['artifact_sha256'] = sha256(destination)
                output['tool_versions'] = {**output.get('tool_versions', {}), 'python': platform.python_version(), 'archive_writer': 'stdlib.zipfile',
                    'xml_schema': 'ComicInfo-2.0', 'schema_sha256': sha256(Path(__file__).parent / 'schemas/ComicInfo-2.0.xsd')}
                if suffix == '.cbr':
                    import rarfile
                    version = subprocess.run([rarfile.UNRAR_TOOL, '-?'], capture_output=True, timeout=3, check=False)
                    output['tool_versions']['rarfile'] = rarfile.__version__
                    output['tool_versions']['unrar'] = version.stdout.decode(errors='replace').splitlines()[1:3]
                os.chmod(destination, 0o600)
            else:
                output = inspect_pdf(path) if suffix == '.pdf' else inspect_archive(path, suffix)
    except PendingFile as exc:
        output = {'status': 'pending', 'reason': str(exc)}
    except Exception as exc:
        unavailable = 'support unavailable' in str(exc) or isinstance(exc, ImportError)
        output = {'status': 'pending' if unavailable else 'rejected',
                  'reason': f'{type(exc).__name__}: {exc}'}
    output['input_sha256'] = file_hash.hexdigest()
    output['reader_policy'] = READER_POLICY
    print(json.dumps(output))


if __name__ == '__main__':
    main()
